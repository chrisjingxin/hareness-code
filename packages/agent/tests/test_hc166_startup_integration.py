"""HC-166 第二轮启动 Host 与 TUI/Web 共享快照的纵向回归测试。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from harness_agent.config.settings import FakeCredentialBackend
from harness_agent.extensions.mcp import McpConnectionManager
from harness_agent.host.agent_host import AgentHost
from harness_agent.plugins.manager import PluginManager


REPO_ROOT = Path(__file__).resolve().parents[3]
QWEN_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "qwen_extensions" / "za38-devagent"
FULL_DEMO_ROOT = REPO_ROOT / "examples" / "plugins" / "harness-full-demo"


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    """构造测试用 JSON-RPC 请求。"""
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params(*, client_kind: str = "test") -> dict[str, Any]:
    """以完整 read 能力启动一个离线 Host，禁止任何真实外部进程。"""
    return {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 8},
        "client": {"name": "hc166-test", "version": "0", "kind": client_kind},
        "capabilities": {
            "requests": [
                "run.cancel",
                "run.multithread",
                "config.read",
                "threads.read",
                "context.manage",
                "skills.read",
                "models.read",
                "models.select",
                "mcp.read",
                "agents.read",
                "plugins.read",
                "settings.read",
            ],
            "handles": [],
        },
    }


async def _initialize(host: AgentHost, *, client_kind: str = "test") -> list[dict[str, Any]]:
    """捕获初始化响应并保持测试通信完全在内存中。"""
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    host.send = capture  # type: ignore[method-assign]
    await host.dispatch(
        _request("initialize", _initialize_params(client_kind=client_kind), "initialize")
    )
    return frames


async def _result(
    host: AgentHost,
    method: str,
    params: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """请求一个只读 consumer RPC，并返回其成功结果。"""
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    host.send = capture  # type: ignore[method-assign]
    await host.dispatch(_request(method, params, request_id))
    response = next(frame for frame in frames if frame.get("id") == request_id)
    assert "error" not in response
    result = response.get("result")
    assert isinstance(result, dict)
    return result


def _write_model_config(home: Path) -> None:
    """给 agents.list 一个完全离线的模型目录，不读取真实凭据。"""
    config = home / ".harness" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """[config]
version = 1

[models]
default_profile = "fast"

