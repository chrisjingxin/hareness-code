"""MCP 配置解析、连接管理和指纹计算的单元测试。"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness_agent.extensions.mcp import (
    McpConnectionManager,
    McpServerConfig,
    build_mcp_snapshot,
    expand_env_vars,
    mcp_config_fingerprint,
    parse_mcp_config,
)


def _manager(configs: list[McpServerConfig] | tuple[McpServerConfig, ...] = ()) -> McpConnectionManager:
    """用 canonical immutable snapshot 构造测试用 MCP owner。"""
    return McpConnectionManager(build_mcp_snapshot(configs, revision="test"))


class TestParseMcpConfig:
    """parse_mcp_config 的配置解析行为。"""

    def test_none_section_returns_empty(self):
        assert parse_mcp_config(None) == []

    def test_empty_dict_returns_empty(self):
        assert parse_mcp_config({}) == []

    def test_missing_servers_key_returns_empty(self):
        assert parse_mcp_config({"other": 1}) == []

    def test_valid_stdio_server(self):
        section = {
            "servers": [
                {
                    "name": "filesystem",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "env": {"API_KEY": "test"},
                }
            ]
        }
        configs = parse_mcp_config(section)
        assert len(configs) == 1
        c = configs[0]
        assert c.name == "filesystem"
        assert c.transport == "stdio"
        assert c.command == "npx"
        assert c.args == ("-y", "@modelcontextprotocol/server-filesystem", "/tmp")
        assert c.env == {"API_KEY": "test"}

    def test_valid_http_server(self):
        section = {
            "servers": [
                {
                    "name": "github",
                    "transport": "http",
                    "url": "http://localhost:3001/mcp",
                    "headers": {"Authorization": "Bearer token"},
                }
            ]
        }
        configs = parse_mcp_config(section)
        assert len(configs) == 1
        c = configs[0]
        assert c.name == "github"
        assert c.transport == "http"
        assert c.url == "http://localhost:3001/mcp"
        assert c.headers == {"Authorization": "Bearer token"}

    def test_valid_sse_server(self):
        section = {
            "servers": [
                {"name": "legacy", "transport": "sse", "url": "http://localhost:8000/sse"}
            ]
        }
        configs = parse_mcp_config(section)
        assert len(configs) == 1
        assert configs[0].transport == "sse"

    def test_default_transport_is_stdio(self):
        section = {
            "servers": [{"name": "test", "command": "python", "args": ["server.py"]}]
        }
        configs = parse_mcp_config(section)
        assert configs[0].transport == "stdio"

    def test_stdio_missing_command_skipped(self):
        section = {"servers": [{"name": "bad", "transport": "stdio"}]}
        assert parse_mcp_config(section) == []

    def test_http_missing_url_skipped(self):
        section = {"servers": [{"name": "bad", "transport": "http"}]}
        assert parse_mcp_config(section) == []

    def test_sse_missing_url_skipped(self):
        section = {"servers": [{"name": "bad", "transport": "sse"}]}
        assert parse_mcp_config(section) == []

    def test_unsupported_transport_skipped(self):
        section = {"servers": [{"name": "bad", "transport": "websocket", "url": "ws://x"}]}
        assert parse_mcp_config(section) == []

    def test_missing_name_skipped(self):
        section = {"servers": [{"transport": "stdio", "command": "x"}]}
        assert parse_mcp_config(section) == []

    def test_invalid_entry_type_skipped(self):
        section = {"servers": ["not-a-dict", {"name": "ok", "command": "x"}]}
        configs = parse_mcp_config(section)
        assert len(configs) == 1
        assert configs[0].name == "ok"

    def test_multiple_servers(self):
        section = {
            "servers": [
                {"name": "a", "command": "cmd-a"},
                {"name": "b", "transport": "http", "url": "http://b"},
                {"name": "c", "transport": "sse", "url": "http://c/sse"},
            ]
        }
        configs = parse_mcp_config(section)
        assert len(configs) == 3
        assert [c.name for c in configs] == ["a", "b", "c"]

    def test_custom_timeout(self):
        section = {"servers": [{"name": "t", "command": "x", "timeout": 10}]}
        configs = parse_mcp_config(section)
        assert configs[0].timeout_seconds == 10.0

    def test_invalid_timeout_uses_default(self):
        section = {"servers": [{"name": "t", "command": "x", "timeout": -5}]}
        configs = parse_mcp_config(section)
        assert configs[0].timeout_seconds == 30.0


class TestExpandEnvVars:
    """expand_env_vars 的环境变量展开行为。"""

    def test_no_vars_returns_unchanged(self):
        assert expand_env_vars("plain text") == "plain text"

    def test_expands_existing_var(self, monkeypatch):
        monkeypatch.setenv("TEST_MCP_VAR", "hello")
        assert expand_env_vars("prefix-${TEST_MCP_VAR}-suffix") == "prefix-hello-suffix"

    def test_missing_var_returns_none(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_MCP_VAR", raising=False)
        assert expand_env_vars("${NONEXISTENT_MCP_VAR}") is None

    def test_multiple_vars_all_present(self, monkeypatch):
        monkeypatch.setenv("A_VAR", "1")
        monkeypatch.setenv("B_VAR", "2")
        assert expand_env_vars("${A_VAR}-${B_VAR}") == "1-2"

    def test_multiple_vars_one_missing(self, monkeypatch):
        monkeypatch.setenv("A_VAR", "1")
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert expand_env_vars("${A_VAR}-${MISSING_VAR}") is None


class TestMcpConfigFingerprint:
    """mcp_config_fingerprint 的指纹计算行为。"""

    def test_empty_configs_returns_disabled_fingerprint(self):
        from harness_agent.runtime.agent_engine_profile import component_fingerprint

        fp = mcp_config_fingerprint([])
        assert fp == component_fingerprint({"transport": "disabled"})

    def test_same_configs_same_fingerprint(self):
        configs = [
            McpServerConfig(name="a", transport="stdio", command="x"),
            McpServerConfig(name="b", transport="http", url="http://b"),
        ]
        assert mcp_config_fingerprint(configs) == mcp_config_fingerprint(configs)

    def test_different_configs_different_fingerprint(self):
        c1 = [McpServerConfig(name="a", transport="stdio", command="x")]
        c2 = [McpServerConfig(name="b", transport="http", url="http://b")]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_sensitive_to_command(self):
        """扩充后：不同命令产生不同指纹（command 影响运行行为）。"""
        c1 = [McpServerConfig(name="srv", transport="stdio", command="cmd-a")]
        c2 = [McpServerConfig(name="srv", transport="stdio", command="cmd-b")]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_is_64_hex(self):
        configs = [McpServerConfig(name="x", transport="stdio", command="y")]
        fp = mcp_config_fingerprint(configs)
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_order_independent(self):
        """服务器顺序不影响指纹（内部按名称排序）。"""
        c1 = [
            McpServerConfig(name="a", transport="stdio", command="x"),
            McpServerConfig(name="b", transport="http", url="http://b"),
        ]
        c2 = [
            McpServerConfig(name="b", transport="http", url="http://b"),
            McpServerConfig(name="a", transport="stdio", command="x"),
        ]
        assert mcp_config_fingerprint(c1) == mcp_config_fingerprint(c2)


class TestMcpConnectionManager:
    """McpConnectionManager 的连接管理行为。"""

    def test_empty_configs_connects_immediately(self):
        mgr = _manager()
        import asyncio
        asyncio.run(mgr.connect_all())
        assert mgr.connected
        assert mgr.get_tools() == []

    def test_get_tool_names_empty(self):
        mgr = _manager()
        assert mgr.get_tool_names() == []

    @pytest.mark.asyncio
    async def test_connect_failure_does_not_raise(self):
        """连接失败不抛异常，标记为已连接但无工具。"""
        configs = [McpServerConfig(name="bad", transport="stdio", command="nonexistent-cmd-xyz")]
        mgr = _manager(configs)
        # 不应抛出异常
        await mgr.connect_all()
        assert mgr.connected
        assert mgr.get_tools() == []

    @pytest.mark.asyncio
    async def test_connects_servers_concurrently_and_keeps_config_order(self):
        """服务器并行连接，但工具仍按配置顺序稳定合并。"""
        configs = [
            McpServerConfig(name="alpha", transport="stdio", command="cmd-alpha"),
            McpServerConfig(name="beta", transport="stdio", command="cmd-beta"),
        ]
        mgr = _manager(configs)
        both_started = asyncio.Event()
        beta_finished = asyncio.Event()
        started: list[str] = []

        tool_alpha = MagicMock()
        tool_alpha.name = "alpha_search"
        tool_beta = MagicMock()
        tool_beta.name = "beta_query"

        async def get_tools(*, server_name: str):
            started.append(server_name)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            if server_name == "alpha":
                await beta_finished.wait()
                return [tool_alpha]
            beta_finished.set()
            return [tool_beta]

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=get_tools)
        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient", return_value=mock_client
        ):
            await asyncio.wait_for(mgr.connect_all(), timeout=0.5)

        assert mgr.get_tool_names() == ["alpha_search", "beta_query"]

    @pytest.mark.asyncio
    async def test_limits_parallel_server_connections_to_four(self):
        """大量 MCP 配置只允许四个连接同时占用启动资源。"""
        configs = [
            McpServerConfig(name=f"server-{index}", transport="stdio", command="cmd")
            for index in range(5)
        ]
        mgr = _manager(configs)
        four_started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0

        async def get_tools(*, server_name: str):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                four_started.set()
            try:
                await release.wait()
                return []
            finally:
                active -= 1

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=get_tools)
        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient", return_value=mock_client
        ):
            connect_task = asyncio.create_task(mgr.connect_all())
            try:
                await asyncio.wait_for(four_started.wait(), timeout=0.5)
            finally:
                release.set()
                await connect_task

        assert peak == 4

    @pytest.mark.asyncio
    async def test_close_all_safe_when_not_connected(self):
        mgr = _manager()
        await mgr.close_all()  # 不应抛出

    def test_build_connections_stdio(self):
        configs = [
            McpServerConfig(
                name="fs", transport="stdio", command="npx",
                args=("-y", "server"), env={"KEY": "val"},
            )
        ]
        mgr = _manager(configs)
        conns = mgr._build_connections()
        assert "fs" in conns
        assert conns["fs"]["transport"] == "stdio"
        assert conns["fs"]["command"] == "npx"
        assert conns["fs"]["args"] == ["-y", "server"]

    def test_plugin_stdio_uses_minimal_environment(self, monkeypatch):
        """Plugin MCP 不继承宿主秘密，只保留最小系统环境和 manifest env。"""
        monkeypatch.setenv("HARNESS_TEST_SECRET", "must-not-leak")
        config = McpServerConfig(
            name="plugin__local__review__check",
            transport="stdio",
            command="check",
            env={"PLUGIN_MODE": "safe"},
            source="plugin:local/review",
            source_fingerprint="a" * 64,
            inherit_environment=False,
        )
        connection = McpConnectionManager([config])._build_single_connection(config)
        assert connection is not None
        assert connection["env"]["PLUGIN_MODE"] == "safe"
        assert "HARNESS_TEST_SECRET" not in connection["env"]

    def test_plugin_stdio_keeps_unknown_placeholders_and_forces_reserved_env(self, tmp_path):
        """Plugin MCP 只接受 adapter 已替换的保留变量，其余值保持字面值。"""
        root = tmp_path / "root"
        data = tmp_path / "data"
        config = McpServerConfig(
            name="plugin__source__name__server",
            transport="stdio",
            command="offline-fixture-server",
            args=("${HOST_ENV}", "${PLUGIN_ROOT}/manifest.json"),
            env={"${ENV_KEY}": "${HOST_ENV}", "PLUGIN_ROOT": "forged-root"},
            source="plugin:source/name",
            inherit_environment=False,
            plugin_root=str(root),
            plugin_data=str(data),
        )
        connection = McpConnectionManager([config])._build_single_connection(config)
        assert connection is not None
        assert connection["command"] == "offline-fixture-server"
        assert connection["args"] == ["${HOST_ENV}", "${PLUGIN_ROOT}/manifest.json"]
        assert connection["env"]["${ENV_KEY}"] == "${HOST_ENV}"
        assert connection["env"]["PLUGIN_ROOT"] == str(root)
        assert connection["env"]["PLUGIN_DATA"] == str(data)

    @pytest.mark.asyncio
    async def test_plugin_http_disables_redirect_following(self, tmp_path):
        """Plugin HTTP/SSE headers 不会随自动跨 origin redirect 转发。"""
        config = McpServerConfig(
            name="plugin__source__name__http",
            transport="http",
            url="https://example.test/mcp",
            headers={"Authorization": "Bearer secret"},
            source="plugin:source/name",
            inherit_environment=False,
            plugin_root=str(tmp_path / "root"),
            plugin_data=str(tmp_path / "data"),
        )
        connection = McpConnectionManager([config])._build_single_connection(config)
        assert connection is not None
        client = connection["httpx_client_factory"](headers=connection["headers"])
        try:
            assert client.follow_redirects is False
            assert client.headers["Authorization"] == "Bearer secret"
        finally:
            await client.aclose()

    @pytest.mark.parametrize("transport", ("http", "sse"))
    @pytest.mark.asyncio
    async def test_claude_plugin_http_disables_redirect_following(self, transport):
        """Claude `.mcp.json` 的 Plugin header 也不能随 redirect 转发。"""
        config = McpServerConfig(
            name=f"plugin__source__claude__{transport}",
            transport=transport,
            url="https://example.test/mcp",
            headers={"Authorization": "Bearer secret"},
            source="plugin:source/claude",
            inherit_environment=False,
            timeout_seconds=7.0,
        )
        connection = McpConnectionManager([config])._build_single_connection(config)
        assert connection is not None
        client = connection["httpx_client_factory"](headers=connection["headers"])
        try:
            assert client.follow_redirects is False
            assert client.headers["Authorization"] == "Bearer secret"
            assert config.timeout_seconds == 7.0
        finally:
            await client.aclose()

    def test_build_connections_http(self):
        configs = [
            McpServerConfig(
                name="gh", transport="http", url="http://localhost:3001/mcp",
                headers={"Authorization": "Bearer tok"},
            )
        ]
        mgr = _manager(configs)
        conns = mgr._build_connections()
        assert "gh" in conns
        assert conns["gh"]["transport"] == "streamable_http"
        assert conns["gh"]["url"] == "http://localhost:3001/mcp"
        assert conns["gh"]["headers"] == {"Authorization": "Bearer tok"}

    def test_build_connections_skips_missing_env(self, monkeypatch):
        monkeypatch.delenv("MISSING_MCP_ENV", raising=False)
        configs = [
            McpServerConfig(
                name="bad", transport="http", url="http://${MISSING_MCP_ENV}/mcp",
            )
        ]
        mgr = _manager(configs)
        conns = mgr._build_connections()
        assert "bad" not in conns


class TestMcpServerStatuses:
    """McpConnectionManager 的逐服务器状态跟踪行为。"""

    def test_empty_config_returns_empty_list(self):
        mgr = _manager()
        assert mgr.get_server_statuses() == []

    @pytest.mark.asyncio
    async def test_all_connected(self):
        """连接成功后所有服务器标记为 connected 并归属工具。"""
        configs = [
            McpServerConfig(name="alpha", transport="stdio", command="cmd-a"),
            McpServerConfig(name="beta", transport="http", url="http://beta/mcp"),
        ]
        mgr = _manager(configs)

        # 构造带名称前缀的 mock 工具
        tool_a = MagicMock()
        tool_a.name = "alpha_search"
        tool_b = MagicMock()
        tool_b.name = "beta_query"

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(return_value=[tool_a, tool_b])

        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient", return_value=mock_client
        ):
            await mgr.connect_all()

        statuses = mgr.get_server_statuses()
        assert len(statuses) == 2

        alpha = next(s for s in statuses if s["name"] == "alpha")
        assert alpha["status"] == "connected"
        assert alpha["transport"] == "stdio"
        assert alpha["tool_names"] == ["alpha_search"]

        beta = next(s for s in statuses if s["name"] == "beta")
        assert beta["status"] == "connected"
        assert beta["transport"] == "http"
        assert beta["tool_names"] == ["beta_query"]

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        """连接异常时服务器标记为 failed 并携带错误信息。"""
        configs = [
            McpServerConfig(name="srv", transport="stdio", command="cmd-x"),
        ]
        mgr = _manager(configs)

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=RuntimeError("boom"))

        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient", return_value=mock_client
        ):
            await mgr.connect_all()

        statuses = mgr.get_server_statuses()
        assert len(statuses) == 1
        assert statuses[0]["status"] == "failed"
        assert statuses[0]["error"] == "boom"
        assert statuses[0]["tool_names"] == []

    @pytest.mark.asyncio
    async def test_one_server_failure_does_not_isolate_siblings(self):
        """单个启动/认证失败只标记对应 server，其他 server 仍加载工具。"""
        configs = [
            McpServerConfig(name="good", transport="stdio", command="cmd-good"),
            McpServerConfig(name="bad", transport="stdio", command="cmd-bad"),
        ]
        mgr = _manager(configs)
        tool = MagicMock()
        tool.name = "good_search"

        async def get_tools(*, server_name=None):
            if server_name == "bad":
                raise PermissionError("auth failed")
            return [tool]

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=get_tools)
        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient", return_value=mock_client
        ):
            await mgr.connect_all()

        statuses = {item["name"]: item for item in mgr.get_server_statuses()}
        assert statuses["good"]["status"] == "connected"
        assert statuses["good"]["tool_names"] == ["good_search"]
        assert statuses["bad"]["status"] == "failed"
        assert statuses["bad"]["error"] == "auth failed"

    @pytest.mark.asyncio
    async def test_skipped_env_vars(self, monkeypatch):
        """环境变量缺失的服务器标记为 skipped。"""
        monkeypatch.delenv("MISSING_STATUS_VAR", raising=False)
        configs = [
            McpServerConfig(
                name="envsrv", transport="stdio",
                command="${MISSING_STATUS_VAR}/bin/server",
            ),
        ]
        mgr = _manager(configs)
        await mgr.connect_all()

        statuses = mgr.get_server_statuses()
        assert len(statuses) == 1
        assert statuses[0]["name"] == "envsrv"
        assert statuses[0]["status"] == "skipped"
        assert statuses[0]["error"] == "environment variable(s) not set"
        assert statuses[0]["tool_names"] == []

    @pytest.mark.asyncio
    async def test_tool_attribution(self):
        """多服务器场景下工具按名称前缀正确归属。"""
        configs = [
            McpServerConfig(name="fs", transport="stdio", command="cmd-fs"),
            McpServerConfig(name="git", transport="stdio", command="cmd-git"),
        ]
        mgr = _manager(configs)

        # 构造归属不同服务器的工具
        tool_fs_read = MagicMock()
        tool_fs_read.name = "fs_read_file"
        tool_fs_write = MagicMock()
        tool_fs_write.name = "fs_write_file"
        tool_git_log = MagicMock()
        tool_git_log.name = "git_log"
        # 不匹配任何服务器前缀的工具
        tool_other = MagicMock()
        tool_other.name = "unknown_tool"

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(
            return_value=[tool_fs_read, tool_fs_write, tool_git_log, tool_other]
        )

        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient", return_value=mock_client
        ):
            await mgr.connect_all()

        statuses = mgr.get_server_statuses()
        fs_status = next(s for s in statuses if s["name"] == "fs")
        git_status = next(s for s in statuses if s["name"] == "git")

        assert fs_status["tool_names"] == ["fs_read_file", "fs_write_file"]
        assert git_status["tool_names"] == ["git_log"]


class TestMcpHotConnectDisconnect:
    """McpConnectionManager 热连接/热断开测试。"""

    @pytest.mark.asyncio
    async def test_add_server_success(self) -> None:
        config = McpServerConfig(name="test", transport="stdio", command="echo")
        manager = _manager()

        mock_tool = MagicMock()
        mock_tool.name = "test_tool1"

        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get_tools.return_value = [mock_tool]
            mock_client_cls.return_value = mock_client

            result = await manager.add_server(config)

        assert result["status"] == "connected"
        assert result["tool_names"] == ["test_tool1"]
        assert len(manager.get_tools()) == 1

    @pytest.mark.asyncio
    async def test_add_server_failure(self) -> None:
        config = McpServerConfig(name="bad", transport="stdio", command="nonexistent")
        manager = _manager()

        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient"
        ) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get_tools.side_effect = ConnectionError("refused")
            mock_client_cls.return_value = mock_client

            result = await manager.add_server(config)

        assert result["status"] == "failed"
        assert "refused" in result["error"]
        assert len(manager.get_tools()) == 0

    @pytest.mark.asyncio
    async def test_add_server_skipped_env(self) -> None:
        config = McpServerConfig(name="env", transport="stdio", command="${MISSING_VAR}")
        manager = _manager()
        result = await manager.add_server(config)
        assert result["status"] == "skipped"

    def test_remove_server(self) -> None:
        from harness_agent.extensions.mcp import build_mcp_snapshot

        snapshot = build_mcp_snapshot(
            [
                McpServerConfig(name="fs", transport="stdio", command="x"),
                McpServerConfig(name="gh", transport="http", url="http://x"),
            ],
            revision="test",
        )
        manager = McpConnectionManager(snapshot)
        tool1 = MagicMock()
        tool1.name = "fs_read"
        tool2 = MagicMock()
        tool2.name = "gh_pr"
        runtime = manager._current_resource.value
        runtime.tools = [tool1, tool2]
        runtime.server_statuses["fs"] = {"name": "fs", "transport": "stdio", "status": "connected"}
        runtime.server_statuses["gh"] = {"name": "gh", "transport": "http", "status": "connected"}

        assert manager.remove_server("fs") is True
        assert len(manager.get_tools()) == 1
        assert manager.get_tools()[0].name == "gh_pr"
        assert manager._current_resource.value.server_statuses["fs"]["status"] == "removed"

    def test_remove_nonexistent_server(self) -> None:
        manager = _manager()
        assert manager.remove_server("nonexistent") is False

    @pytest.mark.asyncio
    async def test_apply_snapshot_replaces_runtime_input(self) -> None:
        from harness_agent.extensions.mcp import build_mcp_snapshot

        manager = _manager()
        snapshot = build_mcp_snapshot(
            [McpServerConfig(name="new", transport="stdio", command="missing-command")],
            revision="rev-2",
        )
        await manager.apply_snapshot(snapshot)
        assert manager.snapshot is snapshot
        assert [item["name"] for item in manager.get_server_statuses()] == ["new"]
        assert manager.get_server_statuses()[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_apply_snapshot_keeps_old_resource_for_borrowing_engine(self) -> None:
        """MCP 热更新不能关闭仍被旧 AgentEngine 借用的 snapshot。"""
        from harness_agent.extensions.mcp import build_mcp_snapshot

        old_tool = MagicMock(name="old_tool")
        old_tool.name = "old_search"
        new_tool = MagicMock(name="new_tool")
        new_tool.name = "new_search"
        old_client = MagicMock()
        old_client.get_tools = AsyncMock(return_value=[old_tool])
        new_client = MagicMock()
        new_client.get_tools = AsyncMock(return_value=[new_tool])
        snapshots = build_mcp_snapshot(
            [McpServerConfig(name="old", transport="stdio", command="old")],
            revision="old",
        )
        updated = build_mcp_snapshot(
            [McpServerConfig(name="new", transport="stdio", command="new")],
            revision="new",
        )
        manager = McpConnectionManager(snapshots)
        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            side_effect=[old_client, new_client],
        ):
            await manager.connect_all()
            old_lease = await manager.acquire(snapshots)
            await manager.apply_snapshot(updated)
            new_lease = await manager.acquire(updated)

        assert [tool.name for tool in old_lease.value.tools] == ["old_search"]
        assert [tool.name for tool in new_lease.value.tools] == ["new_search"]
        assert manager.get_tool_names() == ["new_search"]
        await old_lease.release()
        await new_lease.release()
        await manager.close_all()

    @pytest.mark.asyncio
    async def test_apply_snapshot_reaps_idle_old_resource_after_invalidation(self) -> None:
        """没有活动 AgentEngine 时，MCP 热更新的失效路径立即回收旧连接。"""
        from harness_agent.extensions.mcp import build_mcp_snapshot

        old_client = MagicMock()
        old_client.get_tools = AsyncMock(return_value=[])
        old_client.aclose = AsyncMock()
        new_client = MagicMock()
        new_client.get_tools = AsyncMock(return_value=[])
        new_client.aclose = AsyncMock()
        old_snapshot = build_mcp_snapshot(
            [McpServerConfig(name="old", transport="stdio", command="old")],
            revision="old-idle",
        )
        new_snapshot = build_mcp_snapshot(
            [McpServerConfig(name="new", transport="stdio", command="new")],
            revision="new-idle",
        )
        manager = McpConnectionManager(old_snapshot)
        with patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            side_effect=[old_client, new_client],
        ):
            await manager.connect_all()
            await manager.apply_snapshot(new_snapshot)

        assert old_client.aclose.await_count == 0
        await manager.reap()
        assert old_client.aclose.await_count == 1
        await manager.close_all()
        assert new_client.aclose.await_count == 1


class TestMcpConfigSnapshot:
    """McpConfigSnapshot 构建和不可变性测试。"""

    def test_build_snapshot_sorts_by_name(self):
        from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot

        servers = [
            McpServerConfig(name="zeta", transport="stdio", command="z"),
            McpServerConfig(name="alpha", transport="http", url="http://a"),
        ]
        snapshot = build_mcp_snapshot(servers, revision="rev1")
        assert snapshot.servers[0].name == "alpha"
        assert snapshot.servers[1].name == "zeta"

    def test_build_snapshot_digest_stable(self):
        from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot

        servers = [McpServerConfig(name="a", transport="stdio", command="cmd")]
        s1 = build_mcp_snapshot(servers, revision="r1")
        s2 = build_mcp_snapshot(servers, revision="r1")
        assert s1.digest == s2.digest

    def test_build_snapshot_revision_preserved(self):
        from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot

        servers = [McpServerConfig(name="a", transport="stdio", command="cmd")]
        snapshot = build_mcp_snapshot(servers, revision="abc123")
        assert snapshot.revision == "abc123"

    def test_build_snapshot_empty_servers(self):
        from harness_agent.extensions.mcp import build_mcp_snapshot

        snapshot = build_mcp_snapshot([], revision="rev")
        assert snapshot.servers == ()
        assert snapshot.engine_identity["server_count"] == 0

    def test_snapshot_engine_identity_no_secrets(self):
        from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot

        servers = [
            McpServerConfig(
                name="s",
                transport="stdio",
                command="cmd",
                env={"SECRET_TOKEN": "super-secret-value"},
                headers={"Authorization": "Bearer xyz"},
            )
        ]
        snapshot = build_mcp_snapshot(servers, revision="r")
        identity_str = str(snapshot.engine_identity)
        assert "super-secret-value" not in identity_str
        assert "Bearer xyz" not in identity_str
        assert "SECRET_TOKEN" in identity_str  # key name is included
        assert "Authorization" in identity_str  # key name is included

    def test_snapshot_and_server_config_are_deeply_immutable(self):
        from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot

        server = McpServerConfig(
            name="s",
            transport="stdio",
            command="cmd",
            env={"TOKEN": "value"},
            headers={"X-Key": "value"},
        )
        snapshot = build_mcp_snapshot([server], revision="r")
        with pytest.raises(TypeError):
            server.env["TOKEN"] = "changed"  # type: ignore[index]
        with pytest.raises(TypeError):
            snapshot.engine_identity["server_count"] = 99  # type: ignore[index]

    def test_engine_identity_includes_all_non_secret_connection_fields(self):
        from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot

        snapshot = build_mcp_snapshot(
            [
                McpServerConfig(
                    name="s",
                    transport="stdio",
                    command="cmd",
                    args=("--flag",),
                    timeout_seconds=12,
                    env={"TOKEN": "secret"},
                    headers={"Authorization": "Bearer secret"},
                )
            ],
            revision="r",
        )
        server_identity = snapshot.engine_identity["servers"][0]  # type: ignore[index]
        assert server_identity["command"] == "cmd"
        assert server_identity["args"] == ("--flag",)
        assert server_identity["timeout_seconds"] == 12.0
        assert "secret" not in str(server_identity)


class TestMcpConfigFingerprintExpanded:
    """扩充后的 mcp_config_fingerprint 敏感性和脱敏测试。"""

    def test_fingerprint_sensitive_to_command(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="stdio", command="cmd-a")]
        c2 = [McpServerConfig(name="s", transport="stdio", command="cmd-b")]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_sensitive_to_url(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="http", url="http://a")]
        c2 = [McpServerConfig(name="s", transport="http", url="http://b")]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_sensitive_to_args(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="stdio", command="cmd", args=("--x",))]
        c2 = [McpServerConfig(name="s", transport="stdio", command="cmd", args=("--y",))]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_sensitive_to_env_key_names(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="stdio", command="cmd", env={"VAR_A": "val"})]
        c2 = [McpServerConfig(name="s", transport="stdio", command="cmd", env={"VAR_B": "val"})]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_insensitive_to_env_values(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="stdio", command="cmd", env={"TOKEN": "secret-1"})]
        c2 = [McpServerConfig(name="s", transport="stdio", command="cmd", env={"TOKEN": "secret-2"})]
        assert mcp_config_fingerprint(c1) == mcp_config_fingerprint(c2)

    def test_fingerprint_sensitive_to_header_key_names(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="http", url="http://x", headers={"X-Key": "v"})]
        c2 = [McpServerConfig(name="s", transport="http", url="http://x", headers={"Y-Key": "v"})]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_insensitive_to_header_values(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="http", url="http://x", headers={"Auth": "token-a"})]
        c2 = [McpServerConfig(name="s", transport="http", url="http://x", headers={"Auth": "token-b"})]
        assert mcp_config_fingerprint(c1) == mcp_config_fingerprint(c2)

    def test_fingerprint_redacts_url_query_values(self):
        from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot, mcp_config_fingerprint

        first = McpServerConfig(name="s", transport="http", url="https://example.test/mcp?token=secret-a")
        second = McpServerConfig(name="s", transport="http", url="https://example.test/mcp?token=secret-b")
        assert mcp_config_fingerprint([first]) == mcp_config_fingerprint([second])
        assert "secret-a" not in str(build_mcp_snapshot([first], "r").engine_identity)

    def test_fingerprint_keeps_non_secret_url_query_shape(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        first = McpServerConfig(name="s", transport="http", url="https://example.test/mcp?mode=read")
        second = McpServerConfig(name="s", transport="http", url="https://example.test/mcp?mode=write")
        assert mcp_config_fingerprint([first]) != mcp_config_fingerprint([second])

    def test_fingerprint_sensitive_to_timeout(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [McpServerConfig(name="s", transport="stdio", command="cmd", timeout_seconds=10)]
        c2 = [McpServerConfig(name="s", transport="stdio", command="cmd", timeout_seconds=60)]
        assert mcp_config_fingerprint(c1) != mcp_config_fingerprint(c2)

    def test_fingerprint_sensitive_to_plugin_package_identity(self):
        """同一 MCP 定义来自不同 Plugin 包内容时必须产生不同 Engine 身份。"""
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        first = McpServerConfig(
            name="plugin__local__review__server",
            transport="stdio",
            command="server",
            source="plugin:local/review",
            source_fingerprint="a" * 64,
            inherit_environment=False,
        )
        second = McpServerConfig(
            name=first.name,
            transport="stdio",
            command="server",
            source=first.source,
            source_fingerprint="b" * 64,
            inherit_environment=False,
        )
        assert mcp_config_fingerprint([first]) != mcp_config_fingerprint([second])

    def test_fingerprint_order_independent(self):
        from harness_agent.extensions.mcp import McpServerConfig, mcp_config_fingerprint

        c1 = [
            McpServerConfig(name="a", transport="stdio", command="x"),
            McpServerConfig(name="b", transport="http", url="http://b"),
        ]
        c2 = [
            McpServerConfig(name="b", transport="http", url="http://b"),
            McpServerConfig(name="a", transport="stdio", command="x"),
        ]
        assert mcp_config_fingerprint(c1) == mcp_config_fingerprint(c2)
