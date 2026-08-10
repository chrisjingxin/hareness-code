"""Harness-owned 文件工具 interposition seam。

DeepAgents 0.6.8 的 ``FilesystemMiddleware`` 是受保护的构图脚手架，不能从图中
排除。本模块不再向 ``tools=`` 追加一套同名工具，而是在模型请求阶段去重替换
文件 schema，并在 ToolNode 阶段直接执行注入的 contract，因而 builtin handler
不会收到已接管的 read/write/edit/delete 调用。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from deepagents.backends.protocol import (
    EditResult,
    FileData,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.middleware.filesystem import (
    EditFileSchema,
    GlobSchema,
    GrepSchema,
    LsSchema,
    ReadFileSchema,
    WriteFileSchema,
)
from deepagents.backends.utils import (
    _get_file_type,
    check_empty_content,
    format_content_with_line_numbers,
    format_grep_matches,
    truncate_if_too_long,
    validate_path,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from harness_agent.threads.snapshots import ThreadSnapshotStore
from harness_agent.threads.text_backend import TextMutationBackend
from harness_agent.tools.tools_file import delete_file as _delete_file_impl

logger = logging.getLogger(__name__)

BUILTIN_FILE_TOOL_NAMES = frozenset({"ls", "read_file", "write_file", "edit_file", "glob", "grep"})
"""DeepAgents 0.6.8 自动注册的文件工具名。"""

HARNESS_FILE_TOOL_NAMES = frozenset((*BUILTIN_FILE_TOOL_NAMES, "delete_file"))
"""当前 exact-string 生产 contract 接管的文件工具名。"""


class DeleteFileSchema(BaseModel):
    """Harness delete_file 的当前稳定参数形状。"""

    file_path: str = Field(description="Absolute path to the file to delete. Must be absolute, not relative.")


class FileToolContractError(RuntimeError):
    """文件 contract 接管或 dispatch 失败。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存可被模型恢复逻辑判断的稳定 code。"""
        self.code = code
        super().__init__(message or code)


@runtime_checkable
class FileToolContract(Protocol):
    """可注入的 Harness 文件 schema 与执行 contract。"""

    @property
    def tool_definitions(self) -> Sequence[BaseTool | dict[str, Any]]:
        """返回模型请求中使用的 canonical definitions。"""

    @property
    def handled_tool_names(self) -> frozenset[str]:
        """返回不会落入 DeepAgents builtin handler 的工具名。"""

    @property
    def registration_tools(self) -> Sequence[BaseTool | dict[str, Any]]:
        """返回必须注册到 ToolNode 以进入默认 request 的非 builtin 工具。"""

    def dispatch(self, request: ToolCallRequest) -> ToolMessage | Any:
        """同步接管一个文件工具调用。"""

    async def adispatch(self, request: ToolCallRequest) -> ToolMessage | Any:
        """异步接管一个文件工具调用。"""


def _tool_name(tool: object) -> str:
    """读取 BaseTool、OpenAI function schema 或普通 mapping 的稳定名称。"""
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return str(function["name"])
        return ""
    return str(getattr(tool, "name", ""))


def _placeholder_tool(
    *, name: str, description: str, args_schema: type[BaseModel]
) -> StructuredTool:
    """创建只作为模型 schema/ToolNode registration 的 fail-closed placeholder。"""

    def fail_closed(**_kwargs: Any) -> str:
        """若 interposition 被错误移除，placeholder 不执行真实文件操作。"""
        raise FileToolContractError("FILE_TOOL_INTERPOSITION_REQUIRED")

    return StructuredTool.from_function(
        func=fail_closed,
        name=name,
        description=description,
        infer_schema=False,
        args_schema=args_schema,
    )


class HarnessFileToolsMiddleware(AgentMiddleware):
    """替换模型可见文件 schema，并短路执行到注入的 Harness contract。"""

    def __init__(self, contract: FileToolContract) -> None:
        """校验 contract 的名称唯一性；本 middleware 不注册 ``tools`` 属性。"""
        super().__init__()
        definitions = tuple(contract.tool_definitions)
        names = tuple(_tool_name(tool) for tool in definitions)
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("FILE_TOOL_CONTRACT_SCHEMA_DUPLICATE")
        handled = frozenset(contract.handled_tool_names)
        if handled != frozenset(names):
            raise ValueError("FILE_TOOL_CONTRACT_NAMES_MISMATCH")
        self._contract = contract
        self._definitions = definitions
        self._definitions_by_name = dict(zip(names, definitions, strict=True))
        self._handled_tool_names = handled

    @property
    def handled_tool_names(self) -> frozenset[str]:
        """返回当前 middleware 接管的工具集合，供架构测试观察。"""
        return self._handled_tool_names

    def _replace_file_tools(self, tools: Sequence[object]) -> list[BaseTool | dict[str, Any]]:
        """保留上游顺序并把每个已接管名称压成恰好一个 definition。"""
        replaced: list[BaseTool | dict[str, Any]] = []
        emitted: set[str] = set()
        for tool in tools:
            name = _tool_name(tool)
            if name not in self._handled_tool_names:
                replaced.append(tool)  # type: ignore[arg-type]
                continue
            if name not in emitted:
                replaced.append(self._definitions_by_name[name])
                emitted.add(name)
        return replaced

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """同步模型调用只接收当前上游已授权的 Harness file definitions。"""
        return handler(request.override(tools=self._replace_file_tools(request.tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """异步模型调用复用同一去重逻辑。"""
        return await handler(request.override(tools=self._replace_file_tools(request.tools)))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """已接管调用直接进入 contract，绝不调用 DeepAgents builtin handler。"""
        name = str(request.tool_call.get("name", ""))
        if name not in self._handled_tool_names:
            return handler(request)
        try:
            result = self._contract.dispatch(request)
            if inspect.isawaitable(result):
                return self._error(request, "FILE_TOOL_ASYNC_REQUIRED")
            return result
        except Exception as exc:  # noqa: BLE001 - 工具边界必须转成稳定 ToolMessage
            return self._error(request, _error_code(exc))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步已接管调用直接进入 contract，builtin handler 不可达。"""
        name = str(request.tool_call.get("name", ""))
        if name not in self._handled_tool_names:
            return await handler(request)
        try:
            result = self._contract.adispatch(request)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:  # noqa: BLE001 - 工具边界必须转成稳定 ToolMessage
            return self._error(request, _error_code(exc))

    @staticmethod
    def _error(request: ToolCallRequest, code: str) -> ToolMessage:
        """构造不泄露源码或凭据的稳定错误结果。"""
        name = str(request.tool_call.get("name", "filesystem"))
        return ToolMessage(
            content=f"Harness 文件工具接管失败：{code}。",
            name=name,
            tool_call_id=str(request.tool_call.get("id") or "file-tool"),
            status="error",
        )


