"""Tests for the OpenAI-compatible configuration contract."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from harness_agent.config import ConfigError, RemoteSandboxSettings, load_config
from harness_agent.config import ExecutionSettings, ModelSettings
from harness_agent.execution import create_execution_context
from harness_agent.providers.harness_gateway import create_openai_compatible_model


def _write_config(path: Path, *, name: str, base_url: str, api_key_env: str = "HARNESS_API_KEY") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''[model]
provider = "openai-compatible"
name = "{name}"
base_url = "{base_url}"
api_key_env = "{api_key_env}"

[model.headers]
X-Client = "za38"

[model.headers_env]
X-Tenant = "HARNESS_TENANT"
''',
        encoding="utf-8",
    )


def test_config_precedence_and_redaction(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    explicit = tmp_path / "explicit.toml"
    _write_config(home / ".harness" / "config.toml", name="user", base_url="https://user.example/v1")
    _write_config(workspace / ".harness" / "config.toml", name="project", base_url="https://project.example/v1")
    _write_config(explicit, name="explicit", base_url="https://explicit.example/v1", api_key_env="EXPLICIT_KEY")

    config = load_config(
        workspace=workspace,
        home=home,
        config_path=explicit,
        environ={"HARNESS_MODEL": "environment", "HARNESS_BASE_URL": "https://env.example/v1", "EXPLICIT_KEY": "secret", "HARNESS_TENANT": "team-a"},
    )

    model = config.require_model()
    assert model.name == "explicit"
    assert model.base_url == "https://explicit.example/v1"
    assert model.resolve_headers({"HARNESS_TENANT": "team-a"})["X-Tenant"] == "team-a"
    view = config.redacted({"EXPLICIT_KEY": "secret"})
    assert view["model"]["api_key_configured"] is True
    assert "secret" not in str(view)


def test_config_requires_complete_model_table(tmp_path: Path):
    path = tmp_path / ".harness" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("[model]\nname = 'missing-url'\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="base_url"):
        load_config(workspace=tmp_path, home=tmp_path / "home")


def test_openai_compatible_adapter_is_constructed_without_network(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HARNESS_TEST_KEY", "test-key")
    model = create_openai_compatible_model(
        ModelSettings(
            name="enterprise-model",
            base_url="https://gateway.example.internal/v1",
            api_key_env="HARNESS_TEST_KEY",
        )
    )
    assert model.model_name == "enterprise-model"


def test_execution_defaults_to_local_and_redacts_security_summary(tmp_path: Path):
    """未配置 sandbox 时保持本机执行，并向 TUI 明确暴露未隔离状态。"""
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


@pytest.mark.parametrize("value", ["plan", "default", "auto-edit", "yolo"])
def test_execution_accepts_all_canonical_approval_modes(tmp_path: Path, value: str):
    """四个公开模式都应原样进入最终执行设置。"""
    path = tmp_path / "approval.toml"
    path.write_text(f"[tools]\napproval_mode = '{value}'\n", encoding="utf-8")

    config = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=path)

    assert config.execution.approval_mode == value
    assert config.execution.approval_mode_warning is None


def test_execution_normalizes_legacy_ask_and_invalid_values(tmp_path: Path):
    """旧 ask 兼容为 default，未知值必须安全降级并暴露可展示提示。"""
    legacy = tmp_path / "legacy.toml"
    legacy.write_text("[tools]\napproval_mode = 'ask'\n", encoding="utf-8")
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[tools]\napproval_mode = 'unsafe'\n", encoding="utf-8")

    legacy_config = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=legacy)
    invalid_config = load_config(workspace=tmp_path, home=tmp_path / "home", config_path=invalid)

    assert legacy_config.execution.approval_mode == "default"
    assert "ask 已按默认确认模式执行" in str(legacy_config.execution.approval_mode_warning)
    assert invalid_config.execution.approval_mode == "default"
    assert "安全降级" in str(invalid_config.redacted()["security"]["approval_mode_warning"])


def test_approval_mode_environment_overrides_toml(tmp_path: Path):
    """审批模式遵循执行配置的环境变量优先级。"""
    path = tmp_path / "approval.toml"
    path.write_text("[tools]\napproval_mode = 'plan'\n", encoding="utf-8")

    config = load_config(
        workspace=tmp_path,
        home=tmp_path / "home",
        config_path=path,
        environ={"HARNESS_APPROVAL_MODE": "yolo"},
    )

    assert config.execution.approval_mode == "yolo"


def test_remote_sandbox_requires_trusted_provider_configuration(tmp_path: Path):
    """显式开启远端 sandbox 时缺少 provider 必须在配置阶段失败。"""
    config_path = tmp_path / "remote.toml"
    config_path.write_text("[tools]\nsandbox = true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="sandbox.provider"):
        load_config(workspace=tmp_path, home=tmp_path / "home", config_path=config_path)


def test_project_config_cannot_silently_enable_remote_sandbox(tmp_path: Path):
    """项目目录的 tools/sandbox 表不具备导入企业 provider 的授权。"""
    project_config = tmp_path / ".harness" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        "[tools]\nsandbox = true\n\n[sandbox]\nprovider = 'hostile'\nfactory = 'hostile.module:create'\n",
        encoding="utf-8",
    )

    config = load_config(workspace=tmp_path, home=tmp_path / "home", environ={})
    assert config.execution.sandbox_enabled is False


def test_sandbox_environment_overrides_explicit_config(tmp_path: Path):
    """HARNESS_SANDBOX 按 Qwen 风格优先于显式 TOML 配置。"""
    config_path = tmp_path / "remote.toml"
    config_path.write_text(
        "[tools]\nsandbox = true\napproval_mode = 'auto-edit'\n\n[sandbox]\nprovider = 'corp'\nfactory = 'corp_sandbox:create_backend'\n",
        encoding="utf-8",
    )

    config = load_config(
        workspace=tmp_path,
        home=tmp_path / "home",
        config_path=config_path,
        environ={"HARNESS_SANDBOX": "false"},
    )
    assert config.execution.sandbox_enabled is False
    assert config.execution.approval_mode == "auto-edit"


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