[models.profiles.fast]
model = "offline-model"
base_url = "https://example.test"
""",
        encoding="utf-8",
    )


def _copy_fixture(source: Path, destination: Path) -> Path:
    """复制离线 fixture；测试不读取开发期来源目录的可变文件。"""
    shutil.copytree(source, destination)
    return destination


async def _fake_mcp_connect_all(manager: McpConnectionManager) -> None:
    """只标记 MCP snapshot 已完成 fake 连接，不创建任何外部进程。"""
    runtime = manager._current_resource.value  # noqa: SLF001
    runtime.connected = True
    for config in runtime.snapshot.servers:
        runtime.server_statuses[config.name] = {
            "name": config.name,
            "transport": config.transport,
            "source": config.source,
            "status": "connected",
            "tool_names": [],
        }


async def _consumer_signature(
    host: AgentHost,
    initialize_frames: list[dict[str, Any]],
) -> dict[str, tuple[Any, ...]]:
    """读取 TUI/Web 两个入口实际共享的 canonical consumer 摘要。"""
    initialized = initialize_frames[0]["result"]
    skills = await _result(host, "skills.list", {}, "skills")
    agents = await _result(host, "agents.list", {}, "agents")
    mcp = await _result(host, "mcp.status", {}, "mcp")
    settings = await _result(host, "settings.list", {}, "settings")
    skill_ids = tuple(
        item["id"]
        for item in skills["skills"]
        if isinstance(item, dict) and item.get("source", "").startswith("plugin:")
    )
    agent_ids = tuple(
        item["id"]
        for item in agents["agents"]
        if isinstance(item, dict) and item.get("kind") == "plugin"
    )
    mcp_names = tuple(
        item["name"]
        for item in mcp["servers"]
        if isinstance(item, dict) and item.get("source", "").startswith("plugin:")
    )
    setting_names = tuple(
        (item["name"], item["setting"])
        for item in settings["settings"]
        if isinstance(item, dict)
    )
    return {
        "commands": tuple(
            item["id"]
            for item in initialized["agent_commands"]
            if isinstance(item, dict) and item.get("plugin_id")
        ),
        "skills": tuple(sorted(skill_ids)),
        "agents": tuple(sorted(agent_ids)),
        "contexts": tuple(
            sorted(
                block.key
                for blocks in host._plugin_context_blocks_by_source.values()  # noqa: SLF001
                for block in blocks
            )
        ),
        "mcp": tuple(sorted(mcp_names)),
        "hooks": tuple(
            sorted(
                (hook.plugin_id, hook.event, hook.matcher)
                for hook in host._plugin_runtime_catalog.hooks  # noqa: SLF001
            )
        ),
        "lsp": tuple(
            sorted(
                (server.plugin_id, server.name)
                for server in host._plugin_runtime_catalog.lsp_servers  # noqa: SLF001
            )
        ),
        "settings": tuple(sorted(setting_names)),
        "monitors": tuple(
            sorted(
                (monitor.plugin_id, monitor.name)
                for monitor in host._plugin_runtime_catalog.monitors  # noqa: SLF001
            )
        ),
    }


def _write_skill_plugin(
    root: Path,
    *,
    name: str = "snapshot-plugin",
    version: str = "1.0.0",
) -> None:
    """创建只含离线 Skill 的最小安装 fixture。"""
    (root / "skills" / "review").mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": name,
                "version": version,
            }
        ),
        encoding="utf-8",
    )
    (root / "skills" / "review" / "SKILL.md").write_text(
        f"---\nname: review\ndescription: {version}\n---\n\n{version}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_started_host_keeps_plugin_snapshot_until_close(tmp_path: Path) -> None:
    """外部 Shell 停用后，旧 Host 仍使用启动时的 Skill snapshot。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source"
    _write_skill_plugin(source)
    manager = PluginManager(home=tmp_path / "home")
    manager.install(source)

    old_host = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=workspace,
    )
    await _initialize(old_host)
    old_snapshot_id = old_host._plugin_catalog_snapshot.snapshot_id  # noqa: SLF001
    old_skill_id = next(  # noqa: SLF001
        record.skill_id for record in old_host._skill_registry.records if record.source.startswith("plugin:")
    )

    manager.set_enabled("snapshot-plugin", enabled=False)
    await old_host.dispatch(_request("skills.list", {}, "skills-after-disable"))

    assert old_host._plugin_catalog_snapshot.snapshot_id == old_snapshot_id  # noqa: SLF001
    assert old_host._skill_registry.resolve(old_skill_id)  # noqa: SLF001

    await old_host.close()
    new_host = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=workspace,
    )
    await _initialize(new_host)
    assert not any(  # noqa: SLF001
        record.source.startswith("plugin:") for record in new_host._skill_registry.records
    )
    await new_host.close()


