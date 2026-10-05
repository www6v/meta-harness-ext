"""Tests for codex_bridge — HTTP → WebSocket bridge.

Uses the mock Codex app-server to verify bridge behavior end-to-end.
"""

import asyncio
import json
import subprocess
import sys
import time
import signal
import pytest
import httpx

MOCK_PORT = 18766
BRIDGE_PORT = 18092
MOCK_TOKEN = "test-token-2024"


@pytest.fixture(scope="module")
def mock_server():
    """Start the mock Codex app-server for the duration of the test module."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "mock/mock_codex_server.py",
            "--port", str(MOCK_PORT),
            "--token", MOCK_TOKEN,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)  # wait for server to start
    assert proc.poll() is None, "mock server failed to start"
    yield proc
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def bridge_server(mock_server):
    """Start the codex_bridge for the duration of the test module."""
    import os
    env = os.environ.copy()
    env["CODEX_WS_URL"] = f"ws://127.0.0.1:{MOCK_PORT}"
    env["CODEX_WS_TOKEN"] = MOCK_TOKEN

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m", "uvicorn",
            "codex_bridge:app",
            "--host", "127.0.0.1",
            "--port", str(BRIDGE_PORT),
            "--log-level", "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(2)  # wait for bridge to start and connect
    assert proc.poll() is None, f"bridge failed to start: {proc.stderr.read().decode()}"
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


class TestRPC:
    """Test the generic /codex/rpc endpoint."""

    def test_mcp_server_status_list(self, client):
        resp = client.post("/codex/rpc", json={
            "session_id": "test-session",
            "method": "mcpServerStatus/list",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        servers = data["result"]["servers"]
        assert len(servers) == 3
        names = {s["name"] for s in servers}
        assert names == {"github", "linear", "slack"}

    def test_thread_list(self, client):
        resp = client.post("/codex/rpc", json={
            "session_id": "test-session",
            "method": "thread/list",
        })
        assert resp.status_code == 200
        data = resp.json()
        threads = data["result"]["threads"]
        assert len(threads) == 2

    def test_config_mcp_server_reload(self, client):
        resp = client.post("/codex/rpc", json={
            "session_id": "test-session",
            "method": "config/mcpServer/reload",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["success"] is True

    def test_unknown_method(self, client):
        resp = client.post("/codex/rpc", json={
            "session_id": "test-session",
            "method": "nonexistent/method",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data

    def test_missing_method(self, client):
        resp = client.post("/codex/rpc", json={
            "session_id": "test-session",
        })
        assert resp.status_code == 422

    def test_thread_read(self, client):
        resp = client.post("/codex/rpc", json={
            "session_id": "test-session",
            "method": "thread/read",
            "params": {"thread_id": "thread-001"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["thread"]["id"] == "thread-001"
        assert data["result"]["thread"]["name"] == "Test Session Alpha"

    def test_thread_read_not_found(self, client):
        resp = client.post("/codex/rpc", json={
            "session_id": "test-session",
            "method": "thread/read",
            "params": {"thread_id": "nonexistent"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data


class TestHealth:
    """Test bridge health endpoints."""

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
