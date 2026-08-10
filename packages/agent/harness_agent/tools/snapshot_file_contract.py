"""以 Snapshot prior-read 约束的 canonical 文件工具 contract。

ZC-133 的现有证据只支持 ``exact-string + prior-read``：模型必须先通过
``read_file`` 获得当前 Thread 的短 Snapshot，再以唯一 ``old_string`` 修改已读
内容。这里不保留 ``replace_all``，也不接收尚未通过真实模型门槛的 range/edits[]
候选参数。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import FileData, GlobResult, GrepResult, LsResult, ReadResult
from deepagents.backends.utils import (
    _get_file_type,
    check_empty_content,
    format_content_with_line_numbers,
    format_grep_matches,
    truncate_if_too_long,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.config import get_config

from harness_agent.threads.snapshots import (
    SnapshotRecord,
    ThreadSnapshotStore,
)
from harness_agent.threads.text_backend import (
    TextDocument,
    TextMutationBackend,
    TextMutationError,
)
from harness_agent.tools.file_mutation import (
    FileMutationService,
    MutationMetadata,
    PreparedFileMutation,
    mutation_fingerprint,
)
from harness_agent.tools.file_diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_ITEMS,
    diagnostics_timeout as _diagnostics_timeout,
    diagnostics_unavailable as _diagnostics_unavailable,
    elapsed_ms as _elapsed_ms,
    summarize_diagnostics as _summarize_diagnostics,
)
from harness_agent.tools.file_tool_metrics import FileToolMetrics
from harness_agent.tools.file_tools import (
    BUILTIN_FILE_TOOL_NAMES,
    HARNESS_FILE_TOOL_NAMES,
    FileToolContractError,
)
from harness_agent.tools.snapshot_file_schema import (
    CanonicalDeleteFileSchema,
    CanonicalEditFileSchema,
    CanonicalReadFileSchema,
    CanonicalWriteFileSchema,
    DEFAULT_READ_LIMIT,
    MAX_READ_LIMIT,
    create_file_tool_definitions,
    is_harness_virtual_path as _is_virtual,
    non_negative_int_argument as _non_negative_int,
    optional_path_argument as _optional_path,
    path_argument as _path,
    positive_int_argument as _positive_int,
    require_canonical_argument_keys as _require_canonical_argument_keys,
    snapshot_id_argument as _snapshot_id,
    string_argument as _string_arg,
)
from harness_agent.tools.snapshot_file_support import (
    attach_post_write_drift as _attach_post_write_drift,
    error_code as _error_code,
    error_message as _error_message,
    message as _message,
    next_action as _next_action,
    normalize_line_endings as _normalize_line_endings,
    observation_path as _observation_path,
    render_changed_context as _render_changed_context,
    render_window as _render_window,
    require_actual_document as _require_actual_document,
    require_mutation_size as _require_mutation_size,
    require_mutation_text_size as _require_mutation_text_size,
    require_text_size as _require_text_size,
    shown_lines as _shown_lines,
    source_line_range as _source_line_range,
    tool_payload as _tool_payload,
)

logger = logging.getLogger(__name__)

DEFAULT_DIAGNOSTICS_TIMEOUT_SECONDS = 1.0
"""提交后只读 diagnostics 的短超时，超时不能影响已成功写入。"""


class SnapshotFileToolContract:
    """单一 Harness 文件工具实现，强制 prior-read 与 compare-and-replace。"""

    def __init__(
        self,
        backend: Any,
        *,
        snapshot_store: ThreadSnapshotStore | None,
        text_backend: TextMutationBackend,
        diagnostics_provider: Callable[[str], Awaitable[Mapping[str, object]]] | None = None,
        diagnostics_timeout_seconds: float = DEFAULT_DIAGNOSTICS_TIMEOUT_SECONDS,
        metrics: FileToolMetrics | None = None,
    ) -> None:
        """绑定运行时 backend、可选 diagnostics 与 Host 聚合观测。"""
        if diagnostics_timeout_seconds <= 0:
            raise ValueError("DIAGNOSTICS_TIMEOUT_INVALID")
        self._backend = backend
        self._snapshot_store = snapshot_store or ThreadSnapshotStore()
        self._text_backend = text_backend
        self._mutation_service = FileMutationService(text_backend)
        self._diagnostics_provider = diagnostics_provider
        self._diagnostics_timeout_seconds = diagnostics_timeout_seconds
        self._metrics = metrics or FileToolMetrics()
        self._definitions = create_file_tool_definitions()

    @property
    def tool_definitions(self) -> Sequence[BaseTool | dict[str, Any]]:
        """返回主 Agent、子 Agent 共用的一套模型可见 schema。"""
        return self._definitions

    @property
    def handled_tool_names(self) -> frozenset[str]:
        """返回交给 Harness seam 执行的所有文件工具名。"""
        return HARNESS_FILE_TOOL_NAMES

    @property
    def registration_tools(self) -> Sequence[BaseTool | dict[str, Any]]:
        """只注册 DeepAgents 不提供的 delete_file placeholder。"""
        return tuple(tool for tool in self._definitions if getattr(tool, "name", "") not in BUILTIN_FILE_TOOL_NAMES)

    def dispatch(self, request: ToolCallRequest) -> ToolMessage:
        """同步执行一项 canonical 文件调用，并将失败收敛为稳定 JSON。"""
        message = self._dispatch(request)
        self._observe(message)
        return message

    def _dispatch(self, request: ToolCallRequest) -> ToolMessage:
        """执行同步 CAS 路径；异步入口会在此之后补充 diagnostics。"""
        name, args, tool_call_id = self._call_args(request)
        try:
            _require_canonical_argument_keys(name, args)
            if name == "read_file":
                return self._read_file(request, args, tool_call_id)
            if name == "write_file":
                return self._write_file(request, args, tool_call_id)
            if name == "edit_file":
                return self._edit_file(request, args, tool_call_id)
            if name == "delete_file":
                return self._delete_file(request, args, tool_call_id)
            if name == "ls":
                return self._ls(request, args, tool_call_id)
            if name == "glob":
                return self._glob(request, args, tool_call_id)
            if name == "grep":
                return self._grep(request, args, tool_call_id)
            raise FileToolContractError("FILE_TOOL_NAME_UNSUPPORTED")
        except Exception as exc:  # noqa: BLE001 - 工具边界不得泄露 backend 异常
            return self._error(name, tool_call_id, _error_code(exc))

    async def adispatch(self, request: ToolCallRequest) -> ToolMessage:
        """异步图在提交后限时读取 diagnostics，再记录一次最终结果。"""
        message = await asyncio.to_thread(self._dispatch, request)
        message = await self._attach_async_diagnostics(request, message)
        self._observe(message)
        return message

    def approval_preflight(self, request: ToolCallRequest) -> bool:
        """在 HITL 前准备 mutation；前置条件失败时不产生无法执行的审批。"""
        name = str(request.tool_call.get("name", ""))
        if name not in {"write_file", "edit_file", "delete_file"}:
            return True
        try:
            self._prepare_mutation(request)
        except Exception:  # noqa: BLE001 - 失败由实际 ToolMessage 统一说明
            return False
        return True

    def approval_description(self, tool_call: dict[str, Any], _state: Any, runtime: Any) -> str:
        """把同一份已准备计划的精确有界 diff 交给既有 HITL 审批 payload。"""
        request = type("ApprovalToolCallRequest", (), {"tool_call": tool_call, "runtime": runtime})()
        plan = self._prepare_mutation(request)
        return self._mutation_service.approval_description(plan)

    def _read_file(
        self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str
    ) -> ToolMessage:
        """读取完整 identity，但只把请求窗口放进模型上下文。"""
        path = _path(args, "file_path", "read_file")
        offset = _non_negative_int(args.get("offset", 0), "offset")
        limit = _positive_int(args.get("limit", DEFAULT_READ_LIMIT), "limit")
        if limit > MAX_READ_LIMIT:
            raise FileToolContractError("READ_LIMIT_EXCEEDED")
        if _is_virtual(path):
            return self._read_virtual(request, path, offset, limit, tool_call_id)
        if Path(path).suffix.lower() == ".ipynb":
            return self._read_without_snapshot(request, path, offset, limit, tool_call_id)
        try:
            document = self._text_backend.read_text_document(path)
        except TextMutationError as exc:
            if exc.code not in {"TEXT_ENCODING_UNSUPPORTED", "BACKEND_TEXT_UNSUPPORTED"}:
                raise
            return self._read_without_snapshot(request, path, offset, limit, tool_call_id)
        _require_mutation_size(document)
        rendered, shown_count, context_truncated = _render_window(document.content, offset, limit)
        # 超长输出的可见部分不足以证明模型读过一整源行，不能扩大 seen range。
        record_limit = shown_count if not context_truncated else 0
        record = self._record_snapshot(request, document, offset=offset, limit=record_limit)
        payload = {
            "ok": True,
            "path": path,
            "snapshot_id": record.snapshot_id,
            "shown_lines": _shown_lines(offset, shown_count),
            "total_lines": record.line_count,
            "line_count": record.line_count,
            "byte_length": record.byte_length,
            "content": rendered,
            "truncated": context_truncated,
        }
        return _message("read_file", tool_call_id, payload)

    def _read_without_snapshot(
        self,
        request: ToolCallRequest,
        path: str,
        offset: int,
        limit: int,
        tool_call_id: str,
    ) -> ToolMessage:
        """保留 Notebook 与 non-text 的普通读取，但绝不签发可写 Snapshot。"""
        result = self._resolve_backend(request).read(path, offset=offset, limit=limit)
        if isinstance(result, str):
            content = result
            encoding = "utf-8"
        elif isinstance(result, ReadResult):
            if result.error or result.file_data is None:
                raise FileToolContractError("BACKEND_READ_FAILED")
            content = str(result.file_data.get("content", ""))
            encoding = str(result.file_data.get("encoding", "utf-8"))
        else:
            raise FileToolContractError("BACKEND_READ_FAILED")

        file_type = _get_file_type(path)
        if encoding == "base64" or file_type != "text":
            block_type = file_type if file_type != "text" else "file"
            mime_type = mimetypes.guess_type("file" + Path(path).suffix)[0] or "application/octet-stream"
            return ToolMessage(
                content_blocks=[{"type": block_type, "base64": content, "mime_type": mime_type}],
                name="read_file",
                tool_call_id=tool_call_id,
                additional_kwargs={"read_file_path": path, "read_file_media_type": mime_type},
                status="success",
            )

        empty = check_empty_content(content)
        rendered = empty or format_content_with_line_numbers(content, start_line=offset + 1)
        payload = {
            "ok": True,
            "path": path,
            "snapshot_id": None,
            "shown_lines": _shown_lines(offset, len(content.splitlines())),
            "total_lines": None,
            "content": rendered,
            "truncated": False,
        }
        return _message("read_file", tool_call_id, payload)

    def _read_virtual(
        self,
        request: ToolCallRequest,
        path: str,
        offset: int,
        limit: int,
        tool_call_id: str,
    ) -> ToolMessage:
        """保留 /.harness 的只读 backend 行为，但绝不生成可写 Snapshot。"""
        result = self._resolve_backend(request).read(path, offset=offset, limit=limit)
        if isinstance(result, str):
            content = result
        elif isinstance(result, ReadResult):
            if result.error:
                raise FileToolContractError("BACKEND_READ_FAILED")
            data: FileData | None = result.file_data
            if data is None or not isinstance(data.get("content"), str):
                raise FileToolContractError("BACKEND_READ_FAILED")
            content = str(data["content"])
        else:
            raise FileToolContractError("BACKEND_READ_FAILED")
        payload = {
            "ok": True,
            "path": path,
            "snapshot_id": None,
            "shown_lines": _shown_lines(offset, len(content.splitlines())),
            "total_lines": None,
            "content": content,
            "truncated": False,
        }
        return _message("read_file", tool_call_id, payload)

    def _write_file(
        self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str
    ) -> ToolMessage:
        """提交 create-if-absent，并返回实际版本的首个可编辑局部窗口。"""
        plan = self._prepare_mutation(request)
        committed = self._mutation_service.commit(plan)
        document = _require_actual_document(committed.actual)
        _require_mutation_size(document)
        rendered, offset, shown_count, context_truncated, context_editable = _render_changed_context(
            document,
            committed.changed_range,
        )
        record = self._record_snapshot(
            request,
            document,
            offset=offset,
            limit=shown_count if context_editable else 0,
        )
        payload = {
            "ok": True,
            "path": plan.metadata.path,
            "created": True,
            "snapshot_id": record.snapshot_id,
            "changed_range": committed.changed_range.payload(),
            "shown_lines": _shown_lines(offset, shown_count),
            "total_lines": record.line_count,
            "line_count": record.line_count,
            "content": rendered,
            "truncated": context_truncated,
            "diagnostics": _diagnostics_unavailable(),
        }
        _attach_post_write_drift(payload, committed, plan)
        return _message("write_file", tool_call_id, payload)

    def _edit_file(
        self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str
    ) -> ToolMessage:
        """提交 exact replacement，并返回实际重读的变更范围与局部上下文。"""
        plan = self._prepare_mutation(request)
        committed = self._mutation_service.commit(plan)
        actual = _require_actual_document(committed.actual)
        _require_mutation_size(actual)
        rendered, offset, shown_count, context_truncated, context_editable = _render_changed_context(
            actual,
            committed.changed_range,
        )
        new_record = self._record_snapshot(
            request,
            actual,
            offset=offset,
            limit=shown_count if context_editable else 0,
        )
        payload = {
            "ok": True,
            "path": plan.metadata.path,
            "snapshot_id": new_record.snapshot_id,
            "replaced": 1,
            "changed_range": committed.changed_range.payload(),
            "shown_lines": _shown_lines(offset, shown_count),
            "total_lines": new_record.line_count,
            "line_count": new_record.line_count,
            "content": rendered,
            "truncated": context_truncated,
            "diagnostics": _diagnostics_unavailable(),
        }
        _attach_post_write_drift(payload, committed, plan)
        return _message("edit_file", tool_call_id, payload)

    def _delete_file(
        self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str
    ) -> ToolMessage:
        """提交已审批的 compare-delete，保存钩子重建文件时返回实际 Snapshot。"""
        plan = self._prepare_mutation(request)
        committed = self._mutation_service.commit(plan)
        path = plan.metadata.path
        invalidated = self._snapshot_store.invalidate_path(
            self._thread_id(request), path, self._text_backend.backend_id
        )
        payload: dict[str, Any] = {
            "ok": True,
            "path": path,
            "deleted": committed.actual is None,
            "invalidated_snapshots": invalidated,
        }
        if committed.actual is not None:
            _require_mutation_size(committed.actual)
            rendered, offset, shown_count, context_truncated, context_editable = _render_changed_context(
                committed.actual,
                committed.changed_range,
            )
            actual_record = self._record_snapshot(
                request,
                committed.actual,
                offset=offset,
                limit=shown_count if context_editable else 0,
            )
            payload["snapshot_id"] = actual_record.snapshot_id
            payload["shown_lines"] = _shown_lines(offset, shown_count)
            payload["total_lines"] = actual_record.line_count
            payload["content"] = rendered
            payload["truncated"] = context_truncated
        _attach_post_write_drift(payload, committed, plan)
        return _message("delete_file", tool_call_id, payload)

    async def _attach_async_diagnostics(
        self, request: ToolCallRequest, message: ToolMessage
    ) -> ToolMessage:
        """仅对已成功 create/edit 的实际文件追加有界只读 diagnostics 摘要。"""
        name = str(request.tool_call.get("name", ""))
        if name not in {"write_file", "edit_file"}:
            return message
        payload = _tool_payload(message)
        if payload is None or payload.get("ok") is not True:
            return message
        path = payload.get("path")
        if not isinstance(path, str):
            return message
        payload["diagnostics"] = await self._collect_diagnostics(path)
        return _message(name, str(request.tool_call.get("id") or "file-tool"), payload)

    async def _collect_diagnostics(self, path: str) -> dict[str, object]:
        """在固定短超时内调用可选 provider；任何失败都不影响已完成提交。"""
        provider = self._diagnostics_provider
        if provider is None:
            return _diagnostics_unavailable()
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                provider(path),
                timeout=self._diagnostics_timeout_seconds,
            )
        except TimeoutError:
            return _diagnostics_timeout(_elapsed_ms(started))
        except Exception:  # noqa: BLE001 - provider 细节不能进入模型结果或日志
            return _diagnostics_unavailable(_elapsed_ms(started))
        return _summarize_diagnostics(response, _elapsed_ms(started))

    def _observe(self, message: ToolMessage) -> None:
        """写入不含路径、正文、参数和完整 Snapshot ID 的 Host 聚合指标。"""
        name = str(getattr(message, "name", "filesystem"))
        content = str(message.content)
        payload = _tool_payload(message)
        if payload is None:
            self._metrics.record_result(name, ok=False, result_bytes=len(content.encode("utf-8")), error_code=None)
            return
        ok = payload.get("ok") is True
        error = payload.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        self._metrics.record_result(
            name,
            ok=ok,
            result_bytes=len(content.encode("utf-8")),
            error_code=error_code if isinstance(error_code, str) else None,
        )
        logger.info(
            "file_tool_observed tool=%s path=%s ok=%s code=%s result_bytes=%s",
            name,
            _observation_path(payload.get("path")),
            ok,
            error_code if isinstance(error_code, str) else "-",
            len(content.encode("utf-8")),
        )
        if name == "read_file" and ok:
            snapshot_id = payload.get("snapshot_id")
            self._metrics.record_read(snapshot_id if isinstance(snapshot_id, str) else None)
        diagnostics = payload.get("diagnostics")
        if isinstance(diagnostics, dict):
            status = diagnostics.get("status")
            latency_ms = diagnostics.get("latency_ms")
            self._metrics.record_diagnostics(
                status if isinstance(status, str) else "unavailable",
                float(latency_ms) if isinstance(latency_ms, (int, float)) else None,
            )

    def _prepare_mutation(self, request: ToolCallRequest) -> PreparedFileMutation:
        """把已通过 canonical schema 的工具调用转换为不含原始参数的提交计划。"""
        name, args, tool_call_id = self._call_args(request)
        _require_canonical_argument_keys(name, args)
        if name == "write_file":
            return self._prepare_write(request, args, tool_call_id)
        if name == "edit_file":
            return self._prepare_edit(request, args, tool_call_id)
        if name == "delete_file":
            return self._prepare_delete(request, args, tool_call_id)
        raise FileToolContractError("FILE_TOOL_NAME_UNSUPPORTED")

    def _prepare_write(
        self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str
    ) -> PreparedFileMutation:
        """确认目标当前不存在，再把 create 内容固定为可审批计划。"""
        path = _path(args, "file_path", "write_file")
        content = _string_arg(args, "content")
        _require_mutation_text_size(content)
        thread_id = self._thread_id(request)
        fingerprint = mutation_fingerprint("write_file", args)
        if cached := self._mutation_service.prepared(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            fingerprint=fingerprint,
        ):
            return cached
        if self._mutation_service.was_consumed(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            fingerprint=fingerprint,
        ):
            raise FileToolContractError("COMMIT_CONFLICT")
        try:
            existing = self._text_backend.read_text_document(path)
        except TextMutationError as exc:
            if exc.code != "FILE_NOT_FOUND":
                raise
        else:
            _require_mutation_size(existing)
            raise FileToolContractError("FILE_ALREADY_EXISTS")
        return self._mutation_service.prepare(
            metadata=MutationMetadata(
                operation="write",
                path=path,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
            ),
            current=None,
            proposed_content=content,
            fingerprint=fingerprint,
        )

    def _prepare_edit(
        self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str
    ) -> PreparedFileMutation:
        """验证 prior-read 唯一替换后，固定当前内容、拟议内容和 expected identity。"""
        path = _path(args, "file_path", "edit_file")
        snapshot_id = _snapshot_id(args)
        old_string = _string_arg(args, "old_string")
        new_string = _string_arg(args, "new_string")
        if not old_string:
            raise FileToolContractError("INVALID_EDIT")
        _require_text_size(old_string)
        _require_text_size(new_string)
        thread_id = self._thread_id(request)
        fingerprint = mutation_fingerprint("edit_file", args)
        if cached := self._mutation_service.prepared(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            fingerprint=fingerprint,
        ):
            return cached
        if self._mutation_service.was_consumed(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            fingerprint=fingerprint,
        ):
            raise FileToolContractError("COMMIT_CONFLICT")
        record = self._resolve_snapshot(request, path, snapshot_id)
        current = self._text_backend.read_text_document(path)
        _require_mutation_size(current)
        self._require_current(record, current)
        old_source = _normalize_line_endings(old_string, current)
        new_source = _normalize_line_endings(new_string, current)
        matches = current.content.count(old_source)
        if matches == 0:
            raise FileToolContractError("EXACT_MATCH_NOT_FOUND")
        if matches != 1:
            raise FileToolContractError("AMBIGUOUS_MATCH")
        start = current.content.index(old_source)
        start_line, end_line = _source_line_range(current.content, start, old_source)
        if not self._snapshot_store.has_seen(record, start_line, end_line):
            raise FileToolContractError("UNREAD_RANGE")
        proposed = current.content[:start] + new_source + current.content[start + len(old_source) :]
        if proposed == current.content:
            raise FileToolContractError("NO_CHANGES")
        _require_mutation_text_size(proposed)
        return self._mutation_service.prepare(
            metadata=MutationMetadata(
                operation="edit",
                path=path,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                snapshot_id=snapshot_id,
            ),
            current=current,
            proposed_content=proposed,
            fingerprint=fingerprint,
        )

    def _prepare_delete(
        self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str
    ) -> PreparedFileMutation:
        """验证完整 prior-read 后，固定 compare-delete 所需的 expected identity。"""
        path = _path(args, "file_path", "delete_file")
        snapshot_id = _snapshot_id(args)
        thread_id = self._thread_id(request)
        fingerprint = mutation_fingerprint("delete_file", args)
        if cached := self._mutation_service.prepared(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            fingerprint=fingerprint,
        ):
            return cached
        if self._mutation_service.was_consumed(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            fingerprint=fingerprint,
        ):
            raise FileToolContractError("COMMIT_CONFLICT")
        record = self._resolve_snapshot(request, path, snapshot_id)
        current = self._text_backend.read_text_document(path)
        _require_mutation_size(current)
        self._require_current(record, current)
        if record.line_count and not self._snapshot_store.has_seen(record, 0, record.line_count):
            raise FileToolContractError("UNREAD_RANGE")
        return self._mutation_service.prepare(
            metadata=MutationMetadata(
                operation="delete",
                path=path,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                snapshot_id=snapshot_id,
            ),
            current=current,
            proposed_content=None,
            fingerprint=fingerprint,
        )

    def _ls(self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str) -> ToolMessage:
        """保留既有只读 ls 输出。"""
        result: LsResult = self._resolve_backend(request).ls(_path(args, "path", "ls"))
        if result.error:
            raise FileToolContractError("BACKEND_READ_FAILED")
        return ToolMessage(
            content=str(truncate_if_too_long([item.get("path", "") for item in (result.entries or [])])),
            name="ls",
            tool_call_id=tool_call_id,
            status="success",
        )

    def _glob(self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str) -> ToolMessage:
        """保留既有只读 glob 输出。"""
        path = _optional_path(args, "path", "glob")
        result: GlobResult = self._resolve_backend(request).glob(_string_arg(args, "pattern"), path=path)
        if result.error:
            raise FileToolContractError("BACKEND_READ_FAILED")
        return ToolMessage(
            content=str(truncate_if_too_long([item.get("path", "") for item in (result.matches or [])])),
            name="glob",
            tool_call_id=tool_call_id,
            status="success",
        )

    def _grep(self, request: ToolCallRequest, args: dict[str, Any], tool_call_id: str) -> ToolMessage:
        """保留既有只读 grep 输出和三种输出模式。"""
        output_mode = str(args.get("output_mode", "files_with_matches"))
        if output_mode not in {"files_with_matches", "content", "count"}:
            raise FileToolContractError("GREP_OUTPUT_MODE_INVALID")
        result: GrepResult = self._resolve_backend(request).grep(
            _string_arg(args, "pattern"),
            path=_optional_path(args, "path", "grep"),
            glob=args.get("glob") if isinstance(args.get("glob"), str) else None,
        )
        if result.error:
            raise FileToolContractError("BACKEND_READ_FAILED")
        return ToolMessage(
            content=str(truncate_if_too_long(format_grep_matches(result.matches or [], output_mode))),
            name="grep",
            tool_call_id=tool_call_id,
            status="success",
        )

    def _resolve_snapshot(self, request: ToolCallRequest, path: str, snapshot_id: str) -> SnapshotRecord:
        """按当前 Thread、路径和 backend identity 解析，不匹配时 fail closed。"""
        return self._snapshot_store.resolve(
            snapshot_id,
            self._thread_id(request),
            path,
            self._text_backend.backend_id,
        )

    def _record_snapshot(
        self, request: ToolCallRequest, document: TextDocument, *, offset: int, limit: int
    ) -> SnapshotRecord:
        """为模型实际获得的窗口记录身份与 seen range，不缓存源码。"""
        record = self._snapshot_store.record_read(
            self._thread_id(request),
            document.path,
            self._text_backend.backend_id,
            document.content,
            offset=offset,
            limit=limit,
            encoding=document.identity.encoding,
            has_bom=document.identity.has_bom,
            raw_bytes=_raw_bytes(document),
        )
        if record is None:
            raise FileToolContractError("VIRTUAL_READONLY")
        return record

    @staticmethod
    def _require_current(record: SnapshotRecord, document: TextDocument) -> None:
        """Snapshot 只证明当时版本；当前 identity 不同即拒绝提交。"""
        if (
            record.content_hash != document.identity.digest
            or record.byte_length != document.identity.byte_length
            or record.encoding != document.identity.encoding
            or record.has_bom != document.identity.has_bom
            or record.line_ending != document.identity.line_ending
            or record.has_final_newline != document.identity.has_final_newline
        ):
            raise FileToolContractError("STALE_FILE")

    def _resolve_backend(self, request: ToolCallRequest) -> Any:
        """共享图每次调用从 RunContext 获取已挂载虚拟 backend。"""
        return self._backend(request.runtime) if callable(self._backend) else self._backend

    @staticmethod
    def _thread_id(request: ToolCallRequest) -> str:
        """从当前 RunContext 取得 Thread；缺失时拒绝共享 Snapshot scope。"""
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        thread_id = getattr(context, "thread_id", None)
        if not isinstance(thread_id, str) or not thread_id:
            config = getattr(runtime, "config", None)
            configurable = config.get("configurable") if isinstance(config, Mapping) else None
            thread_id = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            # HumanInTheLoop 的 description callback 不暴露 Runtime.config，
            # 但仍运行在 LangGraph 的当前调用 context 中；直接读取同一份 config，
            # 不使用进程级 sentinel，也不会让不同 Thread 共享 Snapshot scope。
            try:
                config = get_config()
            except RuntimeError:
                config = {}
            configurable = config.get("configurable") if isinstance(config, Mapping) else None
            thread_id = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise FileToolContractError("RUN_CONTEXT_REQUIRED")
        return thread_id

    @staticmethod
    def _call_args(request: ToolCallRequest) -> tuple[str, dict[str, Any], str]:
        """校验工具调用承载的名称、参数对象与追踪 ID。"""
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args") or {}
        if not isinstance(args, dict):
            raise FileToolContractError("FILE_TOOL_ARGUMENTS_INVALID")
        return name, args, str(request.tool_call.get("id") or "file-tool")

    @staticmethod
    def _error(name: str, tool_call_id: str, code: str) -> ToolMessage:
        """输出不含源码、路径外信息或 backend 异常文本的 JSON 错误。"""
        return _message(
            name or "filesystem",
            tool_call_id,
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": _error_message(code),
                    "next_action": _next_action(code),
                },
            },
            status="error",
        )


def create_snapshot_file_tool_contract(
    backend: Any,
    *,
    snapshot_store: ThreadSnapshotStore | None,
    text_backend: TextMutationBackend,
    diagnostics_provider: Callable[[str], Awaitable[Mapping[str, object]]] | None = None,
    diagnostics_timeout_seconds: float = DEFAULT_DIAGNOSTICS_TIMEOUT_SECONDS,
    metrics: FileToolMetrics | None = None,
) -> SnapshotFileToolContract:
    """创建唯一 canonical Snapshot 文件工具 contract。"""
    return SnapshotFileToolContract(
        backend,
        snapshot_store=snapshot_store,
        text_backend=text_backend,
        diagnostics_provider=diagnostics_provider,
        diagnostics_timeout_seconds=diagnostics_timeout_seconds,
        metrics=metrics,
    )


def _raw_bytes(document: TextDocument) -> bytes:
    """重建与 TextMutationBackend identity 等价的 UTF-8 原始字节。"""
    prefix = b"\xef\xbb\xbf" if document.identity.has_bom else b""
    return prefix + document.content.encode("utf-8")


__all__ = [
    "CanonicalDeleteFileSchema",
    "CanonicalEditFileSchema",
    "CanonicalReadFileSchema",
    "CanonicalWriteFileSchema",
    "DEFAULT_READ_LIMIT",
    "DEFAULT_DIAGNOSTICS_TIMEOUT_SECONDS",
    "MAX_DIAGNOSTIC_BYTES",
    "MAX_DIAGNOSTIC_ITEMS",
    "MAX_READ_LIMIT",
    "SnapshotFileToolContract",
    "create_snapshot_file_tool_contract",
]
