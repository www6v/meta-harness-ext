#!/usr/bin/env python3
"""Check MCP test results."""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ssh_helper import get_ssh_client, run_cmd_print


def main():
    API_KEY = "oma_fffvhmckapi3Sh0qbnlC9weLCSfDB1lPjbHQ"
    session_id = "sess-vuwbtafw8sizue9a"

    client = get_ssh_client()

    # Check status
    print("=== Session Status ===")
    status_cmd = f'curl -s -H "X-API-Key: {API_KEY}" http://localhost:8787/v1/sessions/{session_id} | python3 -c "import sys, json; d=json.load(sys.stdin); print(json.dumps({{\\"status\\": d[\\"status\\"], \\"usage\\": d.get(\\"usage\\", {{}})}}, indent=2))"'
    run_cmd_print(client, status_cmd)

    # Get all events
    print("\n=== All Events (summary) ===")
    events_cmd = f'curl -s -H "X-API-Key: {API_KEY}" "http://localhost:8787/v1/sessions/{session_id}/events?limit=500" | python3 -c "import sys, json; data=json.load(sys.stdin); events=data.get(\'data\', []); print(f\'Total events: {len(events)}\'); [print(f\'  - {{e[\'type\']}}\') for e in events]"'
    run_cmd_print(client, events_cmd)

    # Get MCP related events
    print("\n=== MCP Tool Events ===")
    mcp_cmd = f'curl -s -H "X-API-Key: {API_KEY}" "http://localhost:8787/v1/sessions/{session_id}/events?limit=500" | python3 -m json.tool | grep -i -A 5 "mcp"'
    run_cmd_print(client, mcp_cmd)

    # Get final assistant message
    print("\n=== Final Assistant Message ===")
    msg_cmd = f'''curl -s -H "X-API-Key: {API_KEY}" "http://localhost:8787/v1/sessions/{session_id}/events?limit=500" | python3 -c "
import sys, json
data = json.load(sys.stdin)
events = data.get('data', [])
msgs = [e for e in events if e.get('type') == 'agent.message']
if msgs:
    last_msg = msgs[-1]
    content = last_msg.get('data', {{}}).get('content', [])
    for c in content:
        if c.get('type') == 'text':
            print(c['text'][:1000])
"'''
    run_cmd_print(client, msg_cmd)

    # Check bridge logs
    print("\n=== Bridge Logs (MCP related) ===")
    logs_cmd = 'docker logs deploy-codex-bridge-1 2>&1 | grep -i "mcp" | tail -20'
    run_cmd_print(client, logs_cmd)

    client.close()


if __name__ == "__main__":
    main()
