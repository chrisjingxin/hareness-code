"""角色级有效能力视图与工具执行守卫。

本模块把静态 ExecutionPolicy 投影为一次构图使用的不可变能力集合。模型工具
schema、实际 ToolNode 调用、MCP 工具和 Skill catalog 都消费同一个结果，避免
“提示词里隐藏了工具，但伪造 tool call 仍能执行”的权限漂移。
"""

from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from harness_agent.runtime.agent_catalog import EffectiveExecutionPolicy, StringRule
from harness_agent.threads.prompting import canonical_json, sha256_text


FILESYSTEM_READ_TOOLS = frozenset({"ls", "read_file", "glob", "grep", "lsp"})
"""会直接读取工作区文件的内置工具。"""

FILESYSTEM_WRITE_TOOLS = frozenset(
    {"write_file", "edit_file", "delete", "delete_file", "apply_patch"}
)
"""会修改工作区文件的内置工具。"""

SHELL_TOOLS = frozenset({"execute", "monitor"})
"""会启动本机或远端命令的工具。"""

NETWORK_TOOLS = frozenset({"web_search", "web_fetch"})
"""会访问网络的 Harness 工具。"""

DELEGATION_TOOLS = frozenset({"task", "task_output", "task_stop"})
"""会创建或控制子 Agent execution 的工具。"""

BUILTIN_TOOL_NAMES = frozenset(
    {
        *FILESYSTEM_READ_TOOLS,
        *FILESYSTEM_WRITE_TOOLS,
        *SHELL_TOOLS,
        *NETWORK_TOOLS,
        *DELEGATION_TOOLS,
        "write_todos",
        "ask_user",
        "tool_search",
        "enter_plan_mode",
        "exit_plan_mode",
        "memory_search",
        "memory_save",
    }
)
"""当前 DeepAgents 与 Harness 共同提供的稳定内置工具名。"""

_DIRECT_PATH_ARGUMENTS = {
    "ls": "path",
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "delete": "file_path",
    "delete_file": "file_path",
    "lsp": "file_path",
}


@dataclass(frozen=True, slots=True)
class EffectiveCapabilityView:
    """由有效 Policy 和实际资源快照求得的最小权限能力集合。"""

    tool_names: tuple[str, ...]
    mcp_tool_names: tuple[str, ...]
    skill_ids: tuple[str, ...]
    filesystem_read: tuple[str, ...] | None
    filesystem_write: tuple[str, ...] | None
    shell_commands: StringRule | None
    policy_fingerprint: str

    @property
    def fingerprint(self) -> str:
        """返回实际能力集合指纹；描述文字和来源路径不参与。"""
        return sha256_text(
            canonical_json(
                {
                    "tools": list(self.tool_names),
                    "mcp_tools": list(self.mcp_tool_names),
                    "skills": list(self.skill_ids),
                    "filesystem_read": (
                        list(self.filesystem_read)
                        if self.filesystem_read is not None
                        else None
                    ),
                    "filesystem_write": (
                        list(self.filesystem_write)
                        if self.filesystem_write is not None
                        else None
                    ),
                    "shell_commands": (
                        self.shell_commands.record()
                        if self.shell_commands is not None
                        else None
                    ),
                    "policy": self.policy_fingerprint,
                }
            )
        )

    def allows_tool(self, name: str) -> bool:
        """判断稳定工具名是否同时通过资源快照和 Policy。"""
        return name in self.tool_names


def resolve_effective_capability_view(
    policy: EffectiveExecutionPolicy,
    *,
    available_tools: Iterable[str],
    mcp_tool_names: Iterable[str] = (),
    available_skill_ids: Iterable[str] = (),
) -> EffectiveCapabilityView:
    """将 Policy 收敛到当前真实资源；未知名称永远不会凭 Policy 被凭空授权。"""
    available = {name for name in available_tools if name}
    mcp_available = {name for name in mcp_tool_names if name} & available
    visible = {
        name
        for name in available
        if _rule_allows(policy.tools, name)
    }

    if policy.filesystem_read == ():
        visible.difference_update(FILESYSTEM_READ_TOOLS)
    if policy.filesystem_write == ():
        visible.difference_update(FILESYSTEM_WRITE_TOOLS)
    elif (
        policy.filesystem_write is not None
        and policy.filesystem_write != ("**/*",)
    ):
        # apply_patch 可同时包含多个目标，进入执行前无法可靠证明所有路径均属于
        # 一个有限 glob 子集，因此在路径受限角色中保守隐藏。
        visible.discard("apply_patch")
    if policy.shell is not None and not policy.shell.enabled:
        visible.difference_update(SHELL_TOOLS)
    if policy.network is not None and not policy.network.enabled:
        visible.difference_update(NETWORK_TOOLS)
    if policy.delegation is not None and not policy.delegation.enabled:
        visible.difference_update(DELEGATION_TOOLS)

    allowed_mcp = {
        name
        for name in mcp_available
        if name in visible and _rule_allows(policy.mcp_tools, name)
    }
    visible.difference_update(mcp_available - allowed_mcp)
    allowed_skills = tuple(
        sorted(
            skill_id
            for skill_id in set(available_skill_ids)
            if _rule_allows(policy.skills, skill_id)
        )
    )
    return EffectiveCapabilityView(
        tool_names=tuple(sorted(visible)),
        mcp_tool_names=tuple(sorted(allowed_mcp)),
        skill_ids=allowed_skills,
        filesystem_read=policy.filesystem_read,
        filesystem_write=policy.filesystem_write,
        shell_commands=policy.shell.commands if policy.shell and policy.shell.enabled else None,
        policy_fingerprint=policy.fingerprint,
    )


