"""za38 OpenAI 兼容模型网关的配置加载。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """最终生效的 za38 配置不合法时抛出。"""


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
class Za38Config:
    """最终生效的 za38 配置及其参与合并的文件。"""

    model: ModelSettings | None
    paths: tuple[Path, ...]
    workspace: Path

    def require_model(self) -> ModelSettings:
        """返回模型配置；缺失时提供可操作的错误提示。"""
        if self.model is None:
            raise ConfigError(
                "No model configuration found. Add [model] to ~/.za38/config.toml, "
                "<workspace>/.za38/config.toml, or pass --config PATH."
            )
        return self.model

    def redacted(self, environ: Mapping[str, str] | None = None) -> dict[str, object]:
        """返回适合 CLI 或 RPC 响应的脱敏配置。"""
        return {
            "workspace": str(self.workspace),
            "paths": [str(path) for path in self.paths],
            "model": self.model.redacted(environ) if self.model else None,
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
        resolved_home / ".za38" / "config.toml",
        resolved_workspace / ".za38" / "config.toml",
    )
    values: dict[str, object] = {}
    loaded_paths: list[Path] = []
    for path in paths:
        if path.is_file():
            values = _merge_model_values(values, _read_model_table(path))
            loaded_paths.append(path)

    values = _apply_environment(values, environment)
    if config_path is not None:
        explicit_path = Path(config_path).expanduser().resolve()
        values = _merge_model_values(values, _read_model_table(explicit_path))
        loaded_paths.append(explicit_path)

    return Za38Config(
        model=_parse_model(values) if values else None,
        paths=tuple(loaded_paths),
        workspace=resolved_workspace,
    )


def _read_model_table(path: Path) -> dict[str, object]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    model = data.get("model", {})
    if not isinstance(model, dict):
        raise ConfigError(f"[model] in {path} must be a TOML table")
    return dict(model)


def _merge_model_values(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        if key in {"headers", "headers_env"} and isinstance(value, dict):
            existing = merged.get(key, {})
            merged[key] = {**existing, **value} if isinstance(existing, dict) else dict(value)
        else:
            merged[key] = value
    return merged


def _apply_environment(values: dict[str, object], environ: Mapping[str, str]) -> dict[str, object]:
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


def _parse_model(values: Mapping[str, object]) -> ModelSettings:
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
    value = str(values.get(key, "")).strip()
    if not value:
        raise ConfigError(f"model.{key} is required")
    return value


def _number(value: object, key: str, *, minimum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"model.{key} must be a number") from exc
    if number < minimum:
        raise ConfigError(f"model.{key} must be >= {minimum}")
    return number


def _integer(value: object, key: str, *, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"model.{key} must be an integer") from exc
    if number < minimum:
        raise ConfigError(f"model.{key} must be >= {minimum}")
    return number


def _string_mapping(value: object, key: str) -> dict[str, str]:
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