@pytest.mark.asyncio
async def test_started_host_does_not_reinject_context_after_external_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell 停用后，旧 Host 的 Context generation 仍保持启动时内容。"""
    monkeypatch.setattr(McpConnectionManager, "connect_all", _fake_mcp_connect_all)
    source = _copy_fixture(QWEN_FIXTURE_ROOT, tmp_path / "qwen-source")
    home = tmp_path / "home"
    manager = PluginManager(home=home)
    manager.install(source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    try:
        await _initialize(host, client_kind="tui")
        snapshot_id = host._plugin_catalog_snapshot.snapshot_id  # noqa: SLF001
        context_before = {
            source_id: tuple(block.key for block in blocks)
            for source_id, blocks in host._plugin_context_blocks_by_source.items()  # noqa: SLF001
        }
        assert context_before

        manager.set_enabled("ZA38.03_CLI_EXTENSION", enabled=False)
        await _result(host, "skills.list", {}, "skills-after-disable")

        assert host._plugin_catalog_snapshot.snapshot_id == snapshot_id  # noqa: SLF001
        assert {
            source_id: tuple(block.key for block in blocks)
            for source_id, blocks in host._plugin_context_blocks_by_source.items()  # noqa: SLF001
        } == context_before
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_tui_and_web_hosts_share_the_full_startup_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 user 安装在两个新入口中提供完整且相同的 canonical consumer。"""
    monkeypatch.setattr(McpConnectionManager, "connect_all", _fake_mcp_connect_all)
    home = tmp_path / "home"
    _write_model_config(home)
    qwen_source = _copy_fixture(QWEN_FIXTURE_ROOT, tmp_path / "qwen-source")
    qwen_manifest_path = qwen_source / "devagent-extension.json"
    qwen_manifest = json.loads(qwen_manifest_path.read_text(encoding="utf-8"))
    qwen_manifest["settings"] = [
        {
            "name": "Offline token",
            "description": "fixture declaration",
            "envVar": "HC164_FAKE_TOKEN",
        }
    ]
    qwen_manifest_path.write_text(json.dumps(qwen_manifest), encoding="utf-8")
    demo_source = _copy_fixture(FULL_DEMO_ROOT, tmp_path / "demo-source")

    manager = PluginManager(home=home)
    manager.install(qwen_source)
    manager.install(demo_source)
    workspace_tui = tmp_path / "workspace-tui"
    workspace_web = tmp_path / "workspace-web"
    workspace_tui.mkdir()
    workspace_web.mkdir()

    tui_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace_tui,
        settings_backend=FakeCredentialBackend(),
    )
    web_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace_web,
        settings_backend=FakeCredentialBackend(),
    )
    try:
        tui_frames = await _initialize(tui_host, client_kind="tui")
        web_frames = await _initialize(web_host, client_kind="web")
        tui_signature = await _consumer_signature(tui_host, tui_frames)
        web_signature = await _consumer_signature(web_host, web_frames)

        assert tui_signature == web_signature
        assert len(tui_signature["commands"]) >= 4
        assert len(tui_signature["skills"]) >= 2
        assert len(tui_signature["agents"]) >= 6
        assert len(tui_signature["contexts"]) == 1
        assert len(tui_signature["mcp"]) == 2
        assert len(tui_signature["hooks"]) >= 2
        assert len(tui_signature["lsp"]) == 1
        assert tui_signature["settings"] == (("ZA38.03_CLI_EXTENSION", "HC164_FAKE_TOKEN"),)
        assert len(tui_signature["monitors"]) == 1
        assert any(item.endswith("/command/za38-sdd") for item in tui_signature["commands"])
        assert any(item.endswith("/command/health") for item in tui_signature["commands"])
        assert any(item.endswith("/za38-framework") for item in tui_signature["skills"])
        assert any(item.endswith("/project-health") for item in tui_signature["skills"])
        assert {
            "za38-backend-executor",
            "za38-frontend-executor",
            "za38-java-executor",
            "demo-code-reviewer",
            "demo-review-lead",
            "demo-test-reviewer",
        } <= set(tui_signature["agents"])
    finally:
        await tui_host.close()
        await web_host.close()


