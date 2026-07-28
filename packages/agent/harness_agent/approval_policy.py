"""在 Agent 工具调用边界执行审批模式，而不是由 TUI 模拟批准。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from harness_agent.approval_mode import ApprovalMode
from harness_agent.tool_risk import ToolKind, get_tool_kind, get_mode_permission, is_read_only
from harness_agent.permission_rules import PermissionRule, evaluate_rules
from harness_agent.sensitive_paths import requires_safety_check

_DEFAULT_HITL_TOOLS = frozenset(
    {"execute", "write_file", "edit_file", "delete", "delete_file", "task", "web_fetch", "apply_patch", "monitor", "task_stop"}
)
_AUTO_EDIT_HITL_TOOLS = frozenset({"execute", "delete", "delete_file", "task", "web_fetch", "monitor", "task_stop"})
_PLAN_ALLOWED_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "ask_user",
        "write_todos",
        "web_search",
        "lsp",
        "tool_search",
        "memory_search",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)


def interrupt_on_for_approval_mode(
    approval_mode: ApprovalMode,
    *,
    preflight: Callable[[ToolCallRequest], bool] | None = None,
    extra_interrupt_tools: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """返回应由 HumanInTheLoopMiddleware 拦截的工具集合。

    计划模式的拒绝由 ``PlanModeMiddleware`` 完成，YOLO 则只关闭 Harness
    的人工确认；工作区、Shell 和远端 provider 等硬策略不在这里放宽。

    Args:
        extra_interrupt_tools: 需要一并纳入审批的额外工具名（如 MCP 工具）。
            仅在 default 和 auto-edit 模式下生效；plan 和 yolo 忽略。
    """
    if approval_mode in {"plan", "yolo"}:
        return None
    tool_names = (
        _DEFAULT_HITL_TOOLS
        if approval_mode == "default"
        else _AUTO_EDIT_HITL_TOOLS
    )
    if extra_interrupt_tools:
        tool_names = tool_names | extra_interrupt_tools
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
        """同步调用只放行明确声明为只读或 thread 维护的工具。"""
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


# ---------------------------------------------------------------------------
# 多级审批流水线
# ---------------------------------------------------------------------------

PermissionDecision = Literal["allow", "ask", "deny"]
"""审批流水线最终决策：allow 直接执行，ask 需要用户审批，deny 硬拒绝。"""


def evaluate_permission(
    tool_name: str,
    tool_args: dict[str, Any],
    approval_mode: ApprovalMode,
    rules: list[PermissionRule] | None = None,
) -> PermissionDecision:
    """多级审批流水线：L2 deny 硬拦截 → L3 只读放行 → L4 规则评估 → L5 模式覆盖。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典。
        approval_mode: 当前审批模式。
        rules: 已合并的权限规则列表（session + project + user），按优先级排列。

    Returns:
        "allow" 直接执行，"ask" 需要用户审批，"deny" 硬拒绝。
    """
    # L2: deny 规则硬拦截（任何模式不可覆盖）
    if rules:
        resource = _extract_resource(tool_name, tool_args)
        effect = evaluate_rules(tool_name, resource, rules)
        if effect == "deny":
            return "deny"

    # L3: 只读工具放行（READ/INTERACT/PLAN 类别）
    if is_read_only(tool_name):
        return "allow"

    # 敏感路径 safetyCheck（yolo 免疫）
    if requires_safety_check(tool_name, tool_args):
        return "ask"

    # L4: 规则评估（allow/ask 规则）
    if rules:
        resource = _extract_resource(tool_name, tool_args)
        effect = evaluate_rules(tool_name, resource, rules)
        if effect == "allow":
            return "allow"
        if effect == "ask":
            return "ask"

    # L5: 审批模式覆盖（按 ToolKind 查表）
    kind = get_tool_kind(tool_name)
    mode_perm = get_mode_permission(kind, approval_mode)
    return mode_perm  # type: ignore[return-value]


def _extract_resource(tool_name: str, tool_args: dict[str, Any]) -> str:
    """从工具参数中提取资源标识，用于规则匹配。

    - execute/monitor: 使用 command 参数
    - write_file/edit_file/delete_file: 使用 file_path 参数
    - web_fetch: 使用 url 参数
    - 其他: 使用 "*" 通配
    """
    if tool_name in ("execute", "monitor"):
        return str(tool_args.get("command", "*"))
    if tool_name in ("write_file", "edit_file", "delete_file", "apply_patch"):
        return str(tool_args.get("file_path", "*"))
    if tool_name == "web_fetch":
        return str(tool_args.get("url", "*"))
    return "*"
