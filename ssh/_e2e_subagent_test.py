"""End-to-end test: create an agent with callable_agents, run a session,
verify sub-agent events appear.

Requires:
- meta-harness running on http://127.0.0.1:8787 (via start-console.sh)
- codex bridge running on http://127.0.0.1:8092
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"
HEADERS = {"x-api-key": "dev-key", "Content-Type": "application/json"}


def api_get(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def api_post(path, body):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if hasattr(e, "read") else ""
        print(f"  HTTP {e.code} on POST {path}: {body_text[:500]}")
        raise


def ensure_sub_agent() -> str:
    """Create (or find) a worker agent to use as callable sub-agent."""
    # List existing agents.
    try:
        resp = api_get("/v1/agents", {"limit": 100})
    except Exception as e:
        print(f"list agents failed: {e}")
        sys.exit(1)
    agents = resp.get("data", resp) if isinstance(resp, dict) else resp
    for a in agents or []:
        if a.get("name") == "sub-helper":
            print(f"  using existing sub-agent id={a['id']}")
            return a["id"]
    # Create one.
    created = api_post("/v1/agents", {
        "name": "sub-helper",
        "description": "A helper sub-agent that writes Python code.",
        "system_prompt": "You are a Python helper. Always respond with a Python code snippet.",
        "model": "codex",
        "harness": "codex",
    })
    aid = created.get("id") or created.get("data", {}).get("id")
    print(f"  created sub-agent id={aid}")
    return aid


def ensure_parent_agent(sub_agent_id: str) -> str:
    """Create (or find) a parent agent with callable_agents set."""
    try:
        resp = api_get("/v1/agents", {"limit": 100})
    except Exception as e:
        print(f"list agents failed: {e}")
        sys.exit(1)
    agents = resp.get("data", resp) if isinstance(resp, dict) else resp
    for a in agents or []:
        if a.get("name") == "parent-codex":
            print(f"  using existing parent id={a['id']}")
            return a["id"]
    created = api_post("/v1/agents", {
        "name": "parent-codex",
        "description": "Parent coordinator agent using codex harness.",
        "system_prompt": "You are a coordinator. You can delegate to sub-agents when appropriate.",
        "model": "codex",
        "harness": "codex",
        "callable_agents": [
            {"type": "agent", "id": sub_agent_id, "version": 1}
        ],
    })
    aid = created.get("id") or created.get("data", {}).get("id")
    print(f"  created parent agent id={aid}")
    return aid


def create_session(agent_id: str) -> str:
    resp = api_post("/v1/sessions", {
        "agent": agent_id,
        "title": "codex-subagent-e2e",
    })
    if "id" in resp:
        return resp["id"]
    if "data" in resp and "id" in resp["data"]:
        return resp["data"]["id"]
    raise RuntimeError(f"create session failed: {resp}")


def send_message(session_id: str, text: str):
    return api_post(f"/v1/sessions/{session_id}/events", {
        "events": [{"type": "user.message", "content": [{"type": "text", "text": text}]}]
    })


def poll_events(session_id: str, timeout: int = 300):
    start = time.time()
    last_count = 0
    while time.time() - start < timeout:
        resp = api_get(f"/v1/sessions/{session_id}/events", {"limit": 500})
        events = resp.get("data", resp.get("events", []))
        if not isinstance(events, list):
            events = []
        count = len(events)

        has_completed = any(e.get("type") == "turn.completed" for e in events)
        has_error = any(e.get("type") == "session.error" for e in events)

        if has_completed or has_error:
            print(f"  Turn finished: completed={has_completed}, error={has_error}, events={count}")
            return events

        if count != last_count:
            print(f"  events={count}...", end=" ", flush=True)
        last_count = count
        time.sleep(3)

    print(f"  TIMEOUT after {timeout}s with {last_count} events")
    return events


def main() -> int:
    print("=== Codex Sub-Agent E2E Test ===\n")

    print("1. Creating sub-agent...")
    sub_id = ensure_sub_agent()

    print("\n2. Creating parent agent (with callable_agents)...")
    parent_id = ensure_parent_agent(sub_id)

    print("\n3. Creating session...")
    sid = create_session(parent_id)
    print(f"   Session: {sid}")

    print("\n4. Sending message...")
    msg = "Write a Python function that checks if a string is a palindrome. Keep it brief."
    print(f"   Message: {msg}")
    send_message(sid, msg)

    print("\n5. Polling for turn completion (up to 5 min)...")
    events = poll_events(sid, timeout=300)

    # Analyze events.
    print(f"\n=== Event Analysis ({len(events)} events) ===")
    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    for t, c in sorted(type_counts.items()):
        print(f"  {t}: {c}")

    # Check for sub-agent events.
    # The REST API wraps events as {seq, type, ts, data: {inner}} — the SSE
    # stream sends the inner directly. Check inside `data` when present.
    def inner_field(e, key):
        return e.get(key) if key in e else (e.get("data", {}) or {}).get(key)

    thread_created = [e for e in events if e.get("type") == "session.thread_created"]
    sub_started = [e for e in events if e.get("type") == "session.sub_agent_started"]
    sub_completed = [e for e in events if e.get("type") == "session.sub_agent_completed"]
    thread_idle = [e for e in events if e.get("type") == "session.thread_idle"]
    thread_scoped = [e for e in events if inner_field(e, "session_thread_id")]

    print(f"\nSub-agent lifecycle events:")
    print(f"  thread_created: {len(thread_created)}")
    print(f"  sub_agent_started: {len(sub_started)}")
    print(f"  sub_agent_completed: {len(sub_completed)}")
    print(f"  thread_idle: {len(thread_idle)}")
    print(f"  thread-scoped events: {len(thread_scoped)}")

    ok = True
    if thread_created:
        tid = inner_field(thread_created[0], "session_thread_id")
        print(f"\n✓ Sub-agent thread created: {tid}")
        for e in thread_scoped:
            if e.get("type") == "agent.message" or (e.get("data", {}) or {}).get("type") == "agent.message":
                content = inner_field(e, "content") or []
                text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
                print(f"  sub-agent message: {text[:200]}")
                break
    else:
        print("\n✗ No sub-agent thread_created event found")
        ok = False

    print(f"\n=== {'PASSED' if ok else 'FAILED'} ===")
    print(f"Open in browser: {BASE}/sessions/{sid}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
