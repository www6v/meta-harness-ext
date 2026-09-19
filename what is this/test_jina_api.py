#!/usr/bin/env python3
"""Quick test for Jina Reader API."""

import asyncio
import httpx


async def test_jina_api():
    """Test Jina Reader API directly."""
    jina_api_key = "jina_91aea80b9adb44d3b95f9218c7a8ec9dTKitm7fV85nJHP-NuVEwNldJwVxh"
    url = "https://en.wikipedia.org/wiki/Paris"
    jina_url = f"https://r.jina.ai/{url}"

    print(f"Testing Jina Reader API...")
    print(f"URL: {url}")
    print(f"Jina URL: {jina_url}")
    print(f"API Key: {jina_api_key[:20]}...")

    headers = {
        "Authorization": f"Bearer {jina_api_key}",
        "Accept": "text/plain",
        "X-Return-Format": "markdown",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            print("\nSending request...")
            response = await client.get(jina_url, headers=headers)
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
    import sys
    success = asyncio.run(test_jina_api())
    sys.exit(0 if success else 1)
