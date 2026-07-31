"""集中解析 Thread 根模型，并构造一次 Run 的脱敏执行事实。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Literal, Mapping

from harness_agent.config.config import (
    DEFAULT_MODEL_CAPABILITIES,
    ConfigError,
    ModelProfile,
    Za38Config,
)


class ExecutionBindingError(ValueError):
    """持久化执行绑定缺失必需字段或包含未审计数据时抛出。"""


class SelectionOrigin(str, Enum):
    """当前根模型选择的稳定来源；枚举值保持现有 Protocol 字符串。"""

    REQUEST = "thread-primary"
    RECOVERED = "thread-recovered"
    LEGACY = "legacy-binding"
    CONFIG_DEFAULT = "config-default"


@dataclass(frozen=True, slots=True)
class ThreadExecutionSelection:
    """当前 Thread 对下一次 Run 的根模型选择。"""

    root_model_profile_id: str

    def __post_init__(self) -> None:
        """拒绝空 Profile ID，避免无效选择进入历史记录。"""
        if not isinstance(self.root_model_profile_id, str) or not self.root_model_profile_id:
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> "ThreadExecutionSelection":
        """从 SQLite/Protocol 的现有字段名恢复类型化选择。"""
        if set(value) != {"primary_profile"}:
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")
        profile_id = value.get("primary_profile")
        if not isinstance(profile_id, str):
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")
        return cls(root_model_profile_id=profile_id)

    def to_record(self) -> dict[str, object]:
        """生成保持 JSON-RPC v3 与 SQLite v6 兼容的记录。"""
        return {"primary_profile": self.root_model_profile_id}


@dataclass(frozen=True, slots=True)
class SafeModelProfile:
    """可持久化和展示的模型摘要，不携带连接地址或凭据。"""

    profile_id: str
    model: str
    provider_label: str
    context_window_tokens: int
    capabilities: tuple[str, ...]
    is_default: bool
    available: bool
    unavailable_reason: str | None
    source: str

    @classmethod
    def from_profile(cls, profile: ModelProfile) -> "SafeModelProfile":
        """从可信配置 Profile 创建脱敏快照。"""
        settings = profile.settings
        available = settings.api_key_source() != "missing"
        return cls(
            profile_id=profile.profile_id,
            model=settings.name,
            provider_label=settings.provider_label,
            context_window_tokens=settings.context_window_tokens,
            capabilities=tuple(sorted(settings.capabilities)),
            is_default=profile.is_default,
            available=available,
            unavailable_reason=None if available else "API_KEY_MISSING",
            source=profile.source,
        )

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> "SafeModelProfile":
        """校验旧记录的脱敏字段，拒绝额外字段把秘密带入当前路径。"""
        required = {
            "id",
            "model",
            "provider_label",
            "context_window_tokens",
            "capabilities",
            "is_default",
            "available",
            "unavailable_reason",
            "source",
        }
        if set(value) != required:
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")
        profile_id = value.get("id")
        model = value.get("model")
        provider_label = value.get("provider_label")
        context_window_tokens = value.get("context_window_tokens")
        capabilities = value.get("capabilities")
        is_default = value.get("is_default")
        available = value.get("available")
        unavailable_reason = value.get("unavailable_reason")
        source = value.get("source")
        if not all(
            isinstance(item, str) and item
            for item in (profile_id, model, provider_label, source)
        ):
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")
        if (
            not isinstance(context_window_tokens, int)
            or context_window_tokens < 1
            or not isinstance(capabilities, list)
            or not all(isinstance(item, str) for item in capabilities)
            or not isinstance(is_default, bool)
            or not isinstance(available, bool)
            or (unavailable_reason is not None and not isinstance(unavailable_reason, str))
        ):
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")
        return cls(
            profile_id=profile_id,
            model=model,
            provider_label=provider_label,
            context_window_tokens=context_window_tokens,
            capabilities=tuple(capabilities),
            is_default=is_default,
            available=available,
            unavailable_reason=unavailable_reason,
            source=source,
        )

    def to_record(self) -> dict[str, object]:
        """生成现有 ModelProfile wire shape。"""
        return {
            "id": self.profile_id,
            "model": self.model,
            "provider_label": self.provider_label,
            "context_window_tokens": self.context_window_tokens,
            "capabilities": list(self.capabilities),
            "is_default": self.is_default,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class LegacyModelBindings:
    """v5 Thread 角色快照；仅 executor 可作为当前根模型回退。"""

    roles: tuple[tuple[str, SafeModelProfile], ...]

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> "LegacyModelBindings":
        """解析 v5 角色记录并保留其安全展示信息。"""
        if set(value) != {"roles"}:
            raise ExecutionBindingError("THREAD_MODEL_BINDING_INVALID")
        raw_roles = value.get("roles")
        if not isinstance(raw_roles, Mapping):
            raise ExecutionBindingError("THREAD_MODEL_BINDING_INVALID")
        roles: list[tuple[str, SafeModelProfile]] = []
        try:
            for role, raw_profile in sorted(raw_roles.items()):
                if not isinstance(role, str) or not role or not isinstance(raw_profile, Mapping):
                    raise ExecutionBindingError("THREAD_MODEL_BINDING_INVALID")
                roles.append((role, SafeModelProfile.from_record(raw_profile)))
        except ExecutionBindingError as exc:
            raise ExecutionBindingError("THREAD_MODEL_BINDING_INVALID") from exc
        return cls(roles=tuple(roles))

    def executor_profile_id(self) -> str:
        """返回旧 Single Agent 对应的 executor Profile ID。"""
        for role, profile in self.roles:
            if role == "executor":
                return profile.profile_id
        raise ExecutionBindingError("THREAD_MODEL_BINDING_INVALID")

    def protocol_roles(self) -> dict[str, object]:
        """生成 legacy thread_binding 所需的角色摘要。"""
        return {role: profile.to_record() for role, profile in self.roles}


@dataclass(frozen=True, slots=True)
class RunExecutionBinding:
    """一次已受理 Run 的不可变根模型事实。"""

    thread_id: str
    run_id: str
    requested_selection: ThreadExecutionSelection
    actual_primary: SafeModelProfile
    selection_origin: SelectionOrigin
    runtime_profile_id: str
    created_at_ms: int

    def __post_init__(self) -> None:
        """验证身份和时间字段，阻止半成品进入持久化。"""
        if (
            not isinstance(self.thread_id, str)
            or not self.thread_id
            or not isinstance(self.run_id, str)
            or not self.run_id
            or not isinstance(self.runtime_profile_id, str)
            or not self.runtime_profile_id
            or not isinstance(self.created_at_ms, int)
            or self.created_at_ms < 0
        ):
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")

    @classmethod
    def from_records(
        cls,
        *,
        thread_id: str,
        run_id: str,
        requested_selection: Mapping[str, object],
        actual_primary_binding: Mapping[str, object],
        runtime_profile_id: str,
        created_at_ms: int,
    ) -> "RunExecutionBinding":
        """从 SQLite v6 的两段 JSON 恢复不可变绑定。"""
        if set(actual_primary_binding) != {"profile", "source"}:
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")
        profile = actual_primary_binding.get("profile")
        source = actual_primary_binding.get("source")
        if not isinstance(profile, Mapping) or not isinstance(source, str):
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID")
        try:
            origin = SelectionOrigin(source)
        except ValueError as exc:
            raise ExecutionBindingError("RUN_EXECUTION_BINDING_INVALID") from exc
        return cls(
            thread_id=thread_id,
            run_id=run_id,
            requested_selection=ThreadExecutionSelection.from_record(requested_selection),
            actual_primary=SafeModelProfile.from_record(profile),
            selection_origin=origin,
            runtime_profile_id=runtime_profile_id,
            created_at_ms=created_at_ms,
        )

    def requested_selection_record(self) -> dict[str, object]:
        """生成 SQLite v6 requested_selection JSON。"""
        return self.requested_selection.to_record()

    def actual_primary_record(self) -> dict[str, object]:
        """生成 SQLite v6 actual_primary_binding JSON。"""
        return {
            "profile": self.actual_primary.to_record(),
            "source": self.selection_origin.value,
        }

    def protocol_primary_model(self) -> dict[str, object]:
        """生成 run.started/models.list 的现有模型绑定 shape。"""
        return {
            **self.actual_primary_record(),
            "runtime_profile_id": self.runtime_profile_id,
        }


class ExecutionMode(str, Enum):
    """一次 AgentExecution 采用的执行策略。"""

    INLINE = "inline"
    MANAGED = "managed"


class ExecutionStatus(str, Enum):
    """AgentExecution 的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """返回状态是否已经封口。"""
        return self in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class ExecutionRef:
    """一次 AgentExecution 的稳定身份和父子关系。"""

    thread_id: str
    run_id: str
    execution_id: str
    parent_execution_id: str | None = None

    def __post_init__(self) -> None:
        """拒绝缺失身份，避免状态路由退化为字符串拼接。"""
        if not all(
            isinstance(value, str) and value
            for value in (self.thread_id, self.run_id, self.execution_id)
        ):
            raise ExecutionBindingError("AGENT_EXECUTION_REFERENCE_INVALID")
        if self.parent_execution_id is not None and not self.parent_execution_id:
            raise ExecutionBindingError("AGENT_EXECUTION_PARENT_REFERENCE_INVALID")

    @classmethod
    def root(cls, thread_id: str, run_id: str) -> "ExecutionRef":
        """为一次 Run 生成稳定的根 execution ID。"""
        return cls(thread_id, run_id, f"root-{run_id}")

    def checkpoint_namespace(self, project_fingerprint: str) -> str:
        """生成 Managed execution 使用的类型化 namespace。"""
        if not isinstance(project_fingerprint, str) or not project_fingerprint:
            raise ExecutionBindingError("AGENT_EXECUTION_PROJECT_INVALID")
        return ":".join(
            (project_fingerprint, self.thread_id, self.run_id, self.execution_id)
        )


