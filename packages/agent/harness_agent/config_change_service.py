"""受策略保护的用户级交互式配置变更服务。

本模块是 TUI Settings、Permissions Manager 与未来正式 CLI 共用的领域服务。
它只接受明确白名单字段，并在同一文件锁内完成版本比较、完整配置校验和原子替换；
调用方不能通过它编辑秘密、Provider 连接信息或任意 TOML 路径。
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import tomli_w

from harness_agent.config import DEFAULT_MODEL_CAPABILITIES, ConfigError, Za38Config, load_config

if TYPE_CHECKING:
    from harness_agent.mcp import McpConfigSnapshot, McpServerConfig

logger = logging.getLogger(__name__)


ConfigApplyScope = Literal["new-thread", "restart"]


class ConfigChangeError(ValueError):
    """配置变更违反安全、来源或并发边界时抛出的稳定领域错误。"""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        """保存可安全展示的错误码；绝不包含配置原文或秘密值。"""
        super().__init__(message)
        self.code = code
        self.field = field

    def redacted_data(self) -> dict[str, object]:
        """返回 JSON-RPC 可携带的最小安全错误详情。"""
        result: dict[str, object] = {"code": self.code}
        if self.field is not None:
            result["field"] = self.field
        return result


@dataclass(frozen=True, slots=True)
class ConfigFieldDefinition:
    """一个可交互修改字段的类型、来源和生效范围约束。"""

    path: str
    section: str
    value_type: type[str] | type[int] | type[bool]
    applies_to: ConfigApplyScope
    allowed_values: frozenset[str] | None = None


CONFIG_FIELD_DEFINITIONS: tuple[ConfigFieldDefinition, ...] = (
    ConfigFieldDefinition("models.default_profile", "models", str, "new-thread"),
    ConfigFieldDefinition(
        "approval.mode",
        "approval",
        str,
        "restart",
        frozenset({"plan", "default", "auto-edit", "yolo"}),
    ),
    ConfigFieldDefinition(
        "execution.backend",
        "execution",
        str,
        "restart",
        frozenset({"local", "remote"}),
    ),
    ConfigFieldDefinition("runtime_pool.max_profiles", "runtime_pool", int, "restart"),
    ConfigFieldDefinition("runtime_pool.idle_ttl_seconds", "runtime_pool", int, "restart"),
    ConfigFieldDefinition("runtime_pool.close_timeout_seconds", "runtime_pool", int, "restart"),
    ConfigFieldDefinition("runtime_pool.pin_default_profile", "runtime_pool", bool, "restart"),
)

_FIELD_BY_PATH = {field.path: field for field in CONFIG_FIELD_DEFINITIONS}

# 这些路径只用于向 UI 说明拒绝理由，不读取、回显或保存其中的实际值。
IMMUTABLE_FIELD_REASONS: Mapping[str, str] = {
    "models.profiles.*.api_key": "SECRET_FIELD",
    "models.profiles.*.api_key_env": "SECRET_FIELD",
    "models.profiles.*.headers": "SECRET_FIELD",
    "models.profiles.*.headers_env": "SECRET_FIELD",
    "models.profiles.*.base_url": "PROVIDER_CONNECTION_FIELD",
    "execution.remote.factory": "REMOTE_PROVIDER_FACTORY_FIELD",
    "execution.remote.params": "REMOTE_PROVIDER_FACTORY_FIELD",
    "environment.*": "ENVIRONMENT_FIELD",
}


@dataclass(frozen=True, slots=True)
class ManagedConfigPolicy:
    """由未来受管配置层注入的不可修改字段锁。

    当前 v1 尚未加载 managed TOML；该对象让写服务从第一天起就按同一
    fail-closed 边界工作，未来 managed 配置只需提供锁集合而无需复制写逻辑。
    """

    locked_fields: Mapping[str, str] = field(default_factory=dict)

    def lock_reason(self, field_path: str) -> str | None:
        """返回受管策略的安全原因，未锁定时返回 ``None``。"""
        return self.locked_fields.get(field_path)


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """一次预览或提交中的白名单字段更新。"""

    path: str
    value: object


@dataclass(frozen=True, slots=True)
class ConfigFieldDetail:
    """供 Settings/Permissions Manager 展示的脱敏字段详情。"""

    path: str
    value: object | None
    source: str
    editable: bool
    unavailable_reason: str | None
    applies_to: ConfigApplyScope

    def to_dict(self) -> dict[str, object]:
        """转换为稳定 JSON-RPC DTO。"""
        return {
            "path": self.path,
            "value": self.value,
            "source": self.source,
            "editable": self.editable,
            "unavailable_reason": self.unavailable_reason,
            "applies_to": self.applies_to,
        }


@dataclass(frozen=True, slots=True)
class ConfigPreview:
    """未落盘变更的 CAS 版本、脱敏字段差异与生效范围。"""

    revision: str
    changes: tuple[dict[str, object], ...]
    applies_to: tuple[ConfigApplyScope, ...]

    def to_dict(self) -> dict[str, object]:
        """转换为 JSON-RPC DTO，不返回原始 TOML。"""
        return {
            "revision": self.revision,
            "changes": list(self.changes),
            "applies_to": list(self.applies_to),
        }


@dataclass(frozen=True, slots=True)
class ConfigChangeAudit:
    """不含配置值和秘密的配置变更审计摘要。"""

    audit_id: str
    action: Literal["preview", "commit", "rejected"]
    fields: tuple[str, ...]
    outcome: str
    revision: str | None
    created_at_ms: int


class ConfigChangeService:
    """统一执行用户级配置的读取、预览、校验与原子提交。"""

    def __init__(
        self,
        *,
        workspace: Path | str,
        home: Path | None = None,
        config_path: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        managed_policy: ManagedConfigPolicy | None = None,
    ) -> None:
        """绑定来源边界；写入目标始终是用户级 ``~/.harness/config.toml``。"""
        self._workspace = Path(workspace).expanduser().resolve()
        self._home = (home or Path.home()).expanduser().resolve()
        self._target_path = self._home / ".harness" / "config.toml"
        self._explicit_path = Path(config_path).expanduser().resolve() if config_path else None
        self._environ = dict(os.environ if environ is None else environ)
        self._managed_policy = managed_policy or ManagedConfigPolicy()
        self._audits: list[ConfigChangeAudit] = []

    @property
    def audits(self) -> tuple[ConfigChangeAudit, ...]:
        """返回当前进程的安全审计摘要，用于诊断而非配置恢复。"""
        return tuple(self._audits)

    def details(self) -> dict[str, object]:
        """读取当前脱敏详情、可改性原因和无秘密的 CAS 版本。"""
        config = self._load_effective_config()
        revision = self._revision_for_path(self._target_path)
        fields = [self._field_detail(definition, config).to_dict() for definition in CONFIG_FIELD_DEFINITIONS]
        return {
            "revision": revision,
            "fields": fields,
            "immutable_fields": [
                {"path": path, "reason": reason}
                for path, reason in IMMUTABLE_FIELD_REASONS.items()
            ],
        }

    def read_mcp_snapshot(self) -> "McpConfigSnapshot":
        """读取当前有效 MCP 配置，并把用户文件版本绑定到快照。"""
        from harness_agent.mcp import build_mcp_snapshot

        config = self._load_effective_config()
        return build_mcp_snapshot(config.mcp_servers, self._revision_for_path(self._target_path))

    def preview(self, changes: Sequence[ConfigChange]) -> ConfigPreview:
        """验证白名单变更并返回可用于提交的当前文件版本，不写入磁盘。"""
        config = self._load_effective_config()
        revision = self._revision_for_path(self._target_path)
        try:
            normalized = self._normalize_changes(changes, config)
            document = self._read_user_document()
            candidate = self._serialize_candidate(document, normalized)
            self._validate_candidate(candidate, normalized)
            preview = self._build_preview(document, normalized, revision)
        except ConfigChangeError as exc:
            self._audit("rejected", tuple(change.path for change in changes), exc.code, None)
            raise
        self._audit("preview", tuple(change.path for change in normalized), "OK", revision)
        return preview

    def commit(self, *, expected_revision: str, changes: Sequence[ConfigChange]) -> dict[str, object]:
        """在同一文件锁内完成 CAS、完整校验和原子替换。"""
        if not expected_revision:
            raise ConfigChangeError("CONFIG_REVISION_REQUIRED", "配置提交需要预览返回的 revision")
        try:
            with self._exclusive_lock():
                config = self._load_effective_config()
                current_revision = self._revision_for_path(self._target_path)
                if current_revision != expected_revision:
                    raise ConfigChangeError("CONFIG_REVISION_CONFLICT", "配置已被其他操作修改")
                normalized = self._normalize_changes(changes, config)
                document = self._read_user_document()
                candidate = self._serialize_candidate(document, normalized)
                self._validate_candidate(candidate, normalized)
                preview = self._build_preview(document, normalized, current_revision)
                self._write_atomic(candidate)
                revision = self._revision_for_path(self._target_path)
        except ConfigChangeError as exc:
            self._audit("rejected", tuple(change.path for change in changes), exc.code, None)
            raise
        except OSError as exc:
            self._audit("rejected", tuple(change.path for change in changes), "CONFIG_WRITE_FAILED", None)
            raise ConfigChangeError("CONFIG_WRITE_FAILED", "无法原子写入用户配置") from exc
        self._audit("commit", tuple(change.path for change in normalized), "OK", revision)
        return {
            "revision": revision,
            "changes": list(preview.changes),
            "applies_to": list(preview.applies_to),
        }

    def add_mcp_server(
        self,
        server: Mapping[str, object] | "McpServerConfig",
        *,
        expected_revision: str | None = None,
    ) -> "McpConfigSnapshot":
        """在文件锁内添加 MCP 服务器配置，复用 CAS、校验、原子写入和审计。

        不走白名单 _normalize_changes 路径（MCP 是结构化数组操作），
        但复用锁、revision、完整 load_config() 校验和审计基础设施。
        """
        from harness_agent.mcp import McpConfigError, McpServerConfig, build_mcp_snapshot, parse_mcp_config

        try:
            if not isinstance(expected_revision, str) or not expected_revision:
                raise ConfigChangeError("CONFIG_REVISION_REQUIRED", "MCP 配置写入必须携带当前 revision")
            try:
                normalized = (
                    server
                    if isinstance(server, McpServerConfig)
                    else McpServerConfig.from_mapping(server)
                )
            except McpConfigError as exc:
                raise ConfigChangeError(exc.code, str(exc), field=exc.field) from exc
            with self._exclusive_lock():
                config = self._load_effective_config()
                self._assert_mcp_write_allowed(config)
                current_revision = self._revision_for_path(self._target_path)
                if current_revision != expected_revision:
                    raise ConfigChangeError("CONFIG_REVISION_CONFLICT", "配置已被其他操作修改")

                document = self._read_user_document()
                mcp_section = document.setdefault("mcp", {})
                if not isinstance(mcp_section, dict):
                    raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "mcp 区段类型无效", field="mcp")
                servers_list = mcp_section.setdefault("servers", [])
                if not isinstance(servers_list, list):
                    raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "mcp.servers 区段类型无效", field="mcp.servers")

                # 重复检查
                if any(
                    isinstance(s, dict)
                    and isinstance(s.get("name"), str)
                    and s["name"].strip() == normalized.name
                    for s in servers_list
                ):
                    raise ConfigChangeError("MCP_SERVER_DUPLICATE", f"MCP 服务器 '{normalized.name}' 已存在", field="mcp.servers.name")

                servers_list.append(normalized.to_document())

                try:
                    candidate = tomli_w.dumps(document)
                except (TypeError, ValueError) as exc:
                    raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "配置无法序列化") from exc

                # 完整配置校验
                self._validate_mcp_candidate(candidate)

                self._write_atomic(candidate)
                revision = self._revision_for_path(self._target_path)

                # 从写入后的配置构建 snapshot
                parsed = parse_mcp_config(document.get("mcp"))
                snapshot = build_mcp_snapshot(parsed, revision)

        except ConfigChangeError as exc:
            self._audit("rejected", ("mcp.servers",), exc.code, None)
            raise
        except OSError as exc:
            self._audit("rejected", ("mcp.servers",), "CONFIG_WRITE_FAILED", None)
            raise ConfigChangeError("CONFIG_WRITE_FAILED", "无法原子写入用户配置") from exc

        self._audit("commit", ("mcp.servers",), "OK", revision)
        return snapshot

    def remove_mcp_server(
        self,
        name: str,
        *,
        expected_revision: str | None = None,
    ) -> "McpConfigSnapshot":
        """在文件锁内删除 MCP 服务器配置，复用 CAS、校验、原子写入和审计。"""
        from harness_agent.mcp import build_mcp_snapshot, parse_mcp_config

        if not isinstance(name, str) or not name.strip():
            raise ConfigChangeError("MCP_SERVER_NAME_INVALID", "MCP 服务器名称无效", field="mcp.servers.name")
        name = name.strip()

        try:
            if not isinstance(expected_revision, str) or not expected_revision:
                raise ConfigChangeError("CONFIG_REVISION_REQUIRED", "MCP 配置写入必须携带当前 revision")
            with self._exclusive_lock():
                config = self._load_effective_config()
                self._assert_mcp_write_allowed(config)
                current_revision = self._revision_for_path(self._target_path)
                if current_revision != expected_revision:
                    raise ConfigChangeError("CONFIG_REVISION_CONFLICT", "配置已被其他操作修改")

                document = self._read_user_document()
                mcp_section = document.get("mcp", {})
                if not isinstance(mcp_section, dict):
                    raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "mcp 区段类型无效", field="mcp")
                servers_list = mcp_section.get("servers", [])
                if not isinstance(servers_list, list):
                    raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "mcp.servers 区段类型无效", field="mcp.servers")

                # 存在性检查
                original_len = len(servers_list)
                new_servers = [
                    s
                    for s in servers_list
                    if not (
                        isinstance(s, dict)
                        and isinstance(s.get("name"), str)
                        and s["name"].strip() == name
                    )
                ]
                if len(new_servers) == original_len:
                    raise ConfigChangeError("MCP_SERVER_NOT_FOUND", f"MCP 服务器 '{name}' 不存在", field="mcp.servers.name")

                mcp_section["servers"] = new_servers
                document["mcp"] = mcp_section

                try:
                    candidate = tomli_w.dumps(document)
                except (TypeError, ValueError) as exc:
                    raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "配置无法序列化") from exc

                # 完整配置校验
                self._validate_mcp_candidate(candidate)

                self._write_atomic(candidate)
                revision = self._revision_for_path(self._target_path)

                # 从写入后的配置构建 snapshot
                parsed = parse_mcp_config(document.get("mcp"))
                snapshot = build_mcp_snapshot(parsed, revision)

        except ConfigChangeError as exc:
            self._audit("rejected", ("mcp.servers",), exc.code, None)
            raise
        except OSError as exc:
            self._audit("rejected", ("mcp.servers",), "CONFIG_WRITE_FAILED", None)
            raise ConfigChangeError("CONFIG_WRITE_FAILED", "无法原子写入用户配置") from exc

        self._audit("commit", ("mcp.servers",), "OK", revision)
        return snapshot

    def _assert_mcp_write_allowed(self, config: Za38Config) -> None:
        """复用通用配置写入的可信来源、受管锁和目标文件边界。"""
        reason = self._mcp_write_block_reason(config)
        if reason is not None:
            raise ConfigChangeError(reason, "MCP 配置当前不可修改", field="mcp.servers")

    def _mcp_write_block_reason(self, config: Za38Config) -> str | None:
        """返回 MCP 写入被拒绝的稳定原因。"""
        if not self._target_path.is_file():
            return "CONFIG_USER_FILE_MISSING"
        if self._explicit_path is not None and self._explicit_path != self._target_path:
            if _is_within(self._explicit_path, self._workspace):
                return "UNTRUSTED_PROJECT_CONFIGURATION"
            return "EXPLICIT_CONFIGURATION_ACTIVE"
        if self._managed_policy.lock_reason("mcp.servers") is not None:
            return "MANAGED_POLICY_LOCKED"
        source = str(config.sources.get("mcp", "default"))
        if source in {"environment", "cli", "managed", "project-shared", "project-local"}:
            return "SOURCE_OVERRIDE_ACTIVE"
        return None

    def _load_effective_config(self) -> Za38Config:
        """按当前来源优先级加载配置；任何不可加载状态都拒绝交互式编辑。"""
        try:
            return load_config(
                workspace=self._workspace,
                home=self._home,
                config_path=self._explicit_path,
                environ=self._environ,
            )
        except ConfigError as exc:
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "当前配置不可加载，拒绝修改") from exc

    def _field_detail(self, definition: ConfigFieldDefinition, config: Za38Config) -> ConfigFieldDetail:
        """把有效配置映射为不含敏感内容的字段 DTO。"""
        reason = self._write_block_reason(definition, config)
        return ConfigFieldDetail(
            path=definition.path,
            value=self._effective_value(definition.path, config),
            source=str(config.sources.get(definition.section, "default")),
            editable=reason is None,
            unavailable_reason=reason,
            applies_to=definition.applies_to,
        )

    def _normalize_changes(self, changes: Sequence[ConfigChange], config: Za38Config) -> tuple[ConfigChange, ...]:
        """确认字段唯一、可写且值类型符合白名单，不能接受任意配置路径。"""
        if not changes:
            raise ConfigChangeError("CONFIG_CHANGE_EMPTY", "至少需要一个配置修改")
        normalized: list[ConfigChange] = []
        seen: set[str] = set()
        for change in changes:
            definition = _FIELD_BY_PATH.get(change.path)
            if definition is None:
                raise ConfigChangeError("CONFIG_FIELD_NOT_ALLOWED", "该配置字段不支持交互式修改", field=change.path)
            if change.path in seen:
                raise ConfigChangeError("CONFIG_FIELD_DUPLICATED", "同一字段不能在一次提交中重复出现", field=change.path)
            reason = self._write_block_reason(definition, config)
            if reason is not None:
                raise ConfigChangeError(reason, "该配置字段当前不可修改", field=change.path)
            self._validate_value(definition, change.value)
            seen.add(change.path)
            normalized.append(change)
        return tuple(normalized)

    def _write_block_reason(self, definition: ConfigFieldDefinition, config: Za38Config) -> str | None:
        """按受管策略、可信边界和优先级来源决定当前字段能否写入。"""
        if not self._target_path.is_file():
            return "CONFIG_USER_FILE_MISSING"
        if self._explicit_path is not None and self._explicit_path != self._target_path:
            if _is_within(self._explicit_path, self._workspace):
                return "UNTRUSTED_PROJECT_CONFIGURATION"
            return "EXPLICIT_CONFIGURATION_ACTIVE"
        if self._managed_policy.lock_reason(definition.path) is not None:
            return "MANAGED_POLICY_LOCKED"
        source = str(config.sources.get(definition.section, "default"))
        if source in {"environment", "cli", "managed", "project-shared", "project-local"}:
            return "SOURCE_OVERRIDE_ACTIVE"
        return None

    def _validate_value(self, definition: ConfigFieldDefinition, value: object) -> None:
        """在写入前拒绝隐式类型转换和不受支持的枚举值。"""
        if definition.value_type is bool:
            valid_type = isinstance(value, bool)
        elif definition.value_type is int:
            valid_type = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid_type = isinstance(value, str) and bool(value.strip())
        if not valid_type:
            raise ConfigChangeError("CONFIG_FIELD_VALUE_INVALID", "配置字段值类型无效", field=definition.path)
        if definition.allowed_values is not None and value not in definition.allowed_values:
            raise ConfigChangeError("CONFIG_FIELD_VALUE_INVALID", "配置字段值不在允许范围内", field=definition.path)

    def _read_user_document(self) -> dict[str, Any]:
        """读取写入目标的 TOML；损坏文件不能被写服务覆盖。"""
        try:
            content = self._target_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigChangeError("CONFIG_USER_FILE_MISSING", "用户配置文件不存在") from exc
        except OSError as exc:
            raise ConfigChangeError("CONFIG_READ_FAILED", "无法读取用户配置文件") from exc
        try:
            document = tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "用户配置 TOML 无效") from exc
        if not isinstance(document, dict):  # pragma: no cover - tomllib 的固定返回类型。
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "用户配置根节点无效")
        return document

    def _serialize_candidate(self, document: Mapping[str, Any], changes: Sequence[ConfigChange]) -> str:
        """仅修改白名单键后重新序列化；其他已验证内容保持语义不变。"""
        target = _copy_document(document)
        for change in changes:
            section, key = change.path.split(".", 1)
            values = target.get(section)
            if values is None:
                values = {}
                target[section] = values
            if not isinstance(values, dict):
                raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "配置区段类型无效", field=change.path)
            values[key] = change.value
        try:
            return tomli_w.dumps(target)
        except (TypeError, ValueError) as exc:
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "配置无法序列化") from exc

    def _validate_candidate(self, content: str, changes: Sequence[ConfigChange]) -> None:
        """在隔离临时 home 中校验完整配置与默认模型可运行性，绝不先覆盖原文件。"""
        try:
            with tempfile.TemporaryDirectory(prefix="harness-config-validate-") as temporary:
                temporary_home = Path(temporary)
                path = temporary_home / ".harness" / "config.toml"
                path.parent.mkdir(mode=0o700)
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)
                candidate = load_config(
                    workspace=self._workspace,
                    home=temporary_home,
                    environ=self._environ,
                )
                if any(change.path == "models.default_profile" for change in changes):
                    self._validate_default_model_profile(candidate)
        except ConfigChangeError:
            raise
        except ConfigError as exc:
            if "models.default_profile must reference an existing profile" in str(exc):
                raise ConfigChangeError(
                    "MODEL_PROFILE_NOT_FOUND",
                    "默认模型 Profile 不存在",
                    field="models.default_profile",
                ) from exc
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "修改后的完整配置校验失败") from exc
        except OSError as exc:
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "修改后的完整配置校验失败") from exc

    def _validate_mcp_candidate(self, content: str) -> None:
        """在隔离临时 home 中校验含 MCP 变更的完整配置。"""
        try:
            with tempfile.TemporaryDirectory(prefix="harness-config-validate-") as temporary:
                temporary_home = Path(temporary)
                path = temporary_home / ".harness" / "config.toml"
                path.parent.mkdir(mode=0o700)
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)
                load_config(
                    workspace=self._workspace,
                    home=temporary_home,
                    environ=self._environ,
                )
        except ConfigChangeError:
            raise
        except ConfigError as exc:
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "修改后的完整配置校验失败") from exc
        except OSError as exc:
            raise ConfigChangeError("CONFIG_VALIDATION_FAILED", "修改后的完整配置校验失败") from exc

    def _validate_default_model_profile(self, config: Za38Config) -> None:
        """确认待写入的全新 Thread 默认 Profile 可由 Single Agent 安全执行。"""
        if config.model_catalog is None:
            raise ConfigChangeError(
                "MODEL_CATALOG_UNAVAILABLE",
                "默认模型目录不可用，无法更新未来新 Thread 默认值",
                field="models.default_profile",
            )
        try:
            profile = config.model_catalog.require_profile(config.model_catalog.default_profile)
        except ConfigError as exc:  # pragma: no cover - load_config 已验证默认 Profile 引用。
            raise ConfigChangeError(
                "MODEL_PROFILE_NOT_FOUND",
                "默认模型 Profile 不存在",
                field="models.default_profile",
            ) from exc
        if profile.settings.api_key_source(self._environ) == "missing":
            raise ConfigChangeError(
                "MODEL_PROFILE_UNAVAILABLE",
                "默认模型不可用",
                field="models.default_profile",
            )
        if DEFAULT_MODEL_CAPABILITIES - profile.settings.capabilities:
            raise ConfigChangeError(
                "MODEL_PROFILE_CAPABILITY_MISSING",
                "默认模型缺少 Single Agent 所需能力",
                field="models.default_profile",
            )

    def _build_preview(
        self,
        document: Mapping[str, Any],
        changes: Sequence[ConfigChange],
        revision: str,
    ) -> ConfigPreview:
        """生成只含白名单前后值的预览 diff，不返回 TOML 或秘密。"""
        diff: list[dict[str, object]] = []
        scopes: list[ConfigApplyScope] = []
        for change in changes:
            section, key = change.path.split(".", 1)
            values = document.get(section)
            before = values.get(key) if isinstance(values, Mapping) else None
            definition = _FIELD_BY_PATH[change.path]
            diff.append({"path": change.path, "before": before, "after": change.value})
            if definition.applies_to not in scopes:
                scopes.append(definition.applies_to)
        return ConfigPreview(revision=revision, changes=tuple(diff), applies_to=tuple(scopes))

    def _effective_value(self, path: str, config: Za38Config) -> object | None:
        """从解析后的配置读取可安全展示的当前有效值。"""
        if path == "models.default_profile":
            return config.model_profile
        if path == "approval.mode":
            return config.execution.approval_mode
        if path == "execution.backend":
            return "remote" if config.execution.sandbox_enabled else "local"
        if path == "runtime_pool.max_profiles":
            return config.agent_engine_pool.max_profiles
        if path == "runtime_pool.idle_ttl_seconds":
            return config.agent_engine_pool.idle_ttl_seconds
        if path == "runtime_pool.close_timeout_seconds":
            return config.agent_engine_pool.close_timeout_seconds
        if path == "runtime_pool.pin_default_profile":
            return config.agent_engine_pool.pin_default_profile
        raise AssertionError(f"未知的配置字段：{path}")  # pragma: no cover - 静态白名单不变量。

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """使用与配置同目录的锁文件串行化跨进程提交。"""
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - 当前支持环境均提供 fcntl。
            raise ConfigChangeError("CONFIG_LOCK_UNAVAILABLE", "当前平台不支持安全配置锁") from exc
        try:
            self._target_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            lock_path = self._target_path.with_suffix(".toml.lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ConfigChangeError("CONFIG_LOCK_UNAVAILABLE", "无法创建配置锁") from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _write_atomic(self, content: str) -> None:
        """写入、fsync 后同目录替换，并将交互式用户配置收紧为 0600。"""
        self._target_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._target_path.name}.", suffix=".tmp", dir=self._target_path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                os.fchmod(file.fileno(), 0o600)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self._target_path)
            try:
                directory_descriptor = os.open(self._target_path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _revision_for_path(self, path: Path) -> str:
        """以内容 SHA-256 作为 CAS token；只暴露摘要而不暴露 TOML 原文。"""
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            raise ConfigChangeError("CONFIG_READ_FAILED", "无法读取用户配置文件") from exc
        return hashlib.sha256(content).hexdigest()

    def _audit(
        self,
        action: Literal["preview", "commit", "rejected"],
        fields: tuple[str, ...],
        outcome: str,
        revision: str | None,
    ) -> None:
        """记录不含配置值与秘密的进程内审计摘要，同时写安全日志。"""
        audit = ConfigChangeAudit(
            audit_id=uuid.uuid4().hex,
            action=action,
            fields=fields,
            outcome=outcome,
            revision=revision,
            created_at_ms=int(time.time() * 1000),
        )
        self._audits.append(audit)
        logger.info(
            "config_change action=%s outcome=%s fields=%s revision=%s audit_id=%s",
            audit.action,
            audit.outcome,
            ",".join(audit.fields),
            audit.revision[:12] if audit.revision else None,
            audit.audit_id,
        )


def _copy_document(value: Mapping[str, Any]) -> dict[str, Any]:
    """递归复制 TOML 数据，避免预览或提交修改调用方读取到的原对象。"""
    result: dict[str, Any] = {}
    for key, nested in value.items():
        if isinstance(nested, Mapping):
            result[key] = _copy_document(nested)
        elif isinstance(nested, list):
            result[key] = [
                _copy_document(item) if isinstance(item, Mapping) else item
                for item in nested
            ]
        else:
            result[key] = nested
    return result


def _is_within(path: Path, parent: Path) -> bool:
    """判断显式配置是否位于当前工作区，供不可信项目边界使用。"""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
