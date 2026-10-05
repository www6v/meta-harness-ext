"""End-to-end integration test.

Validates the full flow:
1. Mock Codex app-server (WebSocket)
2. Python bridge (HTTP → WebSocket)
3. Go server API endpoints (via HTTP client)

This proves the integration works without needing the real Codex server.
"""

import asyncio
import json
import subprocess
import sys
import time
import signal
import pytest
import httpx

MOCK_PORT = 18767
BRIDGE_PORT = 18093
MOCK_TOKEN = "e2e-test-token"


@pytest.fixture(scope="module")
def mock_server():
    """Start mock Codex server."""
    proc = subprocess.Popen(
        [sys.executable, "mock/mock_codex_server.py", "--port", str(MOCK_PORT), "--token", MOCK_TOKEN],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)
    assert proc.poll() is None, "mock server failed to start"
    yield proc
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def bridge_server(mock_server):
    """Start Python bridge."""
    import os
    env = os.environ.copy()
    env["CODEX_WS_URL"] = f"ws://127.0.0.1:{MOCK_PORT}"
    env["CODEX_WS_TOKEN"] = MOCK_TOKEN

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "codex_bridge:app", "--host", "127.0.0.1", "--port", str(BRIDGE_PORT), "--log-level", "warning"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(2)
    assert proc.poll() is None, f"bridge failed: {proc.stderr.read().decode()}"
    yield proc
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def client(bridge_server):
    """HTTP client for the bridge."""
    return httpx.Client(base_url=f"http://127.0.0.1:{BRIDGE_PORT}", timeout=10)


class TestE2EIntegration:
    """End-to-end tests simulating Go server calls."""

    def test_mcp_status_flow(self, client):
        """Simulate: Go server → bridge → mock Codex → MCP status list."""
        resp = client.post("/codex/rpc", json={
            "session_id": "e2e-session",
            "method": "mcpServerStatus/list",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        servers = data["result"]["servers"]
        assert len(servers) == 3
        assert {s["name"] for s in servers} == {"github", "linear", "slack"}

    def test_thread_sync_flow(self, client):
        """Simulate: Go server → bridge → mock Codex → thread list + read."""
        # List threads
        resp = client.post("/codex/rpc", json={
            "session_id": "e2e-session",
            "method": "thread/list",
        })
        assert resp.status_code == 200
        threads = resp.json()["result"]["threads"]
        assert len(threads) == 2

        # Read a specific thread
        thread_id = threads[0]["id"]
        resp = client.post("/codex/rpc", json={
            "session_id": "e2e-session",
            "method": "thread/read",
            "params": {"thread_id": thread_id},
        })
        assert resp.status_code == 200
        thread = resp.json()["result"]["thread"]
        assert thread["id"] == thread_id

    def test_config_reload_flow(self, client):
        """Simulate: Go server → bridge → mock Codex → config reload."""
        resp = client.post("/codex/rpc", json={
            "session_id": "e2e-session",
            "method": "config/mcpServer/reload",
        })
        assert resp.status_code == 200
        assert resp.json()["result"]["success"] is True

    def test_bridge_health(self, client):
        """Verify bridge health endpoint."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["codex_connected"] is True

    def test_error_handling(self, client):
        """Verify error handling for unknown methods."""
        resp = client.post("/codex/rpc", json={
            "session_id": "e2e-session",
            "method": "nonexistent/method",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
