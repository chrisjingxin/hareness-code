"""Harness TOML v1 的安全加载、环境覆盖和 Agent 配置转换。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from harness_agent.approval_mode import (
    DEFAULT_APPROVAL_MODE,
    ApprovalMode,
    parse_approval_mode,
)
from harness_agent.config_manifest import (
    ConfigManifest,
    ConfigManifestError,
    ConfigSource,
)


class ConfigError(ValueError):
    """最终生效的 Harness 配置不合法时抛出，用于返回可操作的启动错误。"""


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """由 v1 TOML 和环境变量解析出的非秘密 OpenAI 兼容模型配置。"""

    name: str
    base_url: str
    api_key_env: str = "HARNESS_API_KEY"
    timeout_seconds: float = 120.0
    max_retries: int = 2
    context_window_tokens: int = 128_000
    context_window_source: Literal["default", "config"] = "default"
    headers: dict[str, str] = field(default_factory=dict)
    headers_env: dict[str, str] = field(default_factory=dict)

    def resolve_api_key(self, environ: Mapping[str, str] | None = None) -> str:
        """从明确命名的环境变量读取 API Key，绝不写回配置。"""
        value = (environ or os.environ).get(self.api_key_env, "").strip()
        if not value:
            raise ConfigError(
                f"Model API key is missing. Set the {self.api_key_env} environment variable."
            )
        return value

    def resolve_headers(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """合并非秘密固定 Header 和由环境变量提供的 Header。"""
        environment = environ or os.environ
        resolved = dict(self.headers)
        for header, env_name in self.headers_env.items():
            value = environment.get(env_name)
            if value:
                resolved[header] = value
        return resolved

    def redacted(self, environ: Mapping[str, str] | None = None) -> dict[str, object]:
        """返回可用于诊断展示的模型摘要，不包含 API Key 或动态 Header 值。"""
        environment = environ or os.environ
        return {
            "provider": "openai-compatible",
            "name": self.name,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(environment.get(self.api_key_env)),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "context_window_tokens": self.context_window_tokens,
            "context_window_source": self.context_window_source,
            "headers": dict(self.headers),
            "headers_env": dict(self.headers_env),
        }


@dataclass(frozen=True, slots=True)
class RemoteSandboxSettings:
    """企业远端沙箱的非秘密连接描述。

    ``factory`` 只能来自用户或显式配置。项目配置尚未获得可信机制，不能
    通过仓库提交导入任意 Python 代码。
    """

    provider: str
    factory: str
    working_directory: str = "/workspace"
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    """工具执行后端与审批模式的稳定运行时描述。"""

    sandbox_enabled: bool = False
    approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE
    approval_mode_warning: str | None = None
    remote: RemoteSandboxSettings | None = None

    @property
    def mode(self) -> Literal["local", "remote-sandbox"]:
        """返回适合协议和 TUI 展示的稳定执行模式。"""
        return "remote-sandbox" if self.sandbox_enabled else "local"

    def redacted(self) -> dict[str, object]:
        """返回不含认证材料的执行状态摘要。"""
        result: dict[str, object] = {
            "mode": self.mode,
            "sandbox_enabled": self.sandbox_enabled,
            "approval_mode": self.approval_mode,
            "provider": self.remote.provider if self.remote else None,
            "working_directory": self.remote.working_directory if self.remote else None,
        }
        if self.approval_mode_warning:
            result["approval_mode_warning"] = self.approval_mode_warning
        return result


@dataclass(frozen=True, slots=True)
class Za38Config:
    """最终生效的 Harness v1 配置、来源路径和运行时摘要。"""

    model: ModelSettings | None
    model_profile: str | None
    execution: ExecutionSettings
    paths: tuple[Path, ...]
    workspace: Path
    sources: Mapping[str, str]

    def require_model(self) -> ModelSettings:
        """返回默认模型；缺失时给出当前可信配置来源的修复方式。"""
        if self.model is None:
            raise ConfigError(
                "No model configuration found. Add [models] to ~/.harness/config.toml "
                "or pass a trusted file with --config PATH."
            )
        return self.model

    def redacted(self, environ: Mapping[str, str] | None = None) -> dict[str, object]:
        """返回适合 CLI 与 JSON-RPC 摘要的脱敏配置。"""
        return {
            "config_version": ConfigManifest.VERSION,
            "workspace": str(self.workspace),
            "paths": [str(path) for path in self.paths],
            "sources": dict(self.sources),
            "model_profile": self.model_profile,
            "model": self.model.redacted(environ) if self.model else None,
            "security": self.execution.redacted(),
        }


def load_config(
    *,
    workspace: Path | str,
    config_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Za38Config:
    """加载 v1 用户/显式 TOML，再应用环境变量和 CLI 覆盖。

    当前阶段仅信任用户级文件和用户明确传入的 ``--config``。工作区文件即使
    存在也不会被读取：在模型初始化前报错，阻止仓库把 endpoint 与凭据引用
    组合成外泄路径。长期来源优先级记录在配置架构文档中。
    """
    environment = environ or os.environ
    resolved_workspace = Path(workspace).expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser().resolve()
    explicit_path = Path(config_path).expanduser().resolve() if config_path else None
    _reject_untrusted_project_config(resolved_workspace, explicit_path)

    documents: list[tuple[Path, ConfigSource, dict[str, Any]]] = []
    user_path = resolved_home / ".harness" / "config.toml"
    if user_path.is_file():
        documents.append((user_path, ConfigSource.USER, _read_document(user_path, ConfigSource.USER)))
    if explicit_path is not None:
        documents.append(
            (explicit_path, ConfigSource.EXPLICIT, _read_document(explicit_path, ConfigSource.EXPLICIT))
        )

    models, approval_values, execution_values, sources = _merge_documents(documents)
    _apply_environment_overrides(models, approval_values, execution_values, environment, sources)
    _apply_cli_overrides(execution_values, environment, sources)
    model_profile, model = _parse_default_model(models)

    return Za38Config(
        model=model,
        model_profile=model_profile,
        execution=_parse_execution(approval_values, execution_values),
        paths=tuple(path for path, _, _ in documents),
        workspace=resolved_workspace,
        sources=sources,
    )


def _reject_untrusted_project_config(workspace: Path, explicit_path: Path | None) -> None:
    """拒绝未显式选择的项目配置，避免仓库控制模型网关或执行策略。"""
    for candidate in (
        workspace / ".harness" / "config.toml",
        workspace / ".harness" / "config.local.toml",
    ):
        if candidate.is_file() and candidate != explicit_path:
            raise ConfigError(
                f"Project configuration is not supported yet: {candidate}. "
                "Move it to ~/.harness/config.toml or pass this exact file with --config PATH."
            )


def _read_document(path: Path, source: ConfigSource) -> dict[str, Any]:
    """读取并校验一份可信 v1 TOML，绝不在错误中回显配置值。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file does not exist: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read configuration file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllib 始终返回 dict，保留边界。
        raise ConfigError(f"Configuration root must be a TOML table: {path}")
    try:
        ConfigManifest.validate_document(data, source=source)
    except ConfigManifestError as exc:
        raise ConfigError(f"Invalid configuration in {path}: {exc}") from exc
    return data


