#!/usr/bin/env python3
"""Quick test for Tavily web_search and Jina Reader web_fetch."""

import asyncio
import os
import sys

# Add harness to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "meta-harness", "harness"))

# Load .env
env_file = os.path.join(os.path.dirname(__file__), "meta-harness", ".env")
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


async def test_tavily():
    """Test Tavily web_search."""
    print("\n=== Testing Tavily web_search ===")
    try:
        from oma_adapter.web_search.core import search_web
        from oma_adapter.web_search.runtime import WebSearchRuntime, configure_web_search

        # Configure Tavily backend
        configure_web_search(WebSearchRuntime(
            backend="tavily",
            tavily_api_key=os.environ.get("TAVILY_API_KEY"),
        ))

        result = await search_web("What is the capital of France?", max_results=3)
        print(f"✓ Tavily search succeeded")
        print(f"Result preview: {result[:500]}...")
        return True
    except Exception as e:
        print(f"✗ Tavily search failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_jina():
    """Test Jina Reader web_fetch."""
    print("\n=== Testing Jina Reader web_fetch ===")
    try:
        from oma_adapter.web_fetch.core import fetch_web_content

        # Fetch a Wikipedia page
        url = "https://en.wikipedia.org/wiki/Paris"
        result = await fetch_web_content(url, cap=5000)
        print(f"✓ Jina Reader fetch succeeded")
        print(f"Result preview: {result[:500]}...")
        return True
    except Exception as e:
        print(f"✗ Jina Reader fetch failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    print("Testing web tools with new API backends...")
    print(f"TAVILY_API_KEY: {'set' if os.environ.get('TAVILY_API_KEY') else 'NOT SET'}")
    print(f"JINA_API_KEY: {'set' if os.environ.get('JINA_API_KEY') else 'NOT SET'}")

    tavily_ok = await test_tavily()
    jina_ok = await test_jina()

    print("\n=== Summary ===")
    print(f"Tavily web_search: {'✓ OK' if tavily_ok else '✗ FAILED'}")
    print(f"Jina Reader web_fetch: {'✓ OK' if jina_ok else '✗ FAILED'}")

    if tavily_ok and jina_ok:
        print("\n✓ Both tools working! Ready to rerun GAIA benchmark.")
        return 0
    else:
        print("\n✗ Some tools failed. Check errors above.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
