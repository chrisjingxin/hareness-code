"""HC-158 Phase 4B Settings canonical model 与脱敏持久化回归。"""

from __future__ import annotations

import io
import json
import secrets
import ctypes
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import harness_agent.config.settings as settings_module

from harness_agent.config.settings import (
    MAX_SETTING_VALUE_BYTES,
    FakeCredentialBackend,
    LinuxSecretServiceCredentialBackend,
    MacOSCredentialBackend,
    SimulatedSettingsCrash,
    SettingsError,
    SettingsResolver,
    SettingsStore,
    SettingBinding,
    QwenSettingDeclaration,
    create_platform_credential_backend,
    parse_qwen_setting,
    read_secret_stdin,
    validate_setting_value,
)
from harness_agent.plugins.manager import PluginManager


PACKAGE_DIGEST = "a" * 64


def _setting_args() -> dict[str, object]:
    """构造不含真实秘密的 Qwen setting identity。"""
    declaration = parse_qwen_setting(
        {"name": "ZA38 token", "description": "offline", "envVar": "ZA38_TOKEN", "sensitive": True}
    )
    return {
        "plugin_id": "plugin/local/za38",
        "package_digest": PACKAGE_DIGEST,
        "declaration_digest": declaration.declaration_digest,
        "setting_key": declaration.setting_key,
        "env_var": declaration.env_var,
        "name": declaration.name,
        "description": declaration.description,
        "sensitive": declaration.sensitive,
    }


def test_qwen_setting_uses_exact_env_var_and_display_only_digest() -> None:
    """Qwen name/description 只用于展示，envVar 才是稳定 setting identity。"""
    first = parse_qwen_setting(
        {
            "name": "ZA38 API key",
            "description": "first display",
            "envVar": "ZA38_API_KEY",
            "sensitive": True,
        }
    )
    renamed = parse_qwen_setting(
        {
            "name": "Renamed display",
            "description": "second display",
            "envVar": "ZA38_API_KEY",
            "sensitive": True,
        }
    )

    assert first.setting_key == "ZA38_API_KEY"
    assert first.required is False
    assert first.declaration_digest == renamed.declaration_digest
    assert first.name != renamed.name

    for invalid in (
        {"name": "x", "description": "x"},
        {"name": "x", "description": "x", "envVar": ""},
        {"name": "x", "description": "x", "envVar": "bad-name"},
    ):
        with pytest.raises(SettingsError) as error:
            parse_qwen_setting(invalid)
        assert error.value.code == "SETTINGS_DECLARATION_INVALID"

    with pytest.raises(SettingsError) as error:
        QwenSettingDeclaration(
            "Token",
            "offline",
            "DEMO_TOKEN",
            required=True,  # type: ignore[arg-type]
        )
    assert error.value.code == "SETTINGS_DECLARATION_INVALID"


def test_setting_value_validator_has_one_shared_bound_and_no_nul() -> None:
    """Host 与 stdin 使用同一 validator：边界可接受，超限/NUL 稳定拒绝。"""
    assert validate_setting_value("x" * MAX_SETTING_VALUE_BYTES) == "x" * MAX_SETTING_VALUE_BYTES
    with pytest.raises(SettingsError, match="SETTINGS_VALUE_TOO_LARGE"):
        validate_setting_value("x" * (MAX_SETTING_VALUE_BYTES + 1))
    with pytest.raises(SettingsError, match="SETTINGS_VALUE_INVALID"):
        validate_setting_value("bad\x00value")


def test_stdin_validator_allows_one_trailing_newline_at_value_bound() -> None:
    """stdin 的 framing newline 不应把合法的最大值误判为超限。"""
    stream = io.BytesIO(("x" * MAX_SETTING_VALUE_BYTES + "\n").encode())
    assert read_secret_stdin(stream) == "x" * MAX_SETTING_VALUE_BYTES


def test_stdin_validator_accepts_crlf_but_rejects_a_second_record() -> None:
    """stdin 只允许一个 framing newline，不能截断后把多余输入当成成功。"""
    stream = io.BytesIO(("x" * MAX_SETTING_VALUE_BYTES + "\r\n").encode())
    assert read_secret_stdin(stream) == "x" * MAX_SETTING_VALUE_BYTES

    extra = io.BytesIO(("x" * MAX_SETTING_VALUE_BYTES + "\nnext").encode())
    with pytest.raises(SettingsError) as error:
        read_secret_stdin(extra)
    assert error.value.code == "SETTINGS_VALUE_INVALID"


