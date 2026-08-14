"""本机文件工具的工作区路径边界中间件。

文件工具接受主工作区 ``/`` 虚拟路径，以及经 ``WorkspaceRootRegistry`` 授权的
外部绝对路径。未授权的外部路径在预检阶段可进入目录信任审批；非法路径
（``..``、UNC、不可注册目录）仍硬拒绝。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from harness_agent.policy.workspace_roots import (
    DirectoryNotTrustable,
    ExternalPathNotTrusted,
    TrustCandidate,
    WorkspaceRootRegistry,
)
from harness_agent.tools.file_tool_catalog import DIRECT_FILE_TOOL_PATH_ARGUMENTS

_DIRECT_PATH_ARGUMENTS = DIRECT_FILE_TOOL_PATH_ARGUMENTS
_SEARCH_TOOLS = frozenset({"glob", "grep"})
_VIRTUAL_ROOT = "/.harness"
_READ_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})


class WorkspacePathPolicy:
    """判定路径是否属于允许根集合，并返回 backend 路径。"""

    def __init__(self, registry: WorkspaceRootRegistry | str | Path) -> None:
        """绑定可变的根注册表（也可传入主工作区路径，测试兼容）。"""
        if isinstance(registry, WorkspaceRootRegistry):
            self.registry = registry
        else:
            self.registry = WorkspaceRootRegistry(registry, load_persisted=False)

    @property
    def workspace(self) -> Path:
        """主工作区根路径（兼容旧调用方）。"""
        return self.registry.primary.path

    def validate_direct_path(self, value: object, *, tool_name: str) -> Path:
        """验证直接文件工具路径，并返回工作区内的字面宿主路径。

        额外根路径返回真实宿主 Path；主根返回拼接后的字面路径（不解引用 symlink）。
        """
        raw = self._require_path_string(value, tool_name=tool_name, field="path")
        resolved = self.registry.resolve(raw)
        if resolved.root.root_id == "primary":
            return self.registry.primary.path / resolved.backend_path.lstrip("/")
        return Path(resolved.display_path)

    def validate_search_path(self, value: object, *, tool_name: str) -> Path:
        """验证 glob/grep 显式指定的搜索根。"""
        return self.validate_direct_path(value, tool_name=tool_name)

    def backend_path_for(self, value: object, *, tool_name: str, field: str = "path") -> str:
        """验证路径并返回 backend 虚拟路径。"""
        raw = self._require_path_string(value, tool_name=tool_name, field=field)
        return self.registry.resolve(raw).backend_path

    def validate_search_pattern(self, value: object, *, tool_name: str, field: str) -> None:
        """拒绝可将 glob 搜索根移出工作区的绝对或父级模式。"""
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} 必须是非空字符串")
        normalized = value.replace("\\", "/")
        # 搜索 pattern 仍禁止绝对路径与 ..；外部目录通过 path 参数授权
        from pathlib import PureWindowsPath

        if normalized.startswith("/") or PureWindowsPath(value).is_absolute():
            raise ValueError(f"{field} 不能是绝对路径模式")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError(f"{field} 不能包含 '..' 路径段")

    def resolve_or_candidate(
        self,
        value: object,
        *,
        tool_name: str,
        field: str = "path",
        run_id: str | None = None,
    ) -> tuple[str | None, TrustCandidate | None]:
        """解析路径；未授权外部路径返回候选，非法路径抛 ValueError。"""
        raw = self._require_path_string(value, tool_name=tool_name, field=field)
        if _is_virtual_path(raw):
            _validate_virtual_read_path(raw)
            return raw, None
        try:
            return self.registry.resolve(raw, run_id=run_id).backend_path, None
        except ExternalPathNotTrusted as exc:
            if exc.candidate.reason:
                raise DirectoryNotTrustable(exc.candidate.reason) from exc
            return None, exc.candidate

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


class WorkspaceBoundaryMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """在本机文件工具进入 backend 前强制工作区 containment。"""

    def __init__(
        self,
        workspace: str | Path | WorkspaceRootRegistry,
        *,
        auto_trust_session: bool = False,
        allow_trust_prompt: bool = True,
    ) -> None:
        """绑定根注册表。

        Args:
            workspace: 主工作区路径或已有 registry。
            auto_trust_session: yolo 模式下遇到可信任外部路径时自动授予 session 根。
            allow_trust_prompt: 为 False 时（子 Agent）未授权外部路径硬拒绝。
        """
        super().__init__()
        if isinstance(workspace, WorkspaceRootRegistry):
            self.registry = workspace
        else:
            self.registry = WorkspaceRootRegistry(workspace, load_persisted=False)
        self.policy = WorkspacePathPolicy(self.registry)
        self.auto_trust_session = auto_trust_session
        self.allow_trust_prompt = allow_trust_prompt

    def _validate_tool_call(
        self,
        request: ToolCallRequest,
        *,
        rewrite_backend_path: bool = True,
        pending_trust: list[TrustCandidate] | None = None,
    ) -> ToolMessage | None:
        """检查受管工具参数；失败时不让任何内层 handler 收到调用。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        source_args = tool_call.get("args") or {}
        if not isinstance(source_args, dict):
            return self._rejection(tool_name, tool_call.get("id"), "工具参数必须是对象")
        args = source_args if rewrite_backend_path else dict(source_args)
        run_id = self._current_run_id(request)

        try:
            if tool_name in _DIRECT_PATH_ARGUMENTS:
                field = _DIRECT_PATH_ARGUMENTS[tool_name]
                value = args.get(field)
                if _is_virtual_path(value):
                    if tool_name != "read_file":
                        raise ValueError("/.harness 仅允许通过 read_file 只读分页访问")
                    _validate_virtual_read_path(value)
                elif tool_name != "ls" and value == "/":
                    raise ValueError("文件路径不能是根目录")
                else:
                    backend_path = self._resolve_path_arg(
                        value,
                        tool_name=tool_name,
                        field=field,
                        pending_trust=pending_trust,
                        run_id=run_id,
                    )
                    if backend_path is not None and rewrite_backend_path:
                        args[field] = backend_path
            elif tool_name in _SEARCH_TOOLS:
                if args.get("path") is not None:
                    backend_path = self._resolve_path_arg(
                        args.get("path"),
                        tool_name=tool_name,
                        field="path",
                        pending_trust=pending_trust,
                        run_id=run_id,
                    )
                    if backend_path is not None and rewrite_backend_path:
                        args["path"] = backend_path
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
        except (ValueError, DirectoryNotTrustable) as exc:
            return self._rejection(tool_name, tool_call.get("id"), str(exc))
        return None

    def _current_run_id(self, request: ToolCallRequest) -> str | None:
        """从 RunContext 取出当前 run_id，供 once 作用域匹配。"""
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None) if runtime is not None else None
        run_id = getattr(context, "run_id", None)
        return str(run_id) if run_id else None

    def _resolve_path_arg(
        self,
        value: object,
        *,
        tool_name: str,
        field: str,
        pending_trust: list[TrustCandidate] | None,
        run_id: str | None = None,
    ) -> str | None:
        """解析单个路径参数；可信任外部路径按模式自动授予或记入 pending。"""
        try:
            backend_path, candidate = self.policy.resolve_or_candidate(
                value, tool_name=tool_name, field=field, run_id=run_id
            )
        except DirectoryNotTrustable as exc:
            raise ValueError(str(exc)) from exc
        if candidate is None:
            return backend_path
        if self.auto_trust_session:
            self.registry.trust(candidate.directory, "session")
            return self.registry.resolve(str(value), run_id=run_id).backend_path
        if not self.allow_trust_prompt:
            raise ValueError(
                f"只能访问已授权工作目录内的文件；`{candidate.target_path}` 尚未被信任"
            )
        if pending_trust is not None:
            pending_trust.append(candidate)
            return None
        raise ValueError(
            f"路径 `{candidate.target_path}` 不在允许的工作区内，需要先信任目录 "
            f"`{candidate.directory}`"
        )

    def allows_approval(self, request: ToolCallRequest) -> bool:
        """审批预检：合法工作区内路径、或可信任的外部路径返回 True。"""
        pending: list[TrustCandidate] = []
        rejection = self._validate_tool_call(
            request, rewrite_backend_path=False, pending_trust=pending
        )
        if rejection is not None:
            return False
        # 可信任外部路径：需要弹目录信任卡片
        if pending:
            return True
        return True

    def needs_directory_trust(self, request: ToolCallRequest) -> TrustCandidate | None:
        """若调用需要目录信任审批，返回候选；否则返回 None。"""
        pending: list[TrustCandidate] = []
        rejection = self._validate_tool_call(
            request, rewrite_backend_path=False, pending_trust=pending
        )
        if rejection is not None:
            return None
        return pending[0] if pending else None

    def canonical_approval_request(self, request: ToolCallRequest) -> ToolCallRequest | None:
        """返回仅供审批 prepare 使用的 canonical 路径副本，不污染后续执行参数。"""
        tool_call = request.tool_call
        source_args = tool_call.get("args") or {}
        if not isinstance(source_args, dict):
            return None
        # 目录信任审批尚未扩根，无法 canonicalize 到 backend 路径
        if self.needs_directory_trust(request) is not None:
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
                "主工作区请使用以 `/` 开头的虚拟路径；"
                "工作区外请使用真实绝对路径（将触发目录信任确认）；"
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
        """同步入口：拒绝非法路径；执行后消费 once 授权。"""
        once_dirs = self._once_directories_for_request(request)
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        try:
            return handler(request)
        finally:
            self._consume_once_directories(once_dirs, request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步入口：与同步路径相同的边界与 once 消费语义。"""
        once_dirs = self._once_directories_for_request(request)
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        try:
            return await handler(request)
        finally:
            self._consume_once_directories(once_dirs, request)

    def _once_directories_for_request(self, request: ToolCallRequest) -> list[Path]:
        """收集当前调用将使用的 once 授权目录（执行前快照）。"""
        run_id = self._current_run_id(request)
        if not run_id:
            return []
        tool_call = request.tool_call
        args = tool_call.get("args") or {}
        if not isinstance(args, dict):
            return []
        values: list[object] = []
        name = str(tool_call.get("name") or "")
        if name in _DIRECT_PATH_ARGUMENTS:
            values.append(args.get(_DIRECT_PATH_ARGUMENTS[name]))
        elif name in _SEARCH_TOOLS and args.get("path") is not None:
            values.append(args.get("path"))
        directories: list[Path] = []
        for value in values:
            if not isinstance(value, str) or not value or _is_virtual_path(value):
                continue
            try:
                resolved = self.registry.resolve(value, run_id=run_id)
            except (ExternalPathNotTrusted, ValueError, DirectoryNotTrustable):
                continue
            if resolved.root.scope == "once":
                directories.append(resolved.root.path)
        return directories

    def _consume_once_directories(
        self, directories: list[Path], request: ToolCallRequest
    ) -> None:
        """工具执行后消费 once 授权，使同目录再次访问重新询问。"""
        run_id = self._current_run_id(request)
        if not run_id:
            return
        for directory in directories:
            self.registry.consume_once(directory, run_id=run_id)


def _is_virtual_path(value: object) -> bool:
    """判断路径是否指向逻辑虚拟根，不能将它交给宿主 Path.resolve。"""
    return isinstance(value, str) and (value == _VIRTUAL_ROOT or value.startswith(f"{_VIRTUAL_ROOT}/"))


def _validate_virtual_read_path(value: object) -> None:
    """校验只读虚拟路径语法；存在性与 Thread 归属由虚拟 backend 二次校验。"""
    if not isinstance(value, str) or not value.startswith(f"{_VIRTUAL_ROOT}/"):
        raise ValueError("/.harness 路径必须使用绝对逻辑路径")
    if ".." in PurePosixPath(value.replace("\\", "/")).parts:
        raise ValueError("/.harness 路径不能包含 '..' 段")
