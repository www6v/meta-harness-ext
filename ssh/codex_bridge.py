"""HTTP bridge for the Codex app-server.

Runs a small HTTP server that exposes a single ``POST /codex/turn``
endpoint. The Go ``internal/harness/codex_client.go`` calls this
endpoint with an OMA-shaped TurnRequest and receives a TurnResponse
(a list of OMA events + usage).

The bridge:
  - Accepts the turn request (session_id, agent, events...)
  - Opens an SSH tunnel if ``CODEX_SSH_HOST`` is set, otherwise
    connects directly to ``CODEX_WS_URL`` (default ws://127.0.0.1:5432)
  - Calls :class:`codex_client.CodexClient.run_turn`
  - Returns the resulting events

Environment variables:
    CODEX_WS_URL         WebSocket URL (default ws://127.0.0.1:5432)
    CODEX_WS_TOKEN       Bearer token (optional)
    CODEX_SSH_HOST       SSH host to tunnel through (optional)
    CODEX_SSH_USER       SSH user (default root)
    CODEX_SSH_PASSWORD   SSH password (optional)
    CODEX_SSH_PORT       SSH port (default 22)
    CODEX_REMOTE_PORT    Remote WS port when tunneling (default 5432)
    CODEX_LISTEN         Bridge listen address (default 127.0.0.1:8092)
    CODEX_TURN_TIMEOUT   Per-turn timeout in seconds (default 300)
    CODEX_MODEL          Model label surfaced in usage events (default codex)

Run:
    python codex_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

try:
    from .codex_client import CodexClient, CodexConfig
    from .codex_tunnel import SSHTunnel
except ImportError:
    # Allow running directly as `python codex_bridge.py`
    from codex_client import CodexClient, CodexConfig
    from codex_tunnel import SSHTunnel


logger = logging.getLogger("codex.bridge")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _config_from_env() -> dict[str, Any]:
    return {
        "ws_url": os.environ.get("CODEX_WS_URL", "ws://127.0.0.1:8765"),
        "token": os.environ.get("CODEX_WS_TOKEN"),
        "ssh_host": os.environ.get("CODEX_SSH_HOST"),
        "ssh_user": os.environ.get("CODEX_SSH_USER", "root"),
        "ssh_password": os.environ.get("CODEX_SSH_PASSWORD"),
        "ssh_port": int(os.environ.get("CODEX_SSH_PORT", "22")),
        "ssh_key_path": os.environ.get("CODEX_SSH_KEY_PATH"),
        "remote_port": int(os.environ.get("CODEX_REMOTE_PORT", "8765")),
        "listen": os.environ.get("CODEX_LISTEN", "127.0.0.1:8092"),
        "turn_timeout": float(os.environ.get("CODEX_TURN_TIMEOUT", "300")),
        "model": os.environ.get("CODEX_MODEL", "codex"),
        "mcp_config_path": os.environ.get("CODEX_MCP_CONFIG_PATH", "/tmp/oma-mcp-config.json"),
    }


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------


class _ConnPool:
    """Shared CodexClient with a single event loop and SSH tunnel.

    A single WebSocket connection to the codex app-server is shared across
    all OMA sessions. Each session gets its own codex thread (thread_id is
    reset when a new session_id is seen), so conversation state stays
    isolated per session while avoiding multiple WS connections.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._tunnel: Optional[SSHTunnel] = None
        self._client: Optional[CodexClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        # Track which session_id the current thread_id belongs to.
        self._active_session: Optional[str] = None

    def resolve_ws_url(self) -> str:
        if self.cfg.get("ssh_host"):
            if self._tunnel is None:
                self._tunnel = SSHTunnel(
                    ssh_host=self.cfg["ssh_host"],
                    ssh_user=self.cfg["ssh_user"],
                    ssh_password=self.cfg.get("ssh_password"),
                    ssh_port=self.cfg["ssh_port"],
                    remote_port=self.cfg["remote_port"],
                    ssh_key_path=self.cfg.get("ssh_key_path"),
                )
                self._tunnel.start()
            return self._tunnel.local_url
        return self.cfg["ws_url"]

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or not self._loop.is_running():
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                name="codex-loop",
                daemon=True,
            )
            self._thread.start()
        return self._loop

    def _ensure_client(self) -> CodexClient:
        if self._client is None:
            ws_url = self.resolve_ws_url()
            self._client = CodexClient(
                CodexConfig(
                    ws_url=ws_url,
                    token=self.cfg.get("token"),
                    model=self.cfg.get("model", "codex"),
                    turn_timeout=self.cfg.get("turn_timeout", 300.0),
                )
            )
        return self._client

    def get_or_create(self, session_id: str) -> tuple[CodexClient, asyncio.AbstractEventLoop]:
        """Return (client, loop) for the given session.

        A single CodexClient + event loop is shared. When a new session_id
        is seen, the client's thread_id is reset so a fresh codex thread
        is created on the next turn.
        """
        with self._lock:
            loop = self._ensure_loop()
            client = self._ensure_client()
            if session_id != self._active_session:
                # New session → reset thread state so a new codex thread
                # is created with the session's own instructions/skills.
                # Do NOT reset _initialized — initialize is per-connection.
                client._thread_id = None
                self._active_session = session_id
                logger.info("codex pool switched to session=%s", session_id)
            return client, loop

    def run(self, session_id: str, coro) -> Any:
        """Run a coroutine on the shared loop and return the result."""
        _, loop = self.get_or_create(session_id)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=self.cfg.get("turn_timeout", 300.0) + 30)

    def close(self, session_id: str) -> None:
        """No-op for API compat — shared client is kept alive."""
        pass

    def shutdown(self) -> None:
        with self._lock:
            client = self._client
            loop = self._loop
            self._client = None
            self._loop = None
            self._thread = None
            self._active_session = None
        if client is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(client.close(), loop).result(
                    timeout=5.0
                )
            except Exception:  # pragma: no cover
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._tunnel is not None:
            try:
                self._tunnel.stop()
            except Exception:  # pragma: no cover
                pass
            self._tunnel = None