def test_platform_factory_is_lazy_and_selects_concrete_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产工厂只按平台选择 backend，不在构造时读取凭据。"""
    monkeypatch.setattr("harness_agent.config.settings.sys.platform", "darwin")
    assert isinstance(create_platform_credential_backend(), MacOSCredentialBackend)
    monkeypatch.setattr("harness_agent.config.settings.sys.platform", "linux")
    assert isinstance(create_platform_credential_backend(), LinuxSecretServiceCredentialBackend)


def test_settings_store_is_not_available_without_an_explicit_backend(tmp_path) -> None:
    """未通过 capability probe 时不降级明文存储，也不创建 metadata。"""
    store = SettingsStore(home=tmp_path / "home")
    assert store.list(scope="user")["settings"] == []
    assert not (tmp_path / "home" / ".harness" / "settings").exists()


def test_workspace_binding_keeps_symlink_identity_in_scope_digest(tmp_path) -> None:
    """同一 realpath 的直接目录与 symlink 目录不能复用 workspace secret。"""
    home = tmp_path / "home"
    real_workspace = tmp_path / "real-workspace"
    symlink_workspace = tmp_path / "linked-workspace"
    real_workspace.mkdir()
    symlink_workspace.symlink_to(real_workspace, target_is_directory=True)

    direct = SettingsStore(
        home=home,
        workspace=real_workspace,
        backend=FakeCredentialBackend(),
    )
    linked = SettingsStore(
        home=home,
        workspace=symlink_workspace,
        backend=FakeCredentialBackend(),
    )

    assert direct.workspace_binding_digest != linked.workspace_binding_digest


def test_settings_mutation_rejects_symlinked_scope_directory(tmp_path) -> None:
    """Settings lock 不能跟随 scope 目录 symlink 写入受保护根之外。"""
    home = tmp_path / "home"
    scope_parent = home / ".harness" / "settings" / "v1"
    scope_parent.mkdir(parents=True)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (scope_parent / "user").symlink_to(redirected, target_is_directory=True)

    with pytest.raises(SettingsError) as error:
        SettingsStore(home=home, backend=FakeCredentialBackend()).set(
            scope="user",
            value="runtime-fake-value",
            expected_store_revision=0,
            **_setting_args(),
        )

    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"
    assert not (redirected / "index.lock").exists()


def test_settings_rejects_world_readable_intermediate_metadata_directory(tmp_path) -> None:
    """.harness/settings 任一中间目录权限过宽时 list 也 fail closed。"""
    home = tmp_path / "home"
    metadata_root = home / ".harness" / "settings" / "v1"
    metadata_root.mkdir(parents=True, mode=0o700)
    (home / ".harness").chmod(0o755)
    (home / ".harness" / "settings").chmod(0o755)

    with pytest.raises(SettingsError) as error:
        SettingsStore(home=home, backend=FakeCredentialBackend()).list(scope="user")

    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"


def test_settings_list_keeps_declaration_summary_when_backend_is_unavailable(tmp_path) -> None:
    """管理面保留 declaration，并把无法验证的 store 标为 blocked。"""
    declaration = parse_qwen_setting(
        {"name": "Token", "description": "offline", "envVar": "DEMO_TOKEN"}
    )
    binding = SettingBinding(
        plugin_id="plugin/local/demo",
        package_digest=PACKAGE_DIGEST,
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )
    result = SettingsStore(
        home=tmp_path / "home",
        backend=FakeCredentialBackend(available=False),
    ).list(scope="user", declarations=(binding,))

    assert result["store_revision"] == 0
    assert result["settings"][0]["setting_id"] == binding.setting_id
    assert result["settings"][0]["store_state"] == "blocked"
    assert result["settings"][0]["diagnostic"] == "SETTINGS_BACKEND_UNAVAILABLE"


def test_settings_list_reports_backend_failure_with_stable_backend_diagnostic(tmp_path) -> None:
    """已有 durable record 在 backend 失效时也必须报告具体 blocked 原因。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    store.set(scope="user", value="runtime-fake-value", expected_store_revision=0, **_setting_args())
    backend.available = False

    result = store.list(scope="user")

    assert result["settings"][0]["store_state"] == "blocked"
    assert result["settings"][0]["diagnostic"] == "SETTINGS_BACKEND_UNAVAILABLE"


def test_fake_backend_is_explicitly_injectable_for_offline_phase4b() -> None:
    """离线测试只通过显式 fake backend 保存值，不把值写进 metadata。"""
    backend = FakeCredentialBackend()
    backend.set("account-1", "fake-secret")
    assert backend.get("account-1") == "fake-secret"
    assert backend.accounts == ("account-1",)


def test_set_uses_bootstrap_revision_and_metadata_never_contains_secret(tmp_path) -> None:
    """首次 set 必须 expected=0，值只到 fake backend，list 只返回脱敏状态。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    assert store.list(scope="user")["store_revision"] == 0
    assert not (tmp_path / "home" / ".harness" / "settings").exists()

    result = store.set(scope="user", value="offline-secret", expected_store_revision=0, **_setting_args())

    assert result["operation"] == "set"
    assert "applies_to" not in result
    listed = store.list(scope="user")
    assert listed["store_revision"] > 0
    assert listed["settings"][0]["store_state"] == "configured"
    assert "offline-secret" not in str(result)
    assert "offline-secret" not in str(listed)
    metadata = (tmp_path / "home" / ".harness" / "settings" / "v1" / "user" / "index.json").read_text()
    assert "offline-secret" not in metadata


def test_set_rejects_stale_store_revision_without_overwrite(tmp_path) -> None:
    """CAS 冲突不能覆盖现有 credential。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    store.set(scope="user", value="first", expected_store_revision=0, **_setting_args())
    with pytest.raises(SettingsError) as error:
        store.set(scope="user", value="second", expected_store_revision=0, **_setting_args())
    assert error.value.code == "SETTINGS_STORE_REVISION_CONFLICT"
    assert all("second" not in str(item) for item in backend.operations)


def test_metadata_record_identity_tampering_fails_closed(tmp_path) -> None:
    """record 的 opaque identity 必须由 plugin/package/envVar 精确派生。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    store.set(scope="user", value="first", expected_store_revision=0, **_setting_args())
    path = tmp_path / "home" / ".harness" / "settings" / "v1" / "user" / "index.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["records"][0]["setting_id"] = "setting-" + "f" * 32
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        store.list(scope="user")
    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"


def test_metadata_record_generation_tampering_fails_closed(tmp_path) -> None:
    """record generation 必须是可重放 account 的固定 opaque 形状。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    store.set(scope="user", value="first", expected_store_revision=0, **_setting_args())
    path = tmp_path / "home" / ".harness" / "settings" / "v1" / "user" / "index.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["records"][0]["generation"] = "not-a-generation"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SettingsError) as error:
        store.list(scope="user")
    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"


def test_metadata_record_display_shape_tampering_fails_closed(tmp_path) -> None:
    """durable record 的展示字段仍需满足声明的有界文本形状。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    store.set(scope="user", value="first", expected_store_revision=0, **_setting_args())
    path = tmp_path / "home" / ".harness" / "settings" / "v1" / "user" / "index.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["records"][0]["name"] = " "
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SettingsError) as error:
        store.list(scope="user")
    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"


def test_metadata_index_rejects_duplicate_or_ambiguous_entries(tmp_path) -> None:
    """重复 record 不能让 list/resolver 选择依赖文件顺序。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    store.set(scope="user", value="first", expected_store_revision=0, **_setting_args())
    path = tmp_path / "home" / ".harness" / "settings" / "v1" / "user" / "index.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["records"].append(document["records"][0])
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SettingsError) as error:
        store.list(scope="user")
    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"


