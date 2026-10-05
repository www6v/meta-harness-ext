"""Codex Bridge — HTTP → WebSocket bridge for Codex app-server.

Exposes FastAPI HTTP endpoints that the meta-harness Go server calls.
Internally maintains a WebSocket connection to the Codex app-server
(real or mock) and relays JSON-RPC calls.

Endpoints:
    GET  /health         — Bridge health check
    POST /codex/rpc      — Generic JSON-RPC call (mcpServerStatus/list, etc.)
    POST /codex/turn     — Execute a turn (batch, returns events at end)
    POST /codex/turn/sse — Execute a turn with SSE streaming

Environment:
    CODEX_WS_URL   — WebSocket URL for Codex app-server (default: ws://127.0.0.1:8765)
    CODEX_WS_TOKEN — Bearer token for WebSocket auth (optional)
    CODEX_BRIDGE_PORT — HTTP port for this bridge (default: 8092)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
import websockets
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from config_merger import MCPConfigMerger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CODEX_WS_URL = os.environ.get("CODEX_WS_URL", "ws://127.0.0.1:8765")
CODEX_WS_TOKEN = os.environ.get("CODEX_WS_TOKEN")
BRIDGE_PORT = int(os.environ.get("CODEX_BRIDGE_PORT", "8092"))

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class RPCRequest(BaseModel):
    session_id: str
    method: str
    params: Optional[Dict[str, Any]] = None


class TurnRequest(BaseModel):
    session_id: str
    agent: Optional[Dict[str, Any]] = None
    events: Optional[list] = None
    skills: Optional[list] = None
    sub_agents: Optional[list] = None
    tenant_mcp_servers: Optional[list] = None


# ---------------------------------------------------------------------------
# Codex WebSocket Client
# ---------------------------------------------------------------------------

class CodexWSClient:
    """Manages a WebSocket connection to the Codex app-server.

    Handles:
    - Connection lifecycle with auto-reconnect
    - JSON-RPC request/response correlation
    - ServerRequest auto-response (approval requests, etc.)
    - Notification collection during turns
    """

    def __init__(self, ws_url: str, token: Optional[str] = None):
        self._ws_url = ws_url
        self._token = token
        self._ws = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._recv_task: Optional[asyncio.Task] = None
        self._connected = False
        self._notifications: list = []

    async def connect(self) -> None:
        """Establish WebSocket connection and run initialize handshake."""
        extra_headers = {}
        if self._token:
            extra_headers["Authorization"] = f"Bearer {self._token}"

        logger.info("connecting to Codex app-server at %s", self._ws_url)
        self._ws = await websockets.connect(
            self._ws_url,
            additional_headers=extra_headers,
            ping_interval=30,
            ping_timeout=10,
        )
        self._connected = True
        self._recv_task = asyncio.create_task(self._recv_loop())

        # Initialize handshake
        result = await self.call("initialize", {
            "clientInfo": {"name": "codex-bridge", "version": "0.1.0"},
            "capabilities": {},
        })
        logger.info("initialized: %s", result.get("serverInfo", {}))

    async def close(self) -> None:
        self._connected = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()

    async def _recv_loop(self) -> None:
        """Receive messages and route to pending futures or notification list."""
        try:
            async for raw in self._ws:
                msg = json.loads(raw)

                # JSON-RPC response (has id)
                if "id" in msg:
                    req_id = msg["id"]
                    future = self._pending.pop(req_id, None)
                    if future and not future.done():
                        if "error" in msg:
                            future.set_exception(
                                CodexRPCError(
                                    msg["error"].get("code", -1),
                                    msg["error"].get("message", "unknown"),
                                )
                            )
                        else:
                            future.set_result(msg.get("result", {}))
                    continue

                # JSON-RPC notification (no id, has method)
                method = msg.get("method", "")
                params = msg.get("params", {})

                # Handle ServerRequests (server asking client for input)
                if method.endswith("/requestApproval") or method.endswith("/request"):
                    await self._handle_server_request(msg)
                    continue

                # Collect other notifications
                self._notifications.append(msg)
                logger.debug("notification: %s", method)

        except websockets.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            self._connected = False
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("recv loop error")
            self._connected = False

    async def _handle_server_request(self, msg: Dict) -> None:
        """Auto-respond to Codex server requests (approvals, elicitation, MCP tool calls)."""
        req_id = msg.get("id")
        method = msg.get("method", "")
        if req_id is None:
            return

        # Auto-decline all approval requests (unattended mode)
        if "requestApproval" in method:
            response = {"decision": "decline"}
        elif method == "mcpServer/elicitation/request":
            response = {"action": "decline", "content": None}
        elif method == "item/tool/requestUserInput":
            response = {"answers": {}}
        elif method == "mcpServer/tool/call":
            # Handle MCP tool call requests
            params = msg.get("params", {})
            server_name = params.get("server", "")
            tool_name = params.get("tool", "")
            arguments = params.get("arguments", {})
            logger.info("MCP tool call request: server=%s tool=%s", server_name, tool_name)

            # For now, return a stub response - actual MCP execution would
            # require calling the MCP server's tool endpoint
            response = {
                "content": [{"type": "text", "text": f"MCP tool {server_name}/{tool_name} called"}],
                "isError": False,
            }
        else:
            response = {}

        resp_msg = {"jsonrpc": "2.0", "id": req_id, "result": response}
        await self._ws.send(json.dumps(resp_msg))
        logger.debug("auto-responded to %s: %s", method, response)

    async def call(self, method: str, params: Optional[Dict] = None, timeout: float = 30) -> Dict:
        """Send a JSON-RPC request and wait for the response."""
        if not self._connected or self._ws is None:
            raise ConnectionError("Not connected to Codex app-server")

        req_id = self._next_id
        self._next_id += 1

        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        await self._ws.send(json.dumps(request))
        logger.debug("→ %s (id=%d)", method, req_id)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.debug("← %s (id=%d) result=%s", method, req_id, json.dumps(result)[:500])
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"Codex RPC timeout: {method}")

    def drain_notifications(self) -> list:
        """Return and clear accumulated notifications."""
        msgs = self._notifications[:]
        self._notifications.clear()
        return msgs

    @property
    def connected(self) -> bool:
        return self._connected


class CodexRPCError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Codex RPC error {code}: {message}")


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_client: Optional[CodexWSClient] = None
_config_merger: Optional[MCPConfigMerger] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: connect to Codex, initialize config merger."""
    global _client, _config_merger

    _config_merger = MCPConfigMerger()
    _client = CodexWSClient(CODEX_WS_URL, CODEX_WS_TOKEN)

    try:
        await _client.connect()
        logger.info("bridge connected to Codex app-server")
    except Exception as e:
        logger.error("failed to connect to Codex: %s", e)
        # Don't crash — let health check report the failure
        _client = None

    yield

    if _client:
        await _client.close()
    logger.info("bridge shutdown complete")


