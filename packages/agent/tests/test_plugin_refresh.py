"""已安装 Plugin 的 Adapter 重解析、能力重新授权与并发快照测试。"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import harness_agent.plugins.manager as manager_module
from harness_agent.config.config import ConfigError
from harness_agent.host.agent_host import AgentHost, _plugin_reauthorization_error_data
from harness_agent.host.connection import RpcError
from harness_agent.host.run_coordinator import RequestedSkill, StartRun
from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.model import capability_fingerprint


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "qwen_extensions" / "za38-devagent"


def _copy_qwen_fixture(tmp_path: Path, suffix: str = "qwen") -> Path:
    """复制离线 Qwen fixture，测试不读取用户开发期扩展目录。"""
    target = tmp_path / suffix
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def _install_enabled(manager: PluginManager, source: Path) -> str:
    """安装并显式信任 fixture，返回固定 Plugin ID。"""
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    plugin_id = str(installed["id"])
    manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    return plugin_id


def _mark_legacy_hook_report(manager: PluginManager, plugin_id: str) -> str:
    """把当前 fixture 模拟为旧 Adapter 的 invalid Hook 报告。"""
    state = manager.store.read_registry()
    plugin = next(item for item in state.plugins if item.plugin_id == plugin_id)
    components = tuple(
        replace(component, status="invalid", effective=False)
        if component.kind == "hooks"
        else component
        for component in plugin.components
    )
    fingerprint = capability_fingerprint(components)
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(
                item,
                components=components,
                capability_fingerprint=fingerprint,
                trusted_capability_fingerprint=fingerprint,
                adapter_revision=None,
                enabled=True,
            )
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )
    return fingerprint


def test_refresh_reparses_legacy_report_preserves_trust_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """旧报告由当前 Adapter 重建，指纹不变时 trust 保留且重复刷新不写 revision。"""
    manager = PluginManager(home=tmp_path / "home")
    plugin_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path))
    legacy = manager.store.read_registry().plugins[0]
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(item, adapter_revision=None)
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )
    calls = 0
    original_loader = manager_module.load_plugin_descriptor

    def loader(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        descriptor = original_loader(*args, **kwargs)
        return replace(descriptor, description="由当前 Adapter 刷新的描述")

    monkeypatch.setattr(manager_module, "load_plugin_descriptor", loader)
    before_revision = manager.store.read_registry().revision

    first = manager.refresh_catalog()
    refreshed = manager.store.read_registry().plugins[0]

    assert calls == 1
    assert refreshed.description == "由当前 Adapter 刷新的描述"
    assert refreshed.enabled is True
    assert refreshed.trusted_capability_fingerprint == legacy.capability_fingerprint
    assert refreshed.capability_fingerprint == legacy.capability_fingerprint
    assert refreshed.adapter_revision
    assert first.reauthorization_required == ()
    assert first.changed_plugin_ids == (plugin_id,)
    first_revision = refreshed_revision = manager.store.read_registry().revision
    assert first_revision == before_revision + 1

    second = manager.refresh_catalog()

    assert calls == 2
    assert manager.store.read_registry().revision == refreshed_revision
    assert second.changed_plugin_ids == ()
    assert second.reauthorization_required == ()


def test_refresh_changed_capability_preserves_old_trust_until_explicit_reauthorization(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Adapter 增加 process 能力时先原子更新报告并阻断 runtime，确认新指纹后恢复。"""
    manager = PluginManager(home=tmp_path / "home")
    plugin_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path))
    old = manager.store.read_registry().plugins[0]
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(item, adapter_revision=None)
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )
    original_loader = manager_module.load_plugin_descriptor

    def loader(*args: Any, **kwargs: Any) -> Any:
        descriptor = original_loader(*args, **kwargs)
        components = tuple(
            replace(
                component,
                capabilities=(*component.capabilities, "process:hook:v2"),
            )
            if component.kind == "hooks"
            else component
            for component in descriptor.components
        )
        return replace(
            descriptor,
            components=components,
            capability_fingerprint=capability_fingerprint(components),
        )

    monkeypatch.setattr(manager_module, "load_plugin_descriptor", loader)

    refreshed = manager.refresh_catalog()
    current = manager.store.read_registry().plugins[0]

    assert current.enabled is True
    assert current.trusted_capability_fingerprint == old.capability_fingerprint
    assert current.capability_fingerprint != old.capability_fingerprint
    assert current.authorization_state == "reauthorization-required"
    assert refreshed.reauthorization_required == (plugin_id,)
    assert len(refreshed.reauthorization) == 1
    blocked = refreshed.reauthorization[0]
    assert blocked.plugin_id == plugin_id
    assert blocked.authorization_state == "reauthorization-required"
    assert f"plugin/{plugin_id}/command/za38-sdd" in blocked.component_ids
    assert "za38-frontend-executor" in blocked.agent_ids
    assert manager.catalog().plugins == ()

    restored = manager.set_enabled(
        plugin_id,
        enabled=True,
        capability_fingerprint=current.capability_fingerprint,
    )

    assert restored["plugin"]["trusted"] is True  # type: ignore[index]
    assert manager.catalog().plugins[0].plugin_id == plugin_id


