"""Harness TOML v1 的配置清单、来源权限和安全校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ConfigManifestError(ValueError):
    """配置文档违反稳定契约时抛出，不携带用户配置中的敏感值。"""


class ConfigSource(StrEnum):
    """配置来源的稳定名称，用于诊断和后续优先级计算。"""

    USER = "user"
    EXPLICIT = "explicit"
    ENVIRONMENT = "environment"
    CLI = "cli"
    PROJECT_SHARED = "project-shared"
    PROJECT_LOCAL = "project-local"
    MANAGED = "managed"


@dataclass(frozen=True, slots=True)
class ConfigSection:
    """一个顶层 TOML 区段的实现状态和允许来源。"""

    name: str
    status: str
    task_id: str | None
    allowed_sources: frozenset[ConfigSource]


class ConfigManifest:
    """TOML v1 的唯一配置表面定义。

    当前仅激活用户和显式配置。环境变量与 CLI 属于覆盖层，因此不直接
    解析 TOML 文档；项目和 managed 的清单条目提前存在，避免未来功能
    以另一套字段名或来源规则接入。
    """

    VERSION = 1
    ACTIVE_TOML_SOURCES = frozenset({ConfigSource.USER, ConfigSource.EXPLICIT})
    _ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
    _ENV_INTERPOLATION = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")
    _SECRET_KEY_PARTS = frozenset({"api_key", "token", "secret", "password", "credential"})
    _NON_SECRET_TOKEN_FIELDS = frozenset({"context_window_tokens"})
    _SENSITIVE_HEADER_NAMES = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"})

    SECTIONS = {
        "config": ConfigSection("config", "implemented", None, ACTIVE_TOML_SOURCES),
        "models": ConfigSection("models", "implemented", "ZC-019", ACTIVE_TOML_SOURCES),
        "approval": ConfigSection("approval", "implemented", None, ACTIVE_TOML_SOURCES),
        "execution": ConfigSection("execution", "implemented", "ZC-008", ACTIVE_TOML_SOURCES),
        "ui": ConfigSection("ui", "planned", "ZC-042", frozenset()),
        "skills": ConfigSection("skills", "planned", "ZC-014", frozenset()),
        "agents": ConfigSection("agents", "planned", "ZC-015", frozenset()),
        "mcp": ConfigSection("mcp", "planned", "ZC-005", frozenset()),
        "telemetry": ConfigSection("telemetry", "planned", "ZC-021", frozenset()),
        "updates": ConfigSection("updates", "planned", "ZC-025", frozenset()),
        "hooks": ConfigSection("hooks", "planned", "ZC-039", frozenset()),
        "extensions": ConfigSection("extensions", "planned", "ZC-040", frozenset()),
        "plugins": ConfigSection("plugins", "planned", "ZC-041", frozenset()),
        "policy": ConfigSection("policy", "planned", "ZC-038", frozenset({ConfigSource.MANAGED})),
    }

    @classmethod
    def validate_document(cls, document: Mapping[str, Any], *, source: ConfigSource) -> None:
        """验证一份 TOML 文档的顶层结构、来源权限和通用秘密规则。"""
        for section_name, section_value in document.items():
            section = cls.SECTIONS.get(section_name)
            if section is None:
                raise ConfigManifestError(f"Unknown configuration section [{section_name}]")
            if section.status != "implemented":
                task = f" ({section.task_id})" if section.task_id else ""
                raise ConfigManifestError(
                    f"Configuration section [{section_name}] is planned{task} and is not supported yet"
                )
            if source not in section.allowed_sources:
                raise ConfigManifestError(
                    f"Configuration section [{section_name}] is not allowed from {source.value} configuration"
                )
            if not isinstance(section_value, dict):
                raise ConfigManifestError(f"[{section_name}] must be a TOML table")

        config = document.get("config")
        if not isinstance(config, dict):
            raise ConfigManifestError("[config] is required in every Harness TOML file")
        if set(config) != {"version"}:
            raise ConfigManifestError("[config] only supports version")
        version = config.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != cls.VERSION:
            raise ConfigManifestError(f"config.version must be {cls.VERSION}")

        cls._validate_scalars(document, path=())

    @classmethod
    def validate_environment_name(cls, value: object, *, path: str) -> str:
        """校验只允许环境变量名称的 ``*_env`` 字段。"""
        if not isinstance(value, str) or not cls._ENVIRONMENT_NAME.fullmatch(value):
            raise ConfigManifestError(f"{path} must be an uppercase environment variable name")
        return value

    @classmethod
    def validate_static_headers(cls, value: object, *, path: str) -> dict[str, str]:
        """校验固定请求头不包含认证材料，认证头必须改用 ``headers_env``。"""
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigManifestError(f"{path} must be a TOML table")
        result: dict[str, str] = {}
        for header, header_value in value.items():
            if not isinstance(header, str) or not isinstance(header_value, str):
                raise ConfigManifestError(f"{path} must map string header names to string values")
            normalized_header = header.lower()
            if (
                normalized_header in cls._SENSITIVE_HEADER_NAMES
                or "authorization" in normalized_header
                or "api-key" in normalized_header
            ):
                raise ConfigManifestError(f"{path}.{header} must use headers_env instead of a literal credential")
            result[header] = header_value
        return result

    @classmethod
    def validate_environment_headers(cls, value: object, *, path: str) -> dict[str, str]:
        """校验动态请求头只能引用环境变量名，不能写入 Header 值。"""
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigManifestError(f"{path} must be a TOML table")
        result: dict[str, str] = {}
        for header, environment_name in value.items():
            if not isinstance(header, str):
                raise ConfigManifestError(f"{path} must use string header names")
            result[header] = cls.validate_environment_name(
                environment_name, path=f"{path}.{header}"
            )
        return result

    @classmethod
    def _validate_scalars(cls, value: object, *, path: tuple[str, ...]) -> None:
        """拒绝通用环境变量插值和明显的秘密字面量字段。"""
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ConfigManifestError("Configuration keys must be strings")
                nested_path = (*path, key)
                normalized_key = key.lower()
                if (
                    any(part in normalized_key for part in cls._SECRET_KEY_PARTS)
                    and not normalized_key.endswith("_env")
                    and normalized_key not in cls._NON_SECRET_TOKEN_FIELDS
                ):
                    raise ConfigManifestError(
                        f"{'.'.join(nested_path)} must reference an environment variable instead of a literal secret"
                    )
                cls._validate_scalars(nested, path=nested_path)
            return
        if isinstance(value, list):
            for item in value:
                cls._validate_scalars(item, path=path)
            return
        if isinstance(value, str) and cls._ENV_INTERPOLATION.search(value):
            label = ".".join(path) or "configuration"
            raise ConfigManifestError(
                f"{label} does not support $VAR interpolation; use an explicit *_env field"
            )
