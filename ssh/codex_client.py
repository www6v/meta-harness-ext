"""Codex app-server WebSocket client.

Speaks JSON-RPC 2.0 to a remote `codex app-server` per the protocol
documented in codex-ext/analysis/app-server-api-reference.md.

Flow per turn:
    1. initialize
    2. thread/start  (reuse cached thread id for subsequent turns)
    3. turn/start    (with the user prompt)
    4. consume ServerNotifications until turn/completed

Maps Codex events to OMA event types:
    item/agentMessage/delta      -> agent.message (accumulated)
    item/reasoning/textDelta     -> agent.thinking (accumulated)
    item/started                 -> agent.tool_use   (best-effort)
    item/completed               -> agent.tool_result (best-effort)
    turn/completed               -> finalization
    error                        -> session.error
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

try:
    import websockets
    from websockets.asyncio.client import ClientConnection
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "websockets is required: pip install websockets"
    ) from e


logger = logging.getLogger("codex.client")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# OMA event (as dict). The Go layer serializes these to JSON.
OmaEvent = dict[str, Any]

EventHandler = Callable[[OmaEvent], Awaitable[None]] | Callable[[OmaEvent], None]


@dataclass
class CodexConfig:
    """Connection settings for the codex app-server."""

    ws_url: str  # e.g. ws://124.221.28.203:5432
    token: Optional[str] = None  # Bearer token, if any
    model: str = "codex"  # provider label used in usage events
    init_timeout: float = 15.0
    turn_timeout: float = 300.0
    client_name: str = "meta-harness"
    client_version: str = "0.1.0"


@dataclass
class CodexFileOutput:
    """A file extracted from a codex turn."""

    filename: str  # basename, e.g. "report.py"
    path: str  # full path as reported by codex
    content_base64: str  # base64-encoded file content
    media_type: str = "application/octet-stream"
    size_bytes: int = 0


@dataclass
class CodexSubAgent:
    """A sub-agent thread spawned during a codex turn."""

    thread_id: str  # codex child thread id
    parent_thread_id: str  # codex parent thread id
    nickname: str = ""  # agent_nickname from codex
    role: str = ""  # agent_role from codex
    source: str = ""  # e.g. "subagent_thread_spawn"
    started: bool = False
    completed: bool = False


@dataclass
class CodexTurnResult:
    """Result of running one turn."""

    events: list[OmaEvent] = field(default_factory=list)
    final_text: str = ""
    reasoning_text: str = ""
    usage: Optional[dict[str, int]] = None
    terminal_state: str = "unknown"
    # Codex thread id, kept so callers can resume on the same thread.
    thread_id: Optional[str] = None
    # Files created/modified during the turn.
    files: list[CodexFileOutput] = field(default_factory=list)
    # Sub-agent threads spawned during the turn (codex-native spawn_agent).
    sub_agents: list[CodexSubAgent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CodexClient:
    """Async JSON-RPC client for the codex app-server.

    One client manages one WebSocket connection. Call :meth:`connect`
    before :meth:`run_turn`, and :meth:`close` when done. Use as an async
    context manager for convenience.

    The client keeps a cached ``thread_id`` so multiple turns reuse the
    same codex thread (codex maintains conversation state server-side).
    """

    def __init__(self, config: CodexConfig) -> None:
        self.cfg = config
        self._ws: ClientConnection | None = None
        self._rpc_id: int = 0
        self._thread_id: Optional[str] = None
        self._initialized: bool = False

    # ---- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket and perform the JSON-RPC ``initialize``.

        If the connection is already open but not initialized (e.g. after
        a session switch reset), re-run initialize on the existing socket.
        """
        if self._ws is None:
            headers = {}
            if self.cfg.token:
                headers["Authorization"] = f"Bearer {self.cfg.token}"
            logger.info("codex connecting to %s", self.cfg.ws_url)
            self._ws = await websockets.connect(
                self.cfg.ws_url,
                additional_headers=headers or None,
                open_timeout=self.cfg.init_timeout,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            )
        if not self._initialized:
            await self._initialize()

    async def _initialize(self) -> None:
        assert self._ws is not None
        try:
            resp = await self._call(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientInfo": {
                        "name": self.cfg.client_name,
                        "version": self.cfg.client_version,
                    },
                },
                timeout=self.cfg.init_timeout,
            )
            result = resp.get("result", {})
            logger.info("codex initialized: codexHome=%s", result.get("codexHome"))
        except RuntimeError as e:
            # The codex server may reject a second initialize on the same
            # connection with "Already initialized". That's fine — we just
            # need the connection to be usable.
            if "Already initialized" in str(e) or "already initialized" in str(e):
                logger.info("codex already initialized (reusing connection)")
            else:
                raise
        self._initialized = True

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover
                pass
            self._ws = None
            self._initialized = False

    async def __aenter__(self) -> "CodexClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ---- public API --------------------------------------------------------

    async def run_turn(
        self,
        prompt: str,
        *,
        session_id: str = "",
        instructions: Optional[str] = None,
        skills: Optional[list[dict[str, Any]]] = None,
        on_event: Optional[EventHandler] = None,
    ) -> CodexTurnResult:
        """Run a single codex turn and collect events.

        Args:
            prompt: the user message text
            session_id: OMA session id (used for logging / correlation)
            instructions: optional extra instructions prepended to the prompt
            skills: optional list of resolved skill payloads (each with
                ``system_prompt_addition`` and ``files``) to mount in the
                codex workspace and inject into the turn instructions
            on_event: optional callback invoked for each OMA event as it
                is produced (enables streaming). The callback may be sync
                or async.
        """
        if not self._initialized:
            await self.connect()
        assert self._ws is not None

        # 1. Ensure we have a thread.
        if self._thread_id is None:
            # Combine agent instructions with skill prompt additions so
            # codex sees them as part of the thread's system context.
            combined_instructions = _build_instructions(instructions, skills)
            self._thread_id = await self._start_thread(combined_instructions)
            # Mount skill files into the codex workspace so the agent can
            # reference SKILL.md and auxiliary files during the turn.
            if skills:
                await self._mount_skill_files(skills, session_id)
        elif skills:
            # For subsequent turns on an existing thread, we still need to
            # make sure skill files are present in the workspace. The
            # system_prompt_addition was already sent at thread/start.
            await self._mount_skill_files(skills, session_id)

        # 2. Start the turn.
        turn_start = time.monotonic()
        start_resp = await self._call(
            "turn/start",
            {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=30.0,
        )
        logger.info(
            "codex turn started session=%s thread=%s",
            session_id,
            self._thread_id,
        )

        # 3. Consume notifications until turn/completed (or timeout).
        result = CodexTurnResult(thread_id=self._thread_id)
        accumulated_text: list[str] = []
        accumulated_reasoning: list[str] = []
        msg_id = _oma_id()
        thinking_id = _oma_id()
        emitted_text = False
        emitted_thinking = False
        # Track fileChange items that completed successfully during the turn.
        # After the turn, we call fs/readFile to get the final content.
        completed_file_paths: list[str] = []

        deadline = time.monotonic() + self.cfg.turn_timeout
        # Inactivity timeout: if no WebSocket message arrives for 120s after
        # the last one, assume the codex server is done (it may not send
        # turn/completed). The previous 30s was too aggressive — codex turns
        # regularly pause for 30-60s during tool execution, web search,
        # file I/O, or complex reasoning.
        inactivity_timeout = 120.0
        last_msg_at = time.monotonic()

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            # Use the shorter of remaining time and inactivity timeout.
            recv_timeout = min(remaining, inactivity_timeout)
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=recv_timeout)
                last_msg_at = time.monotonic()
            except asyncio.TimeoutError:
                if time.monotonic() - last_msg_at >= inactivity_timeout:
                    # No message for 30s — assume turn is done.
                    result.terminal_state = "completed"
                    break
                # Otherwise the overall deadline expired.
                result.terminal_state = "timeout"
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})

            # Request responses (have "id") — ignore here, only notifications matter.
            if "id" in msg and method == "":
                continue

            if method == "item/agentMessage/delta":
                delta = params.get("textDelta", "") or params.get("delta", "")
                if delta:
                    accumulated_text.append(delta)
                    event = _agent_message_event(
                        msg_id, msg_id, "".join(accumulated_text)
                    )
                    result.events.append(event)
                    await _invoke(on_event, event)
                    emitted_text = True

            elif method == "item/reasoning/textDelta":
                delta = params.get("textDelta", "") or params.get("delta", "")
                if delta:
                    accumulated_reasoning.append(delta)
                    event = _agent_thinking_event(
                        thinking_id, "".join(accumulated_reasoning)
                    )
                    result.events.append(event)
                    await _invoke(on_event, event)
                    emitted_thinking = True

            elif method == "item/started":
                item = params.get("item", params)
                # Sub-agent lifecycle: SubAgentActivity items.
                if item.get("type") == "subAgentActivity":
                    sa = _handle_sub_agent_activity(item, result, self._thread_id or "")
                    if sa is not None:
                        # Emit session.thread_created + session.sub_agent_started.
                        created_event = _thread_created_event(
                            sa.thread_id,
                            sa.parent_thread_id,
                            sa.nickname or sa.role or "sub-agent",
                        )
                        result.events.append(created_event)
                        await _invoke(on_event, created_event)
                        started_event = _sub_agent_started_event(
                            sa.thread_id,
                            sa.nickname or sa.role or "sub-agent",
                        )
                        result.events.append(started_event)
                        await _invoke(on_event, started_event)
                else:
                    tool_event = _item_started_to_tool_use(item)
                    if tool_event is not None:
                        result.events.append(tool_event)
                        await _invoke(on_event, tool_event)

            elif method == "item/completed":
                item = params.get("item", params)
                if item.get("type") == "subAgentActivity":
                    sa = _handle_sub_agent_activity(item, result, self._thread_id or "")
                    if sa is not None and sa.completed:
                        # Emit session.sub_agent_completed + session.thread_idle.
                        completed_event = _sub_agent_completed_event(sa.thread_id)
                        result.events.append(completed_event)
                        await _invoke(on_event, completed_event)
                        idle_event = _thread_idle_event(sa.thread_id)
                        result.events.append(idle_event)
                        await _invoke(on_event, idle_event)
                else:
                    tool_event = _item_completed_to_tool_result(item)
                    if tool_event is not None:
                        result.events.append(tool_event)
                        await _invoke(on_event, tool_event)
                # Track successfully completed fileChange items.
                if item.get("type") == "fileChange":
                    status = item.get("status", "")
                    if status in ("completed", ""):
                        changes = item.get("changes", [])
                        for change in changes:
                            path = change.get("path", "")
                            kind = change.get("kind", "")
                            if path and kind != "delete":
                                completed_file_paths.append(path)

            elif method == "item/commandExecution/outputDelta":
                # Could be surfaced as incremental tool output; skipped for MVP.
                pass

            elif method == "turn/completed":
                status = params.get("status", {})
                turn = params.get("turn", {})
                if isinstance(status, dict):
                    result.terminal_state = status.get("state", "completed")
                    result.usage = status.get("usage")
                else:
                    result.terminal_state = str(status)
                # Pull final content from turn.items as a safety net.
                for item in turn.get("items", []):
                    itype = item.get("type")
                    if itype == "agentMessage":
                        t = item.get("text", "")
                        if t and not emitted_text:
                            accumulated_text.append(t)
                            emitted_text = True
                    elif itype == "reasoning":
                        for s in item.get("summary", []):
                            if isinstance(s, str) and s:
                                accumulated_reasoning.append(s)
                                emitted_thinking = True
                break

            elif method == "turn/started":
                # Informational; nothing to emit.
                pass

            elif method == "error":
                err_msg = params.get("message", str(params))
                event = _session_error_event(f"codex error: {err_msg}")
                result.events.append(event)
                await _invoke(on_event, event)
                result.terminal_state = "error"
                break

            elif method == "warning":
                # Log but don't surface as an OMA event.
                logger.warning(
                    "codex warning: %s", params.get("message", str(params))[:200]
                )

        # Ensure at least one agent.message even when the run produced none.
        if not emitted_text:
            final_text = "".join(accumulated_text)
            event = _agent_message_event(msg_id, msg_id, final_text)
            result.events.append(event)
            await _invoke(on_event, event)

        result.final_text = "".join(accumulated_text)
        result.reasoning_text = "".join(accumulated_reasoning)

        # 4. Extract file content for completed fileChange items.
        if completed_file_paths:
            result.files = await self._extract_files(
                completed_file_paths, session_id
            )

        # 5. Also scan for files created via bash (not fileChange).
        # If there were commandExecution items, try to find recently
        # created files in the workspace.
        bash_tool_uses = [
            e for e in result.events
            if e.get("type") == "agent.tool_use" and e.get("name") == "bash"
        ]
        if bash_tool_uses and not result.files:
            # Only scan if we haven't already extracted files from fileChange
            scanned = await self._scan_workspace_files(result.events, session_id)
            if scanned:
                result.files.extend(scanned)

        # 6. Fetch content from any sub-agent threads spawned during the turn.
        # Each sub-agent runs on a separate codex child thread; the parent
        # only sees SubAgentActivity markers. We read each child thread's
        # items and emit them as OMA events scoped to the child's
        # session_thread_id so the frontend can display the sub-agent's
        # conversation in its own tab.
        if result.sub_agents:
            await self._fetch_sub_agent_content(result, session_id, on_event)

        duration_ms = int((time.monotonic() - turn_start) * 1000)
        logger.info(
            "codex turn done session=%s state=%s chars=%d reasoning=%d "
            "duration_ms=%d events=%d sub_agents=%d",
            session_id,
            result.terminal_state,
            len(result.final_text),
            len(result.reasoning_text),
            duration_ms,
            len(result.events),
            len(result.sub_agents),
        )
        return result

    # ---- internals ---------------------------------------------------------

    async def _start_thread(self, instructions: Optional[str]) -> str:
        assert self._ws is not None
        params: dict[str, Any] = {}
        if instructions:
            params["instructions"] = instructions
        resp = await self._call("thread/start", params, timeout=30.0)
        thread = resp["result"]["thread"]
        tid = thread["id"]
        logger.info("codex thread started id=%s", tid)
        return tid

    async def _read_file(self, path: str) -> Optional[str]:
        """Read a file from the codex workspace via fs/readFile.

        Returns the base64-encoded content, or None on error.
        """
        assert self._ws is not None
        try:
            resp = await self._call(
                "fs/readFile", {"path": path}, timeout=15.0
            )
            result = resp.get("result", {})
            data = result.get("dataBase64") or result.get("data_base64")
            if data:
                logger.info("codex fs/readFile ok path=%s bytes=%d", path, len(data))
            else:
                logger.warning("codex fs/readFile no data path=%s", path)
            return data
        except Exception as e:
            logger.warning("codex fs/readFile failed for %s: %s", path, e)
            return None

    async def _write_file(self, path: str, content_b64: str) -> bool:
        """Write a file to the codex workspace via fs/writeFile.

        The file content is passed as base64-encoded data. Returns True on
        success.
        """
        assert self._ws is not None
        try:
            # Ensure parent directory exists by creating each ancestor
            # level. codex's fs/createDirectory may not support recursive
            # creation (mkdir -p) in a single call.
            parent = os.path.dirname(path)
            if parent:
                await self._ensure_directory(parent)
            await self._call(
                "fs/writeFile",
                {"path": path, "dataBase64": content_b64},
                timeout=15.0,
            )
            logger.info(
                "codex fs/writeFile success path=%s size=%d",
                path, len(content_b64),
            )
            return True
        except Exception as e:
            logger.warning("codex fs/writeFile failed for %s: %s", path, e)
            return False

    async def _ensure_directory(self, path: str) -> None:
        """Recursively create a directory and all parents.

        Walks from root to leaf, calling fs/createDirectory for each
        ancestor. Errors are ignored (directory may already exist).
        """
        # Build list of ancestors: e.g. /home/codex/.skills/foo →
        # ["/home", "/home/codex", "/home/codex/.skills", "/home/codex/.skills/foo"]
        parts: list[str] = []
        cur = path
        while cur and cur != "/" and cur != ".":
            parts.append(cur)
            cur = os.path.dirname(cur)
        parts.reverse()
        for ancestor in parts:
            try:
                await self._call(
                    "fs/createDirectory", {"path": ancestor}, timeout=10.0
                )
            except Exception:
                # Directory likely already exists — safe to ignore.
                pass

    async def _mount_skill_files(
        self, skills: list[dict[str, Any]], session_id: str
    ) -> None:
        """Write resolved skill files into the codex workspace.

        Each skill is mounted at ``/home/codex/.codex/skills/<name>/``. The
        agent can read SKILL.md from that path at any time during the turn.
        """
        assert self._ws is not None
        mounted = 0
        for skill in skills:
            name = skill.get("name") or skill.get("skill_id") or "unnamed"
            mount_root = skill.get("mount_root") or f"/home/codex/.codex/skills/{name}"
            files = skill.get("files") or []
            for f in files:
                filename = f.get("filename", "")
                content_b64 = f.get("content_base64", "")
                if not filename or not content_b64:
                    continue
                path = f"{mount_root}/{filename}"
                ok = await self._write_file(path, content_b64)
                if ok:
                    mounted += 1
        if mounted:
            logger.info(
                "codex mounted %d skill files session=%s",
                mounted, session_id,
            )

    async def _scan_workspace_files(self, events: list[OmaEvent], session_id: str) -> list[CodexFileOutput]:
        """Scan for files created via bash commands during the turn.

        Parses bash tool_use events for file creation patterns (cat > file,
        echo > file, tee file, etc.) and tries to read those files.
        """
        import re
        # Collect bash commands from events
        commands: list[str] = []
        for ev in events:
            if ev.get("type") == "agent.tool_use" and ev.get("name") == "bash":
                inp = ev.get("input", {})
                cmd = inp.get("command", inp.get("preview", ""))
                if isinstance(cmd, str) and cmd:
                    commands.append(cmd)

        logger.debug(
            "codex scan_workspace: found %d bash commands session=%s",
            len(commands), session_id,
        )

        if not commands:
            return []

        # Parse commands for file creation patterns
        paths_to_try: list[str] = []
        file_patterns = [
            r'cat\s*>\s*([^\s;|&"\']+)',      # cat > file
            r'echo\s+[^>]*>\s*([^\s;|&"\']+)',  # echo ... > file
            r'tee\s+([^\s;|&"\']+)',            # tee file
            r'printf\s+[^>]*>\s*([^\s;|&"\']+)',  # printf ... > file
        ]
        for cmd in commands:
            for pattern in file_patterns:
                matches = re.findall(pattern, cmd)
                for match in matches:
                    # Clean up the path
                    path = match.strip("'\"`\\")
                    if path and not path.startswith("/dev/") and "/" in path:
                        # Make absolute if relative
                        if not path.startswith("/"):
                            path = f"/home/codex/{path}"
                        paths_to_try.append(path)

        if not paths_to_try:
            return []

        # Deduplicate
        seen: set[str] = set()
        unique_paths: list[str] = []
        for p in paths_to_try:
            if p not in seen:
                seen.add(p)
                unique_paths.append(p)

        logger.debug(
            "codex scanning %d bash-created files session=%s: %s",
            len(unique_paths), session_id, unique_paths,
        )
        return await self._extract_files(unique_paths, session_id)

    async def _extract_files(
        self, paths: list[str], session_id: str
    ) -> list[CodexFileOutput]:
        """Read file content for each completed fileChange path."""
        files: list[CodexFileOutput] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            content_b64 = await self._read_file(path)
            if content_b64 is None:
                continue
            filename = os.path.basename(path)
            # Decode to get size
            try:
                raw = base64.b64decode(content_b64)
                size = len(raw)
            except Exception:
                size = 0
            media_type = _guess_media_type(filename)
            files.append(
                CodexFileOutput(
                    filename=filename,
                    path=path,
                    content_base64=content_b64,
                    media_type=media_type,
                    size_bytes=size,
                )
            )
        if files:
            logger.info(
                "codex extracted %d files session=%s",
                len(files),
                session_id,
            )
        return files

    async def _fetch_sub_agent_content(
        self,
        result: CodexTurnResult,
        session_id: str,
        on_event: Optional[EventHandler],
    ) -> None:
        """Fetch content from sub-agent child threads and emit OMA events.

        For each CodexSubAgent in result.sub_agents, we call
        ``thread/items/list`` to read the child thread's conversation
        history. Each item is mapped to an OMA event tagged with the
        child's ``session_thread_id`` so the frontend can display the
        sub-agent's session content in its own tab.
        """
        assert self._ws is not None
        for sa in result.sub_agents:
            try:
                items = await self._list_thread_items(sa.thread_id)
            except Exception as e:
                logger.warning(
                    "codex sub-agent fetch failed thread=%s: %s",
                    sa.thread_id, e,
                )
                continue
            logger.info(
                "codex sub-agent thread=%s items=%d nickname=%s",
                sa.thread_id, len(items), sa.nickname,
            )
            # Map items to OMA events scoped to the child thread.
            msg_id = _oma_id()
            thinking_id = _oma_id()
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            for item in items:
                itype = item.get("type", "")
                if itype == "agentMessage":
                    text = item.get("text", "")
                    if text:
                        text_parts.append(text)
                        event = _agent_message_event(
                            _oma_id(), msg_id, text,
                            thread_id=sa.thread_id,
                        )
                        result.events.append(event)
                        await _invoke(on_event, event)
                elif itype == "reasoning":
                    for s in item.get("summary", []):
                        if isinstance(s, str) and s:
                            reasoning_parts.append(s)
                elif itype == "commandExecution":
                    tool_event = _item_started_to_tool_use(item)
                    if tool_event is not None:
                        tool_event["session_thread_id"] = sa.thread_id
                        result.events.append(tool_event)
                        await _invoke(on_event, tool_event)
                elif itype == "fileChange":
                    tool_event = _item_started_to_tool_use(item)
                    if tool_event is not None:
                        tool_event["session_thread_id"] = sa.thread_id
                        result.events.append(tool_event)
                        await _invoke(on_event, tool_event)
            # If no agentMessage items, synthesize a minimal message so the
            # thread tab isn't empty.
            if not text_parts:
                fallback = f"(sub-agent {sa.nickname or sa.thread_id[:8]} completed)"
                event = _agent_message_event(
                    _oma_id(), msg_id, fallback,
                    thread_id=sa.thread_id,
                )
                result.events.append(event)
                await _invoke(on_event, event)

    async def _list_thread_items(self, thread_id: str) -> list[dict[str, Any]]:
        """Fetch items from a codex thread via ``thread/items/list``.

        Returns the list of items (agentMessage, reasoning, etc.) in the
        thread.
        """
        assert self._ws is not None
        resp = await self._call(
            "thread/items/list",
            {"threadId": thread_id},
            timeout=15.0,
        )
        result = resp.get("result", {})
        items = result.get("items", [])
        return items if isinstance(items, list) else []

    async def _call(
        self, method: str, params: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        assert self._ws is not None
        self._rpc_id += 1
        rid = self._rpc_id
        request = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }
        await self._ws.send(json.dumps(request))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # Match the response to our request id; other notifications may
            # arrive in the meantime and are handled in run_turn's loop.
            # Here, for _call outside run_turn, we drop them.
            if msg.get("id") == rid:
                if "error" in msg and msg["error"]:
                    err = msg["error"]
                    raise RuntimeError(
                        f"codex rpc {method} failed: "
                        f"{err.get('code')}: {err.get('message')}"
                    )
                return msg
        raise TimeoutError(f"codex rpc {method} timed out after {timeout}s")


