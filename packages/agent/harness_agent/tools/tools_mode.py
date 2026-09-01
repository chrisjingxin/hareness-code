"""计划模式工具：进入提示，以及把计划文件交给用户审批。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage

from harness_agent.runtime.run_context import (
    RunContextError,
    plan_constraint_active,
    require_run_context,
)
from harness_agent.tools.plan_file import (
    PLAN_VIRTUAL_PATH,
    ensure_plan_file,
    plan_display_path,
    read_plan_markdown,
)

ENTER_PLAN_MODE_HINT = "要进入计划模式，请让用户使用 /plan 或 Shift+Tab。"
NOT_IN_PLAN_ERROR = "当前不在计划模式，不要调用 exit_plan_mode"
NO_PLAN_HANDLE_ERROR = "当前会话不能审批计划"
PLAN_EXPIRED_MESSAGE = "计划审批已结束，仍停留在计划模式。"
PLAN_ENTRY_UNAVAILABLE = "当前会话不能请求进入计划模式"
PLAN_ENTRY_REJECTED = "用户拒绝进入计划模式；保持当前审批模式。"
PLAN_ENTRY_APPROVED = (
    "已进入计划模式。请只读调查并澄清歧义，把完整计划写入 "
    f"`{PLAN_VIRTUAL_PATH}`，完成后调用 exit_plan_mode 交给用户审批。"
)


def enter_plan_mode() -> dict[str, Any]:
    """立即返回进入指引；不切换 Host 审批模式。"""
    return {"success": False, "error": ENTER_PLAN_MODE_HINT}


def exit_plan_mode() -> dict[str, Any]:
    """非计划图里的占位实现：不切档、不停交互。"""
    return {"success": False, "error": NOT_IN_PLAN_ERROR}


def request_plan_entry(request: Any) -> ToolMessage:
    """询问用户是否在当前 Run 开启计划约束；拒绝或无交互能力时 fail closed。"""
    from langgraph.types import interrupt

    tool_call = getattr(request, "tool_call", {}) or {}
    tool_name = str(tool_call.get("name") or "enter_plan_mode")
    tool_call_id = str(tool_call.get("id") or "enter_plan_mode")
    try:
        context = require_run_context(getattr(request, "runtime", None))
    except RunContextError:
        return ToolMessage(
            content=PLAN_ENTRY_UNAVAILABLE,
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    if plan_constraint_active(context):
        return ToolMessage(
            content="当前 Run 已处于计划模式。",
            name=tool_name,
            tool_call_id=tool_call_id,
            status="success",
        )

    response = interrupt(
        {
            "action_requests": [
                {
                    "name": "enter_plan_mode",
                    "args": {},
                    "description": (
                        "Agent 建议在当前 Run 进入计划模式：只允许调查并维护会话计划文件，"
                        "只读命令可运行，未知命令会单次询问；项目写入、明确会写的命令、"
                        "子 Agent 和会写的 MCP 都会被拒绝。"
                    ),
                }
            ]
        }
    )
    payload = response if isinstance(response, dict) else {}
    raw_decisions = payload.get("decisions")
    decisions = raw_decisions if isinstance(raw_decisions, list) else []
    first = decisions[0] if decisions and isinstance(decisions[0], dict) else {}
    if first.get("type") != "approve":
        return ToolMessage(
            content=PLAN_ENTRY_REJECTED,
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    try:
        ensure_plan_file(context.thread_id)
    except (OSError, ValueError):
        return ToolMessage(
            content="创建会话计划文件失败，未进入计划模式。",
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    context.plan_constraint.activate()
    return ToolMessage(
        content=PLAN_ENTRY_APPROVED,
        name=tool_name,
        tool_call_id=tool_call_id,
        status="success",
    )


def submit_plan(request: Any) -> Any:
    """读取会话计划文件并中断等人审；批准/放弃后结束当前图。"""
    from langgraph.graph import END
    from langgraph.types import Command, interrupt

    tool_call = getattr(request, "tool_call", {}) or {}
    tool_name = str(tool_call.get("name") or "exit_plan_mode")
    tool_call_id = str(tool_call.get("id") or "exit_plan_mode")

    try:
        context = require_run_context(getattr(request, "runtime", None))
    except RunContextError:
        return ToolMessage(
            content=NOT_IN_PLAN_ERROR,
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )
    if not plan_constraint_active(context):
        return ToolMessage(
            content=NOT_IN_PLAN_ERROR,
            name=tool_name,
            tool_call_id=tool_call_id,
            status="error",
        )

    markdown, has_plan = read_plan_markdown(context.thread_id)
    response = interrupt(
        {
            "type": "plan",
            "tool_call_id": tool_call_id,
            "has_plan": has_plan,
            "plan_markdown": markdown,
            "plan_virtual_path": PLAN_VIRTUAL_PATH,
            "plan_display_path": plan_display_path(context.thread_id),
            "revision": 0,
        }
    )
    payload = response if isinstance(response, dict) else {}
    decision = str(payload.get("decision") or "abandoned")
    feedback = str(payload.get("feedback") or "")
    expired = payload.get("expired") is True
    result = format_plan_decision(decision, feedback, expired=expired)
    message = plan_decision_tool_message(tool_name, tool_call_id, result)
    update = {"messages": [message]}
    if result.get("terminal"):
        return Command(update=update, goto=END)
    return Command(update=update)


def plan_decision_tool_message(tool_name: str, tool_call_id: str, result: dict[str, Any]) -> ToolMessage:
    """把计划决策编成合法 ToolMessage；status 只能是 success 或 error。"""
    return ToolMessage(
        content=str(result["message"]),
        name=tool_name,
        tool_call_id=tool_call_id,
        status="error" if result.get("error") else "success",
    )


def format_plan_decision(decision: str, feedback: str = "", *, expired: bool = False) -> dict[str, Any]:
    """把用户决策编成工具结果；超时/无 handle 不结束当前 Run。"""
    if expired:
        return {"message": NO_PLAN_HANDLE_ERROR if decision == "abandoned" else PLAN_EXPIRED_MESSAGE, "terminal": False, "error": True}
    if decision == "approved":
        return {"message": "已批准", "terminal": True}
    if decision == "revise":
        text = feedback.strip() or "用户要求继续打磨计划"
        return {"message": text, "terminal": False}
    return {"message": "已放弃", "terminal": True}