@pytest.mark.asyncio
async def test_refresh_catalog_concurrent_managers_commit_one_idempotent_revision(
    tmp_path: Path,
) -> None:
    """两个 Host 同时 refresh 时由 registry lock 串行化，第二次不重复提交。"""
    source = _copy_qwen_fixture(tmp_path)
    manager = PluginManager(home=tmp_path / "home")
    plugin_id = _install_enabled(manager, source)
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(item, adapter_revision=None)
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )
    before = manager.store.read_registry().revision
    first_manager = PluginManager(home=tmp_path / "home")
    second_manager = PluginManager(home=tmp_path / "home")

    results = await asyncio.gather(
        asyncio.to_thread(first_manager.refresh_catalog),
        asyncio.to_thread(second_manager.refresh_catalog),
    )

    assert manager.store.read_registry().revision == before + 1
    assert {result.catalog.plugins[0].capability_fingerprint for result in results} == {
        manager.store.read_registry().plugins[0].capability_fingerprint
    }
    assert all(result.reauthorization_required == () for result in results)


@pytest.mark.asyncio
async def test_host_keeps_unrelated_run_available_while_plugin_requires_reauthorization(
    tmp_path: Path,
) -> None:
    """stale Plugin 仍不进入 runtime，但不能全局阻断无关普通 Run。"""
    home = tmp_path / "home"
    manager = PluginManager(home=home)
    plugin_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path, "host-qwen"))
    _mark_legacy_hook_report(manager, plugin_id)
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=tmp_path / "workspace",
    )
    try:
        command = StartRun(
            thread_id="thread-1",
            run_id="run-1",
            message="执行任务",
            mode="build",
        )
        preparation = await host._prepare_run(command, None)
        assert preparation.skill_registry is not None
        assert host._plugin_reauthorization_required == (plugin_id,)

        current = host._plugin_manager.store.read_registry().plugins[0]
        host._plugin_manager.set_enabled(
            plugin_id,
            enabled=True,
            capability_fingerprint=current.capability_fingerprint,
        )
        preparation = await host._prepare_run(command, None)

        assert preparation.skill_registry is not None
        assert host._plugin_reauthorization_required == ()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_runs_unrelated_echo_with_stale_plugin_before_any_model_call(
    tmp_path: Path,
) -> None:
    """stale Plugin 存在时普通 echo Run 仍完成并发出最终正文。"""
    home = tmp_path / "home"
    manager = PluginManager(home=home)
    plugin_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path, "echo-qwen"))
    _mark_legacy_hook_report(manager, plugin_id)
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=tmp_path / "workspace",
    )
    frames: list[dict[str, object]] = []

    async def capture(message: dict[str, object]) -> None:
        frames.append(message)

    host.send = capture  # type: ignore[method-assign]
    try:
        await host._handle_run_start(
            {
                "mode": "build",
                "message": "普通 stale echo 检查",
                "thread_id": "echo-thread",
                "run_id": "echo-run",
            },
            "echo-request",
        )
        await asyncio.gather(*tuple(host._run_event_tasks))

        assert frames[0]["result"] == {
            "thread_id": "echo-thread",
            "run_id": "echo-run",
            "accepted": True,
        }
        content = [
            frame
            for frame in frames
            if frame.get("method") == "event"
            and frame.get("params", {}).get("type") == "content.delta"  # type: ignore[union-attr]
        ]
        assert content
        assert content[0]["params"]["payload"] == {  # type: ignore[index]
            "text": "普通 stale echo 检查"
        }
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_keeps_other_authorized_plugin_in_executable_catalog(
    tmp_path: Path,
) -> None:
    """一个 stale Plugin 被排除时，另一个已授权 Plugin 仍留在 runtime catalog。"""
    home = tmp_path / "home"
    manager = PluginManager(home=home)
    stale_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path, "stale-qwen"))
    authorized_id = _install_enabled(
        manager,
        _copy_qwen_fixture(tmp_path, "authorized-qwen"),
    )
    _mark_legacy_hook_report(manager, stale_id)
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=tmp_path / "workspace",
    )
    try:
        await host._prepare_run(
            StartRun(
                thread_id="catalog-thread",
                run_id="catalog-run",
                message="普通任务",
                mode="build",
            ),
            None,
        )

        assert host._plugin_catalog_snapshot is not None
        catalog_ids = {
            plugin.plugin_id for plugin in host._plugin_catalog_snapshot.plugins
        }
        assert stale_id not in catalog_ids
        assert authorized_id in catalog_ids
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_blocks_stale_plugin_command_before_model_or_child_dispatch(
    tmp_path: Path,
) -> None:
    """请求 stale Plugin command 时在 Skill/模型/child 前返回可操作门禁。"""
    home = tmp_path / "home"
    manager = PluginManager(home=home)
    plugin_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path, "command-qwen"))
    _mark_legacy_hook_report(manager, plugin_id)
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=tmp_path / "workspace",
    )
    try:
        command = StartRun(
            thread_id="thread-1",
            run_id="run-1",
            message="/za38-sdd 执行任务",
            mode="build",
            requested_skill=RequestedSkill(
                f"plugin/{plugin_id}/command/za38-sdd",
                raw_invocation="/za38-sdd 执行任务",
                command_name="za38-sdd",
            ),
        )
        with pytest.raises(ConfigError) as error:
            await host._prepare_run(command, None)

        message = str(error.value)
        assert "PLUGIN_REAUTHORIZATION_REQUIRED" in message
        assert f"plugin_id={plugin_id}" in message
        assert "authorization_state=reauthorization-required" in message
        current = host._plugin_manager.store.read_registry().plugins[0]
        assert f"capability_fingerprint={current.capability_fingerprint}" in message
        assert "plugins inspect" in message
        assert "plugins enable" in message
        assert str(home) not in message
        summary = host._plugin_reauthorization_index[plugin_id]
        error_data = _plugin_reauthorization_error_data(summary)
        assert error_data["code"] == "PLUGIN_REAUTHORIZATION_REQUIRED"
        assert error_data["details"] == {
            "plugin_id": plugin_id,
            "authorization_state": "reauthorization-required",
            "capability_fingerprint": current.capability_fingerprint,
            "action": {
                "inspect": f"harness plugins inspect {plugin_id}",
                "enable": (
                    f"harness plugins enable {plugin_id} "
                    f"--capability-fingerprint {current.capability_fingerprint}"
                ),
            },
        }
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_run_start_returns_structured_stale_plugin_error_before_acceptance(
    tmp_path: Path,
) -> None:
    """run.start 对 stale command 返回结构化门禁，不登记 Run 或启动 child。"""
    home = tmp_path / "home"
    manager = PluginManager(home=home)
    plugin_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path, "rpc-qwen"))
    _mark_legacy_hook_report(manager, plugin_id)
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=tmp_path / "workspace",
    )
    try:
        with pytest.raises(RpcError) as error:
            await host._handle_run_start(
                {
                    "mode": "build",
                    "message": "/za38-sdd 执行任务",
                    "thread_id": "rpc-thread",
                    "run_id": "rpc-run",
                    "requested_skill": {
                        "id": f"plugin/{plugin_id}/command/za38-sdd",
                        "raw_invocation": "/za38-sdd 执行任务",
                        "command_name": "za38-sdd",
                    },
                },
                "rpc-request",
            )

        current = host._plugin_manager.store.read_registry().plugins[0]
        assert error.value.code == -32004
        assert error.value.message == "PLUGIN_REAUTHORIZATION_REQUIRED"
        assert error.value.data == {
            "code": "PLUGIN_REAUTHORIZATION_REQUIRED",
            "retryable": False,
            "details": {
                "plugin_id": plugin_id,
                "authorization_state": "reauthorization-required",
                "capability_fingerprint": current.capability_fingerprint,
                "action": {
                    "inspect": f"harness plugins inspect {plugin_id}",
                    "enable": (
                        f"harness plugins enable {plugin_id} "
                        f"--capability-fingerprint {current.capability_fingerprint}"
                    ),
                },
            },
        }
        assert host._run_coordinator._runs == {}
        assert host._run_coordinator._starting_runs == {}
        assert str(home) not in repr(error.value.data)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_explicit_stale_plugin_settings_binding_returns_reauthorization(
    tmp_path: Path,
) -> None:
    """Settings 已带 plugin_id provenance 时返回同一脱敏 reauthorization 门禁。"""
    home = tmp_path / "home"
    manager = PluginManager(home=home)
    plugin_id = _install_enabled(manager, _copy_qwen_fixture(tmp_path, "settings-qwen"))
    _mark_legacy_hook_report(manager, plugin_id)
    host = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=tmp_path / "workspace",
    )
    frames: list[dict[str, object]] = []

    async def capture(message: dict[str, object]) -> None:
        frames.append(message)

    host.send = capture  # type: ignore[method-assign]
    try:
        await host.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": "settings-init",
                "params": {
                    "protocol": {"major": 3, "min_minor": 0, "max_minor": 7},
                    "client": {"name": "offline-test", "version": "1", "kind": "test"},
                    "capabilities": {"requests": ["settings.manage"], "handles": []},
                },
            }
        )
        current = host._plugin_manager.store.read_registry().plugins[0]
        await host.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "settings.set",
                "id": "settings-stale",
                "params": {
                    "scope": "user",
                    "plugin_id": plugin_id,
                    "package_digest": "a" * 64,
                    "declaration_digest": "b" * 64,
                    "setting_key": "ZA38_TOKEN",
                    "env_var": "ZA38_TOKEN",
                    "value": "offline-placeholder",
                    "expected_store_revision": 0,
                },
            }
        )

        error = next(frame for frame in frames if frame.get("id") == "settings-stale")
        assert error["error"] == {
            "code": -32004,
            "message": "PLUGIN_REAUTHORIZATION_REQUIRED",
            "data": {
                "code": "PLUGIN_REAUTHORIZATION_REQUIRED",
                "retryable": False,
                "details": {
                    "plugin_id": plugin_id,
                    "authorization_state": "reauthorization-required",
                    "capability_fingerprint": current.capability_fingerprint,
                    "action": {
                        "inspect": f"harness plugins inspect {plugin_id}",
                        "enable": (
                            f"harness plugins enable {plugin_id} "
                            f"--capability-fingerprint {current.capability_fingerprint}"
                        ),
                    },
                },
            },
        }
        assert str(home) not in repr(error)
    finally:
        await host.close()
