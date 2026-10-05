"""MCP Configuration Merger.

Merges three layers of MCP server configuration:
    Layer 1 (lowest): Global defaults from Codex config.toml
    Layer 2: Tenant-specific servers from meta-harness Vault
    Layer 3 (highest): Agent-specific servers from Agent JSON

Priority: agent > tenant > global. Same-name servers at a higher layer
override the lower layer; different-name servers accumulate.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """A single MCP server declaration with its source layer."""

    name: str
    url: str
    type: str = "url"
    scope: str = "agent"  # "global" | "tenant" | "agent"
    authorization_token: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the JSON shape the Codex app-server expects."""
        return {
            "name": self.name,
            "url": self.url,
            "type": self.type,
            "scope": self.scope,
            "authorization_token": self.authorization_token,
        }


class MCPConfigMerger:
    """Merges global, tenant, and agent MCP server configurations.

    The merger loads global defaults from a Codex config.toml file once
    at construction time (and on explicit reload()). Per-turn merge()
    calls combine those globals with the tenant- and agent-layer configs
    passed in by the Go platform.

    Args:
        global_config_path: Path to the Codex config.toml. Defaults to
            ``/home/codex/.codex/config.toml`` (the path inside the Codex
            container). Tests pass a temp file path.
    """

    DEFAULT_GLOBAL_CONFIG = "/home/codex/.codex/config.toml"

    def __init__(self, global_config_path: Optional[str] = None):
        self._config_path = global_config_path or self.DEFAULT_GLOBAL_CONFIG
        self.global_servers: Dict[str, MCPServerConfig] = {}
        self.load_global_config()

    def load_global_config(self) -> None:
        """Load global MCP servers from the Codex config.toml.

        Silently returns an empty set if the file is missing or malformed —
        the platform still works with tenant/agent configs alone.
        """
        self.global_servers = {}
        path = Path(self._config_path)
        if not path.exists():
            logger.debug("global config not found at %s, skipping", path)
            return
        try:
            with open(path, "rb") as f:
                config = tomllib.load(f)
            mcp_servers = config.get("mcp_servers", {})
            if not isinstance(mcp_servers, dict):
                logger.warning(
                    "mcp_servers in %s is not a table, ignoring", path
                )
                return
            for name, server in mcp_servers.items():
                if not isinstance(server, dict):
                    logger.warning("mcp_servers.%s is not a table, skipping", name)
                    continue
                url = server.get("url")
                if not url:
                    logger.warning("mcp_servers.%s has no url, skipping", name)
                    continue
                self.global_servers[name] = MCPServerConfig(
                    name=server.get("name", name),
                    url=url,
                    type=server.get("type", "url"),
                    scope=server.get("scope", "global"),
                )
            logger.info(
                "loaded %d global MCP server(s) from %s",
                len(self.global_servers),
                path,
            )
        except Exception as e:
            logger.warning("failed to load global config %s: %s", path, e)

    def reload(self) -> None:
        """Re-read the global config file. Called after external edits."""
        self.load_global_config()

    def merge(
        self,
        tenant_servers: List[Dict[str, Any]],
        agent_servers: List[Dict[str, Any]],
    ) -> List[MCPServerConfig]:
        """Merge three layers into a flat server list.

        Args:
            tenant_servers: Tenant-layer configs from meta-harness Vault.
                Each dict has at least ``name`` and ``url``.
            agent_servers: Agent-layer configs from Agent JSON. Same shape.

        Returns:
            Deduplicated list of MCPServerConfig. Higher-priority layers
            win on name collisions.
        """
        merged: Dict[str, MCPServerConfig] = {}

        # Layer 1: globals
        for name, server in self.global_servers.items():
            merged[name] = server

        # Layer 2: tenant (overrides global)
        for entry in tenant_servers or []:
            name = entry.get("name")
            if not name:
                continue
            merged[name] = MCPServerConfig(
                name=name,
                url=entry.get("url", ""),
                type=entry.get("type", "url"),
                scope="tenant",
                authorization_token=entry.get("authorization_token"),
            )

        # Layer 3: agent (overrides tenant and global)
        for entry in agent_servers or []:
            name = entry.get("name")
            if not name:
                continue
            merged[name] = MCPServerConfig(
                name=name,
                url=entry.get("url", ""),
                type=entry.get("type", "url"),
                scope="agent",
                authorization_token=entry.get("authorization_token"),
            )

        return list(merged.values())
