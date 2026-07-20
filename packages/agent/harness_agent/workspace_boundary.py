"""本机文件工具的工作区路径边界中间件。

本模块只限制 deepagents 内置文件工具，避免本机模式的 ``LocalShellBackend``
因绝对路径、``..`` 或符号链接而访问 ``--cwd`` 以外的文件。它不是 shell
沙箱，``execute``、MCP 与企业远端 sandbox 由各自的安全机制负责。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

_DIRECT_PATH_ARGUMENTS = {
    "ls": "path",
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    # deepagents 当前未暴露 delete；预先约束常见参数名，新增工具时不会漏管。
    "delete": "file_path",
    "delete_file": "file_path",
}
_SEARCH_TOOLS = frozenset({"glob", "grep"})
_VIRTUAL_ROOT = "/.harness"
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class WorkspacePathPolicy:
    """判定路径是否经真实路径解析后仍包含在指定本机工作区中。"""

    def __init__(self, workspace: str | Path) -> None:
        """解析工作区根目录，作为后续所有 containment 比较的唯一基准。"""
        self.workspace = Path(workspace).resolve(strict=False)

    def validate_direct_path(self, value: object, *, tool_name: str) -> Path:
        """验证直接文件工具的绝对路径并返回 canonical 路径。

        ``Path.resolve(strict=False)`` 会解析已存在的父级符号链接，因此即使
        目标文件尚未创建，也能在写入前发现通过工作区内链接逃逸的路径。
        """
        path = self._require_path_string(value, tool_name=tool_name, field="path")
        if not path.is_absolute():
            raise ValueError("文件路径必须是绝对路径")
        return self._resolve_inside_workspace(path, tool_name=tool_name)

    def validate_search_path(self, value: object, *, tool_name: str) -> Path:
        """验证 glob/grep 显式指定的搜索根目录。"""
        path = self._require_path_string(value, tool_name=tool_name, field="path")
        if not path.is_absolute():
            raise ValueError("搜索根目录必须是绝对路径")
        return self._resolve_inside_workspace(path, tool_name=tool_name)

    def validate_search_pattern(self, value: object, *, tool_name: str, field: str) -> None:
        """拒绝可把 glob 搜索根移出工作区的绝对或父级路径模式。"""
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} 必须是非空字符串")
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or _WINDOWS_ABSOLUTE_PATH.match(value):
            raise ValueError(f"{field} 不能是绝对路径模式")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"{field} 不能包含 '..' 路径段")

    def _require_path_string(self, value: object, *, tool_name: str, field: str) -> Path:
        """规范化输入类型，并在进入 Path 前拒绝 Windows/UNC 路径歧义。"""
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} 必须是非空字符串")
        normalized = value.replace("\\", "/")
        if _WINDOWS_ABSOLUTE_PATH.match(value) or value.startswith("\\"):
            raise ValueError("不支持 Windows 或 UNC 文件路径")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError("文件路径不能包含 '..' 路径段")
        return Path(value)

    def _resolve_inside_workspace(self, path: Path, *, tool_name: str) -> Path:
        """解析符号链接并验证 canonical 路径仍是工作区后代。"""
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self.workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            # ``relative_to`` 的 ValueError 与解析中的 symlink loop 都统一转为
            # 工具可理解的拒绝，避免路径解析异常泄漏到 Agent 主循环。
            raise ValueError(
                f"{tool_name} 只能访问工作目录 `{self.workspace}` 内的文件"
            ) from exc
        return resolved


class WorkspaceBoundaryMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """在本机文件工具执行前强制工作区 containment，失败时不调用处理器。"""

    def __init__(self, workspace: str | Path) -> None:
        """为一个 Agent 中间件实例创建不可变的工作区路径策略。"""
        super().__init__()
        self.policy = WorkspacePathPolicy(workspace)

    def _validate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        """检查受管工具参数；拒绝时构造错误 ToolMessage，成功则返回 None。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            return self._rejection(tool_name, tool_call.get("id"), "工具参数必须是对象")

        try:
            if tool_name in _DIRECT_PATH_ARGUMENTS:
                field = _DIRECT_PATH_ARGUMENTS[tool_name]
                value = args.get(field)
                if _is_virtual_path(value):
                    if tool_name != "read_file":
                        raise ValueError("/.harness 仅允许通过 read_file 只读分页访问")
                    _validate_virtual_read_path(value)
                else:
                    self.policy.validate_direct_path(value, tool_name=tool_name)
            elif tool_name in _SEARCH_TOOLS:
                # 未传 path 时由 LocalShellBackend 以 root_dir 搜索；这是工作区内
                # 的安全默认值。显式 path 必须仍通过 canonical containment。
                if args.get("path") is not None:
                    self.policy.validate_search_path(args["path"], tool_name=tool_name)
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

    def allows_approval(self, request: ToolCallRequest) -> bool:
        """供 HITL 的 ``when`` 预检复用路径规则，避免越界调用先请求审批。

        此方法不替代 ``wrap_tool_call``：模型输出到实际执行之间仍可能被修改，
        因此后者必须继续作为最终的工具执行边界。
        """
        return self._validate_tool_call(request) is None

    def _rejection(self, tool_name: str, tool_call_id: object, reason: str) -> ToolMessage:
        """将策略失败转成模型可纠正的错误结果，而不是抛出图执行异常。"""
        return ToolMessage(
            content=(
                f"工作区边界拒绝 {tool_name}：{reason}。"
                "请使用当前工作目录内的绝对路径；glob/grep 可省略 path，"
                "此时会从当前工作目录搜索。"
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
        """同步入口先执行路径策略，拒绝后不让底层工具获得调用机会。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步入口复用同步验证逻辑，确保两种调用方式安全语义一致。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return await handler(request)


def _is_virtual_path(value: object) -> bool:
    """判断路径是否指向逻辑虚拟根，不能将它交给宿主 Path.resolve。"""
    return isinstance(value, str) and (value == _VIRTUAL_ROOT or value.startswith(f"{_VIRTUAL_ROOT}/"))


def _validate_virtual_read_path(value: object) -> None:
    """只在守卫层做路径语法校验，存在性和 thread 归属由虚拟后端二次校验。"""
    if not isinstance(value, str) or not value.startswith(f"{_VIRTUAL_ROOT}/"):
        raise ValueError("/.harness 路径必须使用绝对逻辑路径")
    normalized = value.replace("\\", "/")
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError("/.harness 路径不能包含 '..' 段")
