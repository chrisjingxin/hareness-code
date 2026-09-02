"""HC-166 Plugin Adapter 重解析与 activation 保留测试。"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import harness_agent.plugins.manager as manager_module
from harness_agent.plugins.manager import PluginManager


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "qwen_extensions" / "za38-devagent"


def _copy_qwen_fixture(tmp_path: Path, suffix: str = "qwen") -> Path:
    """复制离线 Qwen fixture，测试不读取用户开发期扩展目录。"""
    target = tmp_path / suffix
    shutil.copytree(FIXTURE_ROOT, target)
    return target


def _install(manager: PluginManager, source: Path) -> str:
    """安装已由 Adapter 自动识别的 fixture，返回内部 ID 供快照断言。"""
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    assert installed["activation"] == "enabled"
    return manager.store.read_registry().plugins[0].plugin_id


def test_refresh_reparses_legacy_report_preserves_activation_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """旧 Adapter report 由当前 Adapter 重建，不产生重新授权状态。"""
    manager = PluginManager(home=tmp_path / "home")
    plugin_id = _install(manager, _copy_qwen_fixture(tmp_path))
    manager.store.mutate_registry(
        lambda current: tuple(
            replace(item, adapter_revision=None)
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )
    original_loader = manager_module.load_plugin_descriptor
    calls = 0

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
    assert refreshed.activation_user == "enabled"
    assert refreshed.activation_workspaces == ()
    assert first.changed_plugin_ids == (plugin_id,)
    assert manager.store.read_registry().revision == before_revision + 1

    second = manager.refresh_catalog()

    assert calls == 2
    assert manager.store.read_registry().revision == before_revision + 1
    assert second.changed_plugin_ids == ()


def test_refresh_changed_component_report_does_not_require_reauthorization(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Adapter 增加内部能力描述只改变下一代报告，不阻断已启用 Plugin。"""
    manager = PluginManager(home=tmp_path / "home")
    plugin_id = _install(manager, _copy_qwen_fixture(tmp_path))
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
            replace(component, capabilities=(*component.capabilities, "adapter:v2"))
            if component.kind == "hooks"
            else component
            for component in descriptor.components
        )
        return replace(descriptor, components=components)

    monkeypatch.setattr(manager_module, "load_plugin_descriptor", loader)

    refreshed = manager.refresh_catalog()
    current = manager.store.read_registry().plugins[0]

    assert refreshed.changed_plugin_ids == (plugin_id,)
    assert current.activation_user == "enabled"
    assert manager.catalog().plugins[0].plugin_id == plugin_id
    assert not hasattr(current, "authorization_state")


@pytest.mark.parametrize("manager_count", (2,))
def test_concurrent_refresh_commits_one_idempotent_registry_revision(
    tmp_path: Path,
    manager_count: int,
) -> None:
    """多个管理进程的刷新由 registry lock 串行化，结果保持同一 activation。"""
    source = _copy_qwen_fixture(tmp_path)
    first = PluginManager(home=tmp_path / "home")
    plugin_id = _install(first, source)
    first.store.mutate_registry(
        lambda current: tuple(
            replace(item, adapter_revision=None)
            if item.plugin_id == plugin_id
            else item
            for item in current.plugins
        )
    )
    before = first.store.read_registry().revision
    managers = [PluginManager(home=tmp_path / "home") for _ in range(manager_count)]

    results = [manager.refresh_catalog() for manager in managers]

    final = first.store.read_registry()
    assert final.revision == before + 1
    assert final.plugins[0].activation_user == "enabled"
    assert all(result.catalog.plugins[0].plugin_id == plugin_id for result in results)
