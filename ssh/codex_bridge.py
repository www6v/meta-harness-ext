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
        """Auto-respond to Codex server requests (approvals, elicitation)."""
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

        # Start or reuse a thread
        thread_result = await _client.call("thread/start", {
            "instructions": user_text or "",
        })
        thread_id = thread_result.get("threadId", "unknown")

        # Start the turn
        turn_result = await _client.call("turn/start", {
            "threadId": thread_id,
            "input": user_text or "",
        })
        turn_id = turn_result.get("turnId", "unknown")

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

            thread_result = await _client.call("thread/start", {
                "instructions": user_text or "",
            })
            thread_id = thread_result.get("threadId", "unknown")

            turn_result = await _client.call("turn/start", {
                "threadId": thread_id,
                "input": user_text or "",
            })

            # Stream events as they come
            async for event in _stream_turn_events(turn_result.get("turnId")):
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

def _extract_user_message(events: Optional[list]) -> str:
    """Pull the latest user.message text from the events list."""
    if not events:
        return ""
    for ev in reversed(events):
        if isinstance(ev, dict) and ev.get("type") == "user.message":
            return ev.get("content", "")
        if isinstance(ev, dict) and ev.get("type") == "user.message":
            return ev.get("text", "")
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

            if method == "item/agentMessage/delta":
                text = params.get("textDelta", "")
                if text:
                    events.append({
                        "type": "agent.message",
                        "content": text,
                    })
            elif method == "item/reasoning/textDelta":
                text = params.get("textDelta", "")
                if text:
                    events.append({
                        "type": "agent.thinking",
                        "content": text,
                    })
            elif method == "item/started":
                events.append({
                    "type": "agent.tool_use",
                    "name": params.get("name", "unknown"),
                })
            elif method == "item/completed":
                events.append({
                    "type": "agent.tool_result",
                    "name": params.get("name", "unknown"),
                })
            elif method == "turn/completed":
                return events
            elif method == "error":
                events.append({
                    "type": "session.error",
                    "message": params.get("message", "unknown error"),
                })

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

            if method == "item/agentMessage/delta":
                yield {"type": "agent.message", "content": params.get("textDelta", "")}
            elif method == "item/reasoning/textDelta":
                yield {"type": "agent.thinking", "content": params.get("textDelta", "")}
            elif method == "turn/completed":
                return
            elif method == "error":
                yield {"type": "session.error", "message": params.get("message", "")}

        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