def test_set_rejects_declaration_digest_that_does_not_match_exact_qwen_identity(tmp_path) -> None:
    """set 不能仅验证 digest 形状，必须验证它确实属于 envVar+sensitive 声明。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    args = _setting_args()
    args["declaration_digest"] = "b" * 64

    with pytest.raises(SettingsError) as error:
        store.set(scope="user", value="not-authorized", expected_store_revision=0, **args)

    assert error.value.code == "SETTINGS_DECLARATION_STALE"
    assert not (tmp_path / "home" / ".harness" / "settings").exists()
    assert backend.accounts == ()


def test_set_rejects_non_string_display_fields(tmp_path) -> None:
    """管理写入不能把错误类型的展示字段强制转换后落盘。"""
    backend = FakeCredentialBackend()
    store = SettingsStore(home=tmp_path / "home", backend=backend)
    args = _setting_args()
    args["name"] = None

    with pytest.raises(SettingsError) as error:
        store.set(scope="user", value="not-authorized", expected_store_revision=0, **args)

    assert error.value.code == "SETTINGS_DECLARATION_INVALID"
    assert not (tmp_path / "home" / ".harness" / "settings").exists()


def test_journal_closed_union_rejects_fields_from_another_operation(tmp_path) -> None:
    """set journal 不能混入 remove/tombstone 字段而被恢复器默默接受。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"

    def crash_after_credential(point: str) -> None:
        if point == "set.after_credential":
            raise SimulatedSettingsCrash(point)

    crashing = SettingsStore(
        home=home,
        backend=backend,
        failure_injector=crash_after_credential,
    )
    with pytest.raises(SimulatedSettingsCrash):
        crashing.set(scope="user", value="journal-value", expected_store_revision=0, **_setting_args())

    index_path = home / ".harness" / "settings" / "v1" / "user" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    journal_path = home / ".harness" / "settings" / "v1" / "user" / index["journal_refs"][0]["file"]
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["tombstone"] = {}
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(SettingsError) as error:
        SettingsStore(home=home, backend=backend).recover(scope="user")
    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"


def test_recovery_removes_safe_atomic_journal_temp_orphan(tmp_path) -> None:
    """实际 atomic writer 产生的 journal temp orphan 可安全移除，不误报损坏。"""
    home = tmp_path / "home"
    store = SettingsStore(home=home, backend=FakeCredentialBackend())
    journal_dir = home / ".harness" / "settings" / "v1" / "user" / "journal"
    journal_dir.mkdir(parents=True)
    journal_dir.chmod(0o700)
    orphan = journal_dir / ("." + "a" * 32 + ".json.abcdefgh.tmp")
    orphan.write_text("{}", encoding="utf-8")
    orphan.chmod(0o600)

    store.recover(scope="user")

    assert not orphan.exists()


def test_qwen_settings_are_adapted_and_invalid_entries_do_not_authorize_component(tmp_path) -> None:
    """Qwen exact ExtensionSetting 进入 canonical binding，私有字段/坏 envVar fail closed。"""
    source = tmp_path / "extension"
    source.mkdir()
    (source / "qwen-extension.json").write_text(
        '{"name":"settings-demo","settings":[{"name":"Token","description":"token","envVar":"DEMO_TOKEN"}]}'
    )
    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    component = next(item for item in summary["components"] if item["kind"] == "settings")
    assert set(component) == {"kind", "count", "sources"}
    assert component["count"] == 1
    assert component["sources"] == ["DEMO_TOKEN"]

    (source / "qwen-extension.json").write_text(
        '{"name":"settings-demo","settings":[{"name":"Token","description":"token","envVar":"DEMO_TOKEN"},{"name":"Bad","description":"bad","envVar":"PATH"}]}'
    )
    summary = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert not any(item["kind"] == "settings" for item in summary["components"])
    assert any("SETTINGS_ENV_FORBIDDEN" in item for item in summary["warnings"])


def test_set_and_remove_recover_from_each_durable_crash_window(tmp_path) -> None:
    """set/remove 的 journal phase 决定唯一回滚或前滚，不能留下 orphan account。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    store = SettingsStore(home=home, backend=backend)

    def crash_at(target: str):
        def injector(point: str) -> None:
            if point == target:
                raise SimulatedSettingsCrash(target)

        return injector

    crashing_set = SettingsStore(
        home=home,
        backend=backend,
        failure_injector=crash_at("set.after_credential"),
    )
    with pytest.raises(SimulatedSettingsCrash):
        crashing_set.set(scope="user", value="rollback-me", expected_store_revision=0, **_setting_args())
    recovering = SettingsStore(home=home, backend=backend)
    recovering.recover(scope="user")
    assert backend.accounts == ()
    revision = recovering.list(scope="user")["store_revision"]

    store.set(scope="user", value="keep-me", expected_store_revision=revision, **_setting_args())
    crashing_remove = SettingsStore(
        home=home,
        backend=backend,
        failure_injector=crash_at("remove.after_tombstone"),
    )
    with pytest.raises(SimulatedSettingsCrash):
        crashing_remove.remove(
            scope="user",
            expected_store_revision=store.list(scope="user")["store_revision"],
            **_setting_args(),
        )
    backend.fail_on.clear()
    recovering.recover(scope="user")
    assert backend.accounts == ()
    index = recovering._read_index("user", recovering.user_binding_digest)  # noqa: SLF001 - recovery assertion
    assert index is not None
    assert index.tombstones
    assert not index.records
    args = _setting_args()
    declaration = parse_qwen_setting(
        {"name": args["name"], "description": args["description"], "envVar": args["env_var"], "sensitive": args["sensitive"]}
    )
    binding = SettingBinding(
        plugin_id=str(args["plugin_id"]),
        package_digest=str(args["package_digest"]),
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )
    listed_after_remove = recovering.list(scope="user", declarations=(binding,))
    assert listed_after_remove["settings"][0]["store_state"] == "tombstoned"


def test_set_recovery_cleanup_failure_stays_rollback_pending(tmp_path) -> None:
    """set 回滚清理失败时不能把 cleanup_pending 误解释成提交新 record。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"

    def crash_after_credential(point: str) -> None:
        if point == "set.after_credential":
            raise SimulatedSettingsCrash(point)

    with pytest.raises(SimulatedSettingsCrash):
        SettingsStore(
            home=home,
            backend=backend,
            failure_injector=crash_after_credential,
        ).set(scope="user", value="rollback-pending", expected_store_revision=0, **_setting_args())

    backend.fail_on.add("delete")
    with pytest.raises(SettingsError) as error:
        SettingsStore(home=home, backend=backend).recover(scope="user")
    assert error.value.code == "SETTINGS_CLEANUP_PENDING"
    assert backend.accounts
    index = SettingsStore(home=home, backend=backend)._read_index(  # noqa: SLF001
        "user",
        SettingsStore(home=home, backend=backend).user_binding_digest,
    )
    assert index is not None and not index.records

    backend.fail_on.clear()
    SettingsStore(home=home, backend=backend).recover(scope="user")
    assert backend.accounts == ()