# ---------------------------------------------------------------------------
# Event mapping helpers
# ---------------------------------------------------------------------------


def _oma_id() -> str:
    return f"cx_{uuid.uuid4().hex[:20]}"


def _agent_message_event(
    event_id: str,
    message_id: str,
    text: str,
    *,
    thread_id: str = "",
) -> OmaEvent:
    ev: OmaEvent = {
        "type": "agent.message",
        "id": event_id,
        "message_id": message_id,
        "content": [{"type": "text", "text": text}],
    }
    if thread_id:
        ev["session_thread_id"] = thread_id
    return ev


def _agent_thinking_event(event_id: str, text: str) -> OmaEvent:
    return {
        "type": "agent.thinking",
        "id": event_id,
        "content": [{"type": "text", "text": text}],
    }


def _session_error_event(message: str) -> OmaEvent:
    return {
        "type": "session.error",
        "id": _oma_id(),
        "error": {"message": message},
    }


def _thread_created_event(
    thread_id: str,
    parent_thread_id: str,
    agent_name: str,
) -> OmaEvent:
    """``session.thread_created`` — the frontend adds a new thread tab."""
    return {
        "type": "session.thread_created",
        "id": _oma_id(),
        "session_thread_id": thread_id,
        "parent_thread_id": parent_thread_id or "sthr_primary",
        "agent_name": agent_name,
    }


