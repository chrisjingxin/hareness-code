"""HC-158 Phase 4B Host↔Settings Protocol 离线回归。"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from harness_agent.config.settings import (
    FakeCredentialBackend,
    SettingBinding,
    SettingsSnapshot,
    parse_qwen_setting,
)
from harness_agent.extensions.mcp import McpServerConfig, build_mcp_snapshot
from harness_agent.host.agent_host import AgentHost
from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.manager import PluginSettingsLoadResult
from harness_agent.plugins.runtime import PluginRuntimeCatalog, PluginRuntimeManager


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    """构造最小 JSON-RPC 请求。"""
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params(
    *,
    max_minor: int = 8,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """请求 Settings 管理所需能力，同时保持完全离线。"""
    return {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": max_minor},
        "client": {"name": "settings-test", "version": "0.1.0", "kind": "test"},
        "capabilities": {
            "requests": capabilities or ["settings.read", "settings.manage"],
            "handles": [],
        },
    }


def _install_settings_plugin(home: Path, source: Path) -> dict[str, object]:
    """安装一个只含 Qwen Setting 的 fake package。"""
    source.mkdir()
    (source / "qwen-extension.json").write_text(
        json.dumps(
            {
                "name": "settings-demo",
                "version": "1.0.0",
                "settings": [
                    {
                        "name": "Offline token",
                        "description": "fake only",
                        "envVar": "DEMO_TOKEN",
                        "sensitive": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(home=home)
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    return installed


@pytest.mark.asyncio
async def test_host_settings_list_set_and_next_host_snapshot_are_redacted(tmp_path: Path) -> None:
    """Host 只返回脱敏 summary，set 进入 next-host，新 Host 才加载值。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    installed = _install_settings_plugin(home, tmp_path / "source")
    backend = FakeCredentialBackend()
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=backend,
    )
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    host.send = capture
    await host.dispatch(_request("initialize", _initialize_params(), "init"))
    assert next(frame["result"]["protocol"] for frame in frames if frame.get("id") == "init") == {
        "major": 3,
        "minor": 8,
    }
    await host.dispatch(_request("settings.list", {"scope": "user"}, "list-1"))
    listing = next(frame["result"] for frame in frames if frame.get("id") == "list-1")
    assert listing["scope"] == "user"
    summary = listing["settings"][0]
    assert summary["name"] == "settings-demo"
    assert summary["setting"] == "DEMO_TOKEN"
    assert summary["store_state"] == "absent"
    assert "env_var" not in summary
    assert "plugin_id" not in summary
    assert "value" not in str(listing)

    set_params = {
        "scope": "user",
        "name": "settings-demo",
        "setting": "DEMO_TOKEN",
        "value": "generated-fake-secret",
    }
    await host.dispatch(_request("settings.set", set_params, "set-1"))
    mutation = next(frame["result"] for frame in frames if frame.get("id") == "set-1")
    assert "applies_to" not in mutation
    assert "generated-fake-secret" not in str(mutation)
    await host.close()

    next_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=backend,
    )
    next_frames: list[dict[str, Any]] = []

    async def capture_next(message: dict[str, Any]) -> None:
        next_frames.append(message)

    next_host.send = capture_next
    await next_host.dispatch(_request("initialize", _initialize_params(), "init-next"))
    await next_host.dispatch(_request("settings.list", {"scope": "user"}, "list-next"))
    next_listing = next(frame["result"] for frame in next_frames if frame.get("id") == "list-next")
    assert next_listing["settings"][0]["name"] == "settings-demo"
    assert next_listing["settings"][0]["setting"] == "DEMO_TOKEN"
    assert next_listing["settings"][0]["store_state"] == "configured"
    assert next_listing["settings"][0]["runtime_state"] == "loaded"
    assert "generated-fake-secret" not in str(next_listing)
    await next_host.close()