def _merge_documents(
    documents: list[tuple[Path, ConfigSource, dict[str, Any]]],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, str]]:
    """按用户到显式配置的顺序合并已验证字段，并记录最后贡献来源。"""
    models: dict[str, object] = {"profiles": {}}
    approval_values: dict[str, object] = {}
    execution_values: dict[str, object] = {}
    sources = {"models": "default", "approval": "default", "execution": "default"}
    for _, source, document in documents:
        if "models" in document:
            _merge_models(models, document["models"])
            sources["models"] = source.value
        if "approval" in document:
            approval_values = _merge_flat_values(approval_values, document["approval"])
            sources["approval"] = source.value
        if "execution" in document:
            execution_values = _merge_execution_values(execution_values, document["execution"])
            sources["execution"] = source.value
    return models, approval_values, execution_values, sources


def _merge_models(target: dict[str, object], value: object) -> None:
    """合并模型 Profile 表，同时禁止把多个可执行 Profile 提前交给当前内核。"""
    if not isinstance(value, dict):
        raise ConfigError("[models] must be a TOML table")
    unknown = set(value) - {"default_profile", "profiles"}
    if unknown:
        raise ConfigError(f"[models] contains unsupported fields: {', '.join(sorted(unknown))}")
    if "default_profile" in value:
        if not isinstance(value["default_profile"], str) or not value["default_profile"].strip():
            raise ConfigError("models.default_profile must be a non-empty string")
        target["default_profile"] = value["default_profile"].strip()
    if "profiles" not in value:
        return
    profiles = value["profiles"]
    if not isinstance(profiles, dict):
        raise ConfigError("[models.profiles] must be a TOML table")
    target_profiles = target["profiles"]
    if not isinstance(target_profiles, dict):  # pragma: no cover - 内部不变量。
        raise ConfigError("Internal configuration state is invalid")
    for profile_name, profile_values in profiles.items():
        if not isinstance(profile_name, str) or not profile_name:
            raise ConfigError("models.profiles keys must be non-empty strings")
        if not isinstance(profile_values, dict):
            raise ConfigError(f"models.profiles.{profile_name} must be a TOML table")
        existing = target_profiles.get(profile_name, {})
        if not isinstance(existing, dict):  # pragma: no cover - 内部不变量。
            raise ConfigError("Internal configuration state is invalid")
        target_profiles[profile_name] = _merge_profile_values(existing, profile_values)