def _sub_agent_started_event(thread_id: str, agent_name: str) -> OmaEvent:
    """``session.sub_agent_started`` — marks the thread tab as running."""
    return {
        "type": "session.sub_agent_started",
        "id": _oma_id(),
        "session_thread_id": thread_id,
        "agent_name": agent_name,
    }


def _sub_agent_completed_event(thread_id: str) -> OmaEvent:
    """``session.sub_agent_completed`` — marks the sub-agent as done."""
    return {
        "type": "session.sub_agent_completed",
        "id": _oma_id(),
        "session_thread_id": thread_id,
    }


def _thread_idle_event(thread_id: str) -> OmaEvent:
    """``session.thread_idle`` — frontend marks the thread tab as idle."""
    return {
        "type": "session.thread_idle",
        "id": _oma_id(),
        "session_thread_id": thread_id,
    }


def _handle_sub_agent_activity(
    item: dict[str, Any],
    result: CodexTurnResult,
    parent_thread_id: str,
) -> Optional[CodexSubAgent]:
    """Process a ``subAgentActivity`` item and update result.sub_agents.

    Returns the CodexSubAgent when one is newly created or updated.
    """
    kind = (item.get("kind") or "").lower()
    # The child thread id can appear as threadId, thread_id, or childThreadId.
    child_tid = (
        item.get("threadId")
        or item.get("thread_id")
        or item.get("childThreadId")
        or item.get("child_thread_id")
        or ""
    )
    nickname = (
        item.get("agentNickname")
        or item.get("agent_nickname")
        or item.get("nickname")
        or ""
    )
    role = (
        item.get("agentRole")
        or item.get("agent_role")
        or item.get("role")
        or ""
    )
    source = item.get("source") or ""
    if not child_tid:
        logger.debug(
            "codex subAgentActivity without thread id: kind=%s item=%s",
            kind, item,
        )
        return None
    # Find existing or create new.
    sa = next((s for s in result.sub_agents if s.thread_id == child_tid), None)
    if sa is None:
        sa = CodexSubAgent(
            thread_id=child_tid,
            parent_thread_id=parent_thread_id,
            nickname=nickname,
            role=role,
            source=source,
        )
        result.sub_agents.append(sa)
    if kind in ("started",):
        sa.started = True
    elif kind in ("completed",):
        sa.completed = True
    return sa


