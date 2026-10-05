"""Mock Codex App-Server for local testing.

Implements the subset of the Codex app-server JSON-RPC protocol needed
by the codex_bridge. Runs a WebSocket server that speaks the same wire
protocol as the real Codex app-server, returning canned responses.

Supported methods:
    - initialize
    - thread/start
    - turn/start
    - mcpServerStatus/list
    - config/mcpServer/reload
    - thread/list
    - thread/read

Usage:
    python mock/mock_codex_server.py [--port 8765] [--token TOKEN]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import websockets
from websockets.asyncio.server import ServerConnection, serve

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_ts() -> int:
    return int(time.time())


MOCK_THREADS: List[Dict[str, Any]] = [
    {
        "id": "thread-001",
        "name": "Test Session Alpha",
        "status": "active",
        "created_at": _now_ts() - 3600,
        "updated_at": _now_ts() - 60,
    },
    {
        "id": "thread-002",
        "name": "Test Session Beta",
        "status": "idle",
        "created_at": _now_ts() - 7200,
        "updated_at": _now_ts() - 300,
    },
]

MOCK_MCP_SERVERS: List[Dict[str, Any]] = [
    {
        "name": "github",
        "status": "connected",
        "tool_count": 12,
        "resource_count": 3,
    },
    {
        "name": "linear",
        "status": "connected",
        "tool_count": 8,
        "resource_count": 1,
    },
    {
        "name": "slack",
        "status": "disconnected",
        "error": "OAuth token expired",
        "tool_count": 0,
        "resource_count": 0,
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

@dataclass
class MockState:
    """Shared state across connections for a single mock server."""
    token: Optional[str] = None
    reload_count: int = 0
    initialized: Dict[str, bool] = field(default_factory=dict)


def _rpc_response(id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id, "result": result})


def _rpc_error(id: Any, code: int, message: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message},
    })


def _rpc_notification(method: str, params: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------

def handle_initialize(req_id: Any, params: Dict, state: MockState, conn_id: str) -> str:
    state.initialized[conn_id] = True
    return _rpc_response(req_id, {
        "protocolVersion": "2025-03-26",
        "serverInfo": {
            "name": "mock-codex-app-server",
            "version": "0.1.0-mock",
        },
        "capabilities": {
            "mcp": True,
            "threads": True,
        },
    })


def handle_thread_start(req_id: Any, params: Dict, state: MockState) -> str:
    thread_id = f"thread-{uuid.uuid4().hex[:8]}"
    return _rpc_response(req_id, {
        "threadId": thread_id,
        "status": "created",
    })


def handle_turn_start(req_id: Any, params: Dict, state: MockState) -> str:
    turn_id = f"turn-{uuid.uuid4().hex[:8]}"
    return _rpc_response(req_id, {
        "turnId": turn_id,
        "status": "started",
    })


def handle_mcp_server_status_list(req_id: Any, params: Dict, state: MockState) -> str:
    return _rpc_response(req_id, {
        "servers": MOCK_MCP_SERVERS,
    })


def handle_config_mcp_server_reload(req_id: Any, params: Dict, state: MockState) -> str:
    state.reload_count += 1
    logger.info("MCP config reload #%d triggered", state.reload_count)
    return _rpc_response(req_id, {
        "success": True,
        "reload_count": state.reload_count,
    })


def handle_thread_list(req_id: Any, params: Dict, state: MockState) -> str:
    return _rpc_response(req_id, {
        "threads": MOCK_THREADS,
    })


def handle_thread_read(req_id: Any, params: Dict, state: MockState) -> str:
    thread_id = params.get("thread_id", "")
    for t in MOCK_THREADS:
        if t["id"] == thread_id:
            return _rpc_response(req_id, {"thread": t})
    return _rpc_error(req_id, -32602, f"thread not found: {thread_id}")


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

METHODS = {
    "initialize": handle_initialize,
    "thread/start": handle_thread_start,
    "turn/start": handle_turn_start,
    "mcpServerStatus/list": handle_mcp_server_status_list,
    "config/mcpServer/reload": handle_config_mcp_server_reload,
    "thread/list": handle_thread_list,
    "thread/read": handle_thread_read,
}


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

async def handle_connection(ws: ServerConnection, state: MockState) -> None:
    conn_id = uuid.uuid4().hex[:8]
    remote = ws.remote_address
    logger.info("[conn-%s] connected from %s", conn_id, remote)

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(_rpc_error(None, -32700, "Parse error"))
                continue

            method = msg.get("method", "")
            req_id = msg.get("id")
            params = msg.get("params", {})

            logger.debug("[conn-%s] ← %s (id=%s)", conn_id, method, req_id)

            handler = METHODS.get(method)
            if handler is None:
                if req_id is not None:
                    await ws.send(_rpc_error(
                        req_id, -32601, f"Method not found: {method}"
                    ))
                continue

            if method == "initialize":
                response = handler(req_id, params, state, conn_id)
            else:
                response = handler(req_id, params, state)

            logger.debug("[conn-%s] → %s", conn_id, response[:200])
            await ws.send(response)

            # After successful initialize, send initialized notification
            if method == "initialize":
                notif = _rpc_notification("initialized", {})
                await ws.send(notif)

    except websockets.ConnectionClosed:
        logger.info("[conn-%s] closed", conn_id)
    except Exception:
        logger.exception("[conn-%s] unexpected error", conn_id)
    finally:
        state.initialized.pop(conn_id, None)


# ---------------------------------------------------------------------------
# Auth check wrapper
# ---------------------------------------------------------------------------

async def auth_and_handle(ws: ServerConnection, state: MockState) -> None:
    """Validate Bearer token before delegating to handle_connection."""
    if state.token is None:
        # No token configured — accept all connections
        await handle_connection(ws, state)
        return

    # websockets v13+ exposes request headers
    request = getattr(ws, "request", None)
    if request is None:
        await ws.close(1008, "No request headers")
        return

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        await ws.close(1008, "Missing Bearer token")
        return

    provided = auth_header[len("Bearer "):]
    if provided != state.token:
        await ws.close(1008, "Invalid token")
        return

    await handle_connection(ws, state)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

async def run_server(host: str, port: int, token: Optional[str]) -> None:
    state = MockState(token=token)
    logger.info(
        "Starting mock Codex app-server on ws://%s:%d (token=%s)",
        host, port, "required" if token else "none",
    )

    async with serve(
        lambda ws: auth_and_handle(ws, state),
        host,
        port,
    ) as server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Codex App-Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=None, help="Bearer token (none = accept all)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    asyncio.run(run_server(args.host, args.port, args.token))


if __name__ == "__main__":
    main()
