"""Harness TOML v1 的配置来源、安全边界与运行时转换测试。"""

from __future__ import annotations

import sys
import stat
import types
from pathlib import Path

import pytest

import harness_agent.config as config_module
from harness_agent.config import ConfigError, ExecutionSettings, ModelSettings, RemoteSandboxSettings, load_config
from harness_agent.config_manifest import ConfigManifest
from harness_agent.execution import create_execution_context
from harness_agent.providers.harness_gateway import create_openai_compatible_model


def _write_config(
    path: Path,
    *,
    model: str = "enterprise-model",
    base_url: str = "https://gateway.example.internal/v1",
    api_key_env: str = "HARNESS_API_KEY",
    api_key: str | None = None,
    approval_mode: str | None = None,
    backend: str = "local",
    remote: bool = False,
) -> None:
    """生成最小可信 v1 TOML，避免测试散落旧配置结构。"""
    literal_api_key = f'api_key = "{api_key}"\n' if api_key is not None else ""
    approval = f"\n[approval]\nmode = \"{approval_mode}\"\n" if approval_mode else ""
    remote_table = (
        "\n[execution.remote]\nprovider = \"corp\"\nfactory = \"corp_sandbox:create_backend\"\n"
        if remote
        else ""
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''[config]
version = 1

[models]
default_profile = "enterprise"

[models.profiles.enterprise]
provider = "openai-compatible"
model = "{model}"
base_url = "{base_url}"
api_key_env = "{api_key_env}"
{literal_api_key}

[models.profiles.enterprise.headers]
X-Client = "harness"

[models.profiles.enterprise.headers_env]
X-Tenant = "HARNESS_TENANT"
{approval}
[execution]
backend = "{backend}"
{remote_table}''',
        encoding="utf-8",
    )
    if api_key is not None:
        path.chmod(0o600)


def test_config_precedence_and_redaction(tmp_path: Path):
    """用户、显式和环境变量按 v1 优先级覆盖，并保持摘要脱敏。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    explicit = tmp_path / "explicit.toml"
    _write_config(home / ".harness" / "config.toml", model="user", base_url="https://user.example/v1")
    _write_config(explicit, model="explicit", base_url="https://explicit.example/v1", api_key_env="EXPLICIT_KEY")

    config = load_config(
        workspace=workspace,
        home=home,
        config_path=explicit,
        environ={
            "HARNESS_MODEL": "environment",
            "HARNESS_BASE_URL": "https://env.example/v1",
            "EXPLICIT_KEY": "secret",
            "HARNESS_TENANT": "team-a",
        },
    )

    model = config.require_model()
    assert model.name == "environment"
    assert model.base_url == "https://env.example/v1"
    assert model.resolve_headers({"HARNESS_TENANT": "team-a"})["X-Tenant"] == "team-a"
    assert config.model_profile == "enterprise"
    assert config.redacted({"EXPLICIT_KEY": "secret"})["sources"]["models"] == "environment"
    assert "secret" not in str(config.redacted({"EXPLICIT_KEY": "secret"}))


def test_user_toml_api_key_fallback_environment_precedence_and_redaction(tmp_path: Path):
    """非空环境变量始终优先，否则使用不可见的用户 TOML 降级值。"""
    home = tmp_path / "home"
    path = home / ".harness" / "config.toml"
    _write_config(path, api_key="toml-secret")

    config = load_config(
        workspace=tmp_path / "workspace",
        home=home,
        environ={"HARNESS_API_KEY": "   "},
    )
    model = config.require_model()
    assert model.resolve_api_key({"HARNESS_API_KEY": "   "}) == "toml-secret"
    assert model.api_key_source({"HARNESS_API_KEY": "   "}) == "toml"
    assert model.resolve_api_key({"HARNESS_API_KEY": "environment-secret"}) == "environment-secret"
    assert model.api_key_source({"HARNESS_API_KEY": "environment-secret"}) == "environment"

    summary = config.redacted({"HARNESS_API_KEY": "   "})
    assert summary["model"]["api_key_configured"] is True  # type: ignore[index]
    assert summary["model"]["api_key_source"] == "toml"  # type: ignore[index]
    assert "toml-secret" not in repr(model)
    assert "toml-secret" not in str(summary)


def test_api_key_missing_and_blank_literal_fail_without_leaking_values(tmp_path: Path):
    """两种密钥来源均不可用时给出稳定诊断，空白 TOML 值不视为降级密钥。"""
    settings = ModelSettings(name="model", base_url="https://gateway.example/v1")
    with pytest.raises(ConfigError, match="HARNESS_API_KEY"):
        settings.resolve_api_key({})
    assert settings.api_key_source({}) == "missing"
    assert settings.redacted({})["api_key_configured"] is False

    home = tmp_path / "home"
    path = home / ".harness" / "config.toml"
    _write_config(path, api_key="   ")
    with pytest.raises(ConfigError, match="api_key must be a non-empty string"):
        load_config(workspace=tmp_path / "workspace", home=home, environ={})


def test_literal_api_key_is_rejected_outside_user_configuration(tmp_path: Path):
    """显式和项目 TOML 即使由用户选中，也不能携带字面量密钥。"""
    explicit = tmp_path / "explicit.toml"
    project = tmp_path / "workspace" / ".harness" / "config.toml"
    _write_config(explicit, api_key="explicit-secret")
    with pytest.raises(ConfigError, match="must reference an environment variable") as error:
        load_config(
            workspace=tmp_path / "workspace",
            home=tmp_path / "home",
            config_path=explicit,
            environ={},
        )
    assert "explicit-secret" not in str(error.value)

    _write_config(project, api_key="project-secret")
    with pytest.raises(ConfigError, match="must reference an environment variable") as error:
        load_config(
            workspace=tmp_path / "workspace",
            home=tmp_path / "home",
            config_path=project,
            environ={},
        )
    assert "project-secret" not in str(error.value)


@pytest.mark.skipif(config_module.os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_user_toml_api_key_automatically_hardens_posix_permissions(tmp_path: Path):
    """用户 TOML 的明文密钥在加载时自动收紧为仅所有者可读写。"""
    home = tmp_path / "home"
    path = home / ".harness" / "config.toml"
    _write_config(path, api_key="toml-secret")
    path.chmod(0o644)

    assert load_config(
        workspace=tmp_path / "workspace",
        home=home,
        environ={"HARNESS_API_KEY": "environment-secret"},
    ).require_model()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(config_module.os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_user_toml_api_key_reports_when_permissions_cannot_be_hardened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """无法收紧用户配置权限时拒绝加载，避免在宽权限文件中继续使用密钥。"""
    home = tmp_path / "home"
    path = home / ".harness" / "config.toml"
    _write_config(path, api_key="toml-secret")
    path.chmod(0o644)

    def reject_chmod(self: Path, mode: int) -> None:
        """模拟文件系统拒绝修改权限。"""
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "chmod", reject_chmod)
    with pytest.raises(ConfigError, match="Unable to secure configuration file"):
        load_config(workspace=tmp_path / "workspace", home=home, environ={})


def test_literal_api_key_permission_check_is_skipped_without_posix_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Windows 语义不尝试用 POSIX mode 误判 ACL 权限。"""
    home = tmp_path / "home"
    path = home / ".harness" / "config.toml"
    _write_config(path, api_key="toml-secret")
    path.chmod(0o644)
    monkeypatch.setattr(config_module, "_supports_posix_permissions", lambda: False)

    model = load_config(workspace=tmp_path / "workspace", home=home, environ={}).require_model()
    assert model.resolve_api_key({}) == "toml-secret"


def test_context_window_defaults_to_128k_and_rejects_small_explicit_value(tmp_path: Path):
    """窗口未配置时必须可诊断地使用 128K；显式值不能低于安全最小窗口。"""
    path = tmp_path / "window.toml"
    _write_config(path)

    default = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)
    assert default.require_model().context_window_tokens == 128_000
    assert default.require_model().context_window_source == "default"
    assert default.redacted()["model"]["context_window_source"] == "default"  # type: ignore[index]

    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'api_key_env = "HARNESS_API_KEY"',
            'api_key_env = "HARNESS_API_KEY"\ncontext_window_tokens = 65536',
        ),
        encoding="utf-8",
    )
    explicit = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)
    assert explicit.require_model().context_window_tokens == 65_536
    assert explicit.require_model().context_window_source == "config"

    path.write_text(path.read_text(encoding="utf-8").replace("65536", "8000"), encoding="utf-8")
    with pytest.raises(ConfigError, match="context_window_tokens"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)


def test_config_requires_v1_version_and_new_model_structure(tmp_path: Path):
    """旧字段和缺失版本必须被拒绝，而非悄然按旧语义执行。"""
    legacy = tmp_path / "legacy.toml"
    legacy.write_text("[model]\nname = 'old'\n", encoding="utf-8")
    missing_version = tmp_path / "missing-version.toml"
    missing_version.write_text("[models]\ndefault_profile = 'enterprise'\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"Unknown configuration section \[model\]"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=legacy)
    with pytest.raises(ConfigError, match=r"\[config\] is required"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=missing_version)


def test_project_configuration_is_rejected_before_model_resolution(tmp_path: Path):
    """仓库配置不能改变 endpoint；用户主动 --config 时才视为可信。"""
    workspace = tmp_path / "workspace"
    project_config = workspace / ".harness" / "config.toml"
    _write_config(project_config, base_url="https://untrusted.example/v1")

    with pytest.raises(ConfigError, match="Project configuration is not supported yet"):
        load_config(workspace=workspace, home=tmp_path / "home")

    config = load_config(workspace=workspace, home=tmp_path / "home", config_path=project_config)
    assert config.require_model().base_url == "https://untrusted.example/v1"


def test_project_local_configuration_is_rejected(tmp_path: Path):
    """未实现可信机制前，本地项目配置同样不能自动加载。"""
    workspace = tmp_path / "workspace"
    project_config = workspace / ".harness" / "config.local.toml"
    _write_config(project_config)

    with pytest.raises(ConfigError, match="config.local.toml"):
        load_config(workspace=workspace, home=tmp_path / "home")


def test_manifest_rejects_planned_unknown_and_secret_literal_configuration(tmp_path: Path):
    """计划中区段、未知字段和字面量秘密必须在启动前失败。"""
    cases = {
        "planned.toml": "[config]\nversion = 1\n\n[hooks]\n",
        "unknown.toml": "[config]\nversion = 1\n\n[unknown]\n",
        "invalid-version.toml": "[config]\nversion = true\n",
        "secret.toml": "[config]\nversion = 1\n\n[models]\ndefault_profile = 'enterprise'\n\n[models.profiles.enterprise]\nmodel = 'm'\nbase_url = 'https://gateway.example/v1'\napi_key = 'secret'\n",
        "nested-secret.toml": "[config]\nversion = 1\n\n[execution]\nbackend = 'remote'\n\n[execution.remote]\nprovider = 'corp'\nfactory = 'corp:create'\n\n[execution.remote.params]\nclient_secret = 'secret'\n",
        "interpolation.toml": "[config]\nversion = 1\n\n[models]\ndefault_profile = 'enterprise'\n\n[models.profiles.enterprise]\nmodel = '$MODEL'\nbase_url = 'https://gateway.example/v1'\n",
    }
    for name, content in cases.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)


def test_manifest_rejects_literal_authentication_headers(tmp_path: Path):
    """认证 Header 不能伪装为普通固定 Header，必须使用 headers_env。"""
    path = tmp_path / "headers.toml"
    _write_config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'X-Client = "harness"', 'X-Authorization-Token = "secret"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="environment variable"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)


def test_manifest_exposes_all_planned_configuration_sections():
    """模板中的所有计划中区段都必须由唯一 Manifest 指向后续任务。"""
    for name in ("ui", "skills", "agents", "mcp", "telemetry", "updates", "hooks", "extensions", "plugins", "policy"):
        section = ConfigManifest.SECTIONS[name]
        assert section.status == "planned"
        assert section.task_id is not None


def test_multiple_profiles_build_catalog_with_roles_and_safe_picker_summary(tmp_path: Path):
    """多 Profile 保留配置隔离，角色回退到默认项且 Picker 摘要不含 endpoint。"""
    path = tmp_path / "profiles.toml"
    _write_config(path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """

[models.profiles.pro]
provider = "openai-compatible"
provider_label = "Enterprise Pro"
model = "pro-model"
base_url = "https://pro.example/v1"
api_key_env = "PRO_KEY"
capabilities = ["tool-calling", "streaming", "vision"]

[models.roles]
planner = "pro"
executor = "enterprise"
""",
        encoding="utf-8",
    )

    config = load_config(
        workspace=tmp_path,
        home=tmp_path / "home",
        config_path=path,
        environ={"HARNESS_API_KEY": "default-key", "PRO_KEY": "pro-key"},
    )

    assert config.model_catalog is not None
    assert config.model_catalog.default_profile == "enterprise"
    assert config.model_catalog.profile_for_role("planner").profile_id == "pro"
    assert config.model_catalog.profile_for_role("reviewer").profile_id == "enterprise"
    assert config.require_model("pro").name == "pro-model"
    summary = config.require_model_profile("pro").picker_summary({"PRO_KEY": "pro-key"})
    assert summary["provider_label"] == "Enterprise Pro"
    assert summary["available"] is True
    assert summary["is_default"] is False
    assert summary["source"] == "explicit"
    assert "base_url" not in summary
    assert "PRO_KEY" not in str(summary)


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ("[models.roles]\nplanner = \"missing\"\n", "must reference an existing profile"),
        ("[models.roles]\narchitect = \"enterprise\"\n", "not a supported model role"),
    ],
)
def test_model_catalog_rejects_unknown_role_profile_and_capability(
    tmp_path: Path, extra: str, match: str
):
    """Profile 目录的错误引用和未知能力必须在配置加载阶段 fail closed。"""
    path = tmp_path / "invalid-profile.toml"
    _write_config(path)
    path.write_text(path.read_text(encoding="utf-8") + "\n" + extra, encoding="utf-8")

    with pytest.raises(ConfigError, match=match):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)


def test_model_catalog_rejects_unknown_capability(tmp_path: Path):
    """Profile 声明未知能力时不能以默认能力静默回退。"""
    path = tmp_path / "invalid-capability.toml"
    _write_config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'api_key_env = "HARNESS_API_KEY"',
            'api_key_env = "HARNESS_API_KEY"\ncapabilities = ["unknown"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="capabilities contains unsupported"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)


def test_execution_defaults_to_local_and_redacts_security_summary(tmp_path: Path):
    """没有 TOML 时保持可诊断的本机默认与 v1 来源摘要。"""
    config = load_config(workspace=tmp_path, home=tmp_path / "home", environ={})

    assert config.execution.sandbox_enabled is False
    assert config.execution.approval_mode == "default"
    assert config.redacted()["security"] == {
        "mode": "local",
        "sandbox_enabled": False,
        "approval_mode": "default",
        "provider": None,
        "working_directory": None,
    }
    assert config.redacted()["config_version"] == 1
    assert config.redacted()["sources"] == {
        "models": "default",
        "approval": "default",
        "execution": "default",
        "runtime_pool": "default",
    }


def test_runtime_pool_configuration_is_parsed_and_rejects_invalid_values(tmp_path: Path):
    """RuntimePool 的容量、TTL、关闭等待和固定默认 Profile 必须有显式安全边界。"""
    path = tmp_path / "runtime-pool.toml"
    _write_config(path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + """

[runtime_pool]
max_profiles = 3
idle_ttl_seconds = 600
close_timeout_seconds = 8
pin_default_profile = true
""",
        encoding="utf-8",
    )

    config = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)
    assert config.runtime_pool.redacted() == {
        "max_profiles": 3,
        "idle_ttl_seconds": 600,
        "close_timeout_seconds": 8,
        "pin_default_profile": True,
    }
    assert config.redacted()["sources"]["runtime_pool"] == "explicit"  # type: ignore[index]

    path.write_text(
        path.read_text(encoding="utf-8").replace("max_profiles = 3", "max_profiles = 0"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="runtime_pool.max_profiles"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)

    path.write_text(
        path.read_text(encoding="utf-8").replace("max_profiles = 0", "max_profiles = 65"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be <= 64"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)


@pytest.mark.parametrize("value", ["plan", "default", "auto-edit", "yolo"])
def test_execution_accepts_all_canonical_approval_modes(tmp_path: Path, value: str):
    """四个公开模式都应从 v1 [approval] 原样进入最终执行设置。"""
    path = tmp_path / "approval.toml"
    _write_config(path, approval_mode=value)

    config = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)

    assert config.execution.approval_mode == value
    assert config.execution.approval_mode_warning is None


def test_execution_normalizes_ask_and_invalid_values_safely(tmp_path: Path):
    """非法审批值仍安全回落 default，并保留 TUI 可展示的诊断。"""
    ask = tmp_path / "ask.toml"
    invalid = tmp_path / "invalid.toml"
    _write_config(ask, approval_mode="ask")
    _write_config(invalid, approval_mode="unsafe")

    assert load_config(workspace=tmp_path, home=tmp_path / "home", config_path=ask).execution.approval_mode == "default"
    invalid_config = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=invalid)
    assert invalid_config.execution.approval_mode == "default"
    assert "安全降级" in str(invalid_config.redacted()["security"]["approval_mode_warning"])


def test_environment_and_cli_override_execution_in_order(tmp_path: Path):
    """CLI --sandbox 通过内部覆盖层高于 HARNESS_SANDBOX，环境高于 TOML。"""
    path = tmp_path / "execution.toml"
    _write_config(path, backend="remote", remote=True, approval_mode="plan")

    environment_config = load_config(
        workspace=tmp_path,
        home=tmp_path / "home",
        config_path=path,
        environ={"HARNESS_SANDBOX": "false", "HARNESS_APPROVAL_MODE": "yolo"},
    )
    cli_config = load_config(
        workspace=tmp_path,
        home=tmp_path / "home",
        config_path=path,
        environ={"HARNESS_SANDBOX": "remote", "HARNESS_CLI_SANDBOX": "false"},
    )

    assert environment_config.execution.sandbox_enabled is False
    assert environment_config.execution.approval_mode == "yolo"
    assert environment_config.redacted()["sources"]["execution"] == "environment"
    assert cli_config.execution.sandbox_enabled is False
    assert cli_config.redacted()["sources"]["execution"] == "cli"


def test_remote_sandbox_requires_complete_trusted_configuration(tmp_path: Path):
    """显式开启远端 backend 时缺少 provider 仍必须在配置阶段失败。"""
    path = tmp_path / "remote.toml"
    _write_config(path, backend="remote")

    with pytest.raises(ConfigError, match="execution.remote"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)


def test_openai_compatible_adapter_is_constructed_without_network(monkeypatch: pytest.MonkeyPatch):
    """模型 adapter 使用最终解析的 TOML 降级密钥，且不发起网络请求。"""
    monkeypatch.delenv("HARNESS_TEST_KEY", raising=False)
    model = create_openai_compatible_model(
        ModelSettings(
            name="enterprise-model",
            base_url="https://gateway.example.internal/v1",
            api_key_env="HARNESS_TEST_KEY",
            api_key="toml-key",
        )
    )
    assert model.model_name == "enterprise-model"
    assert model.openai_api_key is not None
    assert model.openai_api_key.get_secret_value() == "toml-key"


def test_local_execution_backend_does_not_inherit_model_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """本机兼容模式的 shell 环境不应继承模型网关 Key。"""
    monkeypatch.setenv("HARNESS_API_KEY", "do-not-leak")
    context = create_execution_context(ExecutionSettings(), tmp_path)

    assert context.sandboxed is False
    assert "HARNESS_API_KEY" not in context.backend._env


def test_remote_backend_failure_never_falls_back_to_local(tmp_path: Path):
    """远端 provider 缺失时必须终止启动，不能返回宿主机 backend。"""
    settings = ExecutionSettings(
        sandbox_enabled=True,
        remote=RemoteSandboxSettings(
            provider="corp",
            factory="missing_corp_provider:create_backend",
        ),
    )

    with pytest.raises(ConfigError, match="Remote sandbox provider 'corp' is unavailable"):
        create_execution_context(settings, tmp_path)


def test_remote_backend_factory_receives_workspace_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """企业插件获得宿主工作区和逻辑远端目录，并产出真正的 sandbox backend。"""
    from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

    received: dict[str, object] = {}

    class FakeSandbox(SandboxBackendProtocol):
        @property
        def id(self) -> str:
            return "corp-test"

        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            return ExecuteResponse(output=command, exit_code=0)

    module = types.ModuleType("test_corp_sandbox")

    def create_backend(**kwargs: object) -> SandboxBackendProtocol:
        received.update(kwargs)
        return FakeSandbox()

    module.create_backend = create_backend  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_corp_sandbox", module)
    settings = ExecutionSettings(
        sandbox_enabled=True,
        remote=RemoteSandboxSettings(
            provider="corp",
            factory="test_corp_sandbox:create_backend",
            working_directory="/workspace",
            params={"project": "payments"},
        ),
    )

    context = create_execution_context(settings, tmp_path)
    assert context.sandboxed is True
    assert context.workspace_path == "/workspace"
    assert received["workspace"] == tmp_path
    assert received["provider"] == "corp"
    assert received["params"] == {"project": "payments"}
