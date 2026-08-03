"""压缩后的真实运行态快照与确定性恢复。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

if TYPE_CHECKING:
    from harness_agent.run_context import RunContext


class RuntimeStateError(ValueError):
    """结构化运行态缺失、越界或无法安全序列化时抛出。"""


@dataclass(frozen=True, slots=True)
class RuntimeExecutionPolicy:
    """当前已解析的执行/审批策略，不从历史摘要或旧状态猜测。"""

    execution_mode: str
    approval_mode: str
    capability_fingerprint: str = ""

    def __post_init__(self) -> None:
        """规范化合法字符串，并拒绝把空权限语义当成当前策略。"""
        for field_name in ("execution_mode", "approval_mode"):
            value = _normalize_string(
                getattr(self, field_name),
                f"RUNTIME_STATE_POLICY_{field_name.upper()}_TYPE_INVALID",
            )
            if not value:
                raise RuntimeStateError(
                    f"RUNTIME_STATE_POLICY_{field_name.upper()}_EMPTY"
                )
            object.__setattr__(self, field_name, value)
        capability = _normalize_string(
            self.capability_fingerprint,
            "RUNTIME_STATE_POLICY_CAPABILITY_TYPE_INVALID",
        )
        object.__setattr__(self, "capability_fingerprint", capability)

    @classmethod
    def from_resolved_spec(cls, spec: object) -> "RuntimeExecutionPolicy":
        """从当前 ResolvedAgentSpec 提取真实执行/审批策略。"""
        execution = _source_value(spec, "execution")
        effective_policy = _source_value(spec, "effective_policy")
        execution_mode = _required_source_string(execution, "mode", "execution_mode")
        effective_approval = _optional_source_string(
            effective_policy, "approval_mode", "approval_mode"
        )
        configured_approval = _optional_source_string(
            execution, "approval_mode", "approval_mode"
        )
        approval_mode = effective_approval or configured_approval
        if not approval_mode:
            raise RuntimeStateError("RUNTIME_STATE_POLICY_APPROVAL_MODE_MISSING")
        capability = _optional_source_string(
            effective_policy, "fingerprint", "capability_fingerprint"
        )
        if not capability:
            profile = _source_value(spec, "runtime_profile")
            capability = _optional_source_string(
                profile, "policy_fingerprint", "capability_fingerprint"
            ) or ""
        return cls(
            execution_mode=execution_mode,
            approval_mode=approval_mode,
            capability_fingerprint=capability,
        )


@dataclass(frozen=True, slots=True)
class RuntimeStateSnapshot:
    """只保存可以从真实运行对象证明的状态，不保存摘要文本。"""

    todos: tuple[Mapping[str, object], ...] = ()
    execution_mode: str = ""
    approval_mode: str = ""
    context_snapshot_id: str = ""
    capability_fingerprint: str = ""
    artifact_ids: tuple[str, ...] = ()
    recent_tool_group: str | None = None

    def __post_init__(self) -> None:
        """拒绝会在 ContextState 中形成不确定 JSON 的值。"""
        for field_name in (
            "execution_mode",
            "approval_mode",
            "context_snapshot_id",
            "capability_fingerprint",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_string(
                    getattr(self, field_name), "RUNTIME_STATE_STRING_INVALID"
                ),
            )
        if self.recent_tool_group is not None and not isinstance(
            self.recent_tool_group, str
        ):
            raise RuntimeStateError("RUNTIME_STATE_TOOL_GROUP_INVALID")
        if any(
            not isinstance(artifact_id, str) or not artifact_id
            for artifact_id in self.artifact_ids
        ) or len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise RuntimeStateError("RUNTIME_STATE_ARTIFACT_IDS_INVALID")
        for todo in self.todos:
            if not isinstance(todo, Mapping):
                raise RuntimeStateError("RUNTIME_STATE_TODO_INVALID")
        _strict_json(self.record())

    def record(self) -> dict[str, object]:
        """返回可写入 ContextState 的严格结构化记录。"""
        return {
            "todos": [dict(todo) for todo in self.todos],
            "execution_mode": self.execution_mode,
            "approval_mode": self.approval_mode,
            "context_snapshot_id": self.context_snapshot_id,
            "capability_fingerprint": self.capability_fingerprint,
            "artifact_ids": list(self.artifact_ids),
            "recent_tool_group": self.recent_tool_group,
        }

    @classmethod
    def from_record(cls, value: object) -> "RuntimeStateSnapshot":
        """严格读取持久化运行态；摘要或自由文本不会进入该入口。"""
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise RuntimeStateError("RUNTIME_STATE_RECORD_INVALID")
        todos_value = value.get("todos", [])
        artifact_value = value.get("artifact_ids", [])
        if not isinstance(todos_value, list) or not all(
            isinstance(todo, Mapping) for todo in todos_value
        ):
            raise RuntimeStateError("RUNTIME_STATE_TODO_INVALID")
        if not isinstance(artifact_value, list) or not all(
            isinstance(artifact_id, str) and artifact_id
            for artifact_id in artifact_value
        ):
            raise RuntimeStateError("RUNTIME_STATE_ARTIFACT_IDS_INVALID")
        recent_tool_group = value.get("recent_tool_group")
        if recent_tool_group is not None and not isinstance(recent_tool_group, str):
            raise RuntimeStateError("RUNTIME_STATE_TOOL_GROUP_INVALID")
        return cls(
            todos=tuple(dict(todo) for todo in todos_value),
            execution_mode=_record_string(value, "execution_mode"),
            approval_mode=_record_string(value, "approval_mode"),
            context_snapshot_id=_record_string(value, "context_snapshot_id"),
            capability_fingerprint=_record_string(value, "capability_fingerprint"),
            artifact_ids=tuple(dict.fromkeys(artifact_value)),
            recent_tool_group=recent_tool_group,
        )

    def with_artifacts(self, artifact_ids: Sequence[str]) -> "RuntimeStateSnapshot":
        """返回带当前检查点归档引用的新快照。"""
        merged = tuple(dict.fromkeys((*self.artifact_ids, *artifact_ids)))
        return RuntimeStateSnapshot(
            todos=self.todos,
            execution_mode=self.execution_mode,
            approval_mode=self.approval_mode,
            context_snapshot_id=self.context_snapshot_id,
            capability_fingerprint=self.capability_fingerprint,
            artifact_ids=merged,
            recent_tool_group=self.recent_tool_group,
        )

    def with_execution_policy(
        self, policy: RuntimeExecutionPolicy
    ) -> "RuntimeStateSnapshot":
        """用当前 typed 策略替换旧运行态中的权限语义。"""
        return RuntimeStateSnapshot(
            todos=self.todos,
            execution_mode=policy.execution_mode,
            approval_mode=policy.approval_mode,
            context_snapshot_id=self.context_snapshot_id,
            capability_fingerprint=(
                policy.capability_fingerprint or self.capability_fingerprint
            ),
            artifact_ids=self.artifact_ids,
            recent_tool_group=self.recent_tool_group,
        )


class RuntimeStateRehydrator:
    """从 LangGraph channel 和 RunContext 恢复真实状态的领域组件。"""

    @staticmethod
    def capture(
        langgraph_state: Mapping[str, object] | None,
        run_context: "RunContext | None",
        messages: Sequence[BaseMessage],
        artifact_ids: Sequence[str] = (),
        context_snapshot: object | None = None,
        current_execution_policy: RuntimeExecutionPolicy | None = None,
    ) -> RuntimeStateSnapshot:
        """只读取结构化 channel/RunContext，不解析摘要或 Artifact 正文。"""
        if langgraph_state is None:
            state: Mapping[str, object] = {}
        elif isinstance(langgraph_state, Mapping):
            state = langgraph_state
        else:
            raise RuntimeStateError("RUNTIME_STATE_LANGGRAPH_STATE_INVALID")
        if current_execution_policy is not None and not isinstance(
            current_execution_policy, RuntimeExecutionPolicy
        ):
            raise RuntimeStateError("RUNTIME_STATE_POLICY_INVALID")
        todos = _todos_from_state(state.get("todos"))
        snapshot = (
            context_snapshot
            if context_snapshot is not None
            else _source_value(run_context, "context_snapshot")
        )
        run_execution_mode = _optional_source_string(
            run_context, "execution_mode", "execution_mode"
        )
        state_execution_mode = _optional_source_string(
            state, "execution_mode", "execution_mode"
        )
        run_approval_mode = _optional_source_string(
            run_context, "approval_mode", "approval_mode"
        )
        state_approval_mode = _optional_source_string(
            state, "approval_mode", "approval_mode"
        )
        snapshot_id = _optional_source_string(
            snapshot, "snapshot_id", "context_snapshot_id"
        )
        state_snapshot_id = _optional_source_string(
            state, "context_snapshot_id", "context_snapshot_id"
        )
        snapshot_capability = _optional_source_string(
            snapshot, "system_fingerprint", "capability_fingerprint"
        )
        state_capability = _optional_source_string(
            state, "capability_fingerprint", "capability_fingerprint"
        )
        profile_key = _optional_source_string(
            run_context, "profile_key", "capability_fingerprint"
        )
        execution_mode = (
            current_execution_policy.execution_mode
            if current_execution_policy is not None
            else run_execution_mode or state_execution_mode or ""
        )
        approval_mode = (
            current_execution_policy.approval_mode
            if current_execution_policy is not None
            else run_approval_mode or state_approval_mode or ""
        )
        context_snapshot_id = snapshot_id or state_snapshot_id or ""
        capability_fingerprint = (
            snapshot_capability
            or (
                current_execution_policy.capability_fingerprint
                if current_execution_policy is not None
                else ""
            )
            or state_capability
            or profile_key
            or ""
        )
        recent_tool_group = _latest_tool_group(messages)
        return RuntimeStateSnapshot(
            todos=todos,
            execution_mode=execution_mode,
            approval_mode=approval_mode,
            context_snapshot_id=context_snapshot_id,
            capability_fingerprint=capability_fingerprint,
            artifact_ids=tuple(dict.fromkeys(artifact_ids)),
            recent_tool_group=recent_tool_group,
        )

    @staticmethod
    def rehydrate(snapshot: RuntimeStateSnapshot) -> dict[str, object]:
        """将已验证快照渲染为运行层可消费的字段，不写入 LangGraph。"""
        return snapshot.record()


def _todos_from_state(value: object) -> tuple[Mapping[str, object], ...]:
    """只接受 LangGraph 的结构化 Todo 列表，拒绝从字符串猜测。"""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(todo, Mapping) for todo in value
    ):
        raise RuntimeStateError("RUNTIME_STATE_TODO_INVALID")
    todos = tuple(dict(todo) for todo in value)
    _strict_json([dict(todo) for todo in todos])
    return todos


def _latest_tool_group(messages: Sequence[BaseMessage]) -> str | None:
    """保存最近一个已经闭合的 tool call/result 组的严格编码。"""
    from harness_agent.context_projection import (
        encode_projected_messages,
        validate_atomic_message_groups,
    )

    validate_atomic_message_groups(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        end = index + 1
        while end < len(messages) and isinstance(messages[end], ToolMessage):
            end += 1
        group = tuple(messages[index:end])
        try:
            return encode_projected_messages(group)
        except Exception as exc:  # pragma: no cover - validator already ran.
            raise RuntimeStateError("RUNTIME_STATE_TOOL_GROUP_INVALID") from exc
    return None


_MISSING = object()


def _source_value(source: object, field_name: str) -> object:
    """读取 Mapping/对象字段，并区分字段缺失与显式 None。"""
    if source is None:
        return _MISSING
    if isinstance(source, Mapping):
        return source[field_name] if field_name in source else _MISSING
    try:
        return getattr(source, field_name)
    except AttributeError:
        return _MISSING


def _normalize_string(value: object, error_code: str) -> str:
    """接受普通字符串和 str Enum，拒绝 bool/int/list/dict/NaN 洗白。"""
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise RuntimeStateError(error_code)
    return str(value)


def _optional_source_string(
    source: object, field_name: str, logical_name: str
) -> str | None:
    """读取可缺省字符串；显式非字符串值必须 fail closed。"""
    value = _source_value(source, field_name)
    if value is _MISSING or value is None:
        return None
    return _normalize_string(
        value, f"RUNTIME_STATE_{logical_name.upper()}_TYPE_INVALID"
    ) or None


def _required_source_string(
    source: object, field_name: str, logical_name: str
) -> str:
    """读取当前策略必需的非空字符串。"""
    value = _optional_source_string(source, field_name, logical_name)
    if not value:
        raise RuntimeStateError(f"RUNTIME_STATE_{logical_name.upper()}_MISSING")
    return value


def _record_string(value: Mapping[str, object], field_name: str) -> str:
    """读取持久化字段；缺失/None 是兼容默认，错误类型不是。"""
    raw = value.get(field_name)
    if raw is None:
        return ""
    return _normalize_string(raw, "RUNTIME_STATE_STRING_INVALID")


def _strict_json(value: object) -> str:
    """用严格 JSON 校验结构化运行态，拒绝 NaN/Infinity。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