class LegacyBackendFileToolContract:
    """当前 exact-string 文件工具的 Harness-owned contract。

    该 contract 只负责把既有 DeepAgents backend 行为移到 Harness seam；
    Snapshot 编辑/insert schema 和 CAS mutation pipeline 留给后续任务注入，
    不在这里预先固化 ZC-133 的候选接口。
    """

    def __init__(
        self,
        backend: Any,
        workspace_root: str | Path,
        *,
        snapshot_store: ThreadSnapshotStore | None = None,
        text_backend: TextMutationBackend | None = None,
    ) -> None:
        """绑定可按 RunContext 解析的 backend 和可选的 Snapshot recorder。"""
        self._backend = backend
        self._workspace_root = str(workspace_root)
        self._snapshot_store = snapshot_store
        self._text_backend = text_backend
        self._definitions = (
            _placeholder_tool(
                name="ls",
                description="Lists files in an absolute directory path.",
                args_schema=LsSchema,
            ),
            _placeholder_tool(
                name="read_file",
                description="Reads a text file with 0-based line pagination.",
                args_schema=ReadFileSchema,
            ),
            _placeholder_tool(
                name="write_file",
                description="Creates a new text file and never overwrites an existing file.",
                args_schema=WriteFileSchema,
            ),
            _placeholder_tool(
                name="edit_file",
                description="Replaces an exact string in a file.",
                args_schema=EditFileSchema,
            ),
            _placeholder_tool(
                name="glob",
                description="Finds files matching a glob pattern.",
                args_schema=GlobSchema,
            ),
            _placeholder_tool(
                name="grep",
                description="Searches literal text in files.",
                args_schema=GrepSchema,
            ),
            _placeholder_tool(
                name="delete_file",
                description="Deletes one file after the existing approval policy allows it.",
                args_schema=DeleteFileSchema,
            ),
        )

    @property
    def tool_definitions(self) -> Sequence[BaseTool | dict[str, Any]]:
        """返回当前 exact-string schema；它是可替换的 contract 输入而非 middleware tools。"""
        return self._definitions

    @property
    def handled_tool_names(self) -> frozenset[str]:
        """返回所有当前 Harness-owned 文件工具。"""
        return HARNESS_FILE_TOOL_NAMES

    @property
    def registration_tools(self) -> Sequence[BaseTool | dict[str, Any]]:
        """只注册 DeepAgents 没有的 delete_file，避免同名 builtin duplicate。"""
        return tuple(tool for tool in self._definitions if _tool_name(tool) not in BUILTIN_FILE_TOOL_NAMES)

    def dispatch(self, request: ToolCallRequest) -> ToolMessage:
        """同步执行 exact-string 文件 contract。"""
        return self._dispatch_sync(request)

    async def adispatch(self, request: ToolCallRequest) -> ToolMessage:
        """异步执行 exact-string 文件 contract，优先调用 backend async 方法。"""
        name, args, tool_call_id = self._call_args(request)
        try:
            backend = self._resolve_backend(request)
            if name == "read_file":
                path = self._path(args, "file_path", name)
                offset = _non_negative_int(args.get("offset", 0), "offset")
                limit = _positive_int(args.get("limit", 100), "limit")
                result = await backend.aread(path, offset=offset, limit=limit)
                await self._record_snapshot(request, path, offset, limit)
                return self._read_message(result, path, offset, tool_call_id)
            if name == "ls":
                path = self._path(args, "path", name)
                return self._ls_message(await backend.als(path), tool_call_id)
            if name == "write_file":
                path = self._path(args, "file_path", name)
                content = _string_arg(args, "content")
                return self._write_message(await backend.awrite(path, content), tool_call_id)
            if name == "edit_file":
                path = self._path(args, "file_path", name)
                old_string = _string_arg(args, "old_string")
                new_string = _string_arg(args, "new_string")
                replace_all = bool(args.get("replace_all", False))
                return self._edit_message(
                    await backend.aedit(path, old_string, new_string, replace_all=replace_all),
                    tool_call_id,
                )
            if name == "glob":
                path = self._optional_path(args, "path", name)
                return self._glob_message(await backend.aglob(_string_arg(args, "pattern"), path=path), tool_call_id)
            if name == "grep":
                path = self._optional_path(args, "path", name)
                result = await backend.agrep(
                    _string_arg(args, "pattern"),
                    path=path,
                    glob=args.get("glob") if isinstance(args.get("glob"), str) else None,
                )
                return self._grep_message(result, str(args.get("output_mode", "files_with_matches")), tool_call_id)
            if name == "delete_file":
                return self._delete_message(args, tool_call_id)
            raise FileToolContractError("FILE_TOOL_NAME_UNSUPPORTED")
        except Exception as exc:  # noqa: BLE001 - stable tool result boundary
            return self._error_message(name, tool_call_id, _error_code(exc))

    def _dispatch_sync(self, request: ToolCallRequest) -> ToolMessage:
        """同步执行路径与异步 contract 保持同一参数和返回语义。"""
        name, args, tool_call_id = self._call_args(request)
        try:
            backend = self._resolve_backend(request)
            if name == "read_file":
                path = self._path(args, "file_path", name)
                offset = _non_negative_int(args.get("offset", 0), "offset")
                limit = _positive_int(args.get("limit", 100), "limit")
                result = backend.read(path, offset=offset, limit=limit)
                self._record_snapshot_sync(request, path, offset, limit)
                return self._read_message(result, path, offset, tool_call_id)
            if name == "ls":
                return self._ls_message(backend.ls(self._path(args, "path", name)), tool_call_id)
            if name == "write_file":
                return self._write_message(
                    backend.write(self._path(args, "file_path", name), _string_arg(args, "content")),
                    tool_call_id,
                )
            if name == "edit_file":
                return self._edit_message(
                    backend.edit(
                        self._path(args, "file_path", name),
                        _string_arg(args, "old_string"),
                        _string_arg(args, "new_string"),
                        replace_all=bool(args.get("replace_all", False)),
                    ),
                    tool_call_id,
                )
            if name == "glob":
                return self._glob_message(
                    backend.glob(_string_arg(args, "pattern"), path=self._optional_path(args, "path", name)),
                    tool_call_id,
                )
            if name == "grep":
                return self._grep_message(
                    backend.grep(
                        _string_arg(args, "pattern"),
                        path=self._optional_path(args, "path", name),
                        glob=args.get("glob") if isinstance(args.get("glob"), str) else None,
                    ),
                    str(args.get("output_mode", "files_with_matches")),
                    tool_call_id,
                )
            if name == "delete_file":
                return self._delete_message(args, tool_call_id)
            raise FileToolContractError("FILE_TOOL_NAME_UNSUPPORTED")
        except Exception as exc:  # noqa: BLE001 - stable tool result boundary
            return self._error_message(name, tool_call_id, _error_code(exc))

    def _resolve_backend(self, request: ToolCallRequest) -> Any:
        """按当前 runtime 解析共享图的 run-scoped backend factory。"""
        if callable(self._backend):
            return self._backend(request.runtime)
        return self._backend

    def _call_args(self, request: ToolCallRequest) -> tuple[str, dict[str, Any], str]:
        """校验 ToolCallRequest 的名称、参数对象和可追踪 call ID。"""
        name = str(request.tool_call.get("name", ""))
        args = request.tool_call.get("args") or {}
        if not isinstance(args, dict):
            raise FileToolContractError("FILE_TOOL_ARGUMENTS_INVALID")
        return name, args, str(request.tool_call.get("id") or "file-tool")

    @staticmethod
    def _path(args: dict[str, Any], field: str, name: str) -> str:
        """用 DeepAgents canonical path 校验保留 exact-string 参数。"""
        value = args.get(field)
        if not isinstance(value, str) or not value:
            raise FileToolContractError("FILE_TOOL_PATH_INVALID")
        try:
            path = validate_path(value)
        except ValueError as exc:
            raise FileToolContractError("FILE_TOOL_PATH_INVALID", str(exc)) from exc
        if path.startswith("/.harness") and name != "read_file":
            raise FileToolContractError("VIRTUAL_READONLY")
        return path

    @classmethod
    def _optional_path(cls, args: dict[str, Any], field: str, name: str) -> str | None:
        """校验 glob/grep 的可选搜索根。"""
        value = args.get(field)
        if value is None:
            return None
        return cls._path(args, field, name)

    async def _record_snapshot(
        self,
        request: ToolCallRequest,
        path: str,
        offset: int,
        limit: int,
    ) -> None:
        """读取成功后从 text backend 获取完整版本并按当前 Run Thread 记录范围。"""
        if self._snapshot_store is None or self._text_backend is None or path.startswith("/.harness"):
            return
        try:
            document = await asyncio.to_thread(self._text_backend.read_text_document, path)
            self._record_document(request, document.path, document.content, document.identity, offset, limit)
        except Exception as exc:  # noqa: BLE001 - Snapshot 记录不能改变 read 成功语义
            logger.debug("Snapshot record skipped: %s", _error_code(exc))

    def _record_snapshot_sync(self, request: ToolCallRequest, path: str, offset: int, limit: int) -> None:
        """同步入口对应的 Snapshot 记录。"""
        if self._snapshot_store is None or self._text_backend is None or path.startswith("/.harness"):
            return
        try:
            document = self._text_backend.read_text_document(path)
            self._record_document(request, document.path, document.content, document.identity, offset, limit)
        except Exception as exc:  # noqa: BLE001 - Snapshot 记录不能改变 read 成功语义
            logger.debug("Snapshot record skipped: %s", _error_code(exc))

    def _record_document(
        self,
        request: ToolCallRequest,
        path: str,
        content: str,
        identity: Any,
        offset: int,
        limit: int,
    ) -> None:
        """从 RunContext 取得 Thread，禁止使用构图期闭包中的 Thread。"""
        context = getattr(request.runtime, "context", None)
        thread_id = getattr(context, "thread_id", None) or "ephemeral"
        snapshot_store = getattr(context, "snapshot_store", None) or self._snapshot_store
        if snapshot_store is None:
            return
        snapshot_store.record_read(
            str(thread_id),
            path,
            self._text_backend.backend_id,
            content,
            offset=offset,
            limit=limit,
            encoding=identity.encoding,
            has_bom=identity.has_bom,
            raw_bytes=None,
        )

    def _delete_message(self, args: dict[str, Any], tool_call_id: str) -> ToolMessage:
        """保留当前 delete_file JSON 结果形状，实际执行仍经过 Harness seam。"""
        path = self._path(args, "file_path", "delete_file")
        result = _delete_file_impl(path, self._workspace_root)
        import json

        return ToolMessage(
            content=json.dumps(result, ensure_ascii=False),
            name="delete_file",
            tool_call_id=tool_call_id,
            status="success" if result.get("success") else "error",
        )

    @staticmethod
    def _read_message(result: ReadResult | str, path: str, offset: int, tool_call_id: str) -> ToolMessage:
        """复用 DeepAgents read_file 的文本行号结果形状。"""
        if isinstance(result, str):
            content = result
            encoding = "utf-8"
        else:
            if result.error:
                return LegacyBackendFileToolContract._error_message("read_file", tool_call_id, result.error)
            data: FileData | None = result.file_data
            if data is None:
                return LegacyBackendFileToolContract._error_message("read_file", tool_call_id, "FILE_DATA_MISSING")
            content = str(data.get("content", ""))
            encoding = str(data.get("encoding", "utf-8"))
        if check_empty_content(content):
            rendered = check_empty_content(content) or ""
        elif encoding == "base64" or _get_file_type(path) != "text":
            rendered = content
        else:
            rendered = format_content_with_line_numbers(content, start_line=offset + 1)
        return ToolMessage(content=rendered, name="read_file", tool_call_id=tool_call_id, status="success")

    @staticmethod
    def _ls_message(result: LsResult, tool_call_id: str) -> ToolMessage:
        """格式化目录列举结果。"""
        if result.error:
            return LegacyBackendFileToolContract._error_message("ls", tool_call_id, result.error)
        paths = [entry.get("path", "") for entry in (result.entries or [])]
        return ToolMessage(content=str(truncate_if_too_long(paths)), name="ls", tool_call_id=tool_call_id, status="success")

    @staticmethod
    def _write_message(result: WriteResult, tool_call_id: str) -> ToolMessage:
        """格式化创建文件结果。"""
        if result.error:
            return LegacyBackendFileToolContract._error_message("write_file", tool_call_id, result.error)
        return ToolMessage(content=f"Updated file {result.path}", name="write_file", tool_call_id=tool_call_id, status="success")

    @staticmethod
    def _edit_message(result: EditResult, tool_call_id: str) -> ToolMessage:
        """格式化 exact-string edit 结果。"""
        if result.error:
            return LegacyBackendFileToolContract._error_message("edit_file", tool_call_id, result.error)
        return ToolMessage(
            content=f"Successfully replaced {result.occurrences} instance(s) of the string in '{result.path}'",
            name="edit_file",
            tool_call_id=tool_call_id,
            status="success",
        )

    @staticmethod
    def _glob_message(result: GlobResult, tool_call_id: str) -> ToolMessage:
        """格式化 glob 结果。"""
        if result.error:
            return LegacyBackendFileToolContract._error_message("glob", tool_call_id, result.error)
        paths = [entry.get("path", "") for entry in (result.matches or [])]
        return ToolMessage(content=str(truncate_if_too_long(paths)), name="glob", tool_call_id=tool_call_id, status="success")

    @staticmethod
    def _grep_message(result: GrepResult, output_mode: str, tool_call_id: str) -> ToolMessage:
        """格式化 grep 的三种稳定输出模式。"""
        matches = result.matches or []
        if output_mode not in {"files_with_matches", "content", "count"}:
            return LegacyBackendFileToolContract._error_message("grep", tool_call_id, "GREP_OUTPUT_MODE_INVALID")
        content = format_grep_matches(matches, output_mode)
        if result.error:
            content = f"{result.error}\n\nPartial matches:\n{content}"
        return ToolMessage(
            content=str(truncate_if_too_long(content)),
            name="grep",
            tool_call_id=tool_call_id,
            status="error" if result.error else "success",
        )

    @staticmethod
    def _error_message(name: str, tool_call_id: str, code: str) -> ToolMessage:
        """生成稳定、无源码内容的 contract 错误。"""
        return ToolMessage(
            content=f"Harness 文件工具错误：{code}。",
            name=name,
            tool_call_id=tool_call_id,
            status="error",
        )


