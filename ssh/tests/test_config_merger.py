"""Tests for MCP configuration merger.

Tests the three-layer config merge: global (Codex config.toml) → tenant → agent.
Priority: agent > tenant > global.
"""

import os
import tempfile
import pytest
from config_merger import MCPConfigMerger, MCPServerConfig


class TestMCPServerConfig:
    """Test MCPServerConfig dataclass."""

    def test_to_dict_basic(self):
        cfg = MCPServerConfig(name="github", url="https://mcp.github.com/v1")
        d = cfg.to_dict()
        assert d["name"] == "github"
        assert d["url"] == "https://mcp.github.com/v1"
        assert d["type"] == "url"
        assert d["scope"] == "agent"
        assert d["authorization_token"] is None

    def test_to_dict_with_scope(self):
        cfg = MCPServerConfig(
            name="github", url="https://mcp.github.com/v1", scope="global"
        )
        d = cfg.to_dict()
        assert d["scope"] == "global"

    def test_to_dict_with_token(self):
        cfg = MCPServerConfig(
            name="github",
            url="https://mcp.github.com/v1",
            authorization_token="tok-123",
        )
        d = cfg.to_dict()
        assert d["authorization_token"] == "tok-123"


class TestMCPConfigMergerLoadGlobal:
    """Test loading global config from config.toml."""

    def test_load_global_config(self):
        toml_content = """
[mcp_servers.github]
name = "GitHub"
url = "https://mcp.github.com/v1"
type = "url"
scope = "global"

[mcp_servers.linear]
name = "Linear"
url = "https://mcp.linear.app/v1"
type = "url"
scope = "global"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(toml_content)
            f.flush()
            try:
                merger = MCPConfigMerger(global_config_path=f.name)
                assert len(merger.global_servers) == 2
                assert "github" in merger.global_servers
                assert "linear" in merger.global_servers
                gh = merger.global_servers["github"]
                assert gh.name == "GitHub"
                assert gh.url == "https://mcp.github.com/v1"
                assert gh.scope == "global"
            finally:
                os.unlink(f.name)

    def test_load_global_config_missing_file(self):
        merger = MCPConfigMerger(global_config_path="/nonexistent/config.toml")
        assert len(merger.global_servers) == 0

    def test_load_global_config_empty(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write("# empty config\n")
            f.flush()
            try:
                merger = MCPConfigMerger(global_config_path=f.name)
                assert len(merger.global_servers) == 0
            finally:
                os.unlink(f.name)

    def test_load_global_config_no_mcp_servers_section(self):
        toml_content = """
[model]
name = "qwen3.7-plus"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(toml_content)
            f.flush()
            try:
                merger = MCPConfigMerger(global_config_path=f.name)
                assert len(merger.global_servers) == 0
            finally:
                os.unlink(f.name)


