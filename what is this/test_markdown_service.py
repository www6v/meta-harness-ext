#!/usr/bin/env python3
"""Test the remote Markdown conversion service."""

import asyncio
import os
import sys

import httpx


async def test_markdown_service():
    """Test remote Markdown service."""
    service_url = os.environ.get("MARKDOWN_SERVICE_URL", "http://127.0.0.1:8899")
    test_url = "https://en.wikipedia.org/wiki/Paris"

    print(f"Testing Markdown Service...")
    print(f"Service URL: {service_url}")
    print(f"Test URL: {test_url}")

    endpoint = f"{service_url.rstrip('/')}/convert"
    headers = {"Content-Type": "application/json"}
    payload = {
        "url": test_url,
        "max_length": 5000,
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            print("\nSending request...")
            response = await client.post(endpoint, headers=headers, json=payload)
            print(f"Status: {response.status_code}")
            response.raise_for_status()
            markdown = response.text
            print(f"\n✓ Success! Got {len(markdown)} chars of markdown")
            print(f"\nPreview:\n{markdown[:500]}...")
            return True
    except Exception as e:
        print(f"\n✗ Failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_markdown_service())
    sys.exit(0 if success else 1)