# ---------------------------------------------------------------------------
# Request mapping
# ---------------------------------------------------------------------------


def _extract_prompt(req: dict[str, Any]) -> str:
    """Pull the latest user.message text from an OMA-shaped TurnRequest."""
    events = req.get("events", [])
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "user.message":
            continue
        content = ev.get("content") or []
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "".join(parts).strip()
        if text:
            return text
    return ""


def _extract_skills(req: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resolved skill payloads from the TurnRequest.

    Each resolved skill (from ResourceResolver.ResolveSkillsForTurn) has:
        skill_id, name, display_name, system_prompt_addition, files[]
    Each file has: filename, content_base64
    """
    skills = req.get("skills")
    if not skills:
        return []
    if not isinstance(skills, list):
        # skills may be a JSON string if double-marshaled
        if isinstance(skills, (str, bytes)):
            try:
                skills = json.loads(skills)
            except Exception:
                return []
    return [s for s in skills if isinstance(s, dict)]


def _extract_sub_agents(req: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract OMA sub_agents from the TurnRequest.

    The Go CodexClient marshals req.SubAgents (map[string]AgentSnapshot)
    as a JSON object keyed by agent id. We normalise to a list of dicts
    with id + name + description + system_prompt.
    """
    sub = req.get("sub_agents")
    if not sub:
        return []
    if isinstance(sub, (str, bytes)):
        try:
            sub = json.loads(sub)
        except Exception:
            return []
    if isinstance(sub, dict):
        out: list[dict[str, Any]] = []
        for aid, snap in sub.items():
            if not isinstance(snap, dict):
                continue
            out.append(
                {
                    "id": aid,
                    "name": snap.get("name") or aid,
                    "description": snap.get("description") or "",
                    "system_prompt": snap.get("system_prompt") or snap.get("system") or "",
                }
            )
        return out
    if isinstance(sub, list):
        return [s for s in sub if isinstance(s, dict)]
    return []


def _extract_mcp_config(req: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Extract MCP proxy config from the turn request.

    Returns {base, key, servers, session_id} or None if no MCP config.
    The bridge uses this to write MCP server config to Codex before the turn.
    """
    base = req.get("mcp_proxy_base", "")
    key = req.get("mcp_proxy_api_key", "")
    agent = req.get("agent") or {}
    if isinstance(agent, (str, bytes)):
        try:
            agent = json.loads(agent)
        except Exception:
            agent = {}
    servers = agent.get("mcp_servers") or []
    if isinstance(servers, (str, bytes)):
        try:
            servers = json.loads(servers)
        except Exception:
            servers = []
    if not base or not servers:
        return None
    return {"base": base, "key": key, "servers": servers}


def _inject_sub_agents_info(
    instructions: Optional[str],
    sub_agents: list[dict[str, Any]],
) -> str:
    """Append a "callable sub-agents" section to the codex instructions.

    The codex model has a native ``spawn_agent`` tool that creates child
    threads. By describing the available sub-agents in the system prompt,
    the model can decide when to delegate work to them.
    """
    if not sub_agents:
        return instructions or ""
    lines = [
        "",
        "## Callable Sub-Agents",
        "",
        "You can delegate tasks to the following sub-agents by invoking the",
        "``spawn_agent`` tool. Each sub-agent runs on its own thread and can",
        "be referenced by its id. Use a sub-agent when its specialty matches",
        "a subtask — it will run in parallel and report back when done.",
        "",
    ]
    for sa in sub_agents:
        sid = sa.get("id", "")
        name = sa.get("name", sid)
        desc = sa.get("description", "")
        lines.append(f"- **{name}** (id: ``{sid}``)")
        if desc:
            lines.append(f"  {desc}")
    block = "\n".join(lines)
    if instructions:
        return f"{instructions}\n{block}"
    return block


async def _run_explicit_sub_agents(
    pool: "_ConnPool",
    session_id: str,
    sub_agents: list[dict[str, Any]],
    parent_prompt: str,
) -> list[dict[str, Any]]:
    """Run sub-agents explicitly on separate codex connections.

    For each sub-agent, we open a dedicated CodexClient + thread (the
    shared pool client is reserved for the parent). Each sub-agent turn
    runs with its own system prompt and a task derived from the parent
    prompt. We emit OMA sub-agent lifecycle events:

        session.thread_created
        session.sub_agent_started
        agent.message (scoped to session_thread_id)
        session.sub_agent_completed
        session.thread_idle

    Returns the list of OMA events to prepend to the parent's response.
    """
    cfg = pool.cfg
    ws_url = pool.resolve_ws_url()
    events: list[dict[str, Any]] = []
    for sa in sub_agents:
        sa_id = sa.get("id", "sub_agent")
        sa_name = sa.get("name", sa_id)
        sa_system = sa.get("system_prompt") or sa.get("system") or ""
        # The codex child thread id doubles as the OMA session_thread_id
        # so the frontend can display the sub-agent's messages in their
        # own tab.
        thread_id = f"cx_sub_{sa_id}"
        # Build the sub-agent's task prompt: inherit the parent prompt
        # but scope it to the sub-agent's specialty.
        task_prompt = (
            f"You are the sub-agent \"{sa_name}\" (id: {sa_id}). "
            f"The user's overall request is:\n\n{parent_prompt}\n\n"
            f"Perform the part of this request that matches your role. "
            f"Respond concisely with your findings or output."
        )
        # Lifecycle: created + started.
        events.append({
            "type": "session.thread_created",
            "id": f"evt_{_uuid_hex()}",
            "session_thread_id": thread_id,
            "parent_thread_id": "sthr_primary",
            "agent_name": sa_name,
        })
        events.append({
            "type": "session.sub_agent_started",
            "id": f"evt_{_uuid_hex()}",
            "session_thread_id": thread_id,
            "agent_name": sa_name,
        })
        # Run the sub-agent turn on a fresh connection (the shared pool
        # client is for the parent; running on it would clobber the
        # parent's thread state).
        sub_client = CodexClient(
            CodexConfig(
                ws_url=ws_url,
                token=cfg.get("token"),
                model=cfg.get("model", "codex"),
                turn_timeout=cfg.get("turn_timeout", 300.0),
            )
        )
        try:
            sub_result = await sub_client.run_turn(
                task_prompt,
                session_id=f"{session_id}_sub_{sa_id}",
                instructions=sa_system or None,
            )
        except Exception as e:
            logger.warning("codex sub-agent turn failed %s: %s", sa_id, e)
            events.append({
                "type": "agent.message",
                "id": f"evt_{_uuid_hex()}",
                "session_thread_id": thread_id,
                "content": [{"type": "text", "text": f"(sub-agent {sa_name} failed: {e})"}],
            })
        else:
            # Map the sub-agent's messages into OMA events scoped to
            # this sub-agent's thread.
            if sub_result.final_text:
                events.append({
                    "type": "agent.message",
                    "id": f"evt_{_uuid_hex()}",
                    "session_thread_id": thread_id,
                    "content": [{"type": "text", "text": sub_result.final_text}],
                })
            elif sub_result.events:
                # Pick up any messages the sub-agent produced.
                for ev in sub_result.events:
                    if ev.get("type") == "agent.message":
                        scoped = dict(ev)
                        scoped["session_thread_id"] = thread_id
                        scoped["id"] = f"evt_{_uuid_hex()}"
                        events.append(scoped)
            # Include sub-agent tool calls in the parent's event log
            # (scoped to the sub-agent thread) for the Debug tab.
            for ev in sub_result.events:
                if ev.get("type") in ("agent.tool_use", "agent.tool_result"):
                    scoped = dict(ev)
                    scoped["session_thread_id"] = thread_id
                    events.append(scoped)
        finally:
            await sub_client.close()
        # Lifecycle: completed + idle.
        events.append({
            "type": "session.sub_agent_completed",
            "id": f"evt_{_uuid_hex()}",
            "session_thread_id": thread_id,
        })
        events.append({
            "type": "session.thread_idle",
            "id": f"evt_{_uuid_hex()}",
            "session_thread_id": thread_id,
        })
    return events


def _uuid_hex() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex[:16]


def _result_to_response(result) -> dict[str, Any]:
    """Map a CodexTurnResult to an OMA TurnResponse shape."""
    usage = None
    if result.usage:
        u = result.usage
        usage = {
            "input_tokens": u.get("inputTokens") or u.get("input_tokens") or 0,
            "output_tokens": u.get("outputTokens") or u.get("output_tokens") or 0,
        }
    files = []
    for f in result.files:
        files.append(
            {
                "filename": f.filename,
                "path": f.path,
                "content_base64": f.content_base64,
                "media_type": f.media_type,
                "size_bytes": f.size_bytes,
            }
        )
    return {
        "events": result.events,
        "usage": usage,
        "files": files,
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    pool: _ConnPool  # set by the factory

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("codex.bridge %s", format % args)

    def do_GET(self) -> None:
        if self.path in ("/health", "/codex/health"):
            self._json_response(200, {"status": "ok"})
            return
        self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        # JSON-RPC proxy endpoint — forwards arbitrary JSON-RPC calls to Codex
        if self.path in ("/codex/rpc",):
            self._do_rpc()
            return
        if self.path not in ("/codex/turn", "/turn", "/codex/turn/sse", "/turn/sse"):
            self._json_response(404, {"error": "not found"})
            return
        # SSE streaming endpoint — events emitted as they arrive
        if self.path in ("/codex/turn/sse", "/turn/sse"):
            self._do_post_stream()
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            self._json_response(400, {"error": f"invalid json: {e}"})
            return

        session_id = req.get("session_id", "")
        prompt = _extract_prompt(req)
        if not prompt:
            # Fall back to a continuation marker so codex has something
            # to respond to (matches Hermes/OpenClaw behavior).
            prompt = "(continue)"

        instructions = None
        agent = req.get("agent") or {}
        if isinstance(agent, dict):
            instructions = agent.get("system_prompt") or agent.get("system")
        if not instructions:
            instructions = None

        # Extract resolved skills (from ResourceResolver). We append each
        # skill's system_prompt_addition to the instructions, and tell the
        # client to write skill files into the codex workspace before the
        # turn starts so the agent can reference them.
        skills = _extract_skills(req)

        # Extract OMA sub_agents (callable agents). When present, we
        # describe them to the codex model so it can decide when to spawn
        # them via codex's native ``spawn_agent`` tool. The spawned
        # sub-agent threads are mapped to OMA events (session.thread_created,
        # session.sub_agent_started, etc.) by CodexClient.
        #
        # Additionally, when the model doesn't natively support
        # spawn_agent (e.g. qwen3.7-plus), we explicitly run sub-agent
        # turns before the parent and emit OMA sub-agent lifecycle events.
        sub_agents_info = _extract_sub_agents(req)
        explicit_sub_agent_events: list[dict[str, Any]] = []
        if sub_agents_info:
            instructions = _inject_sub_agents_info(instructions, sub_agents_info)
            # Explicit orchestration: run the first sub-agent as its own
            # codex thread BEFORE the parent turn, and synthesize OMA
            # sub-agent lifecycle events (thread_created, agent.message,
            # thread_idle) scoped to the sub-agent's thread id.
            try:
                explicit_sub_agent_events = self.pool.run(
                    session_id or "default",
                    _run_explicit_sub_agents(
                        self.pool, session_id or "default", sub_agents_info, prompt,
                    ),
                )
            except Exception as e:
                logger.warning("codex explicit sub-agent failed: %s", e)
                explicit_sub_agent_events = []

        # Configure MCP servers before the turn so Codex knows about
        # available MCP tools (e.g. GitHub via OMA proxy).
        mcp_config = _extract_mcp_config(req)
        if mcp_config:
            try:
                self._setup_mcp_servers(session_id or "default", mcp_config)
            except Exception as e:
                logger.warning("codex mcp setup failed (non-fatal): %s", e)

        start = time.monotonic()
        try:
            client, _ = self.pool.get_or_create(session_id or "default")
            result = self.pool.run(
                session_id or "default",
                client.run_turn(
                    prompt,
                    session_id=session_id,
                    instructions=instructions,
                    skills=skills,
                ),
            )
        except Exception as e:
            logger.exception("codex turn failed: %s", e)
            self._json_response(502, {"error": f"codex turn failed: {e}"})
            return
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "codex turn http done session=%s duration_ms=%d events=%d "
            "files=%d explicit_sub_events=%d",
            session_id,
            duration_ms,
            len(result.events),
            len(result.files),
            len(explicit_sub_agent_events),
        )
        response = _result_to_response(result)
        # Prepend explicit sub-agent events so they appear before the
        # parent's events in the session timeline (frontend displays
        # sub-agent thread tab first, then parent messages).
        if explicit_sub_agent_events:
            response["events"] = explicit_sub_agent_events + response["events"]
        self._json_response(200, response)

    def _do_rpc(self) -> None:
        """Handle POST /codex/rpc — forward JSON-RPC to the Codex app-server.

        Request body: {"session_id": "...", "method": "...", "params": {...}}
        Forwards to CodexClient._call() which sends JSON-RPC over WebSocket.
        Used by Go's ListMCPServerStatus, ReloadMCPServers, ListThreads, ReadThread.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            self._json_response(400, {"error": {"message": f"invalid json: {e}"}})
            return

        method = req.get("method", "")
        if not method:
            self._json_response(400, {"error": {"message": "method is required"}})
            return

        params = req.get("params") or {}
        session_id = req.get("session_id", "default")

        try:
            client, _ = self.pool.get_or_create(session_id)
            # Ensure WebSocket is connected before making RPC calls
            if client._ws is None:
                self.pool.run(session_id, client.connect())
            timeout = float(self.pool.cfg.get("turn_timeout", 300))
            result = self.pool.run(session_id, client._call(method, params, timeout=timeout))
            self._json_response(200, {"result": result})
        except Exception as e:
            logger.exception("codex rpc failed method=%s", method)
            self._json_response(502, {"error": {"message": str(e)}})

    def _setup_mcp_servers(self, session_id: str, mcp_config: dict[str, Any]) -> None:
        """Write MCP server config to Codex machine and trigger reload.

        Builds a Codex-compatible MCP config from the OMA proxy details,
        writes it to the remote Codex machine (via SSH), and calls
        config/mcpServer/reload so Codex picks up the new servers.
        """
        import subprocess

        # Build Codex MCP server config pointing to OMA proxy
        servers: dict[str, Any] = {}
        for srv in mcp_config["servers"]:
            name = srv.get("name", "")
            if not name:
                continue
            # URL uses the OMA MCP proxy with session_id placeholder
            proxy_url = f"{mcp_config['base'].rstrip('/')}/{session_id}/{name}"
            server_cfg: dict[str, Any] = {
                "transport": "streamable_http",
                "url": proxy_url,
            }
            if mcp_config.get("key"):
                server_cfg["http_headers"] = {
                    "Authorization": f"Bearer {mcp_config['key']}",
                }
            servers[name] = server_cfg

        config_content = json.dumps({"mcp_servers": servers})
        config_path = self.pool.cfg.get("mcp_config_path", "/tmp/oma-mcp-config.json")

        # Write config to the Codex machine
        ssh_host = self.pool.cfg.get("ssh_host")
        ssh_user = self.pool.cfg.get("ssh_user", "root")
        if ssh_host:
            try:
                # Use SSH to write config file to remote machine
                escaped = config_content.replace("'", "'\\''")
                cmd = f"echo '{escaped}' > {config_path}"
                subprocess.run(
                    ["ssh", "-o", "StrictHostKeyChecking=no",
                     "-o", "ConnectTimeout=5",
                     f"{ssh_user}@{ssh_host}", cmd],
                    capture_output=True, timeout=15,
                    check=False,
                )
                logger.info("codex: wrote MCP config via SSH to %s:%s", ssh_host, config_path)
            except Exception as e:
                logger.warning("codex: SSH MCP config write failed: %s", e)
                # Try fs/writeFile as fallback
                try:
                    client, _ = self.pool.get_or_create(session_id)
                    if client._ws is None:
                        self.pool.run(session_id, client.connect())
                    import base64
                    content_b64 = base64.b64encode(config_content.encode()).decode()
                    timeout = float(self.pool.cfg.get("turn_timeout", 300))
                    self.pool.run(session_id, client._call("fs/writeFile", {
                        "path": config_path,
                        "content": content_b64,
                        "encoding": "base64",
                    }, timeout=timeout))
                    logger.info("codex: wrote MCP config via fs/writeFile to %s", config_path)
                except Exception as e2:
                    logger.warning("codex: fs/writeFile MCP config also failed: %s", e2)
                    return
        else:
            # Local Codex — write directly
            try:
                with open(config_path, "w") as f:
                    f.write(config_content)
                logger.info("codex: wrote MCP config locally to %s", config_path)
            except Exception as e:
                logger.warning("codex: local MCP config write failed: %s", e)
                return

        # Trigger reload via RPC so Codex picks up new MCP servers
        try:
            client, _ = self.pool.get_or_create(session_id)
            if client._ws is None:
                self.pool.run(session_id, client.connect())
            timeout = float(self.pool.cfg.get("turn_timeout", 300))
            self.pool.run(session_id, client._call("config/mcpServer/reload", {}, timeout=timeout))
            logger.info("codex: triggered MCP server reload for session=%s", session_id)
        except Exception as e:
            logger.warning("codex: MCP reload failed: %s", e)

    def _do_post_stream(self) -> None:
        """SSE streaming variant of POST /codex/turn.

        Emits each OMA event as an SSE frame the moment the codex client
        produces it (via the on_event callback), instead of waiting for the
        entire turn to complete.  The HTTP handler thread reads from a
        thread-safe queue fed by the asyncio event-loop thread.
        """
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            self._json_response(400, {"error": f"invalid json: {e}"})
            return

        session_id = req.get("session_id", "")
        prompt = _extract_prompt(req)
        if not prompt:
            prompt = "(continue)"

        instructions = None
        agent = req.get("agent") or {}
        if isinstance(agent, dict):
            instructions = agent.get("system_prompt") or agent.get("system")
        if not instructions:
            instructions = None

        skills = _extract_skills(req)
        sub_agents_info = _extract_sub_agents(req)
        explicit_sub_agent_events: list[dict[str, Any]] = []
        if sub_agents_info:
            instructions = _inject_sub_agents_info(instructions, sub_agents_info)
            # Explicit orchestration: run sub-agents as separate codex threads
            # BEFORE the parent turn, and emit OMA sub-agent lifecycle events
            # through the SSE stream so the frontend sees them immediately.
            try:
                explicit_sub_agent_events = self.pool.run(
                    session_id or "default",
                    _run_explicit_sub_agents(
                        self.pool, session_id or "default", sub_agents_info, prompt,
                    ),
                )
            except Exception as e:
                logger.warning("codex explicit sub-agent (stream) failed: %s", e)
                explicit_sub_agent_events = []

        # Configure MCP servers before the turn so Codex knows about
        # available MCP tools (e.g. GitHub via OMA proxy).
        mcp_config = _extract_mcp_config(req)
        if mcp_config:
            try:
                self._setup_mcp_servers(session_id or "default", mcp_config)
            except Exception as e:
                logger.warning("codex mcp setup (stream) failed (non-fatal): %s", e)

        # SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Emit explicit sub-agent events first so they appear before
        # the parent's events in the frontend session timeline.
        flusher = self.wfile
        for ev in explicit_sub_agent_events:
            payload = json.dumps(ev, ensure_ascii=False).encode("utf-8")
            ev_type = ev.get("type", "")
            if ev_type:
                flusher.write(f"event: {ev_type}\n".encode())
            flusher.write(b"data: ")
            flusher.write(payload)
            flusher.write(b"\n\n")
            flusher.flush()

        # Cross-thread queue: the asyncio event-loop thread puts events
        # here; the HTTP handler thread reads and writes SSE frames.
        event_q: queue.Queue = queue.Queue()
        done = threading.Event()

        def on_event_sync(event: dict[str, Any]) -> None:
            event_q.put(event)

        async def _on_event(event: dict[str, Any]) -> None:
            on_event_sync(event)

        def _stream_runner() -> CodexTurnResult | None:
            """Run on the event-loop thread; returns the turn result."""
            try:
                client, _ = self.pool.get_or_create(session_id or "default")

                async def _turn() -> CodexTurnResult:
                    return await client.run_turn(
                        prompt,
                        session_id=session_id,
                        instructions=instructions,
                        skills=skills,
                        on_event=_on_event,
                    )

                future = asyncio.run_coroutine_threadsafe(_turn(), self.pool._loop)  # noqa: SLF001
                return future.result(timeout=self.pool.cfg.get("turn_timeout", 300.0) + 30)
            except Exception:
                logger.exception("codex streaming turn failed")
                return None
            finally:
                logger.info("codex: sending poison pill to queue session=%s", session_id)
                event_q.put(None)  # poison pill
                done.set()

        # Schedule the turn on the event-loop thread (non-blocking for us).
        loop = self.pool._ensure_loop()  # noqa: SLF001
        threading.Thread(target=_stream_runner, daemon=True).start()

        # Stream events to the client as they arrive.
        try:
            while not done.is_set():
                try:
                    event = event_q.get(timeout=2.0)
                except queue.Empty:
                    # No event yet — send SSE comment keepalive so proxies
                    # don't close the connection.
                    flusher.write(b": keepalive\n\n")
                    flusher.flush()
                    continue
                if event is None:
                    logger.info("codex: received poison pill, closing SSE stream session=%s", session_id)
                    break  # poison pill: turn finished
                payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
                ev_type = event.get("type", "")
                if ev_type:
                    flusher.write(f"event: {ev_type}\n".encode())
                flusher.write(b"data: ")
                flusher.write(payload)
                flusher.write(b"\n\n")
                flusher.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("codex SSE client disconnected session=%s", session_id)
            return
        finally:
            done.set()

    def _json_response(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def make_server(cfg: Optional[dict[str, Any]] = None) -> tuple[ThreadingHTTPServer, _ConnPool]:
    cfg = cfg or _config_from_env()
    pool = _ConnPool(cfg)
    handler_cls = type("_BoundHandler", (_Handler,), {"pool": pool})
    host, _, port = cfg["listen"].rpartition(":")
    host = host or "127.0.0.1"
    port = int(port or "8092")
    server = ThreadingHTTPServer((host, port), handler_cls)
    return server, pool


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _config_from_env()
    logger.info("codex bridge starting on %s", cfg["listen"])
    logger.info(
        "codex ws_url=%s ssh_host=%s remote_port=%d",
        cfg["ws_url"],
        cfg.get("ssh_host") or "(direct)",
        cfg["remote_port"],
    )
    server, pool = make_server(cfg)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("codex bridge interrupted, shutting down")
    finally:
        pool.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
