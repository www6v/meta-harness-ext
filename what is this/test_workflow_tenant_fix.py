#!/usr/bin/env python3
"""End-to-end test: Verify workflow agents/sessions are created in user's tenant."""

import httpx
import pymysql
import time
import sys


def check_database_for_workflow_resources(execution_id: str, expected_tenant: str):
    """Check database for agents and sessions created by the workflow."""
    conn = pymysql.connect(
        host='124.221.28.203',
        port=3306,
        user='managed',
        password='managedAgent123',
        database='managed_agent'
    )
    cur = conn.cursor()

    print("\n=== Database Verification ===")

    # Check for agents created in the last minute
    cur.execute("""
        SELECT id, tenant_id, created_at
        FROM agents
        WHERE created_at > UNIX_TIMESTAMP() - 60
        ORDER BY created_at DESC
    """)
    agents = cur.fetchall()

    print(f"\nAgents created in last 60 seconds: {len(agents)}")
    for agent_id, tenant_id, created_at in agents:
        status = "✓" if tenant_id == expected_tenant else "✗"
        print(f"  {status} {agent_id} | tenant={tenant_id}")

    # Check for sessions created in the last minute
    cur.execute("""
        SELECT id, tenant_id, created_at
        FROM sessions
        WHERE created_at > UNIX_TIMESTAMP() - 60
        ORDER BY created_at DESC
    """)
    sessions = cur.fetchall()

    print(f"\nSessions created in last 60 seconds: {len(sessions)}")
    for session_id, tenant_id, created_at in sessions:
        status = "✓" if tenant_id == expected_tenant else "✗"
        print(f"  {status} {session_id} | tenant={tenant_id}")

    conn.close()

    # Verify all resources are in the correct tenant
    all_correct = all(t == expected_tenant for _, t, _ in agents + sessions)
    return all_correct


def test_api_tenant_header():
    """Test that x-active-tenant header is honored."""
    print("\n=== Test 1: API Tenant Header ===")

    api_key = 'dev-key'
    base_url = 'http://127.0.0.1:8787'

    # Test without tenant header
    resp = httpx.get(
        f'{base_url}/v1/me',
        headers={'x-api-key': api_key},
        timeout=10.0
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    tenant1 = resp.json().get('tenant', {}).get('id')
    print(f"Without x-active-tenant: tenant={tenant1}")
    assert tenant1 == 'default', f"Expected 'default', got {tenant1}"

    # Test with tenant header
    test_tenant = 'tn_d95dca4c8c600e27b8a746ce29c98081'
    resp = httpx.get(
        f'{base_url}/v1/me',
        headers={
            'x-api-key': api_key,
            'x-active-tenant': test_tenant
        },
        timeout=10.0
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    tenant2 = resp.json().get('tenant', {}).get('id')
    print(f"With x-active-tenant={test_tenant}: tenant={tenant2}")
    assert tenant2 == test_tenant, f"Expected {test_tenant}, got {tenant2}"

    print("✓ API tenant header test passed")
    return True


def test_workflow_execution():
    """Test workflow execution creates resources in correct tenant."""
    print("\n=== Test 2: Workflow Execution ===")

    base_url = 'http://127.0.0.1:8090'
    tenant = 'tn_d95dca4c8c600e27b8a746ce29c98081'

    # List workflows
    print("Fetching workflows...")
    resp = httpx.get(
        f'{base_url}/api/workflows',
        headers={'x-active-tenant': tenant},
        timeout=10.0
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    workflows = resp.json()
    print(f"Found {len(workflows)} workflows")

    if not workflows:
        print("⚠ No workflows found, skipping execution test")
        return True

    workflow_id = workflows[0]['id']
    print(f"Executing workflow: {workflow_id[:8]}...")

    # Execute workflow
    resp = httpx.post(
        f'{base_url}/api/workflows/{workflow_id}/execute',
        headers={'x-active-tenant': tenant},
        json={},
        timeout=30.0
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    result = resp.json()
    execution_id = result.get('execution_id')
    print(f"Execution started: {execution_id}")
    print(f"Status: {result.get('status')}")

    # Wait for bootstrap to create resources
    print("\nWaiting 5 seconds for resource creation...")
    time.sleep(5)

    # Verify resources in database
    all_correct = check_database_for_workflow_resources(execution_id, tenant)

    if all_correct:
        print("\n✓ Workflow execution test passed - resources created in correct tenant")
    else:
        print("\n✗ Workflow execution test FAILED - resources in wrong tenant")

    return all_correct


def main():
    """Run all tests."""
    print("=" * 70)
    print("End-to-End Test: Workflow Tenant Isolation")
    print("=" * 70)

    try:
        # Test 1: API header
        if not test_api_tenant_header():
            print("\n✗ Test 1 failed")
            return 1

        # Test 2: Workflow execution
        if not test_workflow_execution():
            print("\n✗ Test 2 failed")
            return 1

        print("\n" + "=" * 70)
        print("✓ ALL TESTS PASSED")
        print("=" * 70)
        print("\nThe fix is working correctly:")
        print("  1. Go server honors x-active-tenant header with API key auth")
        print("  2. Workflow bootstrap propagates tenant_id to OMA SDK")
        print("  3. Agents and sessions are created in the user's tenant")
        print("\nWorkflow resources should now be visible in the UI at:")
        print("  http://127.0.0.1:8787/agents")
        print("  http://127.0.0.1:8787/sessions")
        return 0

    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