@pytest.mark.parametrize(
    ("plugin_format", "manifest_name"),
    (
        ("agent-plugins-1.0", "plugin.json"),
        ("claude-code", ".claude-plugin/plugin.json"),
        ("hybrid", ".claude-plugin/plugin.json"),
        ("qwen-code", "qwen-extension.json"),
    ),
)
@pytest.mark.asyncio
async def test_each_supported_format_enters_startup_skill_and_command_consumers(
    tmp_path: Path,
    plugin_format: str,
    manifest_name: str,
) -> None:
    """四种格式都经 Adapter 进入同一 Skill/Command startup consumer。"""
    source = tmp_path / f"{plugin_format}-source"
    (source / "skills" / "review").mkdir(parents=True)
    (source / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: offline review\n---\n\nReview.\n",
        encoding="utf-8",
    )
    (source / "commands").mkdir()
    (source / "commands" / "check.md").write_text(
        "---\ndescription: offline check\n---\n\nCheck.\n",
        encoding="utf-8",
    )
    if plugin_format == "agent-plugins-1.0":
        manifest: dict[str, object] = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "startup-portable",
            "version": "1.0.0",
            "extensions": {
                "com.za38.harness": {
                    "schemaVersion": "1.0.0",
                    "commands": "commands",
                }
            },
        }
    elif plugin_format == "claude-code":
        manifest = {"name": "startup-claude", "version": "1.0.0"}
    elif plugin_format == "hybrid":
        manifest = {"name": "startup-hybrid", "version": "1.0.0"}
        (source / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    **manifest,
                }
            ),
            encoding="utf-8",
        )
    else:
        manifest = {
            "name": "startup-qwen",
            "version": "1.0.0",
            "commands": ["commands"],
            "skills": ["skills"],
        }
    manifest_path = source / manifest_name
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    home = tmp_path / "home"
    _write_model_config(home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = PluginManager(home=home)
    installed = manager.install(source)
    assert installed["plugin"]["status"] == "loaded"
    assert installed["plugin"]["format"] == plugin_format

    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    try:
        frames = await _initialize(host, client_kind="tui")
        signature = await _consumer_signature(host, frames)
        assert any(record.name == "review" for record in host._skill_registry.records)  # noqa: SLF001
        assert any(record.name == "check" for record in host._skill_registry.records)  # noqa: SLF001
        assert len(signature["commands"]) == 1
        assert len(signature["skills"]) == 1
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_disabled_failed_and_partially_invalid_plugins_are_not_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disabled/failed 项不进入 consumer，部分有效项只保留实际 warning。"""
    monkeypatch.setattr(McpConnectionManager, "connect_all", _fake_mcp_connect_all)
    home = tmp_path / "home"
    _write_model_config(home)

    healthy = tmp_path / "healthy"
    _write_skill_plugin(healthy)
    failed = tmp_path / "failed"
    failed.mkdir()
    (failed / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "failed-plugin",
                "version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )
    warning = tmp_path / "warning"
    _write_skill_plugin(warning, name="warning-plugin")
    (warning / "mcp.json").write_text(
        json.dumps({"mcpServers": {"broken": {"type": "stdio", "command": 42}}}),
        encoding="utf-8",
    )

    manager = PluginManager(home=home)
    manager.install(healthy)
    manager.install(failed)
    manager.install(warning)
    manager.set_enabled("snapshot-plugin", enabled=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    try:
        frames = await _initialize(host, client_kind="tui")
        initialized = frames[0]["result"]
        plugin_ids = {plugin.plugin_id for plugin in host._plugin_catalog_snapshot.plugins}  # noqa: SLF001
        assert len(plugin_ids) == 1
        assert "failed-plugin" not in str(plugin_ids)
        assert "snapshot-plugin" not in str(plugin_ids)
        assert any(plugin.name == "warning-plugin" for plugin in host._plugin_catalog_snapshot.plugins)  # noqa: SLF001
        assert all("failed-plugin" not in str(item) for item in initialized["agent_commands"])
        assert all("failed-plugin" not in record.source for record in host._skill_registry.records)  # noqa: SLF001
        assert host._plugin_mcp_servers == ()  # noqa: SLF001
        assert host._plugin_runtime_catalog.hooks == ()  # noqa: SLF001
        assert host._plugin_runtime_catalog.lsp_servers == ()  # noqa: SLF001

        statuses = await _result(host, "plugins.list", {}, "plugins")
        by_name = {item["name"]: item for item in statuses["plugins"]}
        assert by_name["snapshot-plugin"]["status"] == "disabled"
        assert by_name["failed-plugin"]["status"] == "failed"
        assert by_name["warning-plugin"]["status"] == "warning"
        warning_messages = by_name["warning-plugin"]["warnings"]
        assert warning_messages
        assert all("已接入 Harness" not in message for message in warning_messages)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_update_and_remove_only_change_new_host_visibility(tmp_path: Path) -> None:
    """update/remove 不改写旧 Host 的 Skill snapshot，新 Host 才看到结果。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    v1 = tmp_path / "v1"
    _write_skill_plugin(v1, version="1.0.0")
    v2 = tmp_path / "v2"
    _write_skill_plugin(v2, version="2.0.0")
    manager = PluginManager(home=home)
    manager.install(v1)

    old_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    await _initialize(old_host, client_kind="tui")
    old_record = next(
        record
        for record in old_host._skill_registry.records  # noqa: SLF001
        if record.source.startswith("plugin:")
    )
    old_loaded = old_host._skill_registry.load(old_record.skill_id)  # noqa: SLF001
    old_snapshot_id = old_host._plugin_catalog_snapshot.snapshot_id  # noqa: SLF001

    manager.update("snapshot-plugin", source=v2)
    await _result(old_host, "skills.list", {}, "skills-after-update")
    assert old_host._plugin_catalog_snapshot.snapshot_id == old_snapshot_id  # noqa: SLF001
    assert old_host._skill_registry.load(old_record.skill_id).body == old_loaded.body  # noqa: SLF001

    updated_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    await _initialize(updated_host, client_kind="tui")
    updated_record = next(
        record
        for record in updated_host._skill_registry.records  # noqa: SLF001
        if record.source.startswith("plugin:")
    )
    assert updated_host._skill_registry.load(updated_record.skill_id).body != old_loaded.body  # noqa: SLF001

    manager.remove("snapshot-plugin")
    await _result(old_host, "skills.list", {}, "skills-after-remove")
    assert old_host._skill_registry.resolve(old_record.skill_id)  # noqa: SLF001
    removed_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace,
        settings_backend=FakeCredentialBackend(),
    )
    await _initialize(removed_host, client_kind="web")
    try:
        assert not any(  # noqa: SLF001
            record.source.startswith("plugin:")
            for record in removed_host._skill_registry.records
        )
    finally:
        await old_host.close()
        await updated_host.close()
        await removed_host.close()


