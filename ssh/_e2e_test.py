"""End-to-end test: upload skill -> create codex agent -> run session.

Expects oma-server on :8787 and codex bridge on :8092.
"""
import base64
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8787"
API_KEY = "dev-key"

def api(method, path, body=None, raw=False):
    url = BASE + path
    data = None
    headers = {"X-API-Key": API_KEY}
    if body is not None:
        if raw:
            data = body if isinstance(body, bytes) else body.encode()
            headers["Content-Type"] = "application/octet-stream"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code} {method} {path}: {body[:300]}", file=sys.stderr)
        raise

def main():
    print("=" * 60)
    print("Step 1: Check harness availability")
    print("=" * 60)
    harnesses = api("GET", "/v1/config/harnesses")
    print(f"  harnesses: {harnesses}")
    assert harnesses.get("codex"), "codex harness not enabled!"
    print("  OK: codex harness enabled")

    print()
    print("=" * 60)
    print("Step 2: Create a custom skill")
    print("=" * 60)
    skill_body = """---
name: e2e-test-skill
description: Test skill for end-to-end validation
---

# E2E Test Skill

When asked about the test phrase, respond with:
"The codex skill injection is working!"
"""
    skill_result = api("POST", "/v1/skills", {
        "display_title": "E2E Test Skill",
        "name": "e2e-test-skill",
        "description": "Test skill for e2e validation",
        "files": [
            {"filename": "SKILL.md", "content": skill_body, "encoding": "utf8"},
        ],
    })
    skill_id = skill_result["id"]
    print(f"  created skill: id={skill_id}")

    print()
    print("=" * 60)
    print("Step 3: Create a codex agent with the skill")
    print("=" * 60)
    agent_result = api("POST", "/v1/agents", {
        "name": "codex-e2e-test",
        "model": "codex",
        "system": "You are a helpful assistant. Follow the instructions in any skill files mounted in /home/user/.skills/.",
        "description": "Codex agent for e2e skill test",
        "skills": [{"type": "custom", "skill_id": skill_id, "version": "latest"}],
        "_oma": {"harness": "codex"},
    })
    agent_id = agent_result["id"]
    print(f"  created agent: id={agent_id}")

    print()
    print("=" * 60)
    print("Step 4: Create a session")
    print("=" * 60)
    session_result = api("POST", "/v1/sessions", {
        "agent": {"id": agent_id},
    })
    session_id = session_result["id"]
    print(f"  created session: id={session_id}")

    print()
    print("=" * 60)
    print("Step 5: Send a user message and wait for turn")
    print("=" * 60)
    msg_result = api("POST", f"/v1/sessions/{session_id}/messages", {
        "type": "user.message",
        "content": [{"type": "text", "text": "What does the e2e test skill say?"}],
    })
    print(f"  sent message: {msg_result.get('id', 'ok')}")

    # Poll for agent response (up to 3 minutes — codex can be slow)
    deadline = time.monotonic() + 180
    got_response = False
    last_event_count = 0
    while time.monotonic() < deadline:
        events = api("GET", f"/v1/sessions/{session_id}/events")
        items = events.get("events", events.get("data", []))
        if len(items) > last_event_count:
            for ev in items[last_event_count:]:
                etype = ev.get("type", "")
                if etype == "agent.message":
                    content = ev.get("content", [])
                    text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    if text.strip():
                        print(f"\n  AGENT MESSAGE: {text[:200]}")
                        got_response = True
                elif etype == "session.error":
                    data = ev.get("data", {})
                    err_msg = data.get("error", {}).get("message", "") if isinstance(data.get("error"), dict) else str(data.get("error", ""))
                    print(f"\n  SESSION ERROR: {err_msg[:500]}")
                elif etype == "session.status":
                    phase = ev.get("phase", "")
                    if phase == "idle" and got_response:
                        print(f"\n  Session idle, done.")
                        break
            last_event_count = len(items)
        if got_response and any(
            ev.get("type") == "session.status" and ev.get("phase") == "idle"
            for ev in items
        ):
            break
        time.sleep(3)

    if not got_response:
        print("\n  WARNING: No agent.message received within timeout")
        # Dump all events for debugging
        events = api("GET", f"/v1/sessions/{session_id}/events")
        items = events.get("events", events.get("data", []))
        print(f"  Total events: {len(items)}")
        for ev in items[-10:]:
            etype = ev.get("type", "")
            data = ev.get("data", {})
            print(f"    {etype}:")
            print(f"      {json.dumps(data, indent=2)[:500]}")

    print()
    print("=" * 60)
    print("CLEANUP: Delete session, agent, skill")
    print("=" * 60)
    try: api("DELETE", f"/v1/sessions/{session_id}")
    except: pass
    try: api("DELETE", f"/v1/agents/{agent_id}")
    except: pass
    try: api("DELETE", f"/v1/skills/{skill_id}")
    except: pass
    print("  Done")

    return 0 if got_response else 1

if __name__ == "__main__":
    sys.exit(main())