def _merge_profile_values(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """按层合并同名 Profile，Header 映射采用逐项覆盖。"""
    allowed = {
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "timeout_seconds",
        "max_retries",
        "context_window_tokens",
        "headers",
        "headers_env",
    }
    unknown = set(override) - allowed
    if unknown:
        raise ConfigError(
            f"models.profiles contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    merged = dict(base)
    for key, value in override.items():
        if key in {"headers", "headers_env"}:
            if not isinstance(value, dict):
                raise ConfigError(f"models.profiles.<name>.{key} must be a TOML table")
            existing = merged.get(key, {})
            merged[key] = {**existing, **value} if isinstance(existing, dict) else dict(value)
        else:
            merged[key] = value
    return merged


def _merge_flat_values(base: dict[str, object], override: object) -> dict[str, object]:
    """按优先级逐项覆盖简单 TOML 表，并在读取前校验表类型。"""
    if not isinstance(override, dict):
        raise ConfigError("Configuration section must be a TOML table")
    return {**base, **override}


def _merge_execution_values(base: dict[str, object], override: object) -> dict[str, object]:
    """合并执行表，远端参数作为独立嵌套表逐项覆盖。"""
    values = _merge_flat_values(base, override)
    if not isinstance(override, dict) or "remote" not in override:
        return values
    remote = override["remote"]
    if not isinstance(remote, dict):
        raise ConfigError("[execution.remote] must be a TOML table")
    current = base.get("remote", {})
    values["remote"] = {**current, **remote} if isinstance(current, dict) else dict(remote)
    return values


def _apply_environment_overrides(
    models: dict[str, object],
    approval_values: dict[str, object],
    execution_values: dict[str, object],
    environ: Mapping[str, str],
    sources: dict[str, str],
) -> None:
    """应用公开 ``HARNESS_*`` 环境变量，环境层高于用户和显式 TOML。"""
    mapping = {
        "HARNESS_MODEL": "model",
        "HARNESS_BASE_URL": "base_url",
        "HARNESS_API_KEY_ENV": "api_key_env",
        "HARNESS_TIMEOUT_SECONDS": "timeout_seconds",
        "HARNESS_MAX_RETRIES": "max_retries",
    }
    overrides = {key: environ[name] for name, key in mapping.items() if environ.get(name)}
    if overrides:
        profile_name = str(models.get("default_profile", "default"))
        models["default_profile"] = profile_name
        profiles = models["profiles"]
        if not isinstance(profiles, dict):  # pragma: no cover - 内部不变量。
            raise ConfigError("Internal configuration state is invalid")
        existing = profiles.get(profile_name, {})
        profiles[profile_name] = _merge_profile_values(
            existing if isinstance(existing, dict) else {}, overrides
        )
        sources["models"] = ConfigSource.ENVIRONMENT.value
    if "HARNESS_APPROVAL_MODE" in environ:
        approval_values["mode"] = environ["HARNESS_APPROVAL_MODE"]
        sources["approval"] = ConfigSource.ENVIRONMENT.value
    if "HARNESS_SANDBOX" in environ:
        execution_values["backend"] = _sandbox_backend(environ["HARNESS_SANDBOX"])
        sources["execution"] = ConfigSource.ENVIRONMENT.value


def _apply_cli_overrides(
    execution_values: dict[str, object], environ: Mapping[str, str], sources: dict[str, str]
) -> None:
    """应用 CLI 注入的内部覆盖，保证 ``--sandbox`` 高于普通环境变量。"""
    if "HARNESS_CLI_SANDBOX" not in environ:
        return
    execution_values["backend"] = _sandbox_backend(environ["HARNESS_CLI_SANDBOX"])
    sources["execution"] = ConfigSource.CLI.value


def _parse_default_model(models: Mapping[str, object]) -> tuple[str | None, ModelSettings | None]:
    """解析当前内核唯一可执行的默认 Profile，多 Profile 选择留给 ZC-019。"""
    profiles = models.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        return None, None
    profile_name = models.get("default_profile")
    if not isinstance(profile_name, str) or not profile_name:
        raise ConfigError("models.default_profile is required when models.profiles is configured")
    if profile_name not in profiles:
        raise ConfigError("models.default_profile must reference an existing profile")
    if len(profiles) > 1:
        raise ConfigError("Multiple model profiles require ZC-019 and are not supported yet")
    values = profiles[profile_name]
    if not isinstance(values, dict):  # pragma: no cover - 已在合并阶段验证。
        raise ConfigError("models.default_profile must reference a TOML table")
    provider = str(values.get("provider", "openai-compatible"))
    if provider != "openai-compatible":
        raise ConfigError("Only models.profiles.<name>.provider = 'openai-compatible' is supported")
    name = _required_string(values, "model", "models.profiles.<name>.model")
    base_url = _required_string(values, "base_url", "models.profiles.<name>.base_url").rstrip("/")
    try:
        api_key_env = ConfigManifest.validate_environment_name(
            values.get("api_key_env", "HARNESS_API_KEY"),
            path="models.profiles.<name>.api_key_env",
        )
        headers = ConfigManifest.validate_static_headers(
            values.get("headers"), path="models.profiles.<name>.headers"
        )
        headers_env = ConfigManifest.validate_environment_headers(
            values.get("headers_env"), path="models.profiles.<name>.headers_env"
        )
    except ConfigManifestError as exc:
        raise ConfigError(str(exc)) from exc
    return profile_name, ModelSettings(
        name=name,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_seconds=_number(values.get("timeout_seconds", 120.0), "models.profiles.<name>.timeout_seconds", minimum=0.1),
        max_retries=_integer(values.get("max_retries", 2), "models.profiles.<name>.max_retries", minimum=0),
        context_window_tokens=_integer(
            values.get("context_window_tokens", 128_000),
            "models.profiles.<name>.context_window_tokens",
            minimum=16_384,
        ),
        context_window_source="config" if "context_window_tokens" in values else "default",
        headers=headers,
        headers_env=headers_env,
    )


def _parse_execution(
    approval_values: Mapping[str, object], execution_values: Mapping[str, object]
) -> ExecutionSettings:
    """把 v1 ``[approval]`` 与 ``[execution]`` 转换为现有执行后端设置。"""
    unknown_approval = set(approval_values) - {"mode"}
    if unknown_approval:
        raise ConfigError(f"[approval] contains unsupported fields: {', '.join(sorted(unknown_approval))}")
    approval_mode, approval_mode_warning = parse_approval_mode(approval_values.get("mode"))

    unknown_execution = set(execution_values) - {"backend", "remote"}
    if unknown_execution:
        raise ConfigError(f"[execution] contains unsupported fields: {', '.join(sorted(unknown_execution))}")
    backend = execution_values.get("backend", "local")
    if not isinstance(backend, str) or backend not in {"local", "remote"}:
        raise ConfigError("execution.backend must be 'local' or 'remote'")
    if backend == "local":
        return ExecutionSettings(
            sandbox_enabled=False,
            approval_mode=approval_mode,
            approval_mode_warning=approval_mode_warning,
        )

    remote = execution_values.get("remote")
    if not isinstance(remote, dict):
        raise ConfigError("[execution.remote] is required when execution.backend = 'remote'")
    allowed_remote = {"provider", "factory", "working_directory", "params"}
    unknown_remote = set(remote) - allowed_remote
    if unknown_remote:
        raise ConfigError(
            f"[execution.remote] contains unsupported fields: {', '.join(sorted(unknown_remote))}"
        )
    provider = _required_string(remote, "provider", "execution.remote.provider")
    factory = _required_string(remote, "factory", "execution.remote.factory")
    working_directory = str(remote.get("working_directory", "/workspace")).strip()
    if not working_directory.startswith("/"):
        raise ConfigError("execution.remote.working_directory must be an absolute sandbox path")
    params = remote.get("params", {})
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        raise ConfigError("execution.remote.params must be a TOML table with string keys")
    return ExecutionSettings(
        sandbox_enabled=True,
        approval_mode=approval_mode,
        approval_mode_warning=approval_mode_warning,
        remote=RemoteSandboxSettings(
            provider=provider,
            factory=factory,
            working_directory=working_directory,
            params=dict(params),
        ),
    )


def _sandbox_backend(value: object) -> str:
    """将公开 sandbox 环境变量转换为 v1 的 ``execution.backend`` 值。"""
    normalized = str(value).strip().lower()
    if normalized in {"", "0", "false", "off"}:
        return "local"
    if normalized in {"1", "true", "remote"}:
        return "remote"
    raise ConfigError("HARNESS_SANDBOX must be false, true, or 'remote'")


def _required_string(values: Mapping[str, object], key: str, path: str) -> str:
    """读取必填非空字符串字段，并在错误中保留稳定配置路径。"""
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _number(value: object, path: str, *, minimum: float) -> float:
    """将配置值解析为满足下限的浮点数。"""
    if isinstance(value, bool):
        raise ConfigError(f"{path} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} must be a number") from exc
    if number < minimum:
        raise ConfigError(f"{path} must be >= {minimum}")
    return number


def _integer(value: object, path: str, *, minimum: int) -> int:
    """将配置值解析为满足下限的整数。"""
    if isinstance(value, bool):
        raise ConfigError(f"{path} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} must be an integer") from exc
    if number < minimum:
        raise ConfigError(f"{path} must be >= {minimum}")
    return number
