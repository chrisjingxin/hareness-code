"""za38 OpenAI 兼容模型网关的配置加载。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping


class ConfigError(ValueError):
    """最终生效的 za38 配置不合法时抛出，用于向 CLI 返回可操作错误。"""


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """由 za38 TOML 与环境变量解析出的非秘密模型配置。"""

    name: str
    base_url: str
    api_key_env: str = "ZA38_API_KEY"
    timeout_seconds: float = 120.0
    max_retries: int = 2
    headers: dict[str, str] = field(default_factory=dict)
    headers_env: dict[str, str] = field(default_factory=dict)

    def resolve_api_key(self, environ: Mapping[str, str] | None = None) -> str:
        """从环境变量读取 API Key，绝不写回配置。"""
        value = (environ or os.environ).get(self.api_key_env, "").strip()
        if not value:
            raise ConfigError(
                f"Model API key is missing. Set the {self.api_key_env} environment variable."
            )
        return value

    def resolve_headers(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """合并静态请求头与由环境变量提供的请求头。"""
        environment = environ or os.environ
        resolved = dict(self.headers)
        for header, env_name in self.headers_env.items():
            value = environment.get(env_name)
            if value:
                resolved[header] = value
        return resolved

    def redacted(self, environ: Mapping[str, str] | None = None) -> dict[str, object]:
        """返回可安全用于诊断展示的模型配置。"""
        environment = environ or os.environ
        return {
            "provider": "openai-compatible",
            "name": self.name,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(environment.get(self.api_key_env)),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "headers": dict(self.headers),
            "headers_env": dict(self.headers_env),
        }


@dataclass(frozen=True, slots=True)
class RemoteSandboxSettings:
    """企业远端沙箱的非秘密连接描述。

    ``factory`` 只允许由用户级或显式配置提供，避免项目目录中的配置在
    用户未确认时导入任意 Python 代码。实际 provider 负责把工作区同步到
    远端 ``working_directory``，并返回 deepagents 的 SandboxBackendProtocol。
    """

    provider: str
    factory: str
    working_directory: str = "/workspace"
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    """工具执行模式与审批模式。

    默认保留本机执行以匹配 Qwen Code 的启动体验；只有用户显式开启
    sandbox 时才创建企业远端执行后端，创建失败不能悄然降级到本机。
    """

    sandbox_enabled: bool = False
    approval_mode: Literal["plan", "ask", "auto-edit"] = "ask"
    remote: RemoteSandboxSettings | None = None

    @property
    def mode(self) -> Literal["local", "remote-sandbox"]:
        """返回适合协议和 TUI 展示的稳定执行模式。"""
        return "remote-sandbox" if self.sandbox_enabled else "local"

    def redacted(self) -> dict[str, object]:
        """返回不含认证材料的执行状态摘要。"""
        return {
            "mode": self.mode,
            "sandbox_enabled": self.sandbox_enabled,
            "approval_mode": self.approval_mode,
            "provider": self.remote.provider if self.remote else None,
            "working_directory": self.remote.working_directory if self.remote else None,
        }


@dataclass(frozen=True, slots=True)
class Za38Config:
    """最终生效的 za38 配置及其参与合并的文件。"""

    model: ModelSettings | None
    execution: ExecutionSettings
    paths: tuple[Path, ...]
    workspace: Path

    def require_model(self) -> ModelSettings:
        """返回模型配置；缺失时提供可操作的错误提示。"""
        if self.model is None:
            raise ConfigError(
                "No model configuration found. Add [model] to ~/.harness/config.toml, "
                "<workspace>/.harness/config.toml, or pass --config PATH."
            )
        return self.model

    def redacted(self, environ: Mapping[str, str] | None = None) -> dict[str, object]:
        """返回适合 CLI 或 RPC 响应的脱敏配置。"""
        return {
            "workspace": str(self.workspace),
            "paths": [str(path) for path in self.paths],
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
    """加载用户级、工作区、环境变量和显式 za38 配置。

    优先级为用户 TOML < 工作区 TOML < ZA38_* 环境变量 < 显式 TOML。
    TOML 只能声明保存秘密的环境变量名，不能包含秘密本身。
    """
    environment = environ or os.environ
    resolved_workspace = Path(workspace).expanduser().resolve()
    resolved_home = (home or Path.home()).expanduser().resolve()
    paths = (
        resolved_home / ".harness" / "config.toml",
        resolved_workspace / ".harness" / "config.toml",
    )
    values: dict[str, object] = {}
    execution_values: dict[str, object] = {}
    remote_values: dict[str, object] = {}
    loaded_paths: list[Path] = []
    for path in paths:
        if path.is_file():
            values = _merge_model_values(values, _read_model_table(path))
            # 项目配置可以继续描述模型，但不得静默启用能够导入 provider 的
            # 远端执行后端。执行策略仅信任用户级配置或用户明确传入的文件。
            if path == paths[0]:
                execution_values = _merge_flat_values(
                    execution_values, _read_optional_table(path, "tools")
                )
                remote_values = _merge_flat_values(
                    remote_values, _read_optional_table(path, "sandbox")
                )
            loaded_paths.append(path)

    values = _apply_environment(values, environment)
    if config_path is not None:
        explicit_path = Path(config_path).expanduser().resolve()
        values = _merge_model_values(values, _read_model_table(explicit_path))
        execution_values = _merge_flat_values(
            execution_values, _read_optional_table(explicit_path, "tools")
        )
        remote_values = _merge_flat_values(
            remote_values, _read_optional_table(explicit_path, "sandbox")
        )
        loaded_paths.append(explicit_path)
    execution_values = _apply_execution_environment(execution_values, environment)

    return Za38Config(
        model=_parse_model(values) if values else None,
        execution=_parse_execution(execution_values, remote_values),
        paths=tuple(loaded_paths),
        workspace=resolved_workspace,
    )


def _read_model_table(path: Path) -> dict[str, object]:
    """读取单个 TOML 的 ``[model]`` 表，并把语法错误转换为配置错误。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    model = data.get("model", {})
    if not isinstance(model, dict):
        raise ConfigError(f"[model] in {path} must be a TOML table")
    return dict(model)