class TestMCPConfigMergerMerge:
    """Test the three-layer merge logic."""

    def _make_merger_with_globals(self, globals_dict):
        """Helper: create merger with pre-set global servers."""
        merger = MCPConfigMerger.__new__(MCPConfigMerger)
        merger.global_servers = {}
        for name, url in globals_dict.items():
            merger.global_servers[name] = MCPServerConfig(
                name=name, url=url, scope="global"
            )
        return merger

    def test_merge_global_only(self):
        merger = self._make_merger_with_globals(
            {"github": "https://mcp.github.com/v1"}
        )
        result = merger.merge(tenant_servers=[], agent_servers=[])
        assert len(result) == 1
        assert result[0].name == "github"
        assert result[0].scope == "global"

    def test_merge_tenant_adds_to_global(self):
        merger = self._make_merger_with_globals(
            {"github": "https://mcp.github.com/v1"}
        )
        tenant = [{"name": "wiki", "url": "https://wiki.corp.com/mcp"}]
        result = merger.merge(tenant_servers=tenant, agent_servers=[])
        assert len(result) == 2
        names = {s.name for s in result}
        assert names == {"github", "wiki"}
        wiki = next(s for s in result if s.name == "wiki")
        assert wiki.scope == "tenant"

    def test_merge_agent_adds_to_global_and_tenant(self):
        merger = self._make_merger_with_globals(
            {"github": "https://mcp.github.com/v1"}
        )
        tenant = [{"name": "wiki", "url": "https://wiki.corp.com/mcp"}]
        agent = [{"name": "custom", "url": "https://custom.example.com/mcp"}]
        result = merger.merge(tenant_servers=tenant, agent_servers=agent)
        assert len(result) == 3
        names = {s.name for s in result}
        assert names == {"github", "wiki", "custom"}

    def test_merge_agent_overrides_tenant(self):
        """Agent-level config should override tenant-level for same name."""
        merger = self._make_merger_with_globals({})
        tenant = [{"name": "github", "url": "https://tenant.github.com/mcp"}]
        agent = [{"name": "github", "url": "https://agent.github.com/mcp"}]
        result = merger.merge(tenant_servers=tenant, agent_servers=agent)
        assert len(result) == 1
        assert result[0].url == "https://agent.github.com/mcp"
        assert result[0].scope == "agent"

    def test_merge_agent_overrides_global(self):
        """Agent-level config should override global-level for same name."""
        merger = self._make_merger_with_globals(
            {"github": "https://global.github.com/v1"}
        )
        agent = [{"name": "github", "url": "https://agent.github.com/mcp"}]
        result = merger.merge(tenant_servers=[], agent_servers=agent)
        assert len(result) == 1
        assert result[0].url == "https://agent.github.com/mcp"
        assert result[0].scope == "agent"

    def test_merge_tenant_overrides_global(self):
        """Tenant-level config should override global-level for same name."""
        merger = self._make_merger_with_globals(
            {"github": "https://global.github.com/v1"}
        )
        tenant = [{"name": "github", "url": "https://tenant.github.com/mcp"}]
        result = merger.merge(tenant_servers=tenant, agent_servers=[])
        assert len(result) == 1
        assert result[0].url == "https://tenant.github.com/mcp"
        assert result[0].scope == "tenant"

    def test_merge_full_override_chain(self):
        """Agent > Tenant > Global for the same server name."""
        merger = self._make_merger_with_globals(
            {"github": "https://global.github.com/v1"}
        )
        tenant = [{"name": "github", "url": "https://tenant.github.com/mcp"}]
        agent = [{"name": "github", "url": "https://agent.github.com/mcp"}]
        result = merger.merge(tenant_servers=tenant, agent_servers=agent)
        assert len(result) == 1
        assert result[0].url == "https://agent.github.com/mcp"
        assert result[0].scope == "agent"

    def test_merge_preserves_token_from_agent(self):
        """Authorization token from agent config should be preserved."""
        merger = self._make_merger_with_globals({})
        agent = [
            {
                "name": "custom",
                "url": "https://custom.example.com/mcp",
                "authorization_token": "secret-token",
            }
        ]
        result = merger.merge(tenant_servers=[], agent_servers=agent)
        assert len(result) == 1
        assert result[0].authorization_token == "secret-token"

    def test_merge_empty_everything(self):
        merger = self._make_merger_with_globals({})
        result = merger.merge(tenant_servers=[], agent_servers=[])
        assert result == []

    def test_merge_multiple_servers_per_layer(self):
        merger = self._make_merger_with_globals(
            {
                "github": "https://mcp.github.com/v1",
                "slack": "https://mcp.slack.com/v1",
            }
        )
        tenant = [
            {"name": "wiki", "url": "https://wiki.corp.com/mcp"},
            {"name": "jira", "url": "https://jira.corp.com/mcp"},
        ]
        agent = [
            {"name": "custom1", "url": "https://custom1.example.com/mcp"},
            {"name": "custom2", "url": "https://custom2.example.com/mcp"},
        ]
        result = merger.merge(tenant_servers=tenant, agent_servers=agent)
        assert len(result) == 6
        names = {s.name for s in result}
        assert names == {"github", "slack", "wiki", "jira", "custom1", "custom2"}


class TestMCPConfigMergerReload:
    """Test config hot-reload."""

    def test_reload_picks_up_changes(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(
                '[mcp_servers.github]\nname = "GitHub"\nurl = "https://v1.github.com"\n'
            )
            f.flush()
            try:
                merger = MCPConfigMerger(global_config_path=f.name)
                assert merger.global_servers["github"].url == "https://v1.github.com"

                # Rewrite the file
                with open(f.name, "w") as f2:
                    f2.write(
                        '[mcp_servers.github]\nname = "GitHub"\nurl = "https://v2.github.com"\n'
                    )
                merger.reload()
                assert merger.global_servers["github"].url == "https://v2.github.com"
            finally:
                os.unlink(f.name)
