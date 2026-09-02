"""HC-166 第一轮管理内核的行为边界测试（先行失败测试）。"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.model import (
    InstalledPlugin,
    PluginComponentReport,
    PluginError,
    product_status,
)
from harness_agent.config.settings import (
    FakeCredentialBackend,
    QwenSettingDeclaration,
    SettingBinding,
    SettingsStore,
)


def _seed_v2_registry(tmp_path: Path, *, name: str = "review-tools") -> tuple[object, bytes, Path]:
    """写入带真实离线 package 的 v2 registry，返回 Store、原文和 package 路径。"""
    from harness_agent.plugins.adapters import load_plugin_descriptor
    from harness_agent.plugins.store import PluginStore

    source = tmp_path / f"{name}-source"
    _write_portable_plugin(source, name=name)
    store = PluginStore(home=tmp_path / "home")
    with store.stage(source) as item:
        descriptor = load_plugin_descriptor(
            item.root,
            package_digest=item.package_digest,
            name_hint=item.name_hint,
        )
        store.install_package(item, plugin_name=descriptor.name)
        package_path = store.store_root / item.source_id / descriptor.name / item.package_digest
        legacy = {
            "version": 2,
            "revision": 7,
            "plugins": [
                {
                    "id": f"{item.source_id}/{descriptor.name}",
                    "source_id": item.source_id,
                    "source_label": item.source_label,
                    "name": descriptor.name,
                    "version": descriptor.version,
                    "description": descriptor.description,
                    "format": descriptor.format,
                    "manifest": descriptor.manifest,
                    "package_digest": descriptor.package_digest,
                    "capability_fingerprint": "a" * 64,
                    "components": [component.to_dict() for component in descriptor.components],
                    "diagnostics": list(descriptor.diagnostics),
                    "enabled": True,
                    "trusted_capability_fingerprint": "a" * 64,
                    "installed_at_ms": 1,
                    "adapter_revision": "legacy-adapter",
                }
            ],
        }
    original = json.dumps(legacy, ensure_ascii=False).encode("utf-8")
    store.registry_path.parent.mkdir(parents=True, exist_ok=True)
    store.registry_path.write_bytes(original)
    return store, original, package_path


def _assert_v2_original(store: object, original: bytes) -> None:
    """断言迁移失败仍保留可重试的 v2 registry 原文。"""
    registry_path = store.registry_path  # type: ignore[attr-defined]
    assert registry_path.read_bytes() == original
    assert json.loads(original)["version"] == 2


def _write_portable_plugin(root: Path, *, name: str = "review-tools") -> None:
    """建立不执行外部组件的最小离线 Agent Plugins fixture。"""
    (root / "skills" / "review").mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": name,
                "version": "1.0.0",
                "description": "offline fixture",
            }
        ),
        encoding="utf-8",
    )
    (root / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review safely\n---\n\nOffline fixture.\n",
        encoding="utf-8",
    )


def _write_format_fixture(root: Path, *, plugin_format: str, name: str) -> None:
    """建立四种支持格式共用的离线 Skill fixture，不执行任何外部组件。"""
    (root / "skills" / "review").mkdir(parents=True)
    skill = "---\nname: review\ndescription: Review safely\n---\n\nOffline fixture.\n"
    (root / "skills" / "review" / "SKILL.md").write_text(skill, encoding="utf-8")
    manifest = {"name": name, "version": "1.0.0", "description": "offline fixture"}
    if plugin_format in {"portable", "hybrid"}:
        manifest["$schema"] = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if plugin_format in {"qwen", "hybrid"}:
        manifest_name = "devagent-extension.json" if plugin_format == "qwen" else ".claude-plugin/plugin.json"
        manifest_path = root / manifest_name
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({key: value for key, value in manifest.items() if key != "$schema"}), encoding="utf-8")
    if plugin_format == "claude":
        manifest_path = root / ".claude-plugin" / "plugin.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_install_enables_selected_scope_and_workspace_overrides_user(tmp_path: Path) -> None:
    """安装一次即写 activation；workspace 安装不得影响其他工作区。"""
    source = tmp_path / "source"
    _write_portable_plugin(source)
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    manager = PluginManager(home=tmp_path / "home")
    installed = manager.install(source, scope="workspace", workspace=workspace_a)

    assert installed["plugin"]["name"] == "review-tools"
    assert installed["plugin"]["status"] == "loaded"
    assert manager.list(scope="workspace", workspace=workspace_a)["plugins"][0]["status"] == "loaded"
    assert manager.list(scope="workspace", workspace=workspace_b)["plugins"][0]["status"] == "disabled"


def test_consent_preview_is_bound_to_the_same_package_digest(tmp_path: Path) -> None:
    """consent 后来源若已改变，不能把未预览的 package 写入 store。"""
    source = tmp_path / "source"
    _write_portable_plugin(source)
    manager = PluginManager(home=tmp_path / "home")

    _preview, preview_digest = manager._preview_install_with_identity(source)  # noqa: SLF001
    (source / "plugin.json").write_text(
        (source / "plugin.json").read_text(encoding="utf-8").replace(
            '"version": "1.0.0"', '"version": "2.0.0"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(PluginError) as error:
        manager.install(source, expected_package_digest=preview_digest)

    assert error.value.code == "PLUGIN_OPERATION_CONFLICT"
    assert manager.store.registry_path.exists() is False


def test_plugin_mutations_resolve_case_insensitive_name_and_reject_duplicate(tmp_path: Path) -> None:
    """日常 mutation 使用 manifest name，不接受第二份同名 artifact。"""
    source = tmp_path / "source"
    _write_portable_plugin(source, name="review-tools")
    duplicate = tmp_path / "duplicate"
    _write_portable_plugin(duplicate, name="review-tools")
    manager = PluginManager(home=tmp_path / "home")

    manager.install(source)
    try:
        manager.install(duplicate)
    except Exception as exc:  # noqa: BLE001 - 断言领域错误码而非实现异常类型。
        assert getattr(exc, "code", None) == "PLUGIN_ALREADY_INSTALLED"
    else:
        raise AssertionError("duplicate plugin name must be rejected")

    disabled = manager.set_enabled("review-tools", enabled=False)
    assert disabled["plugin"]["status"] == "disabled"
    inspected = manager.inspect("REVIEW-TOOLS")
    assert inspected["plugin"]["name"] == "review-tools"


def test_management_scope_defaults_to_user_even_when_manager_has_workspace(tmp_path: Path) -> None:
    """省略 scope 的查询必须看 user，不得因 Host workspace 自动切换。"""
    source = tmp_path / "source"
    _write_portable_plugin(source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = PluginManager(home=tmp_path / "home", workspace=workspace)

    manager.install(source, scope="user")
    manager.set_enabled("review-tools", enabled=False, workspace=workspace)

    listed = manager.list(workspace=workspace)
    inspected = manager.inspect("review-tools", workspace=workspace)
    assert listed["scope"] == "user"
    assert listed["plugins"][0]["status"] == "disabled"
    assert inspected["scope"] == "user"
    assert inspected["plugin"]["status"] == "disabled"
    manager.set_enabled("review-tools", enabled=True, scope="workspace", workspace=workspace)
    assert manager.list(workspace=workspace)["plugins"][0]["status"] == "disabled"
    assert manager.list(scope="workspace", workspace=workspace)["plugins"][0]["status"] == "loaded"


def test_user_management_scope_works_with_missing_workspace_but_explicit_workspace_fails_closed(
    tmp_path: Path,
) -> None:
    """user scope 不依赖 workspace 存在；显式 workspace scope 必须 fail closed。"""
    source = tmp_path / "source"
    _write_portable_plugin(source)
    missing_workspace = tmp_path / "not-created"
    manager = PluginManager(home=tmp_path / "home", workspace=missing_workspace)
    manager.install(source, scope="user")

    assert manager.list()["scope"] == "user"
    assert manager.list()["plugins"][0]["status"] == "loaded"
    assert manager.inspect("review-tools")["scope"] == "user"
    with pytest.raises(PluginError) as list_error:
        manager.list(scope="workspace", workspace=missing_workspace)
    assert list_error.value.code == "PLUGIN_SCOPE_INVALID"
    with pytest.raises(PluginError) as inspect_error:
        manager.inspect("review-tools", scope="workspace", workspace=missing_workspace)
    assert inspect_error.value.code == "PLUGIN_SCOPE_INVALID"


def test_registry_v2_migrates_once_without_plugin_fingerprints(tmp_path: Path) -> None:
    """v2 migration drops Plugin trust fields and leaves a durable backup."""
    source = tmp_path / "source"
    _write_portable_plugin(source)
    manager = PluginManager(home=tmp_path / "home")
    store = manager.store
    with store.stage(source) as item:
        descriptor = __import__("harness_agent.plugins.adapters", fromlist=["load_plugin_descriptor"]).load_plugin_descriptor(
            item.root,
            package_digest=item.package_digest,
            name_hint=item.name_hint,
        )
        store.install_package(item, plugin_name=descriptor.name)
        legacy = {
            "version": 2,
            "revision": 4,
            "plugins": [
                {
                    "id": f"{item.source_id}/{descriptor.name}",
                    "source_id": item.source_id,
                    "source_label": item.source_label,
                    "name": descriptor.name,
                    "version": descriptor.version,
                    "description": descriptor.description,
                    "format": descriptor.format,
                    "manifest": descriptor.manifest,
                    "package_digest": descriptor.package_digest,
                    "capability_fingerprint": "a" * 64,
                    "components": [],
                    "diagnostics": ["stale adapter report"],
                    "enabled": True,
                    "trusted_capability_fingerprint": "a" * 64,
                    "installed_at_ms": 1,
                    "adapter_revision": "legacy-adapter",
                }
            ],
        }
    store.root.mkdir(parents=True, exist_ok=True)
    store.registry_path.parent.mkdir(parents=True, exist_ok=True)
    store.registry_path.write_text(json.dumps(legacy), encoding="utf-8")

    state = store.read_registry()
    assert state.plugins[0].activation_user == "enabled"
    assert state.plugins[0].components == descriptor.components
    assert state.plugins[0].diagnostics == descriptor.diagnostics
    assert state.plugins[0].adapter_revision == descriptor.adapter_revision
    record = store.registry_path.read_text(encoding="utf-8")
    assert '"version": 3' in record
    assert "capability_fingerprint" not in record
    assert (store.registry_path.parent / "registry.v2.backup.json").exists()


def test_v2_name_conflict_keeps_original_registry_and_backup(tmp_path: Path) -> None:
    """大小写冲突迁移必须 fail closed，不能把 v2 原文替换成半成品。"""
    source = tmp_path / "source"
    _write_portable_plugin(source, name="review-tools")
    manager = PluginManager(home=tmp_path / "home")
    store = manager.store
    with store.stage(source) as item:
        descriptor = __import__(
            "harness_agent.plugins.adapters",
            fromlist=["load_plugin_descriptor"],
        ).load_plugin_descriptor(
            item.root,
            package_digest=item.package_digest,
            name_hint=item.name_hint,
        )
        store.install_package(item, plugin_name=descriptor.name)
        duplicate_package = (
            store.store_root
            / "legacy-second"
            / "REVIEW-tools"
            / item.package_digest
        )
        duplicate_package.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            store.store_root / item.source_id / descriptor.name / item.package_digest,
            duplicate_package,
        )
        legacy_record = {
            "id": f"{item.source_id}/{descriptor.name}",
            "source_id": item.source_id,
            "source_label": item.source_label,
            "name": descriptor.name,
            "version": descriptor.version,
            "description": descriptor.description,
            "format": descriptor.format,
            "manifest": descriptor.manifest,
            "package_digest": descriptor.package_digest,
            "capability_fingerprint": "a" * 64,
            "components": [component.to_dict() for component in descriptor.components],
            "diagnostics": list(descriptor.diagnostics),
            "enabled": True,
            "trusted_capability_fingerprint": "a" * 64,
            "installed_at_ms": 1,
            "adapter_revision": descriptor.adapter_revision,
        }
    legacy = {
        "version": 2,
        "revision": 4,
        "plugins": [
            legacy_record,
            {
                **legacy_record,
                "id": "legacy-two",
                "source_id": "legacy-second",
                "name": "REVIEW-tools",
            },
        ],
    }
    store.root.mkdir(parents=True, exist_ok=True)
    store.registry_path.write_text(json.dumps(legacy), encoding="utf-8")
    original = store.registry_path.read_bytes()

    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_NAME_CONFLICT"
    assert store.registry_path.read_bytes() == original
    assert (store.registry_path.parent / "registry.v2.backup.json").read_bytes() == original


def test_v2_migration_backup_creation_failure_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """backup 创建失败时不得触碰 v2 registry。"""
    import harness_agent.plugins.store as store_module

    store, original, _package_path = _seed_v2_registry(tmp_path)

    def fail_backup_temp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise OSError("injected backup failure")

    monkeypatch.setattr(store_module.tempfile, "mkstemp", fail_backup_temp)
    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_MIGRATION_BACKUP_FAILED"
    _assert_v2_original(store, original)
    assert not (store.root / "registry.v2.backup.json").exists()


def test_v2_migration_temp_write_failure_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """v3 临时文件创建失败时，v2 原文件仍可重试。"""
    import harness_agent.plugins.store as store_module

    store, original, _package_path = _seed_v2_registry(tmp_path)
    real_backup = store._write_v2_backup_atomic

    def backup_then_fail_temp(_content: bytes) -> None:
        real_backup(_content)

        def fail_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
            raise OSError("injected temp write failure")

        monkeypatch.setattr(store_module.tempfile, "mkstemp", fail_mkstemp)

    monkeypatch.setattr(store, "_write_v2_backup_atomic", backup_then_fail_temp)
    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_WRITE_FAILED"
    _assert_v2_original(store, original)
    assert (store.root / "registry.v2.backup.json").read_bytes() == original


def test_v2_migration_temp_fsync_failure_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """v3 临时文件 fsync 失败发生在 replace 前，v2 原文件不变。"""
    import harness_agent.plugins.store as store_module

    store, original, _package_path = _seed_v2_registry(tmp_path)
    real_backup = store._write_v2_backup_atomic

    def backup_then_fail_fsync(_content: bytes) -> None:
        real_backup(_content)

        def fail_fsync(_fd: int) -> None:
            raise OSError("injected temp fsync failure")

        monkeypatch.setattr(store_module.os, "fsync", fail_fsync)

    monkeypatch.setattr(store, "_write_v2_backup_atomic", backup_then_fail_fsync)
    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_WRITE_FAILED"
    _assert_v2_original(store, original)
    assert (store.root / "registry.v2.backup.json").read_bytes() == original


def test_v2_migration_replace_failure_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """registry replace 尚未发生时失败，v2 原文保持不变。"""
    import harness_agent.plugins.store as store_module

    store, original, _package_path = _seed_v2_registry(tmp_path)
    real_backup = store._write_v2_backup_atomic
    real_replace = store_module.os.replace

    def backup_then_fail_replace(_content: bytes) -> None:
        real_backup(_content)

        def fail_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
            if Path(destination) == store.registry_path:
                raise OSError("injected registry replace failure")
            real_replace(source, destination)

        monkeypatch.setattr(store_module.os, "replace", fail_replace)

    monkeypatch.setattr(store, "_write_v2_backup_atomic", backup_then_fail_replace)
    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_WRITE_FAILED"
    _assert_v2_original(store, original)
    assert (store.root / "registry.v2.backup.json").read_bytes() == original


def test_v2_migration_replace_then_error_is_commit_uncertain_and_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """replace 已执行但抛错时不能误报 replace 前失败，且尽力恢复 v2。"""
    import harness_agent.plugins.store as store_module

    store, original, _package_path = _seed_v2_registry(tmp_path)
    real_backup = store._write_v2_backup_atomic
    real_replace = store_module.os.replace

    def backup_then_replace_then_fail(_content: bytes) -> None:
        real_backup(_content)

        def replace_then_fail(source: str | bytes | Path, destination: str | bytes | Path) -> None:
            if Path(destination) == store.registry_path:
                real_replace(source, destination)
                raise OSError("injected post-replace error")
            real_replace(source, destination)

        monkeypatch.setattr(store_module.os, "replace", replace_then_fail)

    monkeypatch.setattr(store, "_write_v2_backup_atomic", backup_then_replace_then_fail)
    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_COMMIT_UNCERTAIN"
    _assert_v2_original(store, original)
    assert (store.root / "registry.v2.backup.json").read_bytes() == original


def test_v2_migration_directory_fsync_is_commit_uncertain_and_restores_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """replace 后目录 fsync 失败必须报告 commit-uncertain，而非伪称普通写失败。"""
    import harness_agent.plugins.store as store_module

    store, original, _package_path = _seed_v2_registry(tmp_path)
    real_backup = store._write_v2_backup_atomic
    real_fsync_directory = store_module._fsync_directory
    calls = 0

    def backup_then_fail_directory_fsync(_content: bytes) -> None:
        real_backup(_content)

        def fail_second_fsync(directory: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected directory fsync failure")
            real_fsync_directory(directory)

        monkeypatch.setattr(store_module, "_fsync_directory", fail_second_fsync)

    monkeypatch.setattr(store, "_write_v2_backup_atomic", backup_then_fail_directory_fsync)
    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_COMMIT_UNCERTAIN"
    # 当前实现必须尽力恢复可重试的 v2；durability 仍由 commit-uncertain 码诚实标记。
    _assert_v2_original(store, original)
    assert (store.root / "registry.v2.backup.json").read_bytes() == original


def test_v2_migration_unknown_replace_state_is_commit_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """replace 抛错且路径出现非旧非目标 bytes 时必须 fail closed。"""
    import harness_agent.plugins.store as store_module

    store, original, _package_path = _seed_v2_registry(tmp_path)
    real_backup = store._write_v2_backup_atomic
    real_replace = store_module.os.replace
    replace_attempted = False

    def backup_then_leave_ambiguous_state(_content: bytes) -> None:
        real_backup(_content)

        def replace_with_ambiguous_state(
            source: str | bytes | Path,
            destination: str | bytes | Path,
        ) -> None:
            nonlocal replace_attempted
            if Path(destination) == store.registry_path and not replace_attempted:
                replace_attempted = True
                real_replace(source, destination)
                store.registry_path.write_bytes(b"ambiguous registry state")
                raise OSError("injected ambiguous registry replace failure")
            real_replace(source, destination)

        monkeypatch.setattr(store_module.os, "replace", replace_with_ambiguous_state)

    monkeypatch.setattr(store, "_write_v2_backup_atomic", backup_then_leave_ambiguous_state)
    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_COMMIT_UNCERTAIN"
    _assert_v2_original(store, original)
    assert (store.root / "registry.v2.backup.json").read_bytes() == original


@pytest.mark.parametrize("plugin_format", ("qwen", "claude", "portable", "hybrid"))
def test_offline_formats_install_enabled_and_name_mutation_by_name(
    tmp_path: Path,
    plugin_format: str,
) -> None:
    """Qwen/Claude/portable/Hybrid 都完成同一条 install→enabled→name mutation 闭环。"""
    source = tmp_path / plugin_format
    name = f"{plugin_format}-offline"
    _write_format_fixture(source, plugin_format=plugin_format, name=name)
    manager = PluginManager(home=tmp_path / "home")

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    assert installed["activation"] == "enabled"
    assert installed["status"] == "loaded"

    disabled = manager.set_enabled(name.upper(), enabled=False)
    assert disabled["plugin"]["name"] == name
    assert disabled["plugin"]["status"] == "disabled"
    enabled = manager.set_enabled(name, enabled=True)
    assert enabled["plugin"]["name"] == name
    assert enabled["plugin"]["status"] == "loaded"


def test_v2_migration_truncated_registry_fails_before_backup(tmp_path: Path) -> None:
    """截断 v2 在严格 JSON 解码阶段失败，不创建 backup。"""
    from harness_agent.plugins.store import PluginStore

    store = PluginStore(home=tmp_path / "home")
    original = b'{"version":2,"revision":1,"plugins":['
    store.registry_path.parent.mkdir(parents=True, exist_ok=True)
    store.registry_path.write_bytes(original)

    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_REGISTRY_INVALID"
    assert store.registry_path.read_bytes() == original
    assert not (store.root / "registry.v2.backup.json").exists()


def test_v2_migration_missing_package_fails_before_backup(tmp_path: Path) -> None:
    """package 缺失在备份前被发现，避免留下与不可运行 artifact 对应的 v3。"""
    store, original, package_path = _seed_v2_registry(tmp_path)
    missing_path = package_path.with_name(f"{package_path.name}.missing")
    package_path.chmod(0o755)
    package_path.rename(missing_path)

    with pytest.raises(PluginError) as error:
        store.read_registry()

    assert error.value.code == "PLUGIN_STORE_MISSING"
    _assert_v2_original(store, original)
    assert not (store.root / "registry.v2.backup.json").exists()


def test_v2_migration_is_idempotent_and_does_not_rewrite_v3(tmp_path: Path) -> None:
    """第一次迁移后重复读取只读 v3，不再次改变 revision 或 bytes。"""
    store, _original, _package_path = _seed_v2_registry(tmp_path)

    migrated = store.read_registry()
    migrated_bytes = store.registry_path.read_bytes()
    repeated = store.read_registry()

    assert migrated.revision == 7
    assert repeated == migrated
    assert store.registry_path.read_bytes() == migrated_bytes


def test_v2_migration_accepts_matching_existing_backup_and_rejects_different_backup(
    tmp_path: Path,
) -> None:
    """已有相同 backup 可幂等继续；不同 backup 必须拒绝覆盖并保留 v2。"""
    store, original, _package_path = _seed_v2_registry(tmp_path)
    backup = store.root / "registry.v2.backup.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(original)
    assert store.read_registry().revision == 7

    second_store, second_original, _ = _seed_v2_registry(tmp_path / "different")
    different_backup = second_store.root / "registry.v2.backup.json"
    different_backup.parent.mkdir(parents=True, exist_ok=True)
    different_backup.write_bytes(b"different-v2")
    with pytest.raises(PluginError) as error:
        second_store.read_registry()
    assert error.value.code == "PLUGIN_REGISTRY_MIGRATION_BACKUP_CONFLICT"
    _assert_v2_original(second_store, second_original)


def test_update_preserves_user_and_workspace_activation(tmp_path: Path) -> None:
    """update 替换 artifact 时保留 user 默认值和 workspace override。"""
    source_v1 = tmp_path / "source-v1"
    _write_portable_plugin(source_v1, name="review-tools")
    source_v2 = tmp_path / "source-v2"
    _write_portable_plugin(source_v2, name="review-tools")
    (source_v2 / "plugin.json").write_text(
        (source_v2 / "plugin.json").read_text(encoding="utf-8").replace(
            '"version": "1.0.0"', '"version": "2.0.0"'
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()

    manager = PluginManager(home=tmp_path / "home")
    manager.install(source_v1, scope="user")
    manager.set_enabled("review-tools", enabled=False, scope="user")
    manager.set_enabled("review-tools", enabled=True, scope="workspace", workspace=workspace)

    updated = manager.update("REVIEW-TOOLS", source=source_v2)

    assert updated["plugin"]["name"] == "review-tools"
    assert updated["plugin"]["version"] == "2.0.0"
    assert manager.list(scope="workspace", workspace=workspace)["plugins"][0]["status"] == "loaded"
    assert manager.list(scope="workspace", workspace=other_workspace)["plugins"][0]["status"] == "disabled"


def test_product_status_has_only_four_user_states() -> None:
    """状态投影只允许 loaded/disabled/warning/failed 四种产品状态。"""
    component = PluginComponentReport(
        kind="skills",
        status="supported",
        count=1,
        effective=True,
    )
    base = InstalledPlugin(
        plugin_id="fixture/plugin",
        source_id="fixture",
        source_label="plugin",
        name="plugin",
        version="1.0.0",
        description=None,
        format="agent-plugins-1.0",
        manifest="plugin.json",
        package_digest="d" * 64,
        components=(component,),
        diagnostics=(),
        activation_user="enabled",
        activation_workspaces=(),
        installed_at_ms=0,
    )

    assert product_status(base, activation="enabled") == "loaded"
    assert product_status(base, activation="disabled") == "disabled"
    assert product_status(
        replace(base, diagnostics=("PLUGIN_COMPONENT_UNSUPPORTED: mcp",)),
        activation="enabled",
    ) == "warning"
    assert product_status(
        replace(
            base,
            components=(
                PluginComponentReport(
                    kind="skills",
                    status="unsupported",
                    count=0,
                    effective=False,
                ),
            ),
        ),
        activation="enabled",
    ) == "failed"


def _setting_binding(
    *,
    plugin_id: str,
    package_digest: str,
    env_var: str = "REVIEW_API_KEY",
) -> SettingBinding:
    """构造不含真实凭据的 Qwen setting binding。"""
    declaration = QwenSettingDeclaration(
        name="Review API key",
        description="Offline test setting",
        env_var=env_var,
        sensitive=True,
    )
    return SettingBinding(
        plugin_id=plugin_id,
        package_digest=package_digest,
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )


def test_settings_rebind_keeps_value_when_name_and_env_are_unchanged(
    tmp_path: Path,
) -> None:
    """更新 package digest 不应让相同声明的已有 secret 失效。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    old_binding = _setting_binding(plugin_id="fixture/plugin", package_digest="a" * 64)
    new_binding = _setting_binding(plugin_id="fixture/plugin", package_digest="b" * 64)
    store.set(
        scope="user",
        plugin_id=old_binding.plugin_id,
        package_digest=old_binding.package_digest,
        declaration_digest=old_binding.declaration_digest,
        setting_key=old_binding.setting_key,
        env_var=old_binding.env_var,
        value="offline-secret",
        expected_store_revision=0,
        name=old_binding.declaration.name,
        description=old_binding.declaration.description,
        sensitive=True,
    )

    assert store.rebind_plugin_setting(old_binding=old_binding, new_binding=new_binding) == ()
    snapshot = store.resolve(bindings=(new_binding,))
    assert snapshot.value_for(new_binding.setting_id) == "offline-secret"
    assert old_binding.setting_id not in snapshot._values  # noqa: SLF001 - only verifies no old identity leak.


def test_settings_rebind_warns_and_keeps_old_record_when_env_changes(
    tmp_path: Path,
) -> None:
    """声明 envVar 改变时不猜测迁移旧值，只返回可操作 warning。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    old_binding = _setting_binding(plugin_id="fixture/plugin", package_digest="a" * 64)
    changed_binding = _setting_binding(
        plugin_id="fixture/plugin",
        package_digest="b" * 64,
        env_var="REVIEW_TOKEN",
    )
    store.set(
        scope="user",
        plugin_id=old_binding.plugin_id,
        package_digest=old_binding.package_digest,
        declaration_digest=old_binding.declaration_digest,
        setting_key=old_binding.setting_key,
        env_var=old_binding.env_var,
        value="offline-secret",
        expected_store_revision=0,
        name=old_binding.declaration.name,
        description=old_binding.declaration.description,
        sensitive=True,
    )

    assert store.rebind_plugin_setting(
        old_binding=old_binding,
        new_binding=changed_binding,
    ) == ("PLUGIN_SETTING_RECONFIGURE_REQUIRED",)
    old_snapshot = store.resolve(bindings=(old_binding,))
    assert old_snapshot.value_for(old_binding.setting_id) == "offline-secret"