def test_set_recovery_after_metadata_commit_forwards_new_generation_and_replaces_old_record(tmp_path) -> None:
    """metadata 已提交但 phase 尚未推进时按 new generation 前滚，且移除旧 package record。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    store = SettingsStore(home=home, backend=backend)
    store.set(scope="user", value="old", expected_store_revision=0, **_setting_args())
    old_revision = store.list(scope="user")["store_revision"]
    new_args = _setting_args()
    new_args["package_digest"] = "b" * 64

    def crash_after_metadata(point: str) -> None:
        if point == "set.after_metadata":
            raise SimulatedSettingsCrash(point)

    with pytest.raises(SimulatedSettingsCrash):
        SettingsStore(
            home=home,
            backend=backend,
            failure_injector=crash_after_metadata,
        ).set(
            scope="user",
            value="new",
            expected_store_revision=old_revision,
            **new_args,
        )

    recovering = SettingsStore(home=home, backend=backend)
    recovering.recover(scope="user")
    index = recovering._read_index("user", recovering.user_binding_digest)  # noqa: SLF001
    assert index is not None
    assert [record.package_digest for record in index.records] == ["b" * 64]
    assert len(backend.accounts) == 1
    listed = recovering.list(scope="user")
    assert listed["settings"][0]["store_state"] == "configured"


def test_remove_cleanup_failure_keeps_tombstone_and_retries_exact_account(tmp_path) -> None:
    """remove 清理失败时保留 tombstone/journal，恢复只删除原 account。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    store = SettingsStore(home=home, backend=backend)
    store.set(
        scope="user",
        value=secrets.token_urlsafe(18),
        expected_store_revision=0,
        **_setting_args(),
    )
    backend.fail_on.add("delete")

    with pytest.raises(SettingsError) as error:
        store.remove(
            scope="user",
            expected_store_revision=store.list(scope="user")["store_revision"],
            **_setting_args(),
        )
    assert error.value.code == "SETTINGS_CLEANUP_PENDING"
    assert backend.accounts

    backend.fail_on.clear()
    recovering = SettingsStore(home=home, backend=backend)
    recovering.recover(scope="user")
    index = recovering._read_index(  # noqa: SLF001 - verify durable tombstone
        "user",
        recovering.user_binding_digest,
    )
    assert index is not None
    assert not index.records
    assert index.tombstones
    assert backend.accounts == ()


def test_workspace_precedes_user_and_non_runtime_consumers_receive_no_env(tmp_path) -> None:
    """resolver 只把 workspace > user 的值交给 MCP/Hook/LSP，其他 kind 恒为空。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_store = SettingsStore(home=home, backend=backend)
    workspace_store = SettingsStore(home=home, workspace=workspace, backend=backend)
    args = _setting_args()
    user_store.set(scope="user", value="user-value", expected_store_revision=0, **args)
    declaration = parse_qwen_setting(
        {"name": "ZA38 token", "description": "offline", "envVar": "ZA38_TOKEN", "sensitive": True}
    )
    setting_binding = SettingBinding(
        plugin_id=str(args["plugin_id"]),
        package_digest=str(args["package_digest"]),
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )
    resolver = SettingsResolver(user=user_store, workspace=workspace_store)
    snapshot = resolver.resolve((setting_binding,))
    assert snapshot.value_for(setting_binding.setting_id) == "user-value"
    snapshot.release()

    workspace_store.set(scope="workspace", value="workspace-value", expected_store_revision=0, **args)
    snapshot = resolver.resolve((setting_binding,))
    assert snapshot.value_for(setting_binding.setting_id) == "workspace-value"
    snapshot.release()
    assert workspace_store.environment_for(component_kind="commands", bindings=(setting_binding,)) == {}
    assert workspace_store.environment_for(component_kind="skills", bindings=(setting_binding,)) == {}
    assert workspace_store.environment_for(component_kind="agents", bindings=(setting_binding,)) == {}
    assert workspace_store.environment_for(component_kind="mcp", bindings=(setting_binding,))["ZA38_TOKEN"] == "workspace-value"
    assert workspace_store.environment_for(
        component_kind="mcp",
        bindings=(setting_binding,),
        plugin_id="plugin/other",
    ) == {}
    assert workspace_store.environment_for(component_kind="monitors", bindings=(setting_binding,)) == {}


def test_workspace_record_without_credential_does_not_fall_back_to_user(tmp_path) -> None:
    """workspace 记录已存在但 credential 缺失时不能借 user 值越过显式覆盖。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args = _setting_args()
    user_store = SettingsStore(home=home, backend=backend)
    workspace_store = SettingsStore(home=home, workspace=workspace, backend=backend)
    user_store.set(scope="user", value="user-value", expected_store_revision=0, **args)
    workspace_store.set(scope="workspace", value="workspace-value", expected_store_revision=0, **args)

    workspace_index = workspace_store._read_index(  # noqa: SLF001 - 构造 backend 缺失窗口
        "workspace",
        workspace_store.workspace_binding_digest,
    )
    assert workspace_index is not None
    backend.delete(workspace_store._account_for(workspace_index.records[0]))  # noqa: SLF001

    declaration = parse_qwen_setting(
        {"name": args["name"], "description": args["description"], "envVar": args["env_var"], "sensitive": args["sensitive"]}
    )
    binding = SettingBinding(
        plugin_id=str(args["plugin_id"]),
        package_digest=str(args["package_digest"]),
        declaration_digest=declaration.declaration_digest,
        declaration=declaration,
    )
    snapshot = SettingsResolver(user=user_store, workspace=workspace_store).resolve((binding,))
    assert snapshot.value_for(binding.setting_id) is None
    assert snapshot.blocked_plugin_ids == frozenset({binding.plugin_id})
    assert snapshot.diagnostics == (
        f"plugin:{binding.plugin_id}: SETTINGS_RECORD_STALE",
    )
    snapshot.release()