app = FastAPI(title="Codex Bridge", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    connected = _client is not None and _client.connected
    return {
        "status": "ok" if connected else "degraded",
        "codex_connected": connected,
        "codex_ws_url": CODEX_WS_URL,
    }


@app.post("/codex/rpc")
async def codex_rpc(req: RPCRequest):
    """Generic JSON-RPC passthrough to the Codex app-server."""
    if not _client or not _client.connected:
        raise HTTPException(status_code=503, detail="Not connected to Codex")

    try:
        result = await _client.call(req.method, req.params)
        return {"result": result}
    except CodexRPCError as e:
        return {"error": {"code": e.code, "message": e.message}}
    except Exception as e:
        logger.exception("RPC call failed: %s", req.method)
        return {"error": {"code": -1, "message": str(e)}}


@app.post("/codex/turn")
async def codex_turn(req: TurnRequest):
    """Execute a turn against Codex. Returns batch events at end."""
    if not _client or not _client.connected:
        raise HTTPException(status_code=503, detail="Not connected to Codex")

    start = time.monotonic()

    try:
        # Extract the latest user message from events
        user_text = _extract_user_message(req.events)

        # Register MCP servers if provided
        if req.tenant_mcp_servers:
            await _register_mcp_servers(req.tenant_mcp_servers)

        # Start or reuse a thread
        thread_id = await _start_thread(req.session_id, user_text)

        # Start the turn
        # Codex expects input as a list of content blocks, not a plain string
        # If user_text is empty, use a fallback to avoid sending empty input
        if user_text:
            input_blocks = [{"type": "text", "text": user_text}]
        else:
            # Fallback: extract any text from events or use a default
            input_blocks = [{"type": "text", "text": "hello"}]
            logger.warning("empty user message, using fallback for session %s", req.session_id)

        turn_result = await _client.call("turn/start", {
            "threadId": thread_id,
            "input": input_blocks,
        })

        logger.debug("turn/start response: %s", json.dumps(turn_result)[:500])

        # Extract turn ID - Codex returns it in result.turn.id
        turn_id = (
            turn_result.get("turnId")
            or turn_result.get("turn_id")
            or turn_result.get("id")
        )
        if not turn_id and isinstance(turn_result.get("turn"), dict):
            turn_id = turn_result["turn"].get("id")

        if not turn_id:
            raise RuntimeError(f"Codex turn/start returned no turn ID: {turn_result}")

        # Wait for turn completion (collect notifications)
        events = await _wait_for_turn_completion(turn_id, timeout=300)

        duration = time.monotonic() - start
        logger.info(
            "turn completed: session=%s thread=%s turn=%s events=%d duration=%.1fs",
            req.session_id, thread_id, turn_id, len(events), duration,
        )

        return {
            "events": events,
            "files": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    except Exception as e:
        logger.exception("turn failed: session=%s", req.session_id)
        return {
            "events": [],
            "files": [],
            "error": {"message": str(e)},
        }


@app.post("/codex/turn/sse")
async def codex_turn_sse(req: TurnRequest):
    """Execute a turn with SSE streaming. Returns events as they arrive."""
    if not _client or not _client.connected:
        raise HTTPException(status_code=503, detail="Not connected to Codex")

    async def event_stream():
        try:
            user_text = _extract_user_message(req.events)

            # Register MCP servers if provided
            if req.tenant_mcp_servers:
                await _register_mcp_servers(req.tenant_mcp_servers)

            thread_id = await _start_thread(req.session_id, user_text)

            # Codex expects input as a list of content blocks, not a plain string
            # If user_text is empty, use a fallback to avoid sending empty input
            if user_text:
                input_blocks = [{"type": "text", "text": user_text}]
            else:
                input_blocks = [{"type": "text", "text": "hello"}]
                logger.warning("empty user message, using fallback for session %s", req.session_id)

            turn_result = await _client.call("turn/start", {
                "threadId": thread_id,
                "input": input_blocks,
            })

            logger.debug("turn/start response: %s", json.dumps(turn_result)[:500])

            # Extract turn ID - Codex returns it in result.turn.id
            turn_id = (
                turn_result.get("turnId")
                or turn_result.get("turn_id")
                or turn_result.get("id")
            )
            if not turn_id and isinstance(turn_result.get("turn"), dict):
                turn_id = turn_result["turn"].get("id")

            # Stream events as they come
            if turn_id:
                async for event in _stream_turn_events(turn_id):
                    yield f"data: {json.dumps(event)}\n\n"

            yield "data: {\"type\": \"done\"}\n\n"

        except Exception as e:
            error_event = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register_mcp_servers(servers: list) -> None:
    """Register MCP servers with the Codex app-server via config/value/write."""
    for server in servers or []:
        name = server.get("name")
        url = server.get("url")
        if not name or not url:
            logger.warning("skipping MCP server with missing name or url: %s", server)
            continue

        config_value = {
            "url": url,
            "type": server.get("type", "url"),
        }
        if server.get("authorization_token"):
            config_value["authorization_token"] = server["authorization_token"]

        try:
            await _client.call("config/value/write", {
                "keyPath": f"mcp_servers.{name}",
                "value": config_value,
                "mergeStrategy": "replace",
            })
            logger.info("registered MCP server: %s -> %s", name, url)
        except Exception as e:
            logger.error("failed to register MCP server %s: %s", name, e)


async def _handle_mcp_tool_call(params: Dict) -> Dict:
    """Handle an MCP tool call request from the Codex app-server."""
    server_name = params.get("server", "")
    tool_name = params.get("tool", "")
    arguments = params.get("arguments", {})

    logger.info("MCP tool call: server=%s tool=%s args=%s", server_name, tool_name, json.dumps(arguments)[:200])

    # For now, return a stub response - the actual MCP tool execution
    # would require calling the MCP server's tool endpoint
    return {
        "content": [{"type": "text", "text": f"MCP tool {server_name}/{tool_name} called with {json.dumps(arguments)[:100]}"}],
        "isError": False,
    }


async def _start_thread(session_id: str, user_text: str) -> str:
    """Start a Codex thread and return its ID.

    The Codex app-server may return the thread ID in different field names
    (threadId, thread_id, id, or nested in thread.id), so we check multiple
    possibilities.
    """
    thread_result = await _client.call("thread/start", {
        "instructions": user_text or "",
        "sessionId": session_id,
    })

    logger.debug("thread/start response: %s", json.dumps(thread_result)[:500])

    # Try multiple field names for thread ID
    thread_id = (
        thread_result.get("threadId")
        or thread_result.get("thread_id")
        or thread_result.get("id")
    )

    # Check nested thread.id (real Codex app-server returns {thread: {id: ...}})
    if not thread_id and isinstance(thread_result.get("thread"), dict):
        thread_id = thread_result["thread"].get("id")

    if not thread_id:
        logger.error(
            "thread/start returned no thread ID. session=%s response=%s",
            session_id, thread_result
        )
        raise RuntimeError(
            f"Codex thread/start returned no thread ID: {thread_result}"
        )

    logger.debug("started thread %s for session %s", thread_id, session_id)
    return thread_id


def _extract_user_message(events: Optional[list]) -> str:
    """Pull the latest user.message text from the events list.

    Handles two event formats:
    - OMA format: {type: "user.message", data: {content: [{text: "..."}]}}
    - Simple format: {type: "user.message", content: "..."}
    """
    if not events:
        return ""
    for ev in reversed(events):
        if not isinstance(ev, dict) or ev.get("type") != "user.message":
            continue

        # Try OMA format: data.content is an array of content blocks
        data = ev.get("data")
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, list):
                # Extract text from content blocks
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        texts.append(block.get("text", ""))
                text = "".join(texts)
                if text:
                    return text
            elif isinstance(content, str):
                return content

        # Try simple format: top-level content or text field
        content = ev.get("content")
        if isinstance(content, str) and content:
            return content
        text = ev.get("text")
        if isinstance(text, str) and text:
            return text

    return ""


async def _wait_for_turn_completion(turn_id: str, timeout: float = 300) -> list:
    """Wait for turn/completed notification and collect events."""
    events = []
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        notifications = _client.drain_notifications()
        for notif in notifications:
            method = notif.get("method", "")
            params = notif.get("params", {})

            logger.debug("notification: method=%s params_keys=%s", method, list(params.keys()) if isinstance(params, dict) else type(params).__name__)

            # Agent message deltas — try multiple method names and field names
            if method in ("item/agentMessage/delta", "item/agentMessage/textDelta",
                          "turn/agentMessage/delta", "turn/textDelta"):
                text = (params.get("textDelta") or params.get("text")
                        or params.get("delta") or params.get("content") or "")
                if text:
                    events.append({"type": "agent.message", "content": text})
            elif method in ("item/reasoning/textDelta", "turn/reasoning/textDelta"):
                text = params.get("textDelta") or params.get("text") or params.get("delta") or ""
                if text:
                    events.append({"type": "agent.thinking", "content": text})
            elif method in ("turn/completed", "turn/statusChanged", "turn/ended"):
                status = params.get("status", {})
                if isinstance(status, dict):
                    stype = status.get("type", "")
                else:
                    stype = str(status)
                if stype in ("complete", "completed", "idle", "done") or method == "turn/completed":
                    logger.info("turn %s completed (method=%s status=%s)", turn_id, method, stype)
                    return events
            elif method == "error":
                events.append({
                    "type": "session.error",
                    "message": params.get("message", "unknown error"),
                })
            else:
                # Catch-all: if the notification has text content, emit it as agent.message
                text = (params.get("textDelta") or params.get("text")
                        or params.get("content") or "")
                if text and "agent" in method.lower():
                    events.append({"type": "agent.message", "content": text})

        await asyncio.sleep(0.1)

    return events


async def _stream_turn_events(turn_id: str):
    """Yield events as SSE frames during a turn."""
    deadline = time.monotonic() + 300

    while time.monotonic() < deadline:
        notifications = _client.drain_notifications()
        for notif in notifications:
            method = notif.get("method", "")
            params = notif.get("params", {})

            logger.debug("notification: method=%s params_keys=%s", method, list(params.keys()) if isinstance(params, dict) else type(params).__name__)

            # Agent message deltas — try multiple method names and field names
            if method in ("item/agentMessage/delta", "item/agentMessage/textDelta",
                          "turn/agentMessage/delta", "turn/textDelta"):
                text = (params.get("textDelta") or params.get("text")
                        or params.get("delta") or params.get("content") or "")
                if text:
                    yield {"type": "agent.message", "content": text}
            elif method in ("item/reasoning/textDelta", "turn/reasoning/textDelta"):
                text = params.get("textDelta") or params.get("text") or params.get("delta") or ""
                if text:
                    yield {"type": "agent.thinking", "content": text}
            elif method in ("turn/completed", "turn/statusChanged", "turn/ended"):
                status = params.get("status", {})
                if isinstance(status, dict):
                    stype = status.get("type", "")
                else:
                    stype = str(status)
                # Only end on completed/idle/complete status
                if stype in ("complete", "completed", "idle", "done") or method == "turn/completed":
                    logger.info("turn %s completed (method=%s status=%s)", turn_id, method, stype)
                    return
            elif method == "error":
                yield {"type": "session.error", "message": params.get("message", "")}
            else:
                # Catch-all: if the notification has text content, emit it as agent.message
                text = (params.get("textDelta") or params.get("text")
                        or params.get("content") or "")
                if text and "agent" in method.lower():
                    yield {"type": "agent.message", "content": text}

        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
