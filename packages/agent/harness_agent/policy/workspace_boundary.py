"""本机文件工具的工作区路径边界中间件。

文件工具只接受当前工作区内、可转换为 DeepAgents 虚拟路径的目标。审批只能决定
工作区内的已授权操作，不能把文件工具扩展为工作区外的直接写入入口；这保证
write/edit/delete 均会继续进入唯一 Harness FileToolContract。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from harness_agent.tools.file_tool_catalog import DIRECT_FILE_TOOL_PATH_ARGUMENTS

_DIRECT_PATH_ARGUMENTS = DIRECT_FILE_TOOL_PATH_ARGUMENTS
_SEARCH_TOOLS = frozenset({"glob", "grep"})
_VIRTUAL_ROOT = "/.harness"


class WorkspacePathPolicy:
    """判定路径是否经真实路径解析后仍包含在指定本机工作区中。"""

    def __init__(self, workspace: str | Path) -> None:
        """解析工作区根目录，作为后续所有 containment 比较的唯一基准。"""
        self.workspace = Path(workspace).resolve(strict=False)

    def validate_direct_path(self, value: object, *, tool_name: str) -> Path:
        """验证直接文件工具路径，并返回工作区内的字面宿主路径。"""
        return self._resolve_path_string(
            self._require_path_string(value, tool_name=tool_name, field="path"), tool_name=tool_name
        )

    def validate_search_path(self, value: object, *, tool_name: str) -> Path:
        """验证 glob/grep 显式指定的搜索根。"""
        return self._resolve_path_string(
            self._require_path_string(value, tool_name=tool_name, field="path"), tool_name=tool_name
        )

    def validate_search_pattern(self, value: object, *, tool_name: str, field: str) -> None:
        """拒绝可将 glob 搜索根移出工作区的绝对或父级模式。"""
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} 必须是非空字符串")
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or PureWindowsPath(value).is_absolute():
            raise ValueError(f"{field} 不能是绝对路径模式")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"{field} 不能包含 '..' 路径段")

    def _require_path_string(self, value: object, *, tool_name: str, field: str) -> str:
        """校验输入路径是非空、非 UNC、无父级穿越的字符串。"""
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} 必须是非空字符串")
        normalized = value.replace("\\", "/")
        if value.startswith("\\") or normalized.startswith("//"):
            raise ValueError("不支持 UNC 文件路径")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError("文件路径不能包含 '..' 路径段")
        return value

    def _resolve_path_string(self, raw: str, *, tool_name: str) -> Path:
        """将 `/` 虚拟路径映射到工作区，并确认解析后没有越界。"""
        if not raw.startswith("/"):
            raise ValueError(f"{tool_name} 必须使用以 `/` 开头的工作区虚拟路径")
        lexical = self.workspace / raw.lstrip("/")
        real = lexical.resolve(strict=False)
        try:
            real.relative_to(self.workspace)
        except (ValueError, OSError, RuntimeError) as exc:
            raise ValueError(f"{tool_name} 只能访问工作目录 `{self.workspace}` 内的文件") from exc
        # containment 使用解析后的真实路径，交给 backend 时保留字面路径。
        # 这样工作区内 symlink 不会在此被悄悄解引用，底层可以 fail closed。
        return lexical


class WorkspaceBoundaryMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """在本机文件工具进入 backend 前强制工作区 containment。"""

    def __init__(self, workspace: str | Path) -> None:
        """绑定不可变工作区。"""
        super().__init__()
        self.policy = WorkspacePathPolicy(workspace)

    def _validate_tool_call(
        self,
        request: ToolCallRequest,
        *,
        rewrite_backend_path: bool = True,
    ) -> ToolMessage | None:
        """检查受管工具参数；失败时不让任何内层 handler 收到调用。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        source_args = tool_call.get("args") or {}
        if not isinstance(source_args, dict):
            return self._rejection(tool_name, tool_call.get("id"), "工具参数必须是对象")
        args = source_args if rewrite_backend_path else dict(source_args)

        try:
            if tool_name in _DIRECT_PATH_ARGUMENTS:
                field = _DIRECT_PATH_ARGUMENTS[tool_name]
                value = args.get(field)
                if _is_virtual_path(value):
                    if tool_name != "read_file":
                        raise ValueError("/.harness 仅允许通过 read_file 只读分页访问")
                    _validate_virtual_read_path(value)
                else:
                    args[field] = self._backend_path(
                        self.policy.validate_direct_path(value, tool_name=tool_name)
                    )
            elif tool_name in _SEARCH_TOOLS:
                if args.get("path") is not None:
                    args["path"] = self._backend_path(
                        self.policy.validate_search_path(args["path"], tool_name=tool_name)
                    )
                if tool_name == "glob":
                    self.policy.validate_search_pattern(
                        args.get("pattern"), tool_name=tool_name, field="pattern"
                    )
                elif args.get("glob") is not None:
                    self.policy.validate_search_pattern(
                        args["glob"], tool_name=tool_name, field="glob"
                    )
            elif tool_name == "execute" and any(
                isinstance(value, str) and _VIRTUAL_ROOT in value for value in args.values()
            ):
                raise ValueError("execute 不能访问 /.harness 虚拟命名空间")
        except ValueError as exc:
            return self._rejection(tool_name, tool_call.get("id"), str(exc))
        return None

    def _backend_path(self, resolved: Path) -> str:
        """把验证后的宿主路径转换为 LocalShellBackend 虚拟路径。"""
        relative = resolved.relative_to(self.policy.workspace)
        return "/" if not relative.parts else f"/{relative.as_posix()}"

    def allows_approval(self, request: ToolCallRequest) -> bool:
        """审批预检与执行边界复用同一校验，不改写尚未批准的原始 tool call。"""
        return self._validate_tool_call(request, rewrite_backend_path=False) is None

    def canonical_approval_request(self, request: ToolCallRequest) -> ToolCallRequest | None:
        """返回仅供审批 prepare 使用的 canonical 路径副本，不污染后续执行参数。"""
        tool_call = request.tool_call
        source_args = tool_call.get("args") or {}
        if not isinstance(source_args, dict):
            return None
        copied_call = dict(tool_call)
        copied_call["args"] = dict(source_args)
        copied_request = SimpleNamespace(
            tool_call=copied_call,
            runtime=getattr(request, "runtime", None),
        )
        if self._validate_tool_call(copied_request, rewrite_backend_path=True) is not None:
            return None
        return copied_request  # type: ignore[return-value]

    @staticmethod
    def _rejection(tool_name: str, tool_call_id: object, reason: str) -> ToolMessage:
        """将路径策略失败转成可纠正的 ToolMessage。"""
        return ToolMessage(
            content=(
                f"工作区边界拒绝 {tool_name}：{reason}。"
                "请使用以 `/` 开头的工作区虚拟路径；"
                "glob/grep 可省略 path 参数，此时默认从工作区根目录搜索。"
            ),
            name=tool_name or "filesystem",
            tool_call_id=str(tool_call_id or "workspace-boundary"),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步入口拒绝越界调用后才继续进入 canonical contract。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步入口复用同步路径校验。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return await handler(request)


def _is_virtual_path(value: object) -> bool:
    """判断路径是否指向逻辑虚拟根，不能将它交给宿主 Path.resolve。"""
    return isinstance(value, str) and (value == _VIRTUAL_ROOT or value.startswith(f"{_VIRTUAL_ROOT}/"))


def _validate_virtual_read_path(value: object) -> None:
    """校验只读虚拟路径语法；存在性与 Thread 归属由虚拟 backend 二次校验。"""
    if not isinstance(value, str) or not value.startswith(f"{_VIRTUAL_ROOT}/"):
        raise ValueError("/.harness 路径必须使用绝对逻辑路径")
    if ".." in PurePosixPath(value.replace("\\", "/")).parts:
        raise ValueError("/.harness 路径不能包含 '..' 段")
