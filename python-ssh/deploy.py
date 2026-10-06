#!/usr/bin/env python3
"""Deploy meta-harness to 203 server.

Steps:
1. Pull latest code on 203
2. Rebuild Docker images (meta-harness + codex-bridge)
3. Restart containers
4. Verify health
"""
import sys
import time
from ssh_helper import get_ssh_client, run_cmd, run_cmd_print


def deploy():
    client = get_ssh_client()

    print("=" * 60)
    print("Step 1: Pull latest code")
    print("=" * 60)

    # Pull latest code for meta-harness
    run_cmd_print(client, "cd /home/wei/meta-harness/meta-harness && git pull origin master", timeout=60)

    # Pull latest code for meta-harness-ext (if it's a separate repo)
    run_cmd_print(client, "cd /home/wei/meta-harness/meta-harness-ext && git pull origin master 2>/dev/null || echo 'ext not a git repo or no changes'", timeout=60)

    print()
    print("=" * 60)
    print("Step 2: Rebuild Docker images")
    print("=" * 60)

    # Build the images - this takes time
    exit_code, out, err = run_cmd_print(
        client,
        "cd /home/wei/meta-harness/meta-harness/deploy && docker compose build meta-harness codex-bridge",
        timeout=600  # 10 minutes for build
    )

    if exit_code != 0:
        print(f"BUILD FAILED: exit={exit_code}")
        print(err)
        client.close()
        return False

    print()
    print("=" * 60)
    print("Step 3: Restart containers")
    print("=" * 60)

    run_cmd_print(client, "cd /home/wei/meta-harness/meta-harness/deploy && docker compose up -d meta-harness codex-bridge", timeout=120)

    print()
    print("Waiting for containers to start...")
    time.sleep(10)

    print()
    print("=" * 60)
    print("Step 4: Verify health")
    print("=" * 60)

    # Check container status
    run_cmd_print(client, "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'meta-harness|codex-bridge'")

    # Check Go server health
    print()
    run_cmd_print(client, "curl -s http://localhost:8787/health")

    # Check bridge health
    run_cmd_print(client, "curl -s http://localhost:8092/health 2>/dev/null || docker exec deploy-codex-bridge-1 curl -s http://127.0.0.1:8092/health 2>/dev/null || echo 'bridge health check via container'")

    # Test /codex/rpc endpoint
    print()
    print("Testing /codex/rpc endpoint...")
    run_cmd_print(client, """curl -s -X POST http://localhost:8092/codex/rpc -H 'Content-Type: application/json' -d '{"session_id":"deploy-test","method":"mcpServerStatus/list"}' | head -c 500""")

    client.close()
    print()
    print("=" * 60)
    print("Deploy complete!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = deploy()
    sys.exit(0 if success else 1)