def _item_started_to_tool_use(item: dict[str, Any]) -> Optional[OmaEvent]:
    """Map an item/started notification to an agent.tool_use event.

    Codex item types observed: agentMessage, reasoning, commandExecution,
    fileChange, mcpToolCall, plan, autoApprovalReview. We surface
    commandExecution / mcpToolCall / fileChange as tool_use events so the
    Debug tab shows something useful; the rest are ignored.
    """
    itype = item.get("type")
    if itype == "commandExecution":
        cmd = item.get("command") or item.get("shellCommand") or "(shell)"
        return {
            "type": "agent.tool_use",
            "id": _oma_id(),
            "name": "bash",
            "input": {"preview": cmd if isinstance(cmd, str) else str(cmd)},
        }
    if itype == "fileChange":
        path = item.get("path") or item.get("file") or "(file)"
        return {
            "type": "agent.tool_use",
            "id": _oma_id(),
            "name": "edit",
            "input": {"preview": str(path)},
        }
    if itype == "mcpToolCall":
        tool = item.get("tool") or item.get("name") or "mcp_tool"
        server = item.get("server") or item.get("serverName") or ""
        name = f"{server}.{tool}" if server else str(tool)
        inp = item.get("arguments") or item.get("input") or {}
        return {
            "type": "agent.mcp_tool_use",
            "id": _oma_id(),
            "name": name,
            "input": inp if isinstance(inp, dict) else {"preview": str(inp)},
        }
    return None