def test_resolver_blocks_only_plugin_with_active_but_missing_credential(tmp_path) -> None:
    """active record 的 credential 缺失只阻断对应 Plugin，不能污染其他值。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    user_store = SettingsStore(home=home, backend=backend)
    good_args = _setting_args()
    good_args["plugin_id"] = "plugin/local/good"
    bad_args = _setting_args()
    bad_args["plugin_id"] = "plugin/local/bad"

    user_store.set(scope="user", value="good-value", expected_store_revision=0, **good_args)
    revision = int(user_store.list(scope="user")["store_revision"])
    user_store.set(scope="user", value="bad-value", expected_store_revision=revision, **bad_args)
    index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert index is not None
    bad_record = next(item for item in index.records if item.plugin_id == "plugin/local/bad")
    backend.delete(user_store._account_for(bad_record))  # noqa: SLF001

    bindings = tuple(
        SettingBinding(
            plugin_id=str(args["plugin_id"]),
            package_digest=str(args["package_digest"]),
            declaration_digest=str(args["declaration_digest"]),
            declaration=parse_qwen_setting(
                {
                    "name": args["name"],
                    "description": args["description"],
                    "envVar": args["env_var"],
                    "sensitive": args["sensitive"],
                }
            ),
        )
        for args in (good_args, bad_args)
    )

    snapshot = SettingsResolver(user=user_store).resolve(bindings)
    assert snapshot.value_for(bindings[0].setting_id) == "good-value"
    assert snapshot.value_for(bindings[1].setting_id) is None
    assert snapshot.blocked_plugin_ids == frozenset({"plugin/local/bad"})
    assert snapshot.diagnostics == ("plugin:plugin/local/bad: SETTINGS_RECORD_STALE",)
    snapshot.release()


def test_scope_binding_includes_home_identity_roots_and_policy_without_bootstrap_drift(tmp_path) -> None:
    """user home 替换、trusted roots 或 policy 变化必须使 binding stale；首次 bootstrap 不自漂移。"""
    home = tmp_path / "home"
    home.mkdir()
    first = SettingsStore(home=home, backend=FakeCredentialBackend(), policy_version="policy-v1")
    first_digest = first.user_binding_digest
    moved = tmp_path / "moved-home"
    home.rename(moved)
    home.mkdir()
    replaced = SettingsStore(home=home, backend=FakeCredentialBackend(), policy_version="policy-v1")
    assert replaced.user_binding_digest != first_digest

    absent = tmp_path / "absent-home"
    before = SettingsStore(home=absent, backend=FakeCredentialBackend()).user_binding_digest
    absent.mkdir(exist_ok=True)
    after = SettingsStore(home=absent, backend=FakeCredentialBackend()).user_binding_digest
    assert after == before

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "trusted-root"
    root.mkdir()
    base = SettingsStore(
        home=home,
        workspace=workspace,
        workspace_roots=(root,),
        policy_version="policy-v1",
        backend=FakeCredentialBackend(),
    )
    changed_root = SettingsStore(
        home=home,
        workspace=workspace,
        workspace_roots=(),
        policy_version="policy-v1",
        backend=FakeCredentialBackend(),
    )
    changed_policy = SettingsStore(
        home=home,
        workspace=workspace,
        workspace_roots=(root,),
        policy_version="policy-v2",
        backend=FakeCredentialBackend(),
    )
    assert base.workspace_binding_digest != changed_root.workspace_binding_digest
    assert base.workspace_binding_digest != changed_policy.workspace_binding_digest


def test_macos_backend_fake_security_api_distinguishes_not_found_and_failure(monkeypatch) -> None:
    """Security.framework seam 只接受 exact status，probe/delete 不泄露 fake value。"""
    backend = MacOSCredentialBackend()
    values: dict[str, str] = {}

    def query(account: str, *, include_value: bool, value: str | None = None):
        return (account, value), []

    def copy_matching(query_value):
        account, _value = query_value
        if account not in values:
            return backend._NOT_FOUND, None  # noqa: SLF001
        return 0, values[account]

    def add(query_value):
        account, value = query_value
        values[account] = str(value)
        return 0

    def delete(query_value):
        account, _value = query_value
        values.pop(account, None)
        return 0

    backend._binding = {  # noqa: SLF001
        "query": query,
        "copy_matching": copy_matching,
        "add": add,
        "delete": delete,
        "release_many": lambda _items: None,
    }
    monkeypatch.setattr(settings_module.sys, "platform", "darwin")
    assert backend.capability_probe() is True
    assert backend.get("missing-account") is None
    backend.set("opaque-account", "fake-secret")
    assert backend.get("opaque-account") == "fake-secret"
    backend.delete("opaque-account")
    assert backend.get("opaque-account") is None

    backend._binding["copy_matching"] = lambda _query: (-1, None)  # noqa: SLF001
    with pytest.raises(SettingsError) as error:
        backend.get("opaque-account")
    assert error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"
    assert "fake-secret" not in str(error.value)

    backend._binding["query"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: SLF001
        RuntimeError("fake Security.framework failure")
    )
    with pytest.raises(SettingsError) as error:
        backend.get("opaque-account")
    assert error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"
    assert "fake Security.framework failure" not in str(error.value)


def test_linux_backend_fake_secretstorage_requires_exact_match_and_closes_bus(monkeypatch) -> None:
    """Secret Service 只接受一条 exact match，locked/unavailable 与多条结果均 fail closed。"""
    backend = LinuxSecretServiceCredentialBackend()

    class Item:
        def __init__(self, secret: bytes = b"fake-secret") -> None:
            self.secret = secret
            self.deleted = False

        def get_secret(self) -> bytes:
            return self.secret

        def delete(self) -> None:
            self.deleted = True

    class Collection:
        def __init__(self, items: list[Item]) -> None:
            self.items = items
            self.created: list[tuple[dict[str, str], bytes]] = []

        def search_items(self, _attributes):
            return iter(self.items)

        def create_item(self, _label, attributes, secret, replace):
            self.created.append((attributes, secret))

    collection = Collection([Item()])

    @contextmanager
    def fake_collection():
        yield collection

    monkeypatch.setattr(backend, "_collection", fake_collection)
    monkeypatch.setattr(settings_module.sys, "platform", "linux")
    assert backend.get("opaque") == "fake-secret"
    backend.set("opaque", "new-fake")
    backend.delete("opaque")
    assert collection.items[0].deleted is True
    assert collection.created[-1][1] == b"new-fake"

    collection.items = [Item(), Item()]
    with pytest.raises(SettingsError) as error:
        backend.get("opaque")
    assert error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"
    with pytest.raises(SettingsError) as error:
        backend.delete("opaque")
    assert error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"

    class ProbeItem(Item):
        def __init__(self, owner: "ProbeCollection", account: str, secret: bytes) -> None:
            super().__init__(secret)
            self.owner = owner
            self.account = account

        def delete(self) -> None:
            self.owner.items.pop(self.account, None)

    class ProbeCollection:
        def __init__(self) -> None:
            self.items: dict[str, ProbeItem] = {}

        def search_items(self, attributes):
            return iter(
                [self.items[attributes["account"]]]
                if attributes["account"] in self.items
                else []
            )

        def create_item(self, _label, attributes, secret, replace):
            self.items[attributes["account"]] = ProbeItem(
                self,
                attributes["account"],
                secret,
            )

    probe_collection = ProbeCollection()

    @contextmanager
    def probe_context():
        yield probe_collection

    probe_backend = LinuxSecretServiceCredentialBackend()
    monkeypatch.setattr(probe_backend, "_collection", probe_context)
    assert probe_backend.capability_probe() is True


def test_linux_backend_capability_probe_uses_locked_and_unavailable_session_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secret Service session 的 locked/D-Bus failure 都归一化且关闭 bus。"""
    class Bus:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    locked_bus = Bus()
    locked_collection = SimpleNamespace(is_locked=lambda: True)
    locked_module = SimpleNamespace(
        dbus_init=lambda: locked_bus,
        get_default_collection=lambda _bus: locked_collection,
    )
    monkeypatch.setitem(sys.modules, "secretstorage", locked_module)
    monkeypatch.setattr(settings_module.sys, "platform", "linux")
    backend = LinuxSecretServiceCredentialBackend()
    with pytest.raises(SettingsError) as locked_error:
        backend.get("opaque-account")
    assert locked_error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"
    assert locked_bus.closed is True

    unavailable_bus = Bus()

    def unavailable_init():
        raise RuntimeError("fake D-Bus unavailable")

    unavailable_module = SimpleNamespace(
        dbus_init=lambda: unavailable_bus,
        get_default_collection=lambda _bus: unavailable_init(),
    )
    monkeypatch.setitem(sys.modules, "secretstorage", unavailable_module)
    with pytest.raises(SettingsError) as unavailable_error:
        LinuxSecretServiceCredentialBackend().get("opaque-account")
    assert unavailable_error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"
    assert "fake D-Bus unavailable" not in str(unavailable_error.value)


