"""Test sub-agent spawning in codex.

1. Create a codex thread with instructions that mention a sub-agent.
2. Ask the model to delegate to a sub-agent.
3. Collect events — look for SubAgentActivity items.
4. If we see a child thread, fetch its content via thread/items/list.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from codex_tunnel import SSHTunnel
from codex_client import CodexClient, CodexConfig


async def main() -> None:
    with SSHTunnel(
        ssh_host="124.221.28.203",
        ssh_user="root",
        ssh_password="1qaZxsw@",
        remote_port=8765,
    ) as t:
        client = CodexClient(CodexConfig(
            ws_url=t.local_url,
            token="codex-poc-token-2024",
            init_timeout=20.0,
            turn_timeout=180.0,
        ))
        await client.connect()

        instructions = """You are a test coordinator agent.

## Callable Sub-Agents

You can delegate tasks to the following sub-agents by invoking the
`spawn_agent` tool:

- **worker** (id: `sub_worker`): A helper that writes code and answers
  technical questions. Use it for coding subtasks.

When a task has a clear subtask that matches a sub-agent's specialty,
spawn the agent and let it work. Otherwise just respond directly."""

        prompt = (
            "I need a Python function that computes fibonacci numbers. "
            "Please delegate this to the worker sub-agent."
        )

        print(f"\n=== Prompt: {prompt}\n")

        events_seen: list[str] = []

        async def on_event(ev):
            t = ev.get("type", "?")
            events_seen.append(t)
            if "thread" in t or "sub_agent" in t:
                print(f"  >>> {t}: {json.dumps(ev, ensure_ascii=False)[:200]}")

        result = await client.run_turn(
            prompt,
            session_id="subagent-test",
            instructions=instructions,
            on_event=on_event,
        )

        print(f"\n=== Result ===")
        print(f"terminal_state: {result.terminal_state}")
        print(f"sub_agents: {len(result.sub_agents)}")
        for sa in result.sub_agents:
            print(f"  - thread={sa.thread_id} nickname={sa.nickname} "
                  f"started={sa.started} completed={sa.completed}")
        print(f"events: {len(result.events)}")
        type_counts: dict[str, int] = {}
        for ev in result.events:
            t = ev.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        for t, c in sorted(type_counts.items()):
            print(f"  {t}: {c}")
        print(f"\nfinal_text: {result.final_text[:500]}")

        # If we found sub-agents, their content was fetched.
        # Check for thread-scoped events.
        thread_events = [ev for ev in result.events if ev.get("session_thread_id")]
        print(f"\nthread-scoped events: {len(thread_events)}")
        for ev in thread_events[:5]:
            print(f"  {ev.get('type')} thread={ev.get('session_thread_id')}")

        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