def create_legacy_file_tool_contract(
    backend: Any,
    workspace_root: str | Path,
    *,
    snapshot_store: ThreadSnapshotStore | None = None,
    text_backend: TextMutationBackend | None = None,
) -> LegacyBackendFileToolContract:
    """创建当前 exact-string contract，供构图边界注入或测试替换。"""
    return LegacyBackendFileToolContract(
        backend,
        workspace_root,
        snapshot_store=snapshot_store,
        text_backend=text_backend,
    )


def _string_arg(args: dict[str, Any], name: str) -> str:
    """提取必需字符串参数。"""
    value = args.get(name)
    if not isinstance(value, str):
        raise FileToolContractError("FILE_TOOL_ARGUMENT_INVALID")
    return value


def _non_negative_int(value: object, name: str) -> int:
    """提取非负整数参数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FileToolContractError(f"FILE_TOOL_{name.upper()}_INVALID")
    return value


def _positive_int(value: object, name: str) -> int:
    """提取正整数参数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FileToolContractError(f"FILE_TOOL_{name.upper()}_INVALID")
    return value


def _error_code(exc: Exception) -> str:
    """从受控错误读取 code，普通异常统一归类不泄露内部信息。"""
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) and code else "FILE_TOOL_EXECUTION_FAILED"


__all__ = [
    "BUILTIN_FILE_TOOL_NAMES",
    "DeleteFileSchema",
    "FileToolContract",
    "FileToolContractError",
    "HARNESS_FILE_TOOL_NAMES",
    "HarnessFileToolsMiddleware",
    "LegacyBackendFileToolContract",
    "create_legacy_file_tool_contract",
]