@pytest.mark.asyncio
async def test_user_catalog_ignores_missing_workspace_and_override_isolated_by_identity(
    tmp_path: Path,
) -> None:
    """user activation 不依赖 workspace 存在，workspace override 只影响绑定目录。"""
    home = tmp_path / "home"
    source = tmp_path / "source"
    _write_skill_plugin(source)
    manager = PluginManager(home=home)
    manager.install(source)
    missing_workspace = tmp_path / "not-created"
    missing_host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=missing_workspace,
        settings_backend=FakeCredentialBackend(),
    )
    await _initialize(missing_host, client_kind="tui")
    assert any(  # noqa: SLF001
        record.source.startswith("plugin:") for record in missing_host._skill_registry.records
    )
    await missing_host.close()

    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    manager.set_enabled(
        "snapshot-plugin",
        enabled=False,
        scope="workspace",
        workspace=workspace_a,
    )
    host_a = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace_a,
        settings_backend=FakeCredentialBackend(),
    )
    host_b = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=workspace_b,
        settings_backend=FakeCredentialBackend(),
    )
    try:
        await _initialize(host_a, client_kind="tui")
        await _initialize(host_b, client_kind="web")
        assert not any(  # noqa: SLF001
            record.source.startswith("plugin:") for record in host_a._skill_registry.records
        )
        assert any(  # noqa: SLF001
            record.source.startswith("plugin:") for record in host_b._skill_registry.records
        )
    finally:
        await host_a.close()
        await host_b.close()