def _item_completed_to_tool_result(item: dict[str, Any]) -> Optional[OmaEvent]:
    """Map an item/completed notification to an agent.tool_result event."""
    itype = item.get("type")
    if itype in ("commandExecution", "fileChange", "mcpToolCall"):
        name_map = {
            "commandExecution": "bash",
            "fileChange": "edit",
            "mcpToolCall": (
                f"{item.get('server','')}.{item.get('tool','')}".strip(".")
                if item.get("server")
                else item.get("tool", "mcp_tool")
            ),
        }
        name = name_map[itype]
        status = item.get("status") or "completed"
        # Build a short content summary.
        duration = item.get("duration")
        if duration is not None:
            content = f"(completed in {duration})"
        elif status in ("failed", "error", "rejected"):
            content = f"({status})"
        else:
            content = "(completed)"
        event_type = (
            "agent.mcp_tool_result" if itype == "mcpToolCall" else "agent.tool_result"
        )
        return {
            "type": event_type,
            "id": _oma_id(),
            "name": name,
            "content": content,
        }
    return None


async def _invoke(cb: Optional[EventHandler], event: OmaEvent) -> None:
    if cb is None:
        return
    result = cb(event)
    if asyncio.iscoroutine(result):
        await result


def _guess_media_type(filename: str) -> str:
    """Guess the media type from a filename extension."""
    ext = os.path.splitext(filename)[1].lower()
    mapping = {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".py": "text/x-python",
        ".js": "text/javascript",
        ".ts": "text/typescript",
        ".json": "application/json",
        ".html": "text/html",
        ".css": "text/css",
        ".csv": "text/csv",
        ".xml": "application/xml",
        ".yaml": "text/yaml",
        ".yml": "text/yaml",
        ".sh": "text/x-shellscript",
        ".rs": "text/x-rust",
        ".go": "text/x-go",
        ".java": "text/x-java",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".zip": "application/zip",
    }
    return mapping.get(ext, "application/octet-stream")


def _build_instructions(
    base_instructions: Optional[str],
    skills: Optional[list[dict[str, Any]]],
) -> Optional[str]:
    """Combine the agent's system prompt with skill prompt additions.

    Each resolved skill carries a ``system_prompt_addition`` (the
    ``<skill name=…>…</skill>`` block built by the Go ResourceResolver).
    We concatenate them below the agent's own instructions so codex sees
    a single coherent ``instructions`` blob at ``thread/start``.
    """
    if not skills:
        return base_instructions
    additions: list[str] = []
    for skill in skills:
        addition = skill.get("system_prompt_addition") or ""
        if addition:
            additions.append(addition)
    if not additions:
        return base_instructions
    combined = "\n\n".join(additions)
    if base_instructions:
        return f"{base_instructions}\n\n{combined}"
    return combined