@dataclass(frozen=True, slots=True)
class AgentExecutionBinding:
    """一次 AgentExecution 的不可变身份和可收敛终态。"""

    ref: ExecutionRef
    agent_id: str
    mode: ExecutionMode
    depth: int
    model: SafeModelProfile | None = None
    policy_fingerprint: str | None = None
    engine_profile_key: str | None = None
    definition_fingerprint: str | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    usage: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验父子深度并复制 usage，防止调用方修改执行事实。"""
        if not self.agent_id or self.depth < 0:
            raise ExecutionBindingError("AGENT_EXECUTION_BINDING_INVALID")
        if self.depth == 0 and self.ref.parent_execution_id is not None:
            raise ExecutionBindingError("AGENT_EXECUTION_ROOT_PARENT_INVALID")
        if self.depth > 0 and self.ref.parent_execution_id is None:
            raise ExecutionBindingError("AGENT_EXECUTION_PARENT_REQUIRED")
        if not isinstance(self.mode, ExecutionMode) or not isinstance(
            self.status, ExecutionStatus
        ):
            raise ExecutionBindingError("AGENT_EXECUTION_BINDING_INVALID")
        if any(
            not isinstance(value, int) or value < 0
            for value in self.usage.values()
        ):
            raise ExecutionBindingError("AGENT_EXECUTION_USAGE_INVALID")
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))

    def transition(
        self,
        status: ExecutionStatus,
        *,
        now_ms: int,
        usage: Mapping[str, int] | None = None,
    ) -> "AgentExecutionBinding":
        """只更新生命周期字段，身份字段始终保持不变。"""
        if self.status.terminal:
            raise ExecutionBindingError("EXECUTION_ALREADY_TERMINAL")
        if status is ExecutionStatus.RUNNING and self.status is not ExecutionStatus.PENDING:
            raise ExecutionBindingError("EXECUTION_STATE_TRANSITION_INVALID")
        if status is ExecutionStatus.PENDING:
            raise ExecutionBindingError("EXECUTION_STATE_TRANSITION_INVALID")
        if status is not ExecutionStatus.RUNNING and not status.terminal:
            raise ExecutionBindingError("EXECUTION_STATE_TRANSITION_INVALID")
        if (
            self.status is ExecutionStatus.PENDING
            and status.terminal
            and status is not ExecutionStatus.CANCELLED
        ):
            raise ExecutionBindingError("EXECUTION_STATE_TRANSITION_INVALID")
        if now_ms < 0:
            raise ExecutionBindingError("AGENT_EXECUTION_TIMESTAMP_INVALID")
        next_usage = self.usage if usage is None else MappingProxyType(dict(usage))
        if any(
            not isinstance(value, int) or value < 0
            for value in next_usage.values()
        ):
            raise ExecutionBindingError("AGENT_EXECUTION_USAGE_INVALID")
        return replace(
            self,
            status=status,
            started_at_ms=now_ms if status is ExecutionStatus.RUNNING else self.started_at_ms,
            finished_at_ms=now_ms if status.terminal else self.finished_at_ms,
            usage=next_usage,
        )


@dataclass(frozen=True, slots=True)
class PersistedBindingState:
    """解析所需的当前 Run 与只读 legacy 状态。"""

    latest_run: RunExecutionBinding | None = None
    legacy_models: LegacyModelBindings | None = None
    has_legacy_runtime: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedExecutionBinding:
    """一次根模型解析的唯一结果，供 AgentEngine 与 Run 历史共同消费。"""

    selection: ThreadExecutionSelection
    primary_profile: ModelProfile
    safe_primary: SafeModelProfile
    selection_origin: SelectionOrigin

    def bind_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        runtime_profile_id: str,
        created_at_ms: int,
    ) -> RunExecutionBinding:
        """使用同一解析结果固化一次 Run 的执行事实。"""
        return RunExecutionBinding(
            thread_id=thread_id,
            run_id=run_id,
            requested_selection=self.selection,
            actual_primary=self.safe_primary,
            selection_origin=self.selection_origin,
            runtime_profile_id=runtime_profile_id,
            created_at_ms=created_at_ms,
        )


@dataclass(frozen=True, slots=True)
class ThreadBindingView:
    """models.list 所需的类型化 Thread 绑定视图。"""

    state: Literal["bound", "legacy", "unbound"]
    roles: tuple[tuple[str, SafeModelProfile], ...] = ()

    def to_record(self) -> dict[str, object]:
        """生成现有 threadModelBinding wire shape。"""
        return {
            "state": self.state,
            "roles": {role: profile.to_record() for role, profile in self.roles},
        }


def resolve_execution_binding(
    config: Za38Config,
    requested: ThreadExecutionSelection | None,
    persisted: PersistedBindingState,
) -> ResolvedExecutionBinding:
    """按请求、最近 Run、v5 legacy、配置默认的顺序解析根模型。"""
    if config.model_catalog is None:
        if requested is not None:
            raise ConfigError("MODEL_CATALOG_UNAVAILABLE")
        if config.model is None or config.model_profile is None:
            raise ConfigError("MODEL_CONFIGURATION_REQUIRED")
        profile = ModelProfile(
            profile_id=config.model_profile,
            settings=config.model,
            source="compatibility",
            is_default=True,
        )
        origin = SelectionOrigin.CONFIG_DEFAULT
    else:
        profile_id: str
        if requested is not None:
            profile_id = requested.root_model_profile_id
            origin = SelectionOrigin.REQUEST
        elif persisted.latest_run is not None:
            profile_id = persisted.latest_run.requested_selection.root_model_profile_id
            origin = SelectionOrigin.RECOVERED
        elif persisted.legacy_models is not None:
            profile_id = persisted.legacy_models.executor_profile_id()
            origin = SelectionOrigin.LEGACY
        else:
            profile_id = config.model_catalog.default_profile
            origin = SelectionOrigin.CONFIG_DEFAULT
        profile = config.model_catalog.require_profile(profile_id)
    _validate_model_profile(profile)
    selection = ThreadExecutionSelection(root_model_profile_id=profile.profile_id)
    return ResolvedExecutionBinding(
        selection=selection,
        primary_profile=profile,
        safe_primary=SafeModelProfile.from_profile(profile),
        selection_origin=origin,
    )


def describe_thread_binding(persisted: PersistedBindingState) -> ThreadBindingView:
    """将 legacy 持久化状态转换为不可误认当前模型的展示视图。"""
    if persisted.legacy_models is not None:
        return ThreadBindingView(state="bound", roles=persisted.legacy_models.roles)
    if persisted.has_legacy_runtime:
        return ThreadBindingView(state="legacy")
    return ThreadBindingView(state="unbound")


def _validate_model_profile(profile: ModelProfile) -> None:
    """验证当前根 Agent 启动模型的凭据与必要能力。"""
    if profile.settings.api_key_source() == "missing":
        raise ConfigError("MODEL_PROFILE_UNAVAILABLE: API_KEY_MISSING")
    missing_capabilities = DEFAULT_MODEL_CAPABILITIES - profile.settings.capabilities
    if missing_capabilities:
        missing = ",".join(sorted(missing_capabilities))
        raise ConfigError(f"MODEL_PROFILE_CAPABILITY_MISSING: {missing}")
