"""受策略保护的交互式配置变更服务回归测试。"""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness_agent.config import load_config
from harness_agent.config_change_service import (
    ConfigChange,
    ConfigChangeError,
    ConfigChangeService,
    ManagedConfigPolicy,
)


def _write_user_config(path: Path) -> None:
    """写入同时包含 Fast/Pro Profile 的最小可编辑用户配置。"""
    path.parent.mkdir(parents=True)
    path.write_text(
        '''[config]
version = 1

[models]
default_profile = "fast"

[models.profiles.fast]
provider = "openai-compatible"
model = "fast-model"
base_url = "https://gateway.example/v1"
api_key_env = "HARNESS_FAST_KEY"

[models.profiles.pro]
provider = "openai-compatible"
model = "pro-model"
base_url = "https://gateway.example/v1"
api_key_env = "HARNESS_PRO_KEY"

[approval]
mode = "default"

[execution]
backend = "local"

[runtime_pool]
max_profiles = 8
idle_ttl_seconds = 1800
close_timeout_seconds = 15
pin_default_profile = false
''',
        encoding="utf-8",
    )


def _service(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    **kwargs: object,
) -> ConfigChangeService:
    """构造使用隔离 home 的服务，并保证目标用户文件存在。"""
    home = tmp_path / "home"
    _write_user_config(home / ".harness" / "config.toml")
    return ConfigChangeService(
        workspace=tmp_path / "workspace",
        home=home,
        environ={"HARNESS_FAST_KEY": "fast-test", "HARNESS_PRO_KEY": "pro-test"}
        if environ is None
        else environ,
        **kwargs,
    )


def test_preview_and_commit_allowed_default_model_change(tmp_path: Path) -> None:
    """白名单模型默认值先预览，再以 revision 提交且不泄露连接配置。"""
    service = _service(tmp_path)

    details = service.details()
    model_field = next(field for field in details["fields"] if field["path"] == "models.default_profile")
    assert model_field == {
        "path": "models.default_profile",
        "value": "fast",
        "source": "user",
        "editable": True,
        "unavailable_reason": None,
        "applies_to": "new-thread",
    }
    assert "gateway.example" not in str(details)
    assert "HARNESS_FAST_KEY" not in str(details)

    preview = service.preview([ConfigChange("models.default_profile", "pro")])
    assert preview.changes == (
        {"path": "models.default_profile", "before": "fast", "after": "pro"},
    )
    assert preview.applies_to == ("new-thread",)

    result = service.commit(expected_revision=preview.revision, changes=[ConfigChange("models.default_profile", "pro")])
    assert result["revision"] != preview.revision
    assert result["changes"] == list(preview.changes)
    assert load_config(workspace=tmp_path / "workspace", home=tmp_path / "home", environ={}).model_profile == "pro"
    assert service.audits[-1].action == "commit"
    assert service.audits[-1].outcome == "OK"


def test_default_model_commit_rejects_concurrent_revision_without_overwriting_new_content(tmp_path: Path) -> None:
    """CAS 冲突必须保留其他进程已经写入的文件内容。"""
    service = _service(tmp_path)
    preview = service.preview([ConfigChange("models.default_profile", "pro")])
    path = tmp_path / "home" / ".harness" / "config.toml"
    externally_changed = path.read_text(encoding="utf-8").replace('default_profile = "fast"', 'default_profile = "pro"')
    path.write_text(externally_changed, encoding="utf-8")

    with pytest.raises(ConfigChangeError, match="已被其他操作修改") as error:
        service.commit(expected_revision=preview.revision, changes=[ConfigChange("models.default_profile", "pro")])

    assert error.value.code == "CONFIG_REVISION_CONFLICT"
    assert path.read_text(encoding="utf-8") == externally_changed


def test_candidate_validation_failure_never_changes_original_file(tmp_path: Path) -> None:
    """远端 backend 缺少既有 Provider 工厂时，完整校验必须在写前失败。"""
    service = _service(tmp_path)
    path = tmp_path / "home" / ".harness" / "config.toml"
    original = path.read_text(encoding="utf-8")

    with pytest.raises(ConfigChangeError) as error:
        service.preview([ConfigChange("execution.backend", "remote")])

    assert error.value.code == "CONFIG_VALIDATION_FAILED"
    assert path.read_text(encoding="utf-8") == original


