"""Smoke test: create session, ask Codex to create a file, check files are uploaded."""
import urllib.request
import urllib.parse
import json
import time
import sys

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
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def create_session(agent_id):
    resp = api_post("/v1/sessions", {
        "agent": agent_id,
        "title": "codex-smoke-test",
    })
    if "id" in resp:
        return resp["id"]
    if "data" in resp and "id" in resp["data"]:
        return resp["data"]["id"]
    raise RuntimeError(f"Create session failed: {resp}")

def send_message(session_id, text):
    return api_post(f"/v1/sessions/{session_id}/events", {
        "events": [{"type": "user.message", "content": [{"type": "text", "text": text}]}]
    })

def poll_events(session_id, timeout=300):
    start = time.time()
    last_count = 0
    while time.time() - start < timeout:
        resp = api_get(f"/v1/sessions/{session_id}/events", {"limit": 500})
        events = resp.get("data", resp.get("events", []))
        if not isinstance(events, list):
            events = []
        count = len(events)

        has_completed = any(e["type"] == "turn.completed" for e in events)
        has_error = any(e["type"] == "session.error" for e in events)

        if has_completed or has_error:
            print(f"  Turn finished: completed={has_completed}, error={has_error}, events={count}")
            sys.stdout.flush()
            return events

        if count != last_count:
            print(f"  events={count}...", end=" ", flush=True)
        last_count = count
        time.sleep(3)

    print(f"  TIMEOUT after {timeout}s with {last_count} events")
    return events

def analyze_events(events):
    print(f"\n=== Event Analysis ({len(events)} events) ===")
    sys.stdout.flush()

    type_counts = {}
    for e in events:
        t = e["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    print("Event types:")
    for t, c in sorted(type_counts.items()):
        print(f"  {t}: {c}")

    print("\nTool calls:")
    for e in events:
        if e["type"] in ("agent.tool_use", "agent.custom_tool_use"):
            name = e["data"].get("name", "")
            inp = e["data"].get("input", {})
            cmd = inp.get("command", inp.get("content", ""))
            if isinstance(cmd, str) and len(cmd) > 100:
                cmd = cmd[:100] + "..."
            print(f"  seq={e['seq']}: {e['type']} name={name} input={cmd}")

    print("\nTool results with files:")
    files_found = 0
    for e in events:
        if e["type"] == "agent.tool_result":
            data = e["data"]
            files = data.get("files", [])
            if files:
                files_found += len(files)
                print(f"  seq={e['seq']}: {len(files)} files attached!")
                for f in files:
                    print(f"    - {f.get('filename', '?')} ({f.get('size_bytes', '?')} bytes)")

    for e in events:
        if e["type"] == "session.error":
            print(f"\nERROR: {e['data'].get('message', '')[:300]}")

    for e in reversed(events):
        if e["type"] == "agent.message":
            text = "".join(b.get("text", "") for b in e["data"].get("content", []))
            if text.strip():
                print(f"\nLast agent message:\n{text[:500]}")
            break

    print(f"\nTotal files found: {files_found}")
    return events

def main():
    AGENT_ID = "agent-lokumuln7fzabvkq"
    print("=== Codex Smoke Test ===\n")
    sys.stdout.flush()

    print("1. Creating session...")
    sid = create_session(AGENT_ID)
    print(f"   Session: {sid}")
    print(f"   Agent: {AGENT_ID}")
    sys.stdout.flush()

    print("\n2. Sending message...")
    msg = "Create a Python file called hello.py with content that prints 'Hello from Codex!'. Use bash to write the file."
    print(f"   Message: {msg}")
    send_message(sid, msg)
    sys.stdout.flush()

    print("\n3. Polling for turn completion (up to 5 min)...")
    sys.stdout.flush()
    events = poll_events(sid, timeout=300)

    analyze_events(events)

    print("\n4. Checking files via API...")
    try:
        resp = api_get("/v1/files", {"limit": 10})
        files = resp.get("data", resp.get("files", []))
        if not isinstance(files, list):
            files = []
        print(f"   Found {len(files)} files via API")
        for f in files[:10]:
            print(f"   - {f.get('filename', '?')} id={f.get('id', '?')}")
    except Exception as ex:
        print(f"   Files API error: {ex}")

    print("\n=== Done ===")
    return sid

if __name__ == "__main__":
    sid = main()