def test_windows_backend_fake_capability_probe_requires_acl_and_cred_round_trip(monkeypatch) -> None:
    """Windows capability probe 必须同时证明 ACL 与 Cred* round-trip。"""
    backend = settings_module.WindowsCredentialBackend()
    values: dict[str, str] = {}
    monkeypatch.setattr(settings_module.os, "name", "nt")
    monkeypatch.setattr(backend, "_load_api", lambda: None)
    monkeypatch.setattr(backend, "_probe_metadata_acl", lambda: True)
    monkeypatch.setattr(backend, "set", lambda account, value: values.__setitem__(account, value))
    monkeypatch.setattr(backend, "get", lambda account: values.get(account))
    monkeypatch.setattr(backend, "delete", lambda account: values.pop(account, None))
    assert backend.capability_probe() is True
    assert values == {}


def test_windows_backend_fake_credread_only_not_found_is_absent() -> None:
    """CredReadW 的 access denied/backend failure 不能伪装成 optional 缺值。"""
    backend = settings_module.WindowsCredentialBackend()
    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]
    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32),
            ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
            ("LastWritten", FileTime), ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_uint32),
            ("AttributeCount", ctypes.c_uint32), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
        ]
    last_error = 1168
    encoded = ctypes.create_string_buffer(b"fake-secret")
    credential = Credential()
    credential.CredentialBlob = ctypes.cast(encoded, ctypes.POINTER(ctypes.c_ubyte))
    credential.CredentialBlobSize = len(b"fake-secret")
    pointer_address = ctypes.addressof(credential)

    def read(_account, pointer):
        pointer._obj.value = pointer_address
        return False

    api = {
        "read": read,
        "free": lambda _pointer: None,
        "credential_type": Credential,
        "get_last_error": lambda: last_error,
    }
    backend._api = api  # noqa: SLF001
    assert backend.get("opaque") is None
    last_error = 5
    with pytest.raises(SettingsError) as error:
        backend.get("opaque")
    assert error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"
    assert "opaque" not in str(error.value)

    api["read"] = lambda _account, _pointer: (_ for _ in ()).throw(
        RuntimeError("fake CredRead failure")
    )
    with pytest.raises(SettingsError) as error:
        backend.get("opaque")
    assert error.value.code == "SETTINGS_BACKEND_UNAVAILABLE"
    assert "fake CredRead failure" not in str(error.value)


def test_windows_named_mutex_fake_api_provides_exclusive_scope_lock(tmp_path) -> None:
    """Windows lock seam 使用命名 mutex，不能在 fcntl 缺失时静默无锁。"""
    class FakeMutex:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.handle = object()

        def create(self, name: str):
            self.calls.append(("create", name))
            return self.handle

        def wait(self, handle: object) -> int:
            self.calls.append(("wait", handle))
            return 0

        def release(self, handle: object) -> None:
            self.calls.append(("release", handle))

        def close(self, handle: object) -> None:
            self.calls.append(("close", handle))

    api = FakeMutex()
    with settings_module._windows_named_mutex_lock(tmp_path / "settings.lock", api=api):  # noqa: SLF001
        assert any(kind == "wait" for kind, _value in api.calls)
    assert [kind for kind, _value in api.calls][-2:] == ["release", "close"]


