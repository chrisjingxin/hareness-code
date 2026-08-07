"""本机文件工具的工作区路径边界中间件。

本模块只限制 deepagents 内置文件工具，避免本机模式的 ``LocalShellBackend``
因绝对路径、``..`` 或符号链接而访问 ``--cwd`` 以外的文件。它不是 shell
沙箱，``execute``、MCP 与企业远端 sandbox 由各自的安全机制负责。

越界写入按审批模式分流
----------------------
读取、搜索类工具越界时仍然硬拒绝；写入类工具（write_file/edit_file/
delete_file/delete）越界时按审批模式决定去向：

- plan：直接拒绝；
- default/auto/auto-edit：HITL 预检放行到审批流程（弹窗或显式 allow
  规则），批准后由本中间件绕过虚拟根限制，直接向真实路径写出；
- yolo：无审批门禁，由本中间件直接真实写出。

中间件到达执行层时 deny 规则与破坏性守卫已先行裁决（见 agent 构图的
中间件顺序），因此直写不会绕过这些硬策略。

路径格式约定
------------
deepagents 的 ``FilesystemMiddleware`` 在每个工具执行前调用
``validate_path()``，该函数**拒绝 Windows 盘符路径**（如 ``D:\\code``），
只接受以 ``/`` 开头的虚拟路径。因此本中间件在验证前将 Windows 盘符路径
转换为虚拟路径（如 ``/packages/cli``），使后端（``virtual_mode=True``）
能正确解析。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents.backends.utils import perform_string_replacement
from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

_DIRECT_PATH_ARGUMENTS = {
    "ls": "path",
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "delete": "file_path",
    "delete_file": "file_path",
    "lsp": "file_path",
}
_SEARCH_TOOLS = frozenset({"glob", "grep"})
_VIRTUAL_ROOT = "/.harness"
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")

# 支持越界定向放行的写入类工具。apply_patch 不在此列：它的补丁可覆盖多个
# 文件且由工具自身做工作区穿越检查，越界时保持硬拒绝。
_OUTSIDE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "delete"})


class WorkspacePathPolicy:
    """判定路径是否经真实路径解析后仍包含在指定本机工作区中。"""

    def __init__(self, workspace: str | Path) -> None:
        """解析工作区根目录，作为后续所有 containment 比较的唯一基准。"""
        self.workspace = Path(workspace).resolve(strict=False)

    def to_virtual_path(self, value: str) -> str:
        """将任意路径格式转换为 ``/`` 开头的虚拟路径。

        deepagents 的 ``validate_path()`` 拒绝 Windows 盘符路径，只接受
        以 ``/`` 开头的虚拟路径。本方法负责在中间件层完成格式转换：

        - 已是 ``/`` 开头的虚拟路径 → 原样返回
        - Windows 盘符路径（如 ``D:\\code\\project\\src``）→ 去掉工作区
          前缀后加 ``/``（如 ``/src``）；若不在工作区内则原样返回（后续
          验证会拒绝）
        - 其他格式 → 原样返回
        """
        if not isinstance(value, str) or not value:
            return value
        # 已是虚拟路径。
        if value.startswith("/"):
            return value
        # Windows 盘符路径 → 尝试转为虚拟路径。
        if _WINDOWS_ABSOLUTE_PATH.match(value):
            try:
                resolved = Path(value).resolve(strict=False)
                rel = resolved.relative_to(self.workspace)
                return "/" + rel.as_posix()
            except (ValueError, OSError, RuntimeError):
                # 不在工作区内或解析失败，原样返回，由后续验证拒绝。
                return value
        return value

    def validate_direct_path(self, value: object, *, tool_name: str) -> Path:
        """验证直接文件工具的路径并返回 canonical 路径。

        接受 ``/`` 开头的虚拟路径和 OS 绝对路径。虚拟路径先拼接到工作区
        根目录再解析，确保 containment 检查正确。
        """
        path_str = self._require_path_string(value, tool_name=tool_name, field="path")
        resolved = self._resolve_path_string(path_str, tool_name=tool_name)
        return resolved

    def validate_search_path(self, value: object, *, tool_name: str) -> Path:
        """验证 glob/grep 显式指定的搜索根目录。"""
        path_str = self._require_path_string(value, tool_name=tool_name, field="path")
        resolved = self._resolve_path_string(path_str, tool_name=tool_name)
        return resolved

    def validate_search_pattern(self, value: object, *, tool_name: str, field: str) -> None:
        """拒绝可把 glob 搜索根移出工作区的绝对或父级路径模式。"""
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} 必须是非空字符串")
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or _WINDOWS_ABSOLUTE_PATH.match(value):
            raise ValueError(f"{field} 不能是绝对路径模式")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"{field} 不能包含 '..' 路径段")

    def _require_path_string(self, value: object, *, tool_name: str, field: str) -> str:
        """规范化输入类型，拒绝目录穿越和 UNC 设备路径。

        返回原始字符串（不做 Path 转换），由调用方决定如何解析。
        """
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} 必须是非空字符串")
        normalized = value.replace("\\", "/")
        if value.startswith("\\") or normalized.startswith("//"):
            raise ValueError("不支持 UNC 文件路径")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError("文件路径不能包含 '..' 路径段")
        return value

    def _resolve_path_string(self, raw: str, *, tool_name: str) -> Path:
        """将路径字符串解析为真实 OS 路径并验证 containment。

        ``/`` 开头的虚拟路径拼接到工作区根目录后解析；OS 绝对路径直接
        解析。两种方式最终都通过 ``relative_to`` 检查 containment。
        """
        if raw.startswith("/") and sys.platform == "win32":
            # Windows virtual_mode 使用 `/relative`；POSIX 的 `/...` 必须按
            # 真实绝对路径校验，否则 `/var/outside` 会被错误拼回工作区。
            real = (self.workspace / raw.lstrip("/")).resolve(strict=False)
        else:
            real = Path(raw).resolve(strict=False)
        try:
            real.relative_to(self.workspace)
        except (ValueError, OSError, RuntimeError) as exc:
            raise ValueError(
                f"{tool_name} 只能访问工作目录 `{self.workspace}` 内的文件"
            ) from exc
        return real


class WorkspaceBoundaryMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """在本机文件工具执行前强制工作区 containment，失败时不调用处理器。

    越界写入类调用按 ``approval_mode`` 分流：plan 直接拒绝；其余模式由
    本中间件直接向真实路径写出（到达执行层时该调用要么已通过 HITL 批准
    或命中 allow 规则，要么处于无审批门禁的 yolo 模式）。
    """

    def __init__(self, workspace: str | Path, approval_mode: str = "plan") -> None:
        """为一个 Agent 中间件实例创建不可变的工作区路径策略。

        ``approval_mode`` 缺省为最保守的 plan（越界写入硬拒绝），未显式
        传入审批模式的构图路径不会获得越界直写能力。
        """
        super().__init__()
        self.policy = WorkspacePathPolicy(workspace)
        self.approval_mode = approval_mode

    def _validate_tool_call(
        self,
        request: ToolCallRequest,
        *,
        rewrite_backend_path: bool = True,
    ) -> ToolMessage | None:
        """检查受管工具参数；拒绝时构造错误 ToolMessage，成功则返回 None。

        在验证前将 Windows 盘符路径转换为虚拟路径并写回 args，使
        deepagents 的 ``validate_path()`` 和 ``virtual_mode=True`` 后端
        都能正确处理。
        """
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        source_args = tool_call.get("args") or {}
        if not isinstance(source_args, dict):
            return self._rejection(tool_name, tool_call.get("id"), "工具参数必须是对象")
        args = source_args if rewrite_backend_path else dict(source_args)

        # ── 路径归一化：Windows 盘符路径 → 虚拟路径 ──
        # 直接修改 args dict，使 handler 和后端收到 validate_path 可接受的格式。
        # 注意：/.harness 虚拟路径不能归一化，必须先由 _is_virtual_path 拦截。
        if tool_name in _DIRECT_PATH_ARGUMENTS:
            field = _DIRECT_PATH_ARGUMENTS[tool_name]
            raw = args.get(field)
            if isinstance(raw, str) and raw and not _is_virtual_path(raw):
                args[field] = self.policy.to_virtual_path(raw)
        elif tool_name in _SEARCH_TOOLS and args.get("path") is not None:
            raw = args["path"]
            if isinstance(raw, str) and raw and not _is_virtual_path(raw):
                args["path"] = self.policy.to_virtual_path(raw)

        try:
            if tool_name in _DIRECT_PATH_ARGUMENTS:
                field = _DIRECT_PATH_ARGUMENTS[tool_name]
                value = args.get(field)
                if _is_virtual_path(value):
                    if tool_name != "read_file":
                        raise ValueError("/.harness 仅允许通过 read_file 只读分页访问")
                    _validate_virtual_read_path(value)
                else:
                    resolved = self.policy.validate_direct_path(value, tool_name=tool_name)
                    args[field] = self._backend_path(resolved)
            elif tool_name in _SEARCH_TOOLS:
                # 未传 path 时由 LocalShellBackend 以 root_dir 搜索；这是工作区内
                # 的安全默认值。显式 path 必须仍通过 canonical containment。
                if args.get("path") is not None:
                    resolved = self.policy.validate_search_path(args["path"], tool_name=tool_name)
                    args["path"] = self._backend_path(resolved)
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
        """把已验证的宿主绝对路径转换为 LocalShellBackend 的虚拟路径。"""
        relative = resolved.relative_to(self.policy.workspace)
        return "/" if not relative.parts else f"/{relative.as_posix()}"

    def allows_approval(self, request: ToolCallRequest) -> bool:
        """供 HITL 的 ``when`` 预检复用路径规则，避免越界调用先请求审批。

        此方法不替代 ``wrap_tool_call``：模型输出到实际执行之间仍可能被修改，
        因此后者必须继续作为最终的工具执行边界。
        """
        # 预检只能判断，不能把绝对路径提前改写成后端虚拟路径。实际执行还会
        # 再经过一次校验；若此处污染原始 args，第二次会把 `/file` 误认成宿主
        # 绝对路径并拒绝工作区内调用。
        return self._validate_tool_call(request, rewrite_backend_path=False) is None

    def _rejection(self, tool_name: str, tool_call_id: object, reason: str) -> ToolMessage:
        """将策略失败转成模型可纠正的错误结果，而不是抛出图执行异常。"""
        return ToolMessage(
            content=(
                f"工作区边界拒绝 {tool_name}：{reason}。"
                "请使用当前工作目录内的绝对路径；"
                "glob/grep 可省略 path 参数，此时默认从工作区根目录搜索。"
            ),
            name=tool_name or "filesystem",
            tool_call_id=str(tool_call_id or "workspace-boundary"),
            status="error",
        )

    def _maybe_handle_outside_write(self, request: ToolCallRequest) -> ToolMessage | None:
        """越界写入定向放行入口；非越界写入返回 None 走常规边界校验。

        必须在 ``_validate_tool_call`` 之前调用：常规校验会拒绝越界路径，
        而这里按审批模式决定拒绝或真实写出。直写不再调用 handler，因此
        不经过内层的虚拟后端与并发锁；越界写入不触碰 thread 虚拟文件等
        进程内共享状态，且 deny 规则等硬策略在本中间件外层已先行裁决。
        """
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            return None
        target = resolve_outside_workspace_write(tool_name, args, self.policy.workspace)
        if target is None:
            return None

        raw_path = str(args.get(_DIRECT_PATH_ARGUMENTS[tool_name]) or target)
        tool_call_id = str(tool_call.get("id") or "workspace-boundary")
        if self.approval_mode == "plan":
            return self._rejection(tool_name, tool_call_id, "计划模式禁止写入工作区外的文件")
        if tool_name == "write_file":
            return self._perform_outside_write_file(raw_path, target, args, tool_call_id)
        if tool_name == "edit_file":
            return self._perform_outside_edit_file(raw_path, target, args, tool_call_id)
        return self._perform_outside_delete_file(tool_name, raw_path, target, tool_call_id)

    def _perform_outside_write_file(
        self, raw_path: str, target: Path, args: dict[str, Any], tool_call_id: str
    ) -> ToolMessage:
        """复现 deepagents 后端的 write 语义：已存在报错、自动建父目录、不做换行转换。"""
        content = args.get("content")
        if not isinstance(content, str):
            return self._rejection("write_file", tool_call_id, "content 参数必须是字符串")
        try:
            if target.exists():
                return ToolMessage(
                    content=(
                        f"Cannot write to {raw_path} because it already exists. "
                        "Read and then make an edit, or write to a new path."
                    ),
                    name="write_file",
                    tool_call_id=tool_call_id,
                    status="error",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            # 与后端一致：O_NOFOLLOW 避免透过符号链接写出。
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(target, flags, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                file.write(content)
        except (OSError, UnicodeEncodeError) as exc:
            return ToolMessage(
                content=f"Error writing file '{raw_path}': {exc}",
                name="write_file",
                tool_call_id=tool_call_id,
                status="error",
            )
        return ToolMessage(
            content=f"Updated file {raw_path}",
            name="write_file",
            tool_call_id=tool_call_id,
            status="success",
        )

    def _perform_outside_edit_file(
        self, raw_path: str, target: Path, args: dict[str, Any], tool_call_id: str
    ) -> ToolMessage:
        """复现 deepagents 后端的 edit 语义：换行归一化、唯一匹配或 replace_all。"""
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return self._rejection("edit_file", tool_call_id, "old_string/new_string 参数必须是字符串")
        replace_all = bool(args.get("replace_all", False))
        try:
            if not target.exists() or not target.is_file():
                return ToolMessage(
                    content=f"Error: File '{raw_path}' not found",
                    name="edit_file",
                    tool_call_id=tool_call_id,
                    status="error",
                )
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, encoding="utf-8") as file:
                content = file.read()
            # 与后端一致：读入为 universal newlines，old/new 也需归一化后再匹配。
            old_normalized = old_string.replace("\r\n", "\n").replace("\r", "\n")
            new_normalized = new_string.replace("\r\n", "\n").replace("\r", "\n")
            result = perform_string_replacement(content, old_normalized, new_normalized, replace_all)
            if isinstance(result, str):
                return ToolMessage(
                    content=result, name="edit_file", tool_call_id=tool_call_id, status="error"
                )
            new_content, occurrences = result
            flags = os.O_WRONLY | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(target, flags)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                file.write(new_content)
        except (OSError, UnicodeDecodeError, UnicodeEncodeError) as exc:
            return ToolMessage(
                content=f"Error editing file '{raw_path}': {exc}",
                name="edit_file",
                tool_call_id=tool_call_id,
                status="error",
            )
        return ToolMessage(
            content=f"Successfully replaced {occurrences} instance(s) of the string in '{raw_path}'",
            name="edit_file",
            tool_call_id=tool_call_id,
            status="success",
        )

    def _perform_outside_delete_file(
        self, tool_name: str, raw_path: str, target: Path, tool_call_id: str
    ) -> ToolMessage:
        """复现 harness delete_file 工具的 JSON 结果契约。"""
        try:
            if not target.exists():
                payload: dict[str, Any] = {"success": False, "error": f"文件不存在：{raw_path}"}
            elif target.is_dir():
                payload = {"success": False, "error": f"不允许删除目录：{raw_path}"}
            else:
                target.unlink()
                payload = {"success": True, "deleted": raw_path}
        except OSError as exc:
            payload = {"success": False, "error": str(exc)}
        return ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            name=tool_name,
            tool_call_id=tool_call_id,
            status="success" if payload["success"] else "error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步入口先裁决越界写入，再执行路径策略，拒绝后不让底层工具获得调用机会。"""
        if (handled := self._maybe_handle_outside_write(request)) is not None:
            return handled
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步入口复用同步验证逻辑，确保两种调用方式安全语义一致。"""
        if (handled := self._maybe_handle_outside_write(request)) is not None:
            return handled
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return await handler(request)


def resolve_outside_workspace_write(
    tool_name: str, tool_args: object, workspace_root: str | Path | None
) -> Path | None:
    """调用为工作区外文件写入时解析并返回真实目标路径，否则返回 None。

    HITL 组合预检与边界中间件共用同一判定，保证"是否弹窗"与"是否直写"
    基于完全相同的路径语义。以下情形不属于可放行的越界写入，交由常规
    边界校验处理（拒绝或按工作区内流程执行）：

    - 非写入类工具、路径缺失或不是绝对路径（含相对路径）；
    - ``/.harness`` 虚拟路径、UNC 路径、含 ``..`` 的穿越路径；
    - 路径经真实解析后仍在工作区内。
    """
    if tool_name not in _OUTSIDE_WRITE_TOOLS or workspace_root is None:
        return None
    if not isinstance(tool_args, dict):
        return None
    raw = tool_args.get(_DIRECT_PATH_ARGUMENTS[tool_name])
    if not isinstance(raw, str) or not raw:
        return None
    # 相对路径不参与定向放行：其解析结果依赖进程工作目录，语义不可预期。
    if not (raw.startswith("/") or _WINDOWS_ABSOLUTE_PATH.match(raw)):
        return None
    if _is_virtual_path(raw):
        return None
    normalized = raw.replace("\\", "/")
    if raw.startswith("\\") or normalized.startswith("//"):
        return None
    if ".." in PurePosixPath(normalized).parts:
        return None
    workspace = Path(workspace_root).resolve(strict=False)
    if raw.startswith("/") and sys.platform == "win32":
        # Windows virtual_mode 下 `/...` 是工作区相对路径，拼回工作区解析。
        candidate = (workspace / raw.lstrip("/")).resolve(strict=False)
    else:
        candidate = Path(raw).resolve(strict=False)
    try:
        candidate.relative_to(workspace)
    except (ValueError, OSError, RuntimeError):
        return candidate
    return None


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
