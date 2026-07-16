"""在 Agent 工具调用边界执行审批模式，而不是由 TUI 模拟批准。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from harness_agent.approval_mode import ApprovalMode

_DEFAULT_HITL_TOOLS = frozenset(
    {"execute", "write_file", "edit_file", "delete", "task"}
)
_AUTO_EDIT_HITL_TOOLS = frozenset({"execute", "delete", "task"})
_PLAN_ALLOWED_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "ask_user",
        "write_todos",
        # 压缩只维护 LangGraph 会话上下文，不触碰文件、命令或外部资源。
        "compact_conversation",
    }
)


def interrupt_on_for_approval_mode(
    approval_mode: ApprovalMode,
    *,
    preflight: Callable[[ToolCallRequest], bool] | None = None,
) -> dict[str, Any] | None:
    """返回应由 HumanInTheLoopMiddleware 拦截的工具集合。

    计划模式的拒绝由 ``PlanModeMiddleware`` 完成，YOLO 则只关闭 Harness
    的人工确认；工作区、Shell 和远端 provider 等硬策略不在这里放宽。
    """
    if approval_mode in {"plan", "yolo"}:
        return None
    tool_names = (
        _DEFAULT_HITL_TOOLS
        if approval_mode == "default"
        else _AUTO_EDIT_HITL_TOOLS
    )
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

    approval = InterruptOnConfig(allowed_decisions=["approve", "reject"])
    if preflight is not None:
        # HumanInTheLoopMiddleware 在实际 ToolNode 之前暂停。把与执行守卫
        # 共用的预检挂在 `when`，越界文件调用就不会产生无法批准的假审批。
        approval["when"] = preflight
    return {name: approval for name in tool_names}


def approval_mode_prompt(approval_mode: ApprovalMode) -> str:
    """生成追加到系统提示词的模式事实，不让项目指令改变实际策略。"""
    if approval_mode == "plan":
        return """

## 审批模式：计划

当前是严格计划模式。只能调查工作区、提出问题和维护任务清单；不要尝试
修改文件、执行命令、运行解释器、调用子 Agent 或 MCP。请基于已读取的证据
输出可实施的计划。服务端会拒绝未允许的工具调用，不能通过审批绕过。
"""
    if approval_mode == "auto-edit":
        return """

## 审批模式：自动编辑

工作区内的文件创建和编辑会自动执行；命令执行、删除和子 Agent 仍会等待
用户审批。工作区边界和其他安全策略始终有效。
"""
    if approval_mode == "yolo":
        return """

## 审批模式：YOLO

Harness 不会为工具调用请求人工审批。工作区边界、Shell 白名单、远端沙箱
和其他硬性安全策略仍然有效，不能通过此模式绕过。
"""
    return """

## 审批模式：默认确认

文件创建或编辑、命令执行、删除和子 Agent 都需要用户确认；工作区边界和
其他硬性安全策略始终有效。
"""


class PlanModeMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """以工具白名单强制计划模式只读，未知的未来工具也默认拒绝。"""

    def _rejection(self, request: ToolCallRequest) -> ToolMessage:
        """返回可指导模型继续调研和输出计划的错误结果。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", "unknown"))
        return ToolMessage(
            content=(
                f"计划模式拒绝 {tool_name}：当前模式不执行此工具。"
                "请使用读取或搜索工具收集证据，并向用户输出实施计划。"
            ),
            name=tool_name,
            tool_call_id=str(tool_call.get("id") or "plan-mode"),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步调用只放行明确声明为只读或会话维护的工具。"""
        if request.tool_call.get("name") not in _PLAN_ALLOWED_TOOLS:
            return self._rejection(request)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步调用沿用相同白名单，避免执行路径产生策略漂移。"""
        if request.tool_call.get("name") not in _PLAN_ALLOWED_TOOLS:
            return self._rejection(request)
        return await handler(request)