def test_windows_mutex_api_adapts_create_and_wait_win32_arity(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产 Win32 binding 必须传 CreateMutexW/WaitForSingleObject 的完整参数。"""
    class Function:
        def __init__(self, name: str, result: object = 0) -> None:
            self.name = name
            self.result = result
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args: object) -> object:
            self.calls.append(args)
            return self.result

    class Kernel:
        CreateMutexW = Function("create", result=object())
        WaitForSingleObject = Function("wait", result=0)
        ReleaseMutex = Function("release", result=True)
        CloseHandle = Function("close", result=True)

    import ctypes as ctypes_module

    kernel = Kernel()
    monkeypatch.setattr(ctypes_module, "WinDLL", lambda _name: kernel, raising=False)
    api = settings_module._windows_mutex_api()  # noqa: SLF001
    handle = api["create"]("Local\\za38-settings-test")
    api["wait"](handle)

    assert kernel.CreateMutexW.calls == [(None, False, "Local\\za38-settings-test")]
    assert kernel.WaitForSingleObject.calls == [(handle, 0xFFFFFFFF)]


def test_windows_file_lock_proves_acl_and_uses_mutex_when_fcntl_is_absent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows metadata 每次 lock 都先做 ACL probe，不能退回无锁写入。"""
    calls: list[str] = []

    @contextmanager
    def fake_mutex(path):
        calls.append(f"mutex:{path.name}")
        yield

    monkeypatch.setattr(settings_module.os, "name", "nt")
    monkeypatch.setattr(settings_module, "fcntl", None)
    monkeypatch.setattr(
        settings_module.WindowsCredentialBackend,
        "_probe_metadata_acl",
        lambda _backend, target=None: calls.append(
            "acl" if target is not None else "probe"
        ) or True,
    )
    monkeypatch.setattr(settings_module, "_windows_named_mutex_lock", fake_mutex)

    with settings_module._file_lock(tmp_path / "scope" / "index.lock", root=tmp_path):  # noqa: SLF001
        calls.append("body")

    assert calls == ["acl", "mutex:index.lock", "body"]


def test_windows_metadata_acl_probe_rejects_unapproved_allow_principal(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 ACL probe seam 只接受当前 SID 或 LocalSystem 的 allow ACE。"""
    root = tmp_path / "metadata"
    root.mkdir()
    target = root / "index.json"
    target.write_bytes(b"{}\n")
    monkeypatch.setattr(settings_module.os, "name", "nt")

    class Ace(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", ctypes.c_uint16),
            ("Mask", ctypes.c_uint32),
            ("SidStart", ctypes.c_uint32),
        ]

    def fake_api(*, approved: bool) -> dict[str, object]:
        ace = Ace(AceType=0)
        ace_address = ctypes.addressof(ace) + Ace.SidStart.offset

        def get_security_info(_path, _object_type, _security_info, owner, _group, dacl, _sacl, descriptor):
            owner._obj.value = 505
            dacl._obj.value = 202
            descriptor._obj.value = 303
            return 0

        def get_token(_process, _access, token):
            token._obj.value = 404
            return True

        def get_token_info(_token, _info_class, buffer, _size, size):
            size._obj.value = ctypes.sizeof(ctypes.c_void_p)
            if buffer is None:
                return False
            ctypes.memmove(buffer, ctypes.byref(ctypes.c_void_p(505)), ctypes.sizeof(ctypes.c_void_p))
            return True

        def equal_sid(left, right):
            left_value = getattr(left, "value", None)
            right_value = getattr(right, "value", None)
            if left_value == 505 and right_value == 505:
                return True
            if left_value == ace_address and approved and right_value in {505, 606}:
                return True
            return False

        def get_acl_info(_dacl, info, _size, _class):
            ctypes.cast(info, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1
            return True

        def get_ace(_dacl, _index, ace_pointer):
            ace_pointer._obj.value = ctypes.addressof(ace)
            return True

        def convert_sid(_sid, sid):
            sid._obj.value = 606
            return True

        return {
            "get_named_security_info": get_security_info,
            "get_token": get_token,
            "get_token_info": get_token_info,
            "equal_sid": equal_sid,
            "get_acl_info": get_acl_info,
            "get_ace": get_ace,
            "convert_sid": convert_sid,
            "close_handle": lambda _handle: True,
            "local_free": lambda _pointer: None,
            "get_current_process": lambda: 1,
        }

    approved = settings_module.WindowsCredentialBackend(metadata_root=root)
    approved._api = fake_api(approved=True)  # noqa: SLF001
    assert approved._probe_metadata_acl(target=target) is True  # noqa: SLF001

    unapproved = settings_module.WindowsCredentialBackend(metadata_root=root)
    unapproved._api = fake_api(approved=False)  # noqa: SLF001
    assert unapproved._probe_metadata_acl(target=target) is False  # noqa: SLF001


def test_windows_atomic_metadata_write_checks_acl_before_and_after_replace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows metadata 替换前后都必须检查实际目录/文件 ACL。"""
    calls: list[Path] = []
    target = tmp_path / "settings" / "index.json"

    with monkeypatch.context() as patch:
        real_os = settings_module.os

        class WindowsOSProxy:
            name = "nt"

            def __getattr__(self, attribute: str) -> object:
                return getattr(real_os, attribute)

        patch.setattr(settings_module, "os", WindowsOSProxy())
        patch.setattr(
            settings_module,
            "_require_windows_metadata_acl",
            lambda path, *, root: calls.append(path),
        )
        settings_module._atomic_json_write(target, {"ok": True}, root=tmp_path)  # noqa: SLF001

    assert calls == [target.parent, target]


def test_workspace_registry_and_uninstall_cover_all_recorded_scopes(tmp_path) -> None:
    """workspace 首次写入登记到 user index，uninstall 只清理已登记 scope。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_store = SettingsStore(home=home, workspace=workspace, backend=backend)
    workspace_store.set(scope="workspace", value="workspace-secret", expected_store_revision=0, **_setting_args())
    workspace_digest = workspace_store.workspace_binding_digest
    user_store = SettingsStore(
        home=home,
        backend=backend,
        workspace_registry_resolver={workspace_digest: workspace},
    )
    user_index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert user_index is not None
    assert [(item.scope_binding_digest, item.state) for item in user_index.workspace_registry] == [
        (workspace_digest, "registered")
    ]

    result = user_store.uninstall_plugin(
        plugin_id="plugin/local/za38",
        expected_store_revision=user_index.revision,
    )

    assert result["operation"] == "uninstall"
    assert result["partial"] == []
    assert backend.accounts == ()
    workspace_index = workspace_store._read_index("workspace", workspace_digest)  # noqa: SLF001
    assert workspace_index is not None
    assert not workspace_index.records
    final_user_index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert final_user_index is not None
    assert final_user_index.workspace_registry[0].state == "removed"


def test_uninstall_reads_registered_workspace_by_digest_without_backend_enumeration(tmp_path) -> None:
    """user uninstall 只按 metadata locator 找已登记 scope，不要求 backend 枚举或 raw path。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_store = SettingsStore(home=home, workspace=workspace, backend=backend)
    workspace_store.set(
        scope="workspace",
        value=secrets.token_urlsafe(18),
        expected_store_revision=0,
        **_setting_args(),
    )
    digest = workspace_store.workspace_binding_digest
    user_store = SettingsStore(home=home, backend=backend)
    first_index = user_store._read_index(  # noqa: SLF001 - inspect registry state
        "user",
        user_store.user_binding_digest,
    )
    assert first_index is not None

    result = user_store.uninstall_plugin(
        plugin_id="plugin/local/za38",
        expected_store_revision=first_index.revision,
    )
    assert result["partial"] == []
    assert backend.accounts == ()
    final_index = user_store._read_index(  # noqa: SLF001 - inspect registry state
        "user",
        user_store.user_binding_digest,
    )
    assert final_index is not None
    assert final_index.workspace_registry[0].state == "removed"


def test_uninstall_user_cleanup_failure_returns_retryable_partial_result(tmp_path) -> None:
    """user credential cleanup 失败时卸载也返回可重试 partial，而不是丢失事务状态。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    user_store = SettingsStore(home=home, backend=backend)
    user_store.set(scope="user", value=secrets.token_urlsafe(18), expected_store_revision=0, **_setting_args())
    initial = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert initial is not None
    backend.fail_on.add("delete")

    result = user_store.uninstall_plugin(
        plugin_id="plugin/local/za38",
        expected_store_revision=initial.revision,
    )

    assert len(result["partial"]) == 1
    assert str(result["partial"][0]).startswith("user:setting-")
    assert result["diagnostics"] == ["SETTINGS_UNINSTALL_PARTIAL"]
    partial_index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert partial_index is not None and not partial_index.records
    assert partial_index.tombstones

    backend.fail_on.clear()
    recovered_revision = user_store.list(scope="user")["store_revision"]
    retry = user_store.uninstall_plugin(
        plugin_id="plugin/local/za38",
        expected_store_revision=recovered_revision,
    )
    assert retry["partial"] == []
    assert backend.accounts == ()


def test_uninstall_preserves_registry_scopes_that_still_hold_other_plugins(tmp_path) -> None:
    """卸载一个 Plugin 不能把同一 user 下其他 workspace 的登记误删。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first_store = SettingsStore(home=home, workspace=first_workspace, backend=backend)
    second_store = SettingsStore(home=home, workspace=second_workspace, backend=backend)
    first_store.set(scope="workspace", value="first", expected_store_revision=0, **_setting_args())
    other_args = _setting_args()
    other_args["plugin_id"] = "plugin/local/other"
    second_store.set(scope="workspace", value="second", expected_store_revision=0, **other_args)
    user_store = SettingsStore(
        home=home,
        backend=backend,
        workspace_registry_resolver={
            first_store.workspace_binding_digest: first_workspace,
            second_store.workspace_binding_digest: second_workspace,
        },
    )
    user_index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert user_index is not None

    result = user_store.uninstall_plugin(
        plugin_id="plugin/local/za38",
        expected_store_revision=user_index.revision,
    )

    assert result["partial"] == []
    final_user_index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert final_user_index is not None
    states = {
        entry.scope_binding_digest: entry.state
        for entry in final_user_index.workspace_registry
    }
    assert states[first_store.workspace_binding_digest] == "removed"
    assert states[second_store.workspace_binding_digest] == "registered"


def test_uninstall_unknown_plugin_does_not_mutate_unrelated_workspace_registry(tmp_path) -> None:
    """未知 Plugin 卸载不能把无法枚举的 registry scope 改成 partial 或 removed。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_store = SettingsStore(home=home, workspace=workspace, backend=backend)
    workspace_store.set(scope="workspace", value="other", expected_store_revision=0, **_setting_args())
    user_store = SettingsStore(home=home, backend=backend)
    index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert index is not None

    with pytest.raises(SettingsError) as error:
        user_store.uninstall_plugin(
            plugin_id="plugin/local/not-installed",
            expected_store_revision=index.revision,
        )
    assert error.value.code == "SETTINGS_RECORD_NOT_FOUND"
    unchanged = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert unchanged is not None
    assert unchanged.workspace_registry[0].state == "registered"


def test_uninstall_marks_missing_registered_workspace_as_retryable_partial(tmp_path) -> None:
    """已登记 workspace metadata 缺失时不猜测清理范围，保留 partial 状态。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_store = SettingsStore(home=home, workspace=workspace, backend=backend)
    workspace_store.set(
        scope="workspace",
        value="workspace-secret",
        expected_store_revision=0,
        **_setting_args(),
    )
    user_store = SettingsStore(home=home, backend=backend)
    user_revision = user_store.list(scope="user")["store_revision"]
    user_store.set(
        scope="user",
        value="user-secret",
        expected_store_revision=user_revision,
        **_setting_args(),
    )
    user_index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert user_index is not None

    workspace_index = workspace_store._index_path(  # noqa: SLF001
        "workspace",
        workspace_store.workspace_binding_digest,
    )
    workspace_index.unlink()

    result = user_store.uninstall_plugin(
        plugin_id="plugin/local/za38",
        expected_store_revision=user_index.revision,
    )

    assert result["diagnostics"] == ["SETTINGS_UNINSTALL_PARTIAL"]
    assert result["partial"] == [workspace_store.workspace_binding_digest]
    remaining = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert remaining is not None
    assert remaining.workspace_registry[0].state == "partial"


def test_unknown_uninstall_does_not_mark_missing_workspace_registry_partial(tmp_path) -> None:
    """未知 Plugin 遇到无法读取的登记 scope 时 fail closed 且不改 registry。"""
    backend = FakeCredentialBackend()
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_store = SettingsStore(home=home, workspace=workspace, backend=backend)
    other_args = _setting_args()
    other_args["plugin_id"] = "plugin/local/other"
    workspace_store.set(scope="workspace", value="other-secret", expected_store_revision=0, **other_args)
    user_store = SettingsStore(home=home, backend=backend)
    user_index = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert user_index is not None
    workspace_index = workspace_store._index_path(  # noqa: SLF001
        "workspace",
        workspace_store.workspace_binding_digest,
    )
    workspace_index.unlink()

    with pytest.raises(SettingsError) as error:
        user_store.uninstall_plugin(
            plugin_id="plugin/local/not-installed",
            expected_store_revision=user_index.revision,
        )

    assert error.value.code == "SETTINGS_STORAGE_UNAVAILABLE"
    unchanged = user_store._read_index("user", user_store.user_binding_digest)  # noqa: SLF001
    assert unchanged is not None
    assert unchanged.workspace_registry[0].state == "registered"
