"""Quick test: create skill -> agent -> session -> send msg -> check events."""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8787"
API_KEY = "dev-key"

def api(method, path, body=None):
    url = BASE + path
    data = None
    headers = {"X-API-Key": API_KEY}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {method} {path}: {e.read().decode()[:300]}", file=sys.stderr)
        raise

# 1. Create skill
skill_body = """---
name: e2e-test-skill
description: Test skill for end-to-end validation
---

# E2E Test Skill

When asked about the test phrase, respond with:
"The codex skill injection is working!"
"""
skill = api("POST", "/v1/skills", {
    "display_title": "E2E Test Skill",
    "name": "e2e-test-skill",
    "description": "Test skill for e2e validation",
    "files": [
        {"filename": "SKILL.md", "content": skill_body, "encoding": "utf8"},
    ],
})
skill_id = skill["id"]
print(f"skill: {skill_id}")

# 2. Create agent
agent = api("POST", "/v1/agents", {
    "name": "codex-e2e-test",
    "model": "codex",
    "system": "You are a helpful assistant. Follow the instructions in any skill files mounted in /home/codex/.codex/skills/.",
    "description": "Codex agent for e2e skill test",
    "skills": [{"type": "custom", "skill_id": skill_id, "version": "latest"}],
    "_oma": {"harness": "codex"},
})
agent_id = agent["id"]
print(f"agent: {agent_id}")

# 3. Create session
session = api("POST", "/v1/sessions", {"agent": {"id": agent_id}})
session_id = session["id"]
print(f"session: {session_id}")

# 4. Send message
msg = api("POST", f"/v1/sessions/{session_id}/messages", {
    "type": "user.message",
    "content": [{"type": "text", "text": "What does the e2e test skill say?"}],
})
print(f"message sent: {msg.get('id', 'ok')}")

# 5. Poll events
deadline = time.monotonic() + 120
done = False
last_count = 0
while time.monotonic() < deadline:
    resp = api("GET", f"/v1/sessions/{session_id}/events")
    events = resp.get("data", [])
    if len(events) > last_count:
        for ev in events[last_count:]:
            etype = ev.get("type", "")
            if etype == "agent.message":
                inner = ev.get("data", {})
                content = inner.get("content", [])
                text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                if text.strip():
                    print(f"\n  AGENT: {text[:300]}")
                    done = True
            elif etype == "session.error":
                inner = ev.get("data", {})
                print(f"\n  ERROR: {json.dumps(inner)[:300]}")
            elif etype == "session.status_idle":
                if done:
                    print("\n  DONE (idle)")
                    break
        last_count = len(events)
    if done and any(e.get("type") == "session.status_idle" for e in events):
        break
    time.sleep(2)

if not done:
    print(f"\n  TIMEOUT. Total events: {last_count}")

# 6. Cleanup
try: api("DELETE", f"/v1/sessions/{session_id}")
except: pass
try: api("DELETE", f"/v1/agents/{agent_id}")
except: pass
try: api("DELETE", f"/v1/skills/{skill_id}")
except: pass
print("cleaned up")
