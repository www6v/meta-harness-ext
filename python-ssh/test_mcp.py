#!/usr/bin/env python3
"""E2E MCP test with new API key."""
import os
import sys
import json
import time
import re

# Add parent directory to path to import ssh_helper
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ssh_helper import get_ssh_client, run_cmd_print


def main():
    API_KEY = "oma_fffvhmckapi3Sh0qbnlC9weLCSfDB1lPjbHQ"
    CRED_ID = "cred-dtl589qr0b2zmhup"

    client = get_ssh_client()

    # Step 4: Create agent with GitHub MCP server
    print("=== Step 4: Create agent with GitHub MCP server ===")
    create_agent = f'''curl -s -X POST http://localhost:8787/v1/agents -H "X-API-Key: {API_KEY}" -H "Content-Type: application/json" -d '{{"name": "github-test-agent", "system": "You are a helpful assistant with GitHub access.", "model": {{"id": "qwen3.7-plus", "speed": "standard"}}, "harness": "codex", "mcp_servers": [{{"name": "github", "vault_credential_ids": ["{CRED_ID}"]}}], "tools": [{{"type": "agent_toolset_20260401", "default_config": {{"enabled": true, "permission_policy": {{"type": "always_allow"}}}}}}]}}' | python3 -m json.tool'''
    exit_code, out, err = run_cmd_print(client, create_agent)

    # Extract agent ID
    match = re.search(r'"id":\s*"([^"]+)"', out)
    agent_id = match.group(1) if match else None
    print(f"\nAgent ID: {agent_id}")

    # Step 5: List environments
    print("\n=== Step 5: List environments ===")
    list_env = f'curl -s -H "X-API-Key: {API_KEY}" http://localhost:8787/v1/environments | python3 -m json.tool | head -40'
    run_cmd_print(client, list_env)

    # Step 6: Create session
    print("\n=== Step 6: Create session ===")
    create_session = f'''curl -s -X POST http://localhost:8787/v1/sessions -H "X-API-Key: {API_KEY}" -H "Content-Type: application/json" -d '{{"agent": "{agent_id}", "environment": "env-7l08biijf8vawfku"}}' | python3 -m json.tool'''
    exit_code, out, err = run_cmd_print(client, create_session)

    # Extract session ID
    match = re.search(r'"id":\s*"(sess-[^"]+)"', out)
    session_id = match.group(1) if match else None
    print(f"\nSession ID: {session_id}")

    if not session_id:
        print("Failed to create session")
        client.close()
        return

    # Step 7: Send test message
    print("\n=== Step 7: Send test message ===")
    send_msg = f'''curl -s -X POST http://localhost:8787/v1/sessions/{session_id}/messages -H "X-API-Key: {API_KEY}" -H "Content-Type: application/json" -d '{{"role": "user", "content": [{{"type": "text", "text": "我要知道我的github中有多少repo"}}]}}' | python3 -m json.tool'''
    run_cmd_print(client, send_msg)

    # Step 8: Wait for completion
    print("\n=== Step 8: Wait 60 seconds for turn to complete ===")
    time.sleep(60)

    # Step 9: Check session status
    print("\n=== Step 9: Check session status ===")
    status_cmd = f'curl -s -H "X-API-Key: {API_KEY}" http://localhost:8787/v1/sessions/{session_id} | grep -o "\\"status\\":\\"[^\\"]*\\""'
    run_cmd_print(client, status_cmd)

    # Step 10: Get MCP tool events
    print("\n=== Step 10: Get MCP tool events ===")
    events_cmd = f'curl -s -H "X-API-Key: {API_KEY}" "http://localhost:8787/v1/sessions/{session_id}/events?limit=200" | python3 -m json.tool | grep -B 2 -A 10 "mcp_tool_use"'
    run_cmd_print(client, events_cmd)

    # Step 11: Get final assistant message
    print("\n=== Step 11: Get final assistant message ===")
    msg_cmd = f'''curl -s -H "X-API-Key: {API_KEY}" "http://localhost:8787/v1/sessions/{session_id}/events?limit=500" | python3 -c "
import sys, json
data = json.load(sys.stdin)
events = data.get('data', [])
msgs = [e for e in events if e['type'] == 'agent.message']
if msgs:
    last_msg = msgs[-1]
    content = last_msg.get('data', {{}}).get('content', [])
    for c in content:
        if c.get('type') == 'text':
            print(c['text'][:500])
"'''
    run_cmd_print(client, msg_cmd)

    client.close()


if __name__ == "__main__":
    main()