def _read_optional_table(path: Path, name: str) -> dict[str, object]:
    """读取可选顶层 TOML 表，缺失时返回空映射。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] in {path} must be a TOML table")
    return dict(value)


def _merge_model_values(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """按优先级合并模型字段，其中 headers 两类映射采用逐项覆盖。"""
    merged = dict(base)
    for key, value in override.items():
        if key in {"headers", "headers_env"} and isinstance(value, dict):
            existing = merged.get(key, {})
            merged[key] = {**existing, **value} if isinstance(existing, dict) else dict(value)
        else:
            merged[key] = value
    return merged


def _merge_flat_values(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """按优先级逐项覆盖非嵌套执行配置。"""
    return {**base, **override}


def _apply_environment(values: dict[str, object], environ: Mapping[str, str]) -> dict[str, object]:
    """将允许的 ``ZA38_*`` 环境变量覆盖到非秘密模型字段。"""
    result = dict(values)
    mapping = {
        "ZA38_MODEL": "name",
        "ZA38_BASE_URL": "base_url",
        "ZA38_API_KEY_ENV": "api_key_env",
        "ZA38_TIMEOUT_SECONDS": "timeout_seconds",
        "ZA38_MAX_RETRIES": "max_retries",
    }
    for env_name, key in mapping.items():
        value = environ.get(env_name)
        if value:
            result[key] = value
    return result


def _apply_execution_environment(
    values: dict[str, object], environ: Mapping[str, str]
) -> dict[str, object]:
    """以 Qwen 风格让 ``ZA38_SANDBOX`` 和审批环境变量覆盖 TOML。"""
    result = dict(values)
    if "ZA38_SANDBOX" in environ:
        result["sandbox"] = environ["ZA38_SANDBOX"]
    if "ZA38_APPROVAL_MODE" in environ:
        result["approval_mode"] = environ["ZA38_APPROVAL_MODE"]
    return result


def _parse_execution(
    tools: Mapping[str, object], remote: Mapping[str, object]
) -> ExecutionSettings:
    """校验本机默认和显式远端 sandbox 的有限配置集合。"""
    sandbox_value = tools.get("sandbox", False)
    sandbox_enabled = _sandbox_enabled(sandbox_value)
    approval_mode = str(tools.get("approval_mode", "ask")).strip()
    if approval_mode not in {"plan", "ask", "auto-edit"}:
        raise ConfigError("tools.approval_mode must be one of: plan, ask, auto-edit")
    remote_settings: RemoteSandboxSettings | None = None
    if sandbox_enabled:
        provider = _required_sandbox_string(remote, "provider")
        factory = _required_sandbox_string(remote, "factory")
        working_directory = str(remote.get("working_directory", "/workspace")).strip()
        if not working_directory.startswith("/"):
            raise ConfigError("sandbox.working_directory must be an absolute sandbox path")
        params = remote.get("params", {})
        if not isinstance(params, dict) or not all(
            isinstance(key, str) for key in params
        ):
            raise ConfigError("sandbox.params must be a TOML table with string keys")
        remote_settings = RemoteSandboxSettings(
            provider=provider,
            factory=factory,
            working_directory=working_directory,
            params=dict(params),
        )
    return ExecutionSettings(
        sandbox_enabled=sandbox_enabled,
        approval_mode=approval_mode,  # type: ignore[arg-type]
        remote=remote_settings,
    )


def _sandbox_enabled(value: object) -> bool:
    """解析仅支持布尔值、``true`` 与 ``remote`` 的显式沙箱开关。"""
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"", "0", "false"}:
        return False
    if normalized in {"1", "true", "remote"}:
        return True
    raise ConfigError("tools.sandbox must be false, true, or 'remote'")


def _required_sandbox_string(values: Mapping[str, object], key: str) -> str:
    """读取远端 sandbox 的必填文本字段，并保留正确的配置路径。"""
    value = str(values.get(key, "")).strip()
    if not value:
        raise ConfigError(f"sandbox.{key} is required when tools.sandbox is enabled")
    return value


def _parse_model(values: Mapping[str, object]) -> ModelSettings:
    """校验合并结果，并构建唯一支持的 OpenAI 兼容模型配置。"""
    provider = str(values.get("provider", "openai-compatible"))
    if provider != "openai-compatible":
        raise ConfigError("Only model.provider = 'openai-compatible' is supported in v0.1")

    name = _required_string(values, "name")
    base_url = _required_string(values, "base_url").rstrip("/")
    api_key_env = str(values.get("api_key_env", "ZA38_API_KEY")).strip()
    if not api_key_env:
        raise ConfigError("model.api_key_env must be a non-empty environment variable name")

    timeout = _number(values.get("timeout_seconds", 120.0), "timeout_seconds", minimum=0.1)
    retries = _integer(values.get("max_retries", 2), "max_retries", minimum=0)
    return ModelSettings(
        name=name,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_seconds=timeout,
        max_retries=retries,
        headers=_string_mapping(values.get("headers", {}), "headers"),
        headers_env=_string_mapping(values.get("headers_env", {}), "headers_env"),
    )


def _required_string(values: Mapping[str, object], key: str) -> str:
    """读取必填非空字符串字段，缺失时统一抛出 ConfigError。"""
    value = str(values.get(key, "")).strip()
    if not value:
        raise ConfigError(f"model.{key} is required")
    return value


def _number(value: object, key: str, *, minimum: float) -> float:
    """将配置值解析为满足下限的浮点数。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"model.{key} must be a number") from exc
    if number < minimum:
        raise ConfigError(f"model.{key} must be >= {minimum}")
    return number


def _integer(value: object, key: str, *, minimum: int) -> int:
    """将配置值解析为满足下限的整数。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"model.{key} must be an integer") from exc
    if number < minimum:
        raise ConfigError(f"model.{key} must be >= {minimum}")
    return number


def _string_mapping(value: object, key: str) -> dict[str, str]:
    """校验 TOML 映射仅包含字符串键和值，适用于请求头配置。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"model.{key} must be a TOML table")
    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str) or not isinstance(item_value, str):
            raise ConfigError(f"model.{key} entries must map strings to strings")
        result[item_key] = item_value
    return result