def test_invalid_user_toml_fails_closed_and_is_not_rewritten(tmp_path: Path) -> None:
    """已损坏的用户 TOML 不能被交互式服务覆盖修复。"""
    service = _service(tmp_path)
    path = tmp_path / "home" / ".harness" / "config.toml"
    broken = "[config\nversion = 1\n"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigChangeError) as error:
        service.preview([ConfigChange("approval.mode", "plan")])

    assert error.value.code == "CONFIG_VALIDATION_FAILED"
    assert path.read_text(encoding="utf-8") == broken


def test_default_model_disk_write_failure_preserves_loadable_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """临时文件或 rename 失败时，原配置仍必须可被正常加载。"""
    service = _service(tmp_path)
    preview = service.preview([ConfigChange("models.default_profile", "pro")])
    path = tmp_path / "home" / ".harness" / "config.toml"
    original = path.read_text(encoding="utf-8")

    def fail_write(_content: str) -> None:
        """模拟文件系统在最终写入阶段拒绝操作。"""
        raise OSError("disk full")

    monkeypatch.setattr(service, "_write_atomic", fail_write)
    with pytest.raises(ConfigChangeError) as error:
        service.commit(expected_revision=preview.revision, changes=[ConfigChange("models.default_profile", "pro")])

    assert error.value.code == "CONFIG_WRITE_FAILED"
    assert path.read_text(encoding="utf-8") == original
    assert load_config(workspace=tmp_path / "workspace", home=tmp_path / "home", environ={}).model_profile == "fast"


def test_default_model_preview_rejects_missing_unavailable_or_incapable_profile(tmp_path: Path) -> None:
    """未来新 Thread 默认值必须在写前完成 Profile、凭据和能力校验。"""
    missing = _service(tmp_path / "missing")
    with pytest.raises(ConfigChangeError) as missing_error:
        missing.preview([ConfigChange("models.default_profile", "unknown")])
    assert missing_error.value.code == "MODEL_PROFILE_NOT_FOUND"

    unavailable = _service(tmp_path / "unavailable", environ={"HARNESS_FAST_KEY": "fast-test"})
    with pytest.raises(ConfigChangeError) as unavailable_error:
        unavailable.preview([ConfigChange("models.default_profile", "pro")])
    assert unavailable_error.value.code == "MODEL_PROFILE_UNAVAILABLE"
    assert "pro-test" not in str(unavailable_error.value)

    incapable_root = tmp_path / "incapable"
    incapable = _service(incapable_root)
    path = incapable_root / "home" / ".harness" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'api_key_env = "HARNESS_PRO_KEY"',
            'api_key_env = "HARNESS_PRO_KEY"\ncapabilities = ["streaming"]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigChangeError) as capability_error:
        incapable.preview([ConfigChange("models.default_profile", "pro")])
    assert capability_error.value.code == "MODEL_PROFILE_CAPABILITY_MISSING"
    assert "HARNESS_PRO_KEY" not in str(capability_error.value)


