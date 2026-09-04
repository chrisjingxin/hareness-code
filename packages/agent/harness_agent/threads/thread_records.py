"""Thread/Transcript 记录模型与纯转换逻辑。

本模块不持有 SQLite 连接、事务或迁移生命周期；``thread_persistence`` 继续
兼容导出这些对象，调用方无需随内部拆分迁移。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from harness_agent.compose.models import ThreadMode
from harness_agent.runtime.execution_binding import RunExecutionBinding
from harness_agent.threads.context_lifecycle import RunContextSnapshot
from harness_agent.threads.context_projection import (
    CompressionCheckpoint,
    CompressionCheckpointDraft,
    strict_json_loads,
)
from harness_agent.threads.runtime_state import RuntimeStateSnapshot

_MAX_PREVIEW_CHARS = 160
_MAX_INLINE_TOOL_BYTES = 64 * 1024
_TRANSCRIPT_KINDS = ("user", "assistant", "tool", "context")


class ThreadPersistenceError(RuntimeError):
    """线程存储不可用、损坏或版本不兼容时返回的可诊断错误。"""


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """恢复选择器所需的当前 project 线程摘要；内部 ID 不应直接展示给用户。"""

    thread_id: str
    created_at_ms: int
    updated_at_ms: int
    first_message: str
    latest_message: str
    message_count: int


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """由规范记录归一化出的稳定消息历史，供 CLI 表现层回放。"""

    kind: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None
    created_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class OpenThread:
    """已校验归属 project 的线程快照和可回放消息。"""

    summary: ThreadSummary
    messages: tuple[ThreadMessage, ...]
    legacy_incomplete_history: bool = False
    # Compose 有界 activity 审计；不进入模型 context，仅供 Timeline 恢复。
    compose_activities: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """当前 Thread 的 checkpoint 消息与 Context 熔断状态。"""

    messages: tuple[Any, ...]
    state: ContextState
    recoverable: bool


@dataclass(frozen=True, slots=True)
class ContextArtifact:
    """仅当前 project/thread 可见的不可变会话归档。"""

    artifact_id: str
    kind: str
    content: str
    source_start: int
    source_end: int
    created_at_ms: int
    content_sha256: str = ""
    byte_length: int = 0


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """一次上下文重写的结构化摘要和来源范围。"""

    rewrite_version: str
    content: str
    source_start: int
    source_end: int
    artifact_ids: tuple[str, ...]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ContextState:
    """自动压缩熔断和最近一次真实运行态。"""

    failures: int = 0
    circuit_open: bool = False
    last_action: str = "none"
    runtime_state: RuntimeStateSnapshot | None = None


@dataclass(frozen=True, slots=True)
class AcceptRun:
    """受理 Run 所需的完整领域输入；SQLite 不再接收散落字段。"""

    message: str
    binding: RunExecutionBinding
    context_snapshot: RunContextSnapshot | None = None
    mode: ThreadMode = ThreadMode.BUILD


@dataclass(frozen=True, slots=True)
class TranscriptAppend:
    """追加一条完整语义记录所需的 typed 输入。"""

    thread_id: str
    record_id: str
    kind: Literal["user", "assistant", "tool", "context"]
    content: str
    run_id: str | None = None
    execution_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_call_id_status: str | None = None
    legacy_invalid_fields: tuple[str, ...] = ()
    tool_calls: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """一条追加且可审计的 Thread 规范记录。"""

    record_id: str
    thread_id: str
    run_id: str | None
    execution_id: str | None
    sequence: int
    kind: Literal["user", "assistant", "tool", "context"]
    payload: Mapping[str, object]
    content_sha256: str
    byte_length: int
    artifact_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class RunAcceptance:
    """Run 受理结果；``created=False`` 表示同一请求的幂等重试。"""

    created: bool
    binding: RunExecutionBinding


@dataclass(frozen=True, slots=True)
class ContextArtifactDraft:
    """待写入 Context 归档的领域值，不包含存储生成的 artifact ID。"""

    kind: str
    content: str
    source_start: int = 0
    source_end: int = 0
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextSummaryDraft:
    """待写入摘要；``artifact_indexes`` 引用同一事务中的归档草稿。"""

    rewrite_version: str
    content: str
    source_start: int
    source_end: int
    artifact_indexes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitContextRewrite:
    """一次 Context 状态转换，归档、摘要和熔断状态同成同败。"""

    thread_id: str
    artifacts: tuple[ContextArtifactDraft, ...] = ()
    summary: ContextSummaryDraft | None = None
    state: ContextState | None = None
    checkpoint: CompressionCheckpointDraft | None = None


@dataclass(frozen=True, slots=True)
class ContextCommit:
    """Context 状态转换提交后的 typed 结果。"""

    artifacts: tuple[ContextArtifact, ...] = ()
    summary: ContextSummary | None = None
    state: ContextState | None = None
    checkpoint: CompressionCheckpoint | None = None


def workspace_fingerprint(project: Path) -> str:
    """从规范化 project 路径生成不可逆 namespace，禁止原始路径进入数据库。"""
    return hashlib.sha256(str(project.expanduser().resolve()).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    """延迟导入时间模块，保持路径和数据转换函数的纯粹性。"""
    import time

    return int(time.time() * 1000)


def _preview(value: str) -> str:
    """将用户消息压缩为单行有限摘要，避免选择器被超长或换行文本破坏。"""
    compact = " ".join(value.split())
    return compact[:_MAX_PREVIEW_CHARS] or "(空消息)"


def _summary(row: Mapping[str, Any]) -> ThreadSummary:
    """将 SQLite 行转换为不携带 project 路径的线程摘要。"""
    return ThreadSummary(
        thread_id=str(row["thread_id"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        first_message=str(row["first_message"]),
        latest_message=str(row["latest_message"]),
        message_count=int(row["message_count"]),
    )


def _content_sha256(content: str) -> str:
    """计算 UTF-8 内容摘要，避免把原文复制进诊断或索引。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _strict_json(value: object) -> str:
    """以严格确定 JSON 保存投影元数据，拒绝 NaN 和隐式转字符串。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _rewrite_artifact_id(
        project_fingerprint: str,
        thread_id: str,
        checkpoint_id: str,
        index: int,
        draft: ContextArtifactDraft,
) -> str:
    """为带 checkpoint 的无 ID Artifact 生成可跨进程重试的稳定 ID。"""
    material = _strict_json(
        {
            "project_fingerprint": project_fingerprint,
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
            "index": index,
            "kind": draft.kind,
            "content": draft.content,
            "source_start": max(0, draft.source_start),
            "source_end": max(max(0, draft.source_start), draft.source_end),
        }
    )
    return f"{draft.kind}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _context_commit_payload(
        thread_id: str,
        artifacts: list[ContextArtifact],
        summary: ContextSummary | None,
        state: ContextState | None,
        checkpoint: CompressionCheckpointDraft,
        projected_messages: str,
        source_record_sequence: int,
        source_digest_value: str,
) -> str:
    """编码整个 CommitContextRewrite 的稳定语义，排除生成时间。"""
    return _strict_json(
        {
            "version": 1,
            "thread_id": thread_id,
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "kind": artifact.kind,
                    "content": artifact.content,
                    "source_start": artifact.source_start,
                    "source_end": artifact.source_end,
                    "content_sha256": artifact.content_sha256,
                    "byte_length": artifact.byte_length,
                }
                for artifact in artifacts
            ],
            "summary": (
                None
                if summary is None
                else {
                    "rewrite_version": summary.rewrite_version,
                    "content": summary.content,
                    "source_start": summary.source_start,
                    "source_end": summary.source_end,
                    "artifact_ids": summary.artifact_ids,
                }
            ),
            "state": (
                None
                if state is None
                else {
                    "failures": state.failures,
                    "circuit_open": state.circuit_open,
                    "last_action": state.last_action,
                    "runtime_state": (
                        state.runtime_state.record()
                        if state.runtime_state is not None
                        else None
                    ),
                }
            ),
            "checkpoint": {
                "checkpoint_id": checkpoint.checkpoint_id,
                "source_record_sequence": source_record_sequence,
                "source_digest": source_digest_value,
                "mode": checkpoint.mode,
                "rewrite_version": checkpoint.rewrite_version,
                "projected_messages": projected_messages,
                "artifact_ids": checkpoint.artifact_ids,
                "trigger": checkpoint.trigger,
                "pressure_before": dict(checkpoint.pressure_before),
                "pressure_after": dict(checkpoint.pressure_after),
                "legacy_incomplete": checkpoint.legacy_incomplete,
            },
        }
    )


def _context_artifact_from_row(row: Mapping[str, Any]) -> ContextArtifact:
    return ContextArtifact(
        artifact_id=str(row["artifact_id"]),
        kind=str(row["kind"]),
        content=str(row["content"]),
        source_start=int(row["source_start"]),
        source_end=int(row["source_end"]),
        created_at_ms=int(row["created_at_ms"]),
        content_sha256=str(row["content_sha256"] or ""),
        byte_length=int(row["byte_length"] or 0),
    )


def _user_record_id(run_id: str) -> str:
    """为新 Run 生成稳定用户记录身份。"""
    return f"run:{run_id}:user"


def _root_execution_id(run_id: str) -> str:
    """复用 RunCoordinator 的根 execution 命名，不为 legacy 数据补身份。"""
    return f"root-{run_id}"


def _transcript_artifact_id(
        project_fingerprint: str, thread_id: str, record_id: str
) -> str:
    """生成不含路径和原文的确定性 Transcript Artifact ID。"""
    seed = f"{project_fingerprint}:{thread_id}:{record_id}".encode("utf-8")
    return f"transcript-{hashlib.sha256(seed).hexdigest()[:32]}"


def _legacy_record_id(project_fingerprint: str, thread_id: str, sequence: int) -> str:
    """为 v6 可证明的 checkpoint 消息生成无 Run 身份的记录 ID。"""
    seed = f"legacy:{project_fingerprint}:{thread_id}:{sequence}".encode("utf-8")
    return f"legacy-{hashlib.sha256(seed).hexdigest()[:32]}"


def _payload_content(payload: str | Mapping[str, object]) -> str:
    """从规范 payload 读取选择器所需的可见内容。"""
    value: object
    if isinstance(payload, str):
        try:
            decoded = strict_json_loads(payload)
        except (ValueError, json.JSONDecodeError):
            return payload
        value = decoded
    else:
        value = payload
    if isinstance(value, Mapping):
        content = value.get("content")
        return content if isinstance(content, str) else str(content or "")
    return ""


def _normalize_transcript_tool_calls(
        tool_calls: tuple[Mapping[str, object], ...],
        *,
        allow_legacy_invalid: bool = False,
) -> list[dict[str, object]]:
    """规范化 assistant tool calls，使幂等比较不依赖调用方字典顺序。"""
    normalized: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for call in tool_calls:
        if not isinstance(call, Mapping):
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
        legacy_invalid_fields = call.get("legacy_invalid_fields")
        if legacy_invalid_fields is not None:
            if not allow_legacy_invalid:
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
            if (
                    not isinstance(legacy_invalid_fields, (list, tuple))
                    or not legacy_invalid_fields
                    or not all(
                isinstance(field, str) and field
                for field in legacy_invalid_fields
            )
            ):
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
            try:
                canonical_invalid = strict_json_loads(
                    json.dumps(
                        dict(call),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID") from exc
            if not isinstance(canonical_invalid, dict):
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
            canonical_invalid["arguments_status"] = "invalid"
            normalized.append(canonical_invalid)
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ID_INVALID")
        if call_id in seen_ids:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ID_DUPLICATE")
        seen_ids.add(call_id)
        name = call.get("name", "tool")
        if not isinstance(name, str) or not name:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_NAME_INVALID")
        try:
            canonical = strict_json_loads(
                json.dumps(
                    dict(call),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID") from exc
        if not isinstance(canonical, dict):
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
        canonical["id"] = call_id
        canonical["name"] = name
        if "arguments_status" not in canonical:
            canonical["arguments_status"] = (
                "valid"
                if "arguments" in canonical or "args" in canonical
                else "unavailable"
            )
        if canonical["arguments_status"] == "valid" and not (
                "arguments" in canonical or "args" in canonical
        ):
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ARGUMENTS_MISSING")
        if canonical["arguments_status"] == "valid":
            arguments = (
                canonical["args"] if "args" in canonical else canonical["arguments"]
            )
            if not isinstance(arguments, Mapping):
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ARGUMENTS_INVALID")
        normalized.append(canonical)
    return normalized


def _transcript_record(row: Mapping[str, Any]) -> TranscriptRecord:
    """将 SQLite 行解析为不暴露表名的 typed Transcript。"""
    payload = strict_json_loads(str(row["payload"]))
    if not isinstance(payload, Mapping):
        raise ThreadPersistenceError("TRANSCRIPT_PAYLOAD_INVALID")
    kind = str(row["kind"])
    if kind not in _TRANSCRIPT_KINDS:
        raise ThreadPersistenceError("TRANSCRIPT_KIND_INVALID")
    return TranscriptRecord(
        record_id=str(row["record_id"]),
        thread_id=str(row["thread_id"]),
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        execution_id=(
            str(row["execution_id"]) if row["execution_id"] is not None else None
        ),
        sequence=int(row["sequence"]),
        kind=kind,  # type: ignore[arg-type]
        payload=dict(payload),
        content_sha256=str(row["content_sha256"]),
        byte_length=int(row["byte_length"]),
        artifact_id=str(row["artifact_id"]) if row["artifact_id"] is not None else None,
        created_at_ms=int(row["created_at_ms"]),
    )


def _transcript_matches(
        record: TranscriptRecord,
        command: TranscriptAppend,
        *,
        project_fingerprint: str,
        allow_legacy_invalid: bool = False,
) -> bool:
    """判断重复追加是否是同一语义，而不是吞掉 Run ID 冲突。"""
    content_bytes = command.content.encode("utf-8")
    expected_artifact_id = (
        _transcript_artifact_id(
            project_fingerprint, command.thread_id, command.record_id
        )
        if command.kind == "tool" and len(content_bytes) > _MAX_INLINE_TOOL_BYTES
        else None
    )
    expected_content = (
        _preview(command.content)
        if command.kind == "tool" and len(content_bytes) > _MAX_INLINE_TOOL_BYTES
        else command.content
    )
    payload = record.payload
    if (
            record.thread_id != command.thread_id
            or record.run_id != command.run_id
            or record.execution_id != command.execution_id
            or record.kind != command.kind
            or record.content_sha256 != _content_sha256(command.content)
            or record.byte_length != len(content_bytes)
            or payload.get("content") != expected_content
            or payload.get("content_sha256") != record.content_sha256
            or payload.get("original_bytes") != record.byte_length
    ):
        return False
    if command.kind != "tool":
        if record.artifact_id is not None:
            return False
        try:
            expected_tool_calls = _normalize_transcript_tool_calls(
                command.tool_calls, allow_legacy_invalid=allow_legacy_invalid
            )
        except ThreadPersistenceError:
            return False
        return payload.get("tool_calls", []) == expected_tool_calls
    return (
            payload.get("tool_call_id") == (command.tool_call_id or command.record_id)
            and (
                    ("name" not in payload and "name" in command.legacy_invalid_fields)
                    or payload.get("name") == (command.tool_name or "tool")
            )
            and (
                    ("status" not in payload and "status" in command.legacy_invalid_fields)
                    or payload.get("status") == (command.tool_status or "success")
            )
            and payload.get("tool_call_id_status") == command.tool_call_id_status
            and payload.get("legacy_invalid_fields", [])
            == list(command.legacy_invalid_fields)
            and record.artifact_id == expected_artifact_id
    )


def _thread_message_from_transcript(record: TranscriptRecord) -> ThreadMessage | None:
    """将 Transcript 的可见 payload 映射为不变的 v3 ThreadMessage。"""
    if record.kind not in {"user", "assistant", "tool"}:
        return None
    content = record.payload.get("content")
    if not isinstance(content, str):
        content = ""
    raw_tool_name = record.payload.get("name")
    tool_name = (
        raw_tool_name
        if record.kind == "tool"
           and isinstance(raw_tool_name, str)
           and raw_tool_name
        else None
    )
    return ThreadMessage(
        kind=record.kind,
        content=content,
        tool_name=tool_name,
        created_at_ms=record.created_at_ms,
    )  # type: ignore[arg-type]


def _checkpoint_messages(checkpoint: Mapping[str, Any] | Any) -> list[Any] | None:
    """从非增量 LangGraph checkpoint 读取消息 channel；DeltaChannel 返回 None 交给回放。"""
    if not isinstance(checkpoint, Mapping):
        return None
    channels = checkpoint.get("channel_values")
    if not isinstance(channels, Mapping):
        return None
    messages = channels.get("messages")
    return list(messages) if isinstance(messages, list) else None


def _replay_delta_messages(history: Mapping[str, Any]) -> list[Any]:
    """使用 DeepAgents 的确定性 reducer 回放 DeltaChannel seed 和历史 writes。"""
    entry = history.get("messages")
    if not isinstance(entry, Mapping):
        return []
    seed = entry.get("seed")
    seed_messages = getattr(seed, "value", seed)
    base = list(seed_messages) if isinstance(seed_messages, list) else []
    writes = entry.get("writes")
    values = [write[2] for write in writes if isinstance(write, tuple) and len(write) >= 3] if isinstance(writes,
                                                                                                          list) else []
    if not values:
        return base
    from deepagents._messages_reducer import _messages_delta_reducer

    return list(_messages_delta_reducer(base, values))


def _normalize_message(value: Any) -> ThreadMessage | None:
    """把 LangChain 消息收敛为 TUI 可安全回放的 project/thread/message 领域值。"""
    name = type(value).__name__
    content = _message_content(getattr(value, "content", ""))
    if name == "HumanMessage":
        return ThreadMessage(kind="user", content=content)
    if name == "AIMessage":
        return ThreadMessage(kind="assistant", content=content)
    if name == "ToolMessage":
        raw_tool_name = getattr(value, "name", None)
        return ThreadMessage(
            kind="tool",
            content=content,
            tool_name=(
                raw_tool_name
                if isinstance(raw_tool_name, str) and raw_tool_name
                else None
            ),
        )
    return None


def _legacy_tool_calls(
        value: Any,
        *,
        project_fingerprint: str,
        thread_id: str,
        sequence: int,
) -> tuple[Mapping[str, object], ...]:
    """保留 v6 checkpoint 中 AI tool call 的可证明字段，不读取 Tool 结果猜参数。"""
    if type(value).__name__ != "AIMessage":
        return ()
    raw_calls = [
        call
        for call in (getattr(value, "tool_calls", None) or ())
        if isinstance(call, Mapping)
    ]
    # LangChain places calls whose JSON arguments could not be decoded in
    # ``invalid_tool_calls``.  They are still checkpoint facts and must remain
    # visible as raw/invalid typed payload rather than disappearing.
    raw_calls.extend(
        call
        for call in (getattr(value, "invalid_tool_calls", None) or ())
        if isinstance(call, Mapping)
    )
    normalized: list[Mapping[str, object]] = []
    for index, call in enumerate(raw_calls):
        invalid_fields: list[str] = []
        raw_call_id = call.get("id")
        if isinstance(raw_call_id, str) and raw_call_id:
            call_id: str | None = raw_call_id
        elif raw_call_id is None or raw_call_id == "":
            call_id = _legacy_tool_call_id(
                project_fingerprint, thread_id, sequence, index
            )
        else:
            call_id = None
            invalid_fields.append("id")
        raw_name = call.get("name")
        normalized_call: dict[str, object] = {}
        if call_id is not None:
            normalized_call["id"] = call_id
        if isinstance(raw_name, str) and raw_name:
            normalized_call["name"] = raw_name
        else:
            invalid_fields.append("name")
        raw_type = call.get("type")
        if raw_type is not None:
            if isinstance(raw_type, str):
                normalized_call["type"] = raw_type
            else:
                invalid_fields.append("type")
        arguments = call.get("args", call.get("arguments"))
        _set_legacy_tool_call_arguments(normalized_call, arguments)
        raw_arguments_status = call.get("arguments_status")
        if raw_arguments_status is not None and raw_arguments_status != "valid":
            if isinstance(raw_arguments_status, str) and raw_arguments_status in {
                "invalid",
                "unavailable",
            }:
                normalized_call["arguments_status"] = raw_arguments_status
            else:
                normalized_call["arguments_status"] = "invalid"
                invalid_fields.append("arguments_status")
        if "error" in call:
            raw_error = call["error"]
            if isinstance(raw_error, str):
                # 保持旧语义：空字符串等同没有错误，非空字符串是可证明的错误事实。
                if raw_error:
                    normalized_call["arguments_error"] = raw_error
                    normalized_call["arguments_status"] = "invalid"
            elif raw_error is not None:
                normalized_call["arguments_error_type"] = type(raw_error).__name__
                invalid_fields.append("error")
                normalized_call["arguments_status"] = "invalid"
        if invalid_fields:
            normalized_call["legacy_invalid_fields"] = invalid_fields
            normalized_call["arguments_status"] = "invalid"
        normalized.append(normalized_call)
    return tuple(normalized)


def _set_legacy_tool_call_arguments(
        call: dict[str, object], value: object
) -> None:
    """将 legacy 参数转成严格 JSON，无法解析时保留 raw/invalid。"""
    if value is None:
        call["arguments_status"] = "unavailable"
        return
    if isinstance(value, str):
        call["arguments_raw"] = value
        if not value:
            call["arguments_status"] = "unavailable"
            return
        try:
            parsed = strict_json_loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            call["arguments_status"] = "invalid"
            call["arguments_error"] = type(exc).__name__
            return
    else:
        parsed = value
    try:
        encoded = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = strict_json_loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        call["arguments_raw"] = repr(value)
        call["arguments_status"] = "invalid"
        call["arguments_error"] = type(exc).__name__
        return
    if not isinstance(normalized, Mapping):
        call["arguments_status"] = "invalid"
        call["arguments_error"] = "ToolArgumentsObjectRequired"
        return
    call["arguments"] = dict(normalized)
    call["arguments_json"] = encoded
    call["arguments_status"] = "valid"


def _legacy_tool_call_id(
        project_fingerprint: str, thread_id: str, sequence: int, index: int
) -> str:
    """为 checkpoint 明确没有 call ID 的事实生成可重复的内部标识。"""
    seed = f"legacy-call:{project_fingerprint}:{thread_id}:{sequence}:{index}"
    return f"legacy-call-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _message_content(value: Any) -> str:
    """从 LangChain string 或内容块列表中提取稳定文本，避免原始对象越过模块边界。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    # 不支持的 legacy 对象不是可证明的文本事实；不要通过 str() 把它
    # 伪造成合法 Transcript，周围历史仍由 legacy_incomplete 标记说明边界。
    return ""
