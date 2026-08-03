"""从 append-only Transcript 确定生成模型工作投影。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

if TYPE_CHECKING:
    from harness_agent.thread_persistence import ThreadPersistence, TranscriptRecord


PROJECTED_MESSAGES_VERSION = 1
SUPPORTED_REWRITE_VERSIONS = frozenset({"legacy-incomplete-v1"})
_ARTIFACT_REFERENCE_PATTERN = re.compile(
    r"/\.harness/history/([A-Za-z0-9_-]+)\.md"
)


class ContextProjectionError(RuntimeError):
    """投影数据损坏、越界或无法保持工具原子组时失败关闭。"""


@dataclass(frozen=True, slots=True)
class CompressionCheckpoint:
    """一份可审计的模型投影版本，不是 LangGraph checkpoint。"""

    checkpoint_id: str
    thread_id: str
    source_record_sequence: int
    source_digest: str
    mode: Literal["micro", "full"]
    rewrite_version: str
    projected_messages: tuple[BaseMessage, ...]
    artifact_ids: tuple[str, ...]
    trigger: str
    pressure_before: Mapping[str, object]
    pressure_after: Mapping[str, object]
    created_at_ms: int
    legacy_incomplete: bool = False


@dataclass(frozen=True, slots=True)
class CompressionCheckpointDraft:
    """待与 Artifact/Summary 同一事务提交的检查点。"""

    checkpoint_id: str
    mode: Literal["micro", "full"]
    rewrite_version: str
    projected_messages: tuple[BaseMessage, ...]
    artifact_ids: tuple[str, ...] = ()
    trigger: str = "automatic"
    pressure_before: Mapping[str, object] = field(default_factory=dict)
    pressure_after: Mapping[str, object] = field(default_factory=dict)
    source_record_sequence: int | None = None
    legacy_incomplete: bool = False


@dataclass(frozen=True, slots=True)
class ModelProjection:
    """Projector 输出及其可诊断来源。"""

    messages: tuple[BaseMessage, ...]
    checkpoint: CompressionCheckpoint | None
    tail_start_sequence: int
    source_record_sequence: int


class ContextProjector:
    """从最新有效检查点和 Transcript tail 构造唯一模型历史。"""

    def __init__(self, persistence: "ThreadPersistence") -> None:
        """绑定已经 project-scoped 的持久化领域入口。"""
        self._persistence = persistence

    async def project(
        self,
        thread_id: str,
        *,
        exclude_record_id: str | None = None,
    ) -> ModelProjection:
        """加载 latest-valid checkpoint，追加 tail 并校验工具原子组。"""
        records = await self._persistence.load_transcript(thread_id)
        if exclude_record_id is not None:
            if not records or records[-1].record_id != exclude_record_id:
                raise ContextProjectionError("PROJECTION_EXCLUDED_RECORD_NOT_TAIL")
            records = records[:-1]
        checkpoint = await self._persistence.load_latest_valid_compression_checkpoint(
            thread_id,
            max_source_sequence=records[-1].sequence if records else 0,
        )
        boundary = checkpoint.source_record_sequence if checkpoint is not None else 0
        messages = list(checkpoint.projected_messages if checkpoint is not None else ())
        for record in records:
            if record.sequence <= boundary or record.kind == "context":
                continue
            message = await self._message_from_record(record)
            if message is not None:
                messages.append(message)
        validate_atomic_message_groups(messages)
        return ModelProjection(
            messages=tuple(messages),
            checkpoint=checkpoint,
            tail_start_sequence=boundary + 1,
            source_record_sequence=records[-1].sequence if records else 0,
        )

    async def sync_cache(
        self,
        agent: Any,
        thread_id: str,
        *,
        projection: ModelProjection | None = None,
        exclude_record_id: str | None = None,
    ) -> ModelProjection:
        """从 Harness 事实单向刷新 LangGraph messages 缓存。"""
        if projection is None:
            projection = await self.project(
                thread_id, exclude_record_id=exclude_record_id
            )
        elif exclude_record_id is not None:
            raise ContextProjectionError("PROJECTION_PRECOMPUTED_EXCLUSION_CONFLICT")
        await agent.aupdate_state(
            # CompiledStateGraph 把非空 namespace 解释为子图；project 隔离
            # 由 ProjectScopedAsyncSqliteSaver 在根图写入时统一补齐。
            {"configurable": {"thread_id": thread_id}},
            {"messages": self.cache_rewrite(projection.messages)},
            as_node="model",
        )
        return projection

    @staticmethod
    def cache_rewrite(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        """生成 reducer 所需的全量缓存替换，不改动 Transcript。"""
        validate_atomic_message_groups(messages)
        return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]

    async def _message_from_record(
        self, record: "TranscriptRecord"
    ) -> BaseMessage | None:
        """严格校验 Transcript payload 与 Artifact，不从旧缓存补事实。"""
        if record.kind == "context":
            return None
        content = record.payload.get("content")
        if not isinstance(content, str):
            raise ContextProjectionError("PROJECTION_TRANSCRIPT_CONTENT_INVALID")
        if record.artifact_id is not None:
            artifact = await self._persistence.load_context_artifact(
                record.thread_id, record.artifact_id
            )
            if artifact is None:
                raise ContextProjectionError("PROJECTION_TRANSCRIPT_ARTIFACT_MISSING")
            content = artifact.content
        encoded = content.encode("utf-8")
        if (
            hashlib.sha256(encoded).hexdigest() != record.content_sha256
            or len(encoded) != record.byte_length
        ):
            raise ContextProjectionError("PROJECTION_TRANSCRIPT_DIGEST_MISMATCH")
        if record.kind == "user":
            return HumanMessage(content=content, id=record.record_id)
        if record.kind == "assistant":
            raw_calls = record.payload.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise ContextProjectionError("PROJECTION_TOOL_CALLS_INVALID")
            tool_calls = [_decode_transcript_tool_call(call) for call in raw_calls]
            return AIMessage(content=content, tool_calls=tool_calls, id=record.record_id)
        if record.kind == "tool":
            if record.payload.get("legacy_invalid_fields"):
                raise ContextProjectionError("PROJECTION_TOOL_RESULT_IDENTITY_INVALID")
            if record.payload.get("tool_call_id_status") == "unmatched":
                raise ContextProjectionError("PROJECTION_TOOL_RESULT_UNMATCHED")
            call_id = record.payload.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ContextProjectionError("PROJECTION_TOOL_RESULT_ID_INVALID")
            status = record.payload.get("status", "success")
            if status not in {"success", "error"}:
                raise ContextProjectionError("PROJECTION_TOOL_RESULT_STATUS_INVALID")
            name = record.payload.get("name", "tool")
            if not isinstance(name, str) or not name:
                raise ContextProjectionError("PROJECTION_TOOL_RESULT_NAME_INVALID")
            return ToolMessage(
                content=content,
                tool_call_id=call_id,
                name=name,
                status=status,
                id=record.record_id,
            )
        raise ContextProjectionError("PROJECTION_TRANSCRIPT_KIND_INVALID")


def encode_projected_messages(messages: Sequence[BaseMessage]) -> str:
    """以版本化严格 JSON 保存支持的 user/assistant/tool 消息。"""
    validate_atomic_message_groups(messages)
    records = [_message_record(message) for message in messages]
    try:
        return json.dumps(
            {"version": PROJECTED_MESSAGES_VERSION, "messages": records},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContextProjectionError("PROJECTION_MESSAGES_JSON_INVALID") from exc


def decode_projected_messages(encoded: str) -> tuple[BaseMessage, ...]:
    """拒绝损坏 JSON、未知版本和未知消息类型。"""
    try:
        value = strict_json_loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContextProjectionError("PROJECTION_MESSAGES_JSON_INVALID") from exc
    if (
        not isinstance(value, Mapping)
        or set(value) != {"version", "messages"}
        or value.get("version") != PROJECTED_MESSAGES_VERSION
    ):
        raise ContextProjectionError("PROJECTION_MESSAGES_VERSION_UNSUPPORTED")
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list):
        raise ContextProjectionError("PROJECTION_MESSAGES_INVALID")
    messages = tuple(_message_from_record(item) for item in raw_messages)
    validate_atomic_message_groups(messages)
    return messages


def validate_atomic_message_groups(messages: Sequence[BaseMessage]) -> None:
    """工具声明和结果必须连续、唯一且完整，禁止孤儿或残缺 tail。"""
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            if pending:
                raise ContextProjectionError("PROJECTION_TOOL_GROUP_INCOMPLETE")
            calls = getattr(message, "tool_calls", None) or ()
            invalid_calls = getattr(message, "invalid_tool_calls", None) or ()
            if invalid_calls:
                raise ContextProjectionError(
                    "PROJECTION_TOOL_CALL_ARGUMENTS_INVALID"
                )
            ids = [call.get("id") for call in calls if isinstance(call, Mapping)]
            if (
                len(ids) != len(calls)
                or any(not isinstance(call_id, str) or not call_id for call_id in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ContextProjectionError("PROJECTION_TOOL_CALL_ID_INVALID")
            pending.update(ids)
            continue
        if isinstance(message, ToolMessage):
            call_id = message.tool_call_id
            if not isinstance(call_id, str) or not call_id:
                raise ContextProjectionError("PROJECTION_TOOL_RESULT_ID_INVALID")
            if call_id not in pending:
                raise ContextProjectionError("PROJECTION_TOOL_RESULT_ORPHAN")
            pending.remove(call_id)
            continue
        if pending:
            raise ContextProjectionError("PROJECTION_TOOL_GROUP_INCOMPLETE")
        if not isinstance(message, HumanMessage):
            raise ContextProjectionError("PROJECTION_MESSAGE_TYPE_UNSUPPORTED")
    if pending:
        raise ContextProjectionError("PROJECTION_TOOL_GROUP_INCOMPLETE")


def source_digest(records: Sequence["TranscriptRecord"], sequence: int) -> str:
    """对检查点覆盖的 Transcript 前缀计算确定性摘要。"""
    covered = [record for record in records if record.sequence <= sequence]
    if (
        sequence < 0
        or [record.sequence for record in covered]
        != list(range(1, sequence + 1))
    ):
        raise ContextProjectionError("PROJECTION_SOURCE_SEQUENCE_INVALID")
    material = [
        {
            "record_id": record.record_id,
            "thread_id": record.thread_id,
            "run_id": record.run_id,
            "execution_id": record.execution_id,
            "sequence": record.sequence,
            "kind": record.kind,
            "payload": dict(record.payload),
            "content_sha256": record.content_sha256,
            "byte_length": record.byte_length,
            "artifact_id": record.artifact_id,
        }
        for record in covered
    ]
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def artifact_references(messages: Sequence[BaseMessage]) -> tuple[str, ...]:
    """按出现顺序提取投影中的虚拟 Artifact 引用。"""
    return tuple(
        dict.fromkeys(
            match
            for message in messages
            for match in _ARTIFACT_REFERENCE_PATTERN.findall(
                _content_text(message.content)
            )
        )
    )


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item
            if isinstance(item, str)
            else item.get("text", "")
            if isinstance(item, Mapping) and isinstance(item.get("text", ""), str)
            else ""
            for item in value
        )
    return ""


def _message_record(message: BaseMessage) -> dict[str, object]:
    content = _strict_content(message.content)
    base: dict[str, object] = {"content": content}
    if message.id is not None:
        if not isinstance(message.id, str) or not message.id:
            raise ContextProjectionError("PROJECTION_MESSAGE_ID_INVALID")
        base["id"] = message.id
    if isinstance(message, HumanMessage):
        return {"type": "user", **base}
    if isinstance(message, AIMessage):
        if message.invalid_tool_calls:
            raise ContextProjectionError("PROJECTION_TOOL_CALL_ARGUMENTS_INVALID")
        calls = []
        for call in message.tool_calls or ():
            if not isinstance(call, Mapping):
                raise ContextProjectionError("PROJECTION_TOOL_CALL_INVALID")
            calls.append(_encode_tool_call(call))
        return {"type": "assistant", **base, "tool_calls": calls}
    if isinstance(message, ToolMessage):
        status = getattr(message, "status", "success")
        if status not in {"success", "error"}:
            raise ContextProjectionError("PROJECTION_TOOL_RESULT_STATUS_INVALID")
        if not isinstance(message.tool_call_id, str) or not message.tool_call_id:
            raise ContextProjectionError("PROJECTION_TOOL_RESULT_ID_INVALID")
        if message.name is not None and (
            not isinstance(message.name, str) or not message.name
        ):
            raise ContextProjectionError("PROJECTION_TOOL_RESULT_NAME_INVALID")
        return {
            "type": "tool",
            **base,
            "tool_call_id": message.tool_call_id,
            "name": message.name or "tool",
            "status": status,
        }
    raise ContextProjectionError("PROJECTION_MESSAGE_TYPE_UNSUPPORTED")


def _message_from_record(value: object) -> BaseMessage:
    if not isinstance(value, Mapping):
        raise ContextProjectionError("PROJECTION_MESSAGE_INVALID")
    kind = value.get("type")
    allowed = {
        "user": {"type", "content", "id"},
        "assistant": {"type", "content", "id", "tool_calls"},
        "tool": {"type", "content", "id", "tool_call_id", "name", "status"},
    }.get(kind)
    if (
        allowed is None
        or not set(value).issubset(allowed)
        or not {"type", "content"}.issubset(value)
    ):
        raise ContextProjectionError("PROJECTION_MESSAGE_INVALID")
    content = _strict_content(value.get("content"))
    raw_message_id = value.get("id")
    if raw_message_id is not None and (
        not isinstance(raw_message_id, str) or not raw_message_id
    ):
        raise ContextProjectionError("PROJECTION_MESSAGE_ID_INVALID")
    message_id = raw_message_id
    if kind == "user":
        return HumanMessage(content=content, id=message_id)
    if kind == "assistant":
        calls = value.get("tool_calls", [])
        if not isinstance(calls, list):
            raise ContextProjectionError("PROJECTION_TOOL_CALLS_INVALID")
        return AIMessage(
            content=content,
            tool_calls=[_decode_tool_call(call) for call in calls],
            id=message_id,
        )
    if kind == "tool":
        call_id = value.get("tool_call_id")
        name = value.get("name", "tool")
        status = value.get("status", "success")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
            or status not in {"success", "error"}
        ):
            raise ContextProjectionError("PROJECTION_TOOL_RESULT_INVALID")
        return ToolMessage(
            content=content,
            tool_call_id=call_id,
            name=name,
            status=status,
            id=message_id,
        )
    raise ContextProjectionError("PROJECTION_MESSAGE_TYPE_UNSUPPORTED")


def _strict_content(value: object) -> str | list[str | dict[str, object]]:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        try:
            normalized = strict_json_loads(
                json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextProjectionError("PROJECTION_MESSAGE_CONTENT_INVALID") from exc
        if isinstance(normalized, list) and all(
            isinstance(item, (str, dict)) for item in normalized
        ):
            return normalized
    raise ContextProjectionError("PROJECTION_MESSAGE_CONTENT_INVALID")


def _encode_tool_call(call: Mapping[str, object]) -> dict[str, object]:
    if call.get("legacy_invalid_fields"):
        raise ContextProjectionError("PROJECTION_TOOL_CALL_IDENTITY_INVALID")
    call_id = call.get("id")
    name = call.get("name")
    if (
        not isinstance(call_id, str)
        or not call_id
        or not isinstance(name, str)
        or not name
    ):
        raise ContextProjectionError("PROJECTION_TOOL_CALL_INVALID")
    arguments_status = call.get("arguments_status")
    if arguments_status is not None and arguments_status != "valid":
        raise ContextProjectionError("PROJECTION_TOOL_CALL_ARGUMENTS_INVALID")
    if "args" not in call and "arguments" not in call:
        raise ContextProjectionError("PROJECTION_TOOL_CALL_ARGUMENTS_MISSING")
    raw_arguments = call["args"] if "args" in call else call["arguments"]
    try:
        arguments = strict_json_loads(
            json.dumps(raw_arguments, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContextProjectionError("PROJECTION_TOOL_CALL_ARGUMENTS_INVALID") from exc
    if not isinstance(arguments, dict):
        raise ContextProjectionError("PROJECTION_TOOL_CALL_ARGUMENTS_INVALID")
    return {"id": call_id, "name": name, "args": arguments, "type": "tool_call"}


def _decode_tool_call(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ContextProjectionError("PROJECTION_TOOL_CALL_INVALID")
    if (
        set(value) != {"id", "name", "args", "type"}
        or value.get("type") != "tool_call"
    ):
        raise ContextProjectionError("PROJECTION_TOOL_CALL_INVALID")
    return _encode_tool_call(value)


def _decode_transcript_tool_call(value: object) -> dict[str, object]:
    """Transcript 含参数校验元数据，只提取已证明的有效字段。"""
    if not isinstance(value, Mapping):
        raise ContextProjectionError("PROJECTION_TOOL_CALL_INVALID")
    return _encode_tool_call(value)


def strict_json_loads(encoded: str) -> object:
    """严格解析持久化 JSON，拒绝 Python decoder 默认接受的非有限常量。"""

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant: {value}")

    return json.loads(encoded, parse_constant=reject_constant)
