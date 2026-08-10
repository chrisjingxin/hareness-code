"""Snapshot 文件工具的窗口渲染、大小限制和稳定错误响应。"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage

from harness_agent.threads.text_backend import TextDocument
from harness_agent.tools.file_mutation import (
    CommittedFileMutation,
    MutationChangedRange,
    PreparedFileMutation,
)
from harness_agent.tools.file_tools import FileToolContractError
from harness_agent.tools.snapshot_file_schema import MAX_READ_LIMIT

MAX_READ_RESULT_BYTES = 32 * 1024
MAX_EDIT_TEXT_BYTES = 64 * 1024
MAX_MUTATION_BYTES = 2 * 1024 * 1024
LOCAL_CONTEXT_PADDING_LINES = 3


def require_actual_document(document: TextDocument | None) -> TextDocument:
    """确保 mutation commit 返回可重新读取的真实文本版本。"""
    if document is None:
        raise FileToolContractError("POST_WRITE_DRIFT")
    return document


def attach_post_write_drift(
    payload: dict[str, Any], committed: CommittedFileMutation, plan: PreparedFileMutation
) -> None:
    """返回批准版本与实际版本的两段有界 diff。"""
    if committed.drift is None:
        return
    payload["warning"] = {
        "code": "POST_WRITE_DRIFT",
        "message": "提交后实际文件内容与已批准内容不同，请使用返回的实际 Snapshot。",
        "proposed_diff": plan.diff.text,
        "actual_diff": committed.drift.text,
        "added_lines": committed.drift.added_lines,
        "removed_lines": committed.drift.removed_lines,
        "truncated": plan.diff.truncated or committed.drift.truncated,
    }


def render_changed_context(
    document: TextDocument, changed_range: MutationChangedRange
) -> tuple[str, int, int, bool, bool]:
    """以实际新版本行号返回变更附近的有界窗口。"""
    line_count = len(document.content.splitlines(keepends=True))
    changed_start = min(max(changed_range.start_line - 1, 0), line_count)
    changed_end = min(max(changed_range.end_line, changed_start), line_count)
    offset = max(changed_start - LOCAL_CONTEXT_PADDING_LINES, 0)
    desired_end = min(changed_end + LOCAL_CONTEXT_PADDING_LINES, line_count)
    desired_limit = max(desired_end - offset, 0)
    limit = min(desired_limit, MAX_READ_LIMIT)
    rendered, shown_count, byte_truncated = render_window(document.content, offset, limit)
    return rendered, offset, shown_count, byte_truncated or desired_limit > limit, not byte_truncated


def tool_payload(message: ToolMessage) -> dict[str, Any] | None:
    """只解析本 contract 生成的 JSON payload。"""
    try:
        payload = json.loads(str(message.content))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def observation_path(value: object) -> str:
    """将 virtual path 投影为有限相对日志字段。"""
    if not isinstance(value, str):
        return "-"
    relative = value.lstrip("/").replace("\n", "?").replace("\r", "?")
    return relative[:256] or "."


def render_window(content: str, offset: int, limit: int) -> tuple[str, int, bool]:
    """返回带普通源行号的局部文本；字节截断时不授予 seen range。"""
    lines = content.splitlines(keepends=True)
    selected = lines[offset : offset + limit]
    rendered = "\n".join(
        f"{offset + index + 1}\t{line.rstrip(chr(13) + chr(10))}"
        for index, line in enumerate(selected)
    )
    encoded = rendered.encode("utf-8")
    if len(encoded) <= MAX_READ_RESULT_BYTES:
        return rendered, len(selected), False
    prefix = encoded[:MAX_READ_RESULT_BYTES].decode("utf-8", errors="ignore")
    return f"{prefix}\n[输出因字节上限截断；此窗口不可用于编辑]", 0, True


def shown_lines(offset: int, count: int) -> dict[str, int | None]:
    """以 1-based source line 报告实际放入 context 的区间。"""
    if count == 0:
        return {"start_line": None, "end_line": None}
    return {"start_line": offset + 1, "end_line": offset + count}


def normalize_line_endings(value: str, document: TextDocument) -> str:
    """把模型复制的换行统一回实际文件换行风格。"""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if document.identity.line_ending == "crlf":
        return normalized.replace("\n", "\r\n")
    if document.identity.line_ending == "cr":
        return normalized.replace("\n", "\r")
    return normalized


def source_line_range(content: str, start: int, old_string: str) -> tuple[int, int]:
    """计算唯一 exact match 覆盖的 0-based 半开源行区间。"""
    start_line = _logical_line_break_count(content[:start])
    touched = _logical_line_break_count(old_string)
    if not old_string.endswith(("\n", "\r")):
        touched += 1
    return start_line, start_line + max(1, touched)


def _logical_line_break_count(value: str) -> int:
    """统计 CRLF、LF 与 CR 逻辑换行数，CRLF 只计一次。"""
    return value.count("\n") + value.replace("\r\n", "").count("\r")


def require_text_size(value: str) -> None:
    """限制单个 exact-string 参数。"""
    if len(value.encode("utf-8")) > MAX_EDIT_TEXT_BYTES:
        raise FileToolContractError("EDIT_TEXT_TOO_LARGE")


def require_mutation_text_size(value: str) -> None:
    """限制创建或替换后的完整文本。"""
    if len(value.encode("utf-8")) > MAX_MUTATION_BYTES:
        raise FileToolContractError("EDIT_TEXT_TOO_LARGE")


def require_mutation_size(document: TextDocument) -> None:
    """拒绝超过完整 mutation 上限的文件。"""
    if document.identity.byte_length > MAX_MUTATION_BYTES:
        raise FileToolContractError("UNSUPPORTED_FILE_TYPE")


def message(
    name: str, tool_call_id: str, payload: dict[str, Any], *, status: str = "success"
) -> ToolMessage:
    """序列化稳定工具 JSON，不把内部异常带进模型上下文。"""
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        name=name,
        tool_call_id=tool_call_id,
        status=status,
    )


def error_code(exc: Exception) -> str:
    """将受控 adapter/store 错误映射为公开稳定 code。"""
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        return "COMMIT_FAILED"
    return {
        "TEXT_ENCODING_UNSUPPORTED": "UNSUPPORTED_FILE_TYPE",
        "TEXT_NEWLINE_MIXED": "UNSUPPORTED_FILE_TYPE",
        "BACKEND_CREATE_FAILED": "COMMIT_FAILED",
        "BACKEND_REPLACE_FAILED": "COMMIT_FAILED",
        "BACKEND_DELETE_FAILED": "COMMIT_FAILED",
        "BACKEND_READ_FAILED": "COMMIT_FAILED",
        "BACKEND_TEXT_UNSUPPORTED": "BACKEND_CAS_UNSUPPORTED",
        "BACKEND_READ_UNSUPPORTED": "BACKEND_CAS_UNSUPPORTED",
        "BACKEND_DOCUMENT_INVALID": "BACKEND_CAS_UNSUPPORTED",
        "IS_DIRECTORY": "UNSUPPORTED_FILE_TYPE",
    }.get(code, code)


def error_message(code: str) -> str:
    """提供短、不会暴露文件正文或后端细节的中文错误说明。"""
    messages = {
        "SNAPSHOT_REQUIRED": "修改或删除必须先读取文件并提供 Snapshot。",
        "SNAPSHOT_EXPIRED": "Snapshot 已过期。",
        "SNAPSHOT_SCOPE_MISMATCH": "Snapshot 不属于当前 Thread、路径或 backend。",
        "STALE_FILE": "文件在读取后已变化。",
        "UNREAD_RANGE": "目标文本不完全位于已读取的源行范围。",
        "FILE_ALREADY_EXISTS": "目标文件已存在，write_file 不会覆盖。",
        "FILE_NOT_FOUND": "目标文件不存在。",
        "UNSUPPORTED_FILE_TYPE": "文件不是可安全修改的受支持文本。",
        "NO_CHANGES": "替换后内容没有变化。",
        "COMMIT_CONFLICT": "提交前文件已变化。",
        "BACKEND_CAS_UNSUPPORTED": "当前 backend 不支持安全的 compare-and-replace。",
        "POST_WRITE_DRIFT": "提交后文件被外部保存钩子改变，无法生成可用 Snapshot。",
        "PATH_SYMLINK_UNSUPPORTED": "文件路径不能包含符号链接。",
        "EXACT_MATCH_NOT_FOUND": "已读原文本不再存在。",
        "AMBIGUOUS_MATCH": "old_string 在文件中不是唯一匹配。",
        "VIRTUAL_READONLY": "虚拟文件只允许读取。",
        "FILE_TOOL_SCHEMA_INVALID": "文件工具参数不属于当前 canonical schema。",
    }
    return messages.get(code, "文件工具无法安全完成该操作。")


def next_action(code: str) -> str:
    """每个公开错误仅给出一个确定的恢复动作。"""
    if code in {"SNAPSHOT_REQUIRED", "SNAPSHOT_EXPIRED", "STALE_FILE", "UNREAD_RANGE"}:
        return "重新读取受影响文件或区间后重试。"
    if code == "SNAPSHOT_SCOPE_MISMATCH":
        return "在当前 Thread 重新读取目标路径。"
    if code == "FILE_ALREADY_EXISTS":
        return "先读取现有文件，再使用 edit_file。"
    if code in {"EXACT_MATCH_NOT_FOUND", "AMBIGUOUS_MATCH"}:
        return "重新读取目标区间并提交唯一的 old_string。"
    if code == "BACKEND_CAS_UNSUPPORTED":
        return "切换到支持安全文本 mutation 的执行模式。"
    return "检查参数后重试。"


__all__ = [
    "attach_post_write_drift",
    "error_code",
    "error_message",
    "message",
    "next_action",
    "normalize_line_endings",
    "observation_path",
    "render_changed_context",
    "render_window",
    "require_actual_document",
    "require_mutation_size",
    "require_mutation_text_size",
    "require_text_size",
    "shown_lines",
    "source_line_range",
    "tool_payload",
]