def _rule_allows(rule: StringRule | None, value: str) -> bool:
    """应用有限 allow/deny 规则；deny 始终优先。"""
    if rule is None:
        return True
    if value in rule.deny:
        return False
    return rule.allow is None or value in rule.allow


def _tool_name(tool: object) -> str:
    """从 LangChain 工具或规范化 mapping 读取稳定名称。"""
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))


class CapabilityPolicyMiddleware(AgentMiddleware):
    """在模型请求和工具执行两层强制同一 EffectiveCapabilityView。"""

    def __init__(
        self,
        view: EffectiveCapabilityView,
        *,
        workspace: str | Path,
    ) -> None:
        """固定能力视图和路径匹配基准，运行期间不接受可变配置。"""
        super().__init__()
        self._view = view
        self._workspace = Path(workspace).resolve(strict=False)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """同步模型调用只暴露已授权工具 schema。"""
        return handler(request.override(tools=self._visible_tools(request.tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """异步模型调用使用与同步入口相同的工具集合。"""
        return await handler(request.override(tools=self._visible_tools(request.tools)))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步工具入口拒绝伪造名称和越过角色路径子集的调用。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步工具入口复用同一 fail-closed 校验。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return await handler(request)

    def _visible_tools(self, tools: Sequence[object]) -> list[object]:
        """保持上游顺序，仅删除能力视图之外的 schema。"""
        return [tool for tool in tools if self._view.allows_tool(_tool_name(tool))]

    def _validate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        """返回拒绝消息或 None；拒绝时绝不调用底层 handler。"""
        call = request.tool_call
        name = str(call.get("name", ""))
        if not self._view.allows_tool(name):
            return self._rejection(call, f"角色未授权工具 {name or 'unknown'}")
        args = call.get("args") or {}
        if not isinstance(args, dict):
            return self._rejection(call, "工具参数必须是对象")
        patterns = self._path_patterns(name)
        if patterns is not None and patterns != ("**/*",):
            raw_path = self._path_argument(name, args)
            if raw_path is None:
                if name in {"glob", "grep"}:
                    relative = "."
                else:
                    return self._rejection(call, "无法证明目标路径属于角色授权范围")
            else:
                try:
                    relative = self._relative_path(raw_path)
                except ValueError as exc:
                    return self._rejection(call, str(exc))
            if not any(_matches_path(relative, pattern) for pattern in patterns):
                return self._rejection(call, "目标路径不在角色授权范围内")
        return None

    def _path_patterns(self, tool_name: str) -> tuple[str, ...] | None:
        """按工具风险返回读或写路径规则。"""
        if tool_name in FILESYSTEM_READ_TOOLS:
            return self._view.filesystem_read
        if tool_name in FILESYSTEM_WRITE_TOOLS:
            return self._view.filesystem_write
        return None

    def _path_argument(self, tool_name: str, args: dict[str, Any]) -> object | None:
        """读取文件工具的直接路径；搜索工具允许省略路径。"""
        if tool_name in _DIRECT_PATH_ARGUMENTS:
            return args.get(_DIRECT_PATH_ARGUMENTS[tool_name])
        if tool_name in {"glob", "grep"}:
            return args.get("path")
        return None

    def _relative_path(self, value: object) -> str:
        """把宿主绝对路径或 DeepAgents 虚拟路径转换成工作区相对路径。"""
        if not isinstance(value, str) or not value:
            raise ValueError("文件路径必须是非空字符串")
        normalized = value.replace("\\", "/")
        if ".." in PurePosixPath(normalized).parts:
            raise ValueError("文件路径不能包含 '..' 路径段")
        if normalized.startswith("/.harness/"):
            return normalized.lstrip("/")
        candidate = Path(value).resolve(strict=False)
        try:
            return candidate.relative_to(self._workspace).as_posix() or "."
        except ValueError:
            # DeepAgents 本机 backend 使用 `/src/file` 表示工作区虚拟路径；
            # 真实宿主绝对路径若不在工作区，后续 WorkspaceBoundary 仍会拒绝。
            return normalized.lstrip("/")

    def _rejection(self, call: dict[str, Any], reason: str) -> ToolMessage:
        """把授权失败转换为稳定错误结果，避免图异常或审批误导。"""
        name = str(call.get("name", "")) or "policy"
        return ToolMessage(
            content=f"角色能力策略拒绝 {name}：{reason}。",
            name=name,
            tool_call_id=str(call.get("id") or "capability-policy"),
            status="error",
        )


def _matches_path(relative: str, pattern: str) -> bool:
    """让 `src/**` 同时匹配目录本身及其后代，其他规则使用 POSIX glob。"""
    normalized = relative.strip("/")
    if pattern == "**/*":
        return True
    prefix = pattern.removesuffix("/**")
    if prefix != pattern and (normalized == prefix or normalized.startswith(f"{prefix}/")):
        return True
    return fnmatch.fnmatchcase(normalized, pattern)