def test_permission_failure_closes_before_changing_user_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无法创建锁文件时以稳定错误失败，原用户配置保持不变。"""
    service = _service(tmp_path)
    preview = service.preview([ConfigChange("approval.mode", "plan")])
    path = tmp_path / "home" / ".harness" / "config.toml"
    original = path.read_text(encoding="utf-8")
    original_open = os.open

    def deny_lock(path_value: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777) -> int:
        """只拒绝锁文件创建，避免影响其他测试文件操作。"""
        if str(path_value).endswith("config.toml.lock"):
            raise OSError("permission denied")
        return original_open(path_value, flags, mode)

    monkeypatch.setattr(os, "open", deny_lock)
    with pytest.raises(ConfigChangeError) as error:
        service.commit(expected_revision=preview.revision, changes=[ConfigChange("approval.mode", "plan")])

    assert error.value.code == "CONFIG_LOCK_UNAVAILABLE"
    assert path.read_text(encoding="utf-8") == original


def test_managed_lock_and_untrusted_project_configuration_are_never_editable(tmp_path: Path) -> None:
    """受管字段与当前工作区内显式项目配置均必须拒绝交互式写入。"""
    managed = _service(
        tmp_path,
        managed_policy=ManagedConfigPolicy({"models.default_profile": "enterprise policy"}),
    )
    details = managed.details()
    default_model = next(field for field in details["fields"] if field["path"] == "models.default_profile")
    assert default_model["editable"] is False
    assert default_model["unavailable_reason"] == "MANAGED_POLICY_LOCKED"
    with pytest.raises(ConfigChangeError) as error:
        managed.preview([ConfigChange("models.default_profile", "pro")])
    assert error.value.code == "MANAGED_POLICY_LOCKED"

    home = tmp_path / "project-home"
    _write_user_config(home / ".harness" / "config.toml")
    workspace = tmp_path / "project-workspace"
    project_config = workspace / ".harness" / "config.toml"
    _write_user_config(project_config)
    project_service = ConfigChangeService(
        workspace=workspace,
        home=home,
        config_path=project_config,
        environ={},
    )
    with pytest.raises(ConfigChangeError) as project_error:
        project_service.preview([ConfigChange("models.default_profile", "pro")])
    assert project_error.value.code == "UNTRUSTED_PROJECT_CONFIGURATION"


def test_rejects_secret_and_arbitrary_paths_without_auditing_values(tmp_path: Path) -> None:
    """API Key、Header、环境变量与任意 TOML 路径都不能进入通用写服务。"""
    service = _service(tmp_path)
    secret = "never-log-this-secret"
    for path in (
        "models.profiles.fast.api_key",
        "models.profiles.fast.headers.Authorization",
        "environment.HARNESS_MODEL",
        "execution.remote.factory",
    ):
        with pytest.raises(ConfigChangeError) as error:
            service.preview([ConfigChange(path, secret)])
        assert error.value.code == "CONFIG_FIELD_NOT_ALLOWED"

    assert secret not in str(service.audits)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_successful_interactive_write_hardens_user_config_permissions(tmp_path: Path) -> None:
    """交互式原子写入一律把用户配置收紧到 0600。"""
    service = _service(tmp_path)
    path = tmp_path / "home" / ".harness" / "config.toml"
    path.chmod(0o644)
    preview = service.preview([ConfigChange("runtime_pool.max_profiles", 9)])
    service.commit(expected_revision=preview.revision, changes=[ConfigChange("runtime_pool.max_profiles", 9)])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert tomllib.loads(path.read_text(encoding="utf-8"))["runtime_pool"]["max_profiles"] == 9


class TestMcpServerOperations:
    """ConfigChangeService MCP 领域操作测试。"""

    @pytest.fixture(autouse=True)
    def _patch_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """在 Windows 上 mock fcntl 锁和 fchmod，使测试跨平台可运行。"""
        import contextlib

        from harness_agent.config_change_service import ConfigChangeService

        @contextlib.contextmanager
        def _noop_lock(self: ConfigChangeService) -> Iterator[None]:
            yield

        def _simple_write(self: ConfigChangeService, content: str) -> None:
            self._target_path.parent.mkdir(parents=True, exist_ok=True)
            self._target_path.write_text(content, encoding="utf-8")

        monkeypatch.setattr(ConfigChangeService, "_exclusive_lock", _noop_lock)
        monkeypatch.setattr(ConfigChangeService, "_write_atomic", _simple_write)

    def _make_service(self, tmp_path: Path, *, managed_policy: object = None) -> ConfigChangeService:
        """构建带有效用户配置的 ConfigChangeService。"""
        home = tmp_path / "home"
        harness_dir = home / ".harness"
        harness_dir.mkdir(parents=True)
        config_file = harness_dir / "config.toml"
        config_file.write_text(
            '[config]\nversion = 1\n\n[models]\ndefault_profile = "default"\n\n'
            '[models.profiles.default]\nprovider = "openai-compatible"\nmodel = "gpt-4o"\n'
            'base_url = "https://gateway.example/v1"\napi_key_env = "KEY"\n',
            encoding="utf-8",
        )
        return ConfigChangeService(
            workspace=tmp_path / "ws",
            home=home,
            environ={"KEY": "test-key"},
            managed_policy=managed_policy,
        )

    def test_add_mcp_server_success(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        snapshot = service.add_mcp_server(
            {"name": "my-server", "transport": "stdio", "command": "npx", "args": ["-y", "mcp"]}
        )
        assert snapshot.revision != "missing"
        assert len(snapshot.servers) == 1
        assert snapshot.servers[0].name == "my-server"
        # 验证文件已写入
        config_content = (tmp_path / "home" / ".harness" / "config.toml").read_text(encoding="utf-8")
        assert "my-server" in config_content

    def test_add_mcp_server_duplicate_rejected(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        service.add_mcp_server({"name": "srv", "transport": "stdio", "command": "cmd"})
        with pytest.raises(ConfigChangeError, match="已存在"):
            service.add_mcp_server({"name": "srv", "transport": "http", "url": "http://x"})

    def test_add_mcp_server_invalid_name_rejected(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        with pytest.raises(ConfigChangeError) as exc_info:
            service.add_mcp_server({"name": "", "transport": "stdio", "command": "cmd"})
        assert exc_info.value.code == "MCP_SERVER_NAME_INVALID"

    def test_add_mcp_server_cas_conflict(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        with pytest.raises(ConfigChangeError) as exc_info:
            service.add_mcp_server(
                {"name": "srv", "transport": "stdio", "command": "cmd"},
                expected_revision="stale-revision",
            )
        assert exc_info.value.code == "CONFIG_REVISION_CONFLICT"

    def test_add_mcp_server_managed_policy_locked(self, tmp_path: Path) -> None:
        policy = ManagedConfigPolicy(locked_fields={"mcp.servers": "MANAGED_POLICY_LOCKED"})
        service = self._make_service(tmp_path, managed_policy=policy)
        with pytest.raises(ConfigChangeError) as exc_info:
            service.add_mcp_server({"name": "srv", "transport": "stdio", "command": "cmd"})
        assert exc_info.value.code == "MANAGED_POLICY_LOCKED"

    def test_remove_mcp_server_success(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        service.add_mcp_server({"name": "srv", "transport": "stdio", "command": "cmd"})
        snapshot = service.remove_mcp_server("srv")
        assert len(snapshot.servers) == 0
        config_content = (tmp_path / "home" / ".harness" / "config.toml").read_text(encoding="utf-8")
        assert "srv" not in config_content or "servers" in config_content

    def test_remove_mcp_server_not_found(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        with pytest.raises(ConfigChangeError) as exc_info:
            service.remove_mcp_server("nonexistent")
        assert exc_info.value.code == "MCP_SERVER_NOT_FOUND"

    def test_remove_mcp_server_cas_conflict(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        service.add_mcp_server({"name": "srv", "transport": "stdio", "command": "cmd"})
        with pytest.raises(ConfigChangeError) as exc_info:
            service.remove_mcp_server("srv", expected_revision="stale")
        assert exc_info.value.code == "CONFIG_REVISION_CONFLICT"

    def test_add_mcp_server_preserves_other_sections(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        service.add_mcp_server({"name": "srv", "transport": "stdio", "command": "cmd"})
        config_content = (tmp_path / "home" / ".harness" / "config.toml").read_text(encoding="utf-8")
        assert "default_profile" in config_content
        assert "gpt-4o" in config_content

    def test_add_mcp_server_audit_recorded(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        service.add_mcp_server({"name": "srv", "transport": "stdio", "command": "cmd"})
        audits = service.audits
        assert len(audits) >= 1
        assert audits[-1].action == "commit"
        assert "mcp.servers" in audits[-1].fields

    def test_add_mcp_server_write_failure_preserves_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        service = self._make_service(tmp_path)
        original_content = (tmp_path / "home" / ".harness" / "config.toml").read_text(encoding="utf-8")

        # 模拟写盘失败
        def _fail_write(self_inner: object, content: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(type(service), "_write_atomic", _fail_write)
        with pytest.raises(ConfigChangeError) as exc_info:
            service.add_mcp_server({"name": "srv", "transport": "stdio", "command": "cmd"})
        assert exc_info.value.code == "CONFIG_WRITE_FAILED"

        # 文件内容不变
        assert (tmp_path / "home" / ".harness" / "config.toml").read_text(encoding="utf-8") == original_content
