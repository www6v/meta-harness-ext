"""Quick connectivity check: SSH tunnel + WebSocket + codex initialize.

Usage:
    python _connectivity_test.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from client import SSHClient
from codex_tunnel import SSHTunnel
from codex_client import CodexClient, CodexConfig


def check_ssh() -> None:
    print("\n=== Step 1: SSH connectivity ===")
    try:
        with SSHClient("124.221.28.203", "root", "1qaZxsw@", timeout=15.0, connect=True) as c:
            r = c.run("hostname && docker ps --format 'table {{.Names}}\t{{.Status}}' | head -20", timeout=20)
            print(f"exit={r.exit_code}")
            print(r.stdout)
            if r.stderr:
                print("stderr:", r.stderr)
    except Exception as e:
        print(f"SSH FAILED: {e}")
        raise


def check_tunnel_and_ws() -> None:
    print("\n=== Step 2: SSH tunnel + WebSocket to codex ===")
    with SSHTunnel(
        ssh_host="124.221.28.203",
        ssh_user="root",
        ssh_password="1qaZxsw@",
        remote_port=8765,
    ) as t:
        print(f"tunnel local_url = {t.local_url}")
        asyncio.run(_probe_ws(t.local_url))


async def _probe_ws(url: str) -> None:
    token = "codex-poc-token-2024"
    client = CodexClient(CodexConfig(ws_url=url, token=token, init_timeout=20.0))
    try:
        await client.connect()
        print(f"initialized: thread_id={client._thread_id}")

        # Try a minimal thread/start + turn
        print("\n=== Step 3: minimal turn ===")
        result = await client.run_turn(
            "Say hello in one sentence.",
            session_id="connectivity-test",
        )
        print(f"terminal_state={result.terminal_state}")
        print(f"final_text={result.final_text[:300]}")
        print(f"events={len(result.events)}")
    except Exception as e:
        print(f"WS FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()


if __name__ == "__main__":
    check_ssh()
    check_tunnel_and_ws()
    print("\n=== ALL GOOD ===")
