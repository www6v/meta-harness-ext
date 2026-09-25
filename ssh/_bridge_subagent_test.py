"""Test the explicit sub-agent orchestration via the bridge HTTP API.

This test starts the codex bridge in a background thread, then POSTs a
TurnRequest with sub_agents to verify:
1. The bridge runs the sub-agent turn
2. Returns session.thread_created + agent.message (scoped to thread_id) + session.thread_idle
3. The parent turn still runs normally
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    # Ensure env vars point at the real codex deployment.
    os.environ.setdefault("CODEX_WS_URL", "ws://124.221.28.203:8765")
    os.environ.setdefault("CODEX_WS_TOKEN", "codex-poc-token-2024")
    os.environ.setdefault("CODEX_SSH_HOST", "124.221.28.203")
    os.environ.setdefault("CODEX_SSH_USER", "root")
    os.environ.setdefault("CODEX_SSH_PASSWORD", "1qaZxsw@")
    os.environ.setdefault("CODEX_REMOTE_PORT", "8765")
    os.environ.setdefault("CODEX_LISTEN", "127.0.0.1:18092")
    os.environ.setdefault("CODEX_TURN_TIMEOUT", "120")

    from codex_bridge import make_server
    server, pool = make_server()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"bridge listening on http://127.0.0.1:18092")
    time.sleep(1)

    # Build a TurnRequest with a sub-agent.
    req = {
        "session_id": "test-subagent-explicit",
        "agent": {
            "id": "agt_parent",
            "name": "parent-agent",
            "system_prompt": "You are the parent coordinator.",
        },
        "events": [
            {
                "type": "user.message",
                "content": [{"type": "text", "text": "Hello, please greet me briefly."}],
            }
        ],
        "sub_agents": {
            "sub_helper": {
                "id": "sub_helper",
                "name": "Helper Bot",
                "description": "Answers questions about Python.",
                "system_prompt": "You are a Python helper. Always respond with a Python one-liner.",
            }
        },
    }

    data = json.dumps(req).encode()
    r = urllib.request.Request(
        "http://127.0.0.1:18092/codex/turn",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print("sending turn request...")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            body = resp.read()
            result = json.loads(body)
    except Exception as e:
        print(f"FAILED: {e}")
        pool.shutdown()
        server.server_close()
        sys.exit(1)
    duration = time.monotonic() - start
    print(f"response in {duration:.1f}s, events={len(result.get('events', []))}")

    events = result.get("events", [])
    type_counts: dict[str, int] = {}
    for ev in events:
        t = ev.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print("\nEvent types:")
    for t, c in sorted(type_counts.items()):
        print(f"  {t}: {c}")

    # Check for sub-agent lifecycle events.
    thread_created = [e for e in events if e.get("type") == "session.thread_created"]
    thread_idle = [e for e in events if e.get("type") == "session.thread_idle"]
    sub_started = [e for e in events if e.get("type") == "session.sub_agent_started"]
    sub_completed = [e for e in events if e.get("type") == "session.sub_agent_completed"]
    thread_scoped = [e for e in events if e.get("session_thread_id")]
    print(f"\nSub-agent lifecycle:")
    print(f"  thread_created: {len(thread_created)}")
    print(f"  sub_agent_started: {len(sub_started)}")
    print(f"  sub_agent_completed: {len(sub_completed)}")
    print(f"  thread_idle: {len(thread_idle)}")
    print(f"  thread-scoped events: {len(thread_scoped)}")

    if thread_created:
        print(f"\n✓ Sub-agent thread created: {thread_created[0].get('session_thread_id')}")
        # Print first scoped message
        for e in thread_scoped:
            if e.get("type") == "agent.message":
                content = e.get("content", [])
                text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
                print(f"  sub-agent message: {text[:200]}")
                break
    else:
        print("\n✗ No sub-agent thread_created event found")

    pool.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
