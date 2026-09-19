#!/usr/bin/env python3
"""Quick test for Jina Reader web_fetch."""

import asyncio
import os
import sys

# Set environment variables
os.environ["JINA_API_KEY"] = "jina_91aea80b9adb44d3b95f9218c7a8ec9dTKitm7fV85nJHP-NuVEwNldJwVxh"

# Add harness to path
sys.path.insert(0, "/Users/t-wangwei07/Downloads/workspacePy/mycode/oma/meta-harness/harness")


async def test_jina():
    """Test Jina Reader web_fetch directly."""
    print("Testing Jina Reader directly...")
    print(f"JINA_API_KEY: {os.environ.get('JINA_API_KEY')[:20]}...")

    # Import after setting env vars
    from oma_adapter.web_fetch.core import _jina_reader_fetch

    url = "https://en.wikipedia.org/wiki/Paris"
    print(f"\nFetching: {url}")

    result = await _jina_reader_fetch(url, cap=5000)

    if result:
        print(f"\n✓ Jina Reader succeeded!")
        print(f"Got {len(result)} chars of markdown")
        print(f"\nPreview:\n{result[:500]}...")
        return True
    else:
        print(f"\n✗ Jina Reader failed - returned None")
        return False


if __name__ == "__main__":
    success = asyncio.run(test_jina())
    sys.exit(0 if success else 1)