@pytest.mark.asyncio
async def test_settings_rpc_respects_minor_and_capability_gates(tmp_path: Path) -> None:
    """旧 minor/缺 capability 不能在协商成功后偷偷调用 Settings RPC。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    old_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    old_frames: list[dict[str, Any]] = []

    async def capture_old(message: dict[str, Any]) -> None:
        old_frames.append(message)

    old_host.send = capture_old
    await old_host.dispatch(_request("initialize", _initialize_params(max_minor=5), "old-init"))
    old_result = next(frame["result"] for frame in old_frames if frame.get("id") == "old-init")
    assert old_result["protocol"]["minor"] == 5
    await old_host.dispatch(_request("settings.list", {"scope": "user"}, "old-settings"))
    old_error = next(frame["error"] for frame in old_frames if frame.get("id") == "old-settings")
    assert old_error["message"] == "SETTINGS_PROTOCOL_MINOR_REQUIRED"
    await old_host.close()

    capability_host = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "capability-home",
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    capability_frames: list[dict[str, Any]] = []

    async def capture_capability(message: dict[str, Any]) -> None:
        capability_frames.append(message)

    capability_host.send = capture_capability
    await capability_host.dispatch(
        _request(
            "initialize",
            _initialize_params(capabilities=["plugins.read"]),
            "capability-init",
        )
    )
    await capability_host.dispatch(
        _request("settings.list", {"scope": "user"}, "capability-settings")
    )
    capability_error = next(
        frame["error"]
        for frame in capability_frames
        if frame.get("id") == "capability-settings"
    )
    assert capability_error["message"] == "SETTINGS_CAPABILITY_REQUIRED"
    await capability_host.close()


@pytest.mark.asyncio
async def test_settings_rpc_maps_value_schema_failures_to_stable_settings_errors(tmp_path: Path) -> None:
    """RPC schema 先拒绝 value 时仍返回 Settings 专属稳定错误码。"""
    host = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
        settings_backend=FakeCredentialBackend(),
    )
    (tmp_path / "workspace").mkdir()
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    host.send = capture
    await host.dispatch(_request("initialize", _initialize_params(), "init"))
    common = {
        "scope": "user",
        "name": "demo",
        "setting": "DEMO_TOKEN",
    }
    await host.dispatch(_request("settings.set", {**common, "value": "bad\x00value"}, "nul"))
    await host.dispatch(
        _request(
            "settings.set",
            {**common, "value": "x" * 65_537},
            "large",
        )
    )

    assert next(frame["error"]["message"] for frame in frames if frame.get("id") == "nul") == (
        "SETTINGS_VALUE_INVALID"
    )
    assert next(frame["error"]["message"] for frame in frames if frame.get("id") == "large") == (
        "SETTINGS_VALUE_TOO_LARGE"
    )
    await host.close()


@pytest.mark.asyncio
async def test_host_applies_qwen_settings_only_to_runtime_child_configs(tmp_path: Path) -> None:
    """新 Host 把同一插件的已配置值交给 MCP/Hook/LSP，不修改全局环境。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "settings-mcp"
    source.mkdir()
    (source / "qwen-extension.json").write_text(
        json.dumps(
            {
                "name": "settings-mcp",
                "version": "1.0.0",
                "settings": [
                    {
                        "name": "Offline token",
                        "description": "fake only",
                        "envVar": "DEMO_TOKEN",
                    }
                ],
                "mcpServers": {
                    "fake": {"command": "node", "env": {"PUBLIC": "yes"}}
                },
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(home=home)
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    backend = FakeCredentialBackend()

    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=backend,
    )
    host._build_skill_registry()  # noqa: SLF001 - construct the startup catalog without spawning
    host._refresh_settings_snapshot()  # noqa: SLF001
    binding = host._settings_bindings[0]  # noqa: SLF001
    host._settings_user_store.set(  # noqa: SLF001
        scope="user",
        plugin_id=binding.plugin_id,
        package_digest=binding.package_digest,
        declaration_digest=binding.declaration_digest,
        setting_key=binding.setting_key,
        env_var=binding.env_var,
        value="host-snapshot-fake",
        expected_store_revision=0,
        name=binding.declaration.name,
        description=binding.declaration.description,
        sensitive=binding.declaration.sensitive,
    )

    next_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=backend,
    )
    next_host._build_skill_registry()  # noqa: SLF001
    next_host._refresh_settings_snapshot()  # noqa: SLF001
    assert next_host._plugin_mcp_servers[0].env["DEMO_TOKEN"] == "host-snapshot-fake"  # noqa: SLF001
    assert next_host._plugin_mcp_servers[0].env["PUBLIC"] == "yes"  # noqa: SLF001
    assert next_host._settings_snapshot.to_dict()["state"] == "loaded"  # noqa: SLF001
    assert "DEMO_TOKEN" not in os.environ
    await host.close()
    await next_host.close()


def test_host_uses_platform_backend_when_no_test_backend_is_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 Host 走平台 backend 工厂，测试仍可显式注入 fake。"""
    backend = FakeCredentialBackend()
    monkeypatch.setattr(
        "harness_agent.host.agent_host.create_platform_credential_backend",
        lambda **_kwargs: backend,
    )
    host = AgentHost(config_home=tmp_path / "home", workspace=tmp_path / "workspace")
    assert host._settings_user_store.backend is backend  # noqa: SLF001


def test_settings_backend_failure_keeps_declaration_and_blocks_executable_consumers(
    tmp_path: Path,
) -> None:
    """backend 不可用时保留可审计 declaration，但 MCP 不得构造可执行配置。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "settings-mcp"
    source.mkdir()
    (source / "qwen-extension.json").write_text(
        json.dumps(
            {
                "name": "settings-mcp",
                "settings": [
                    {
                        "name": "Offline token",
                        "description": "fake only",
                        "envVar": "DEMO_TOKEN",
                    }
                ],
                "mcpServers": {"fake": {"command": "node"}},
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(home=home)
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)

    host = AgentHost(
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(available=False),
    )
    host._build_skill_registry()  # noqa: SLF001 - 只构造，不启动子进程
    host._refresh_settings_snapshot()  # noqa: SLF001

    assert len(host._settings_bindings) == 1  # noqa: SLF001
    assert "SETTINGS_BACKEND_UNAVAILABLE" in host._settings_diagnostics  # noqa: SLF001
    assert host._plugin_mcp_servers == ()  # noqa: SLF001
    assert host._settings_snapshot.state == "not_loaded"  # noqa: SLF001


def test_host_without_settings_does_not_probe_credential_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 Qwen Settings declaration 时普通 Host 启动不接触 credential manager。"""
    class CountingBackend(FakeCredentialBackend):
        def __init__(self) -> None:
            super().__init__()
            self.probe_calls = 0

        def capability_probe(self) -> bool:
            self.probe_calls += 1
            return super().capability_probe()

    backend = CountingBackend()
    monkeypatch.setattr(
        "harness_agent.host.agent_host.create_platform_credential_backend",
        lambda **_kwargs: backend,
    )
    host = AgentHost(config_home=tmp_path / "home", workspace=tmp_path / "workspace")
    host._refresh_settings_snapshot()  # noqa: SLF001
    assert backend.probe_calls == 0
    assert host._settings_snapshot.state == "loaded"  # noqa: SLF001
    assert host._settings_diagnostics == ()  # noqa: SLF001


def test_settings_snapshot_replacement_releases_previous_generation_values(tmp_path: Path) -> None:
    """刷新 generation snapshot 时释放旧引用，但不在 Run terminal 释放 Host snapshot。"""
    host = AgentHost(config_home=tmp_path / "home", workspace=tmp_path / "workspace")
    previous = SettingsSnapshot.loaded(1, {"setting-id": "generated-fake-secret"})
    host._settings_snapshot = previous  # noqa: SLF001
    host._refresh_settings_snapshot()  # noqa: SLF001
    assert previous.value_for("setting-id") is None
    assert host._settings_snapshot.state == "loaded"  # noqa: SLF001


def test_settings_snapshot_failure_removes_previous_mcp_env_overlay(tmp_path: Path) -> None:
    """snapshot 失效或替换后，MCP 只能回到 immutable base env，不能残留旧值。"""
    declaration = parse_qwen_setting(
        {"name": "Token", "description": "fake", "envVar": "DEMO_TOKEN"}
    )
    binding = SettingBinding(
        plugin_id="plugin/local/demo",
        package_digest="a" * 64,
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )
    host = AgentHost(config_home=tmp_path / "home", workspace=tmp_path / "workspace")
    host._settings_bindings = (binding,)  # noqa: SLF001
    base_servers = (  # noqa: SLF001
        McpServerConfig(
            name="fake",
            transport="stdio",
            command="/usr/bin/true",
            env={"PUBLIC": "yes"},
            source="plugin:plugin/local/demo",
        ),
    )
    host._base_plugin_mcp_servers = base_servers  # noqa: SLF001
    host._plugin_mcp_servers = base_servers  # noqa: SLF001
    host._settings_snapshot = SettingsSnapshot.loaded(1, {binding.setting_id: "generated-fake-secret"})  # noqa: SLF001
    host._apply_settings_snapshot_to_children()  # noqa: SLF001
    assert host._plugin_mcp_servers[0].env["DEMO_TOKEN"] == "generated-fake-secret"  # noqa: SLF001

    host._settings_snapshot = SettingsSnapshot.not_loaded()  # noqa: SLF001
    host._apply_settings_snapshot_to_children()  # noqa: SLF001
    assert dict(host._plugin_mcp_servers[0].env) == {"PUBLIC": "yes"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_host_close_releases_settings_snapshot_and_all_child_overlays(tmp_path: Path) -> None:
    """Host close 清除 snapshot、Hook/LSP overlay，并把旧 MCP 对象恢复为 base env。"""
    declaration = parse_qwen_setting(
        {"name": "Token", "description": "fake", "envVar": "DEMO_TOKEN"}
    )
    binding = SettingBinding(
        plugin_id="plugin/local/demo",
        package_digest="a" * 64,
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )
    host = AgentHost(config_home=tmp_path / "home", workspace=tmp_path / "workspace")
    runtime = PluginRuntimeManager(PluginRuntimeCatalog())
    host._plugin_runtime_manager = runtime  # noqa: SLF001
    class FakeLspClient:
        plugin_id = binding.plugin_id

        def __init__(self) -> None:
            self._settings_environment = {"DEMO_TOKEN": "generated-fake-secret"}

        def set_settings_environment(self, values: Mapping[str, str]) -> None:
            self._settings_environment.clear()
            self._settings_environment.update(values)

        async def aclose(self) -> None:
            self._settings_environment.clear()

    fake_lsp_client = FakeLspClient()
    runtime.lsp._clients["fake"] = fake_lsp_client  # noqa: SLF001
    host._settings_bindings = (binding,)  # noqa: SLF001
    base = McpServerConfig(
        name="fake",
        transport="stdio",
        command="/usr/bin/true",
        env={"PUBLIC": "yes"},
        source="plugin:plugin/local/demo",
    )
    host._base_plugin_mcp_servers = (base,)  # noqa: SLF001
    host._plugin_mcp_servers = (base,)  # noqa: SLF001
    snapshot = SettingsSnapshot.loaded(1, {binding.setting_id: "generated-fake-secret"})
    host._settings_snapshot = snapshot  # noqa: SLF001
    host._apply_settings_snapshot_to_children()  # noqa: SLF001
    old_mcp = host._plugin_mcp_servers[0]  # noqa: SLF001
    assert old_mcp.env["DEMO_TOKEN"] == "generated-fake-secret"
    assert runtime.hooks._settings_environment  # noqa: SLF001
    assert runtime.lsp._settings_environment  # noqa: SLF001
    assert fake_lsp_client._settings_environment == {"DEMO_TOKEN": "generated-fake-secret"}

    await host.close()

    assert snapshot.value_for(binding.setting_id) is None
    assert runtime.hooks._settings_environment == {}  # noqa: SLF001
    assert runtime.lsp._settings_environment == {}  # noqa: SLF001
    assert fake_lsp_client._settings_environment == {}
    assert "DEMO_TOKEN" not in old_mcp.env
    assert dict(old_mcp.env) == {"PUBLIC": "yes"}


@pytest.mark.asyncio
async def test_host_close_releases_settings_from_retained_mcp_snapshot(tmp_path: Path) -> None:
    """generation 替换后仍被旧 snapshot 持有的 MCP 配置也不能保留 secret。"""
    declaration = parse_qwen_setting(
        {"name": "Token", "description": "fake", "envVar": "DEMO_TOKEN"}
    )
    binding = SettingBinding(
        plugin_id="plugin/local/demo",
        package_digest="a" * 64,
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )
    host = AgentHost(config_home=tmp_path / "home", workspace=tmp_path / "workspace")
    base = McpServerConfig(
        name="fake",
        transport="stdio",
        command="/usr/bin/true",
        env={"PUBLIC": "yes"},
        source="plugin:plugin/local/demo",
    )
    host._base_plugin_mcp_servers = (base,)  # noqa: SLF001
    host._plugin_mcp_servers = (base,)  # noqa: SLF001
    host._settings_bindings = (binding,)  # noqa: SLF001
    host._settings_snapshot = SettingsSnapshot.loaded(1, {binding.setting_id: "generated-fake-secret"})  # noqa: SLF001
    host._apply_settings_snapshot_to_children()  # noqa: SLF001

    stale_snapshot_server = replace(
        host._plugin_mcp_servers[0],  # noqa: SLF001
        env={"PUBLIC": "yes", "DEMO_TOKEN": "generated-fake-secret"},
    )
    host._mcp_snapshot = build_mcp_snapshot((stale_snapshot_server,), revision="stale")  # noqa: SLF001
    host._plugin_mcp_servers = (base,)  # noqa: SLF001

    await host.close()

    assert dict(stale_snapshot_server.env) == {"PUBLIC": "yes"}


def test_agent_host_freezes_settings_roots_policy_and_home_binding(tmp_path: Path) -> None:
    """Host 启动时把可信 roots/policy/home identity 传给 SettingsStore。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    trusted_root = tmp_path / "trusted-root"
    workspace.mkdir()
    trusted_root.mkdir()
    host = AgentHost(
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
        settings_workspace_roots=(trusted_root,),
        settings_policy_version="policy-test-v1",
    )

    assert host._settings_user_store.policy_version == "policy-test-v1"  # noqa: SLF001
    assert host._settings_workspace_store.workspace_roots == (trusted_root.absolute(),)  # noqa: SLF001
    assert host._settings_workspace_store.policy_version == "policy-test-v1"  # noqa: SLF001

    changed = AgentHost(
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
        settings_workspace_roots=(),
        settings_policy_version="policy-test-v2",
    )
    assert (
        host._settings_workspace_store.workspace_binding_digest  # noqa: SLF001
        != changed._settings_workspace_store.workspace_binding_digest  # noqa: SLF001
    )


def test_host_resolver_blocks_only_bad_plugin_consumers(tmp_path: Path) -> None:
    """坏 Plugin 的 MCP/Hook/LSP 不得启动，健康 Plugin 仍保留。"""
    from harness_agent.plugins.runtime import HookDefinition, LspServerDefinition, PluginRuntimeCatalog

    def hook(plugin_id: str) -> HookDefinition:
        return HookDefinition(
            plugin_id=plugin_id,
            event="PreToolUse",
            matcher="*",
            command="/usr/bin/true",
            args=(),
            timeout_seconds=1.0,
            asynchronous=False,
            shell=None,
            root=tmp_path,
            data=tmp_path,
            workspace=tmp_path,
        )

    def lsp(plugin_id: str, name: str, extension: str) -> LspServerDefinition:
        return LspServerDefinition(
            plugin_id=plugin_id,
            name=name,
            command="/usr/bin/true",
            args=(),
            extension_to_language=((extension, name),),
            env=(),
            initialization_options={},
            settings={},
            workspace_folder=tmp_path,
            startup_timeout_seconds=1.0,
            shutdown_timeout_seconds=1.0,
            root=tmp_path,
            data=tmp_path,
            cwd=tmp_path,
        )

    host = AgentHost(
        config_home=tmp_path / "home",
        workspace=tmp_path / "workspace",
        settings_backend=FakeCredentialBackend(),
    )
    bindings = (
        SettingBinding(
            plugin_id="plugin/local/bad",
            package_digest="a" * 64,
            declaration_digest=parse_qwen_setting(
                {"name": "Bad", "description": "fake", "envVar": "BAD_TOKEN"}
            ).declaration_digest,
            declaration=parse_qwen_setting(
                {"name": "Bad", "description": "fake", "envVar": "BAD_TOKEN"}
            ),
        ),
        SettingBinding(
            plugin_id="plugin/local/good",
            package_digest="b" * 64,
            declaration_digest=parse_qwen_setting(
                {"name": "Good", "description": "fake", "envVar": "GOOD_TOKEN"}
            ).declaration_digest,
            declaration=parse_qwen_setting(
                {"name": "Good", "description": "fake", "envVar": "GOOD_TOKEN"}
            ),
        ),
    )
    host._settings_bindings = bindings  # noqa: SLF001
    for index, binding in enumerate(bindings):
        expected_revision = int(host._settings_user_store.list(scope="user")["store_revision"])  # noqa: SLF001
        host._settings_user_store.set(  # noqa: SLF001
            scope="user",
            plugin_id=binding.plugin_id,
            package_digest=binding.package_digest,
            declaration_digest=binding.declaration_digest,
            setting_key=binding.setting_key,
            env_var=binding.env_var,
            value=f"value-{index}",
            expected_store_revision=expected_revision,
            name=binding.declaration.name,
            description=binding.declaration.description,
            sensitive=binding.declaration.sensitive,
        )
    user_index = host._settings_user_store._read_index(  # noqa: SLF001
        "user", host._settings_user_store.user_binding_digest
    )
    assert user_index is not None
    bad_record = next(item for item in user_index.records if item.plugin_id == "plugin/local/bad")
    host._settings_user_store.backend.delete(  # noqa: SLF001
        host._settings_user_store._account_for(bad_record)  # noqa: SLF001
    )
    host._plugin_runtime_catalog = PluginRuntimeCatalog(
        hooks=(hook("plugin/local/bad"), hook("plugin/local/good")),
        lsp_servers=(
            lsp("plugin/local/bad", "bad", ".bad"),
            lsp("plugin/local/good", "good", ".good"),
        ),
    )
    host._base_plugin_runtime_catalog = host._plugin_runtime_catalog  # noqa: SLF001
    host._base_plugin_mcp_servers = (
        McpServerConfig(name="bad", transport="stdio", command="/usr/bin/true", env={}, source="plugin:plugin/local/bad"),
        McpServerConfig(name="good", transport="stdio", command="/usr/bin/true", env={}, source="plugin:plugin/local/good"),
    )
    host._plugin_mcp_servers = host._base_plugin_mcp_servers

    host._plugin_manager.setting_bindings = lambda _catalog: PluginSettingsLoadResult(  # type: ignore[method-assign]
        bindings=bindings,
        diagnostics=(),
        blocked_plugin_ids=(),
    )
    host._refresh_settings_snapshot()  # noqa: SLF001
    assert host._settings_snapshot.diagnostics == (
        "plugin:plugin/local/bad: SETTINGS_RECORD_STALE",
    )  # noqa: SLF001

    assert [item.source for item in host._plugin_mcp_servers] == ["plugin:plugin/local/good"]  # noqa: SLF001
    assert host._plugin_mcp_servers[0].env["GOOD_TOKEN"] == "value-1"  # noqa: SLF001
    assert [item.plugin_id for item in host._plugin_runtime_catalog.hooks] == ["plugin/local/good"]  # noqa: SLF001
    assert [item.plugin_id for item in host._plugin_runtime_catalog.lsp_servers] == ["plugin/local/good"]  # noqa: SLF001
    assert "plugin/local/bad" in host._settings_blocked_plugin_ids  # noqa: SLF001
