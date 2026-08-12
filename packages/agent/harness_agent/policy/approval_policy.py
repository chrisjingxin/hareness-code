"""在 Agent 工具调用边界执行审批模式，而不是由 TUI 模拟批准。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from harness_agent.policy.approval_mode import ApprovalMode
from harness_agent.policy.auto_mode import evaluate_auto_mode
from harness_agent.policy.tool_risk import ToolKind, get_tool_kind, get_mode_permission, is_read_only
from harness_agent.policy.permission_rules import (
    PermissionRule,
    evaluate_tool_rules,
    extract_tool_resource,
)
from harness_agent.policy.sensitive_paths import requires_safety_check
from harness_agent.policy.safe_commands import is_safe_command
from harness_agent.policy.bash_floors import evaluate_safety_floors
from harness_agent.policy.bash_parser import extract_segments
from harness_agent.tools.file_tool_catalog import FILESYSTEM_WRITE_TOOL_NAMES

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from harness_agent.policy.classifier import SafetyClassifier

logger = logging.getLogger(__name__)

_DEFAULT_HITL_TOOLS = frozenset(
    {"execute", *FILESYSTEM_WRITE_TOOL_NAMES, "task", "web_fetch", "monitor", "task_stop"}
)
# auto-edit 模式下编辑类工具必须纳入 HITL 集合：敏感路径（如 .git/config）的
# 编辑仍需由预检弹窗确认；工作区内非敏感编辑由预检自动放行，不会真正弹窗。
_AUTO_EDIT_HITL_TOOLS = frozenset(
    {"execute", *FILESYSTEM_WRITE_TOOL_NAMES, "task", "web_fetch", "monitor", "task_stop"}
)
# auto 模式下编辑类工具同样必须纳入 HITL 集合：让编辑类调用经过四层过滤器
# 判断——F1 快速通道自动放行，敏感路径与 F4 回退仍走弹窗审批。
_AUTO_HITL_TOOLS = frozenset(
    {"execute", *FILESYSTEM_WRITE_TOOL_NAMES, "task", "web_fetch", "monitor", "task_stop"}
)
_PLAN_ALLOWED_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "ask_user",
        "write_todos",
        "web_search",
        "web_fetch",
        "lsp",
        "tool_search",
        "memory_search",
        "task_output",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)


# ---------------------------------------------------------------------------
# Extension 权限钩子接口（预留，当前为空列表）
# ---------------------------------------------------------------------------

class ExtensionPermissionHook(Protocol):
    """Extension/Plugin 权限钩子协议（预留接口）。

    Extension 钩子只能收紧权限（deny 覆盖 allow），不能放宽（主流程 deny 不可覆盖）。
    后续 /extension 功能上线时，用户自定义的权限逻辑通过此接口注册。
    """

    def check(
        self, tool_name: str, tool_args: dict[str, Any], base_decision: str
    ) -> Literal["allow", "deny", "passthrough"]:
        """检查工具调用权限。

        Args:
            tool_name: 工具名称。
            tool_args: 工具参数。
            base_decision: 主审批管线的决策结果（"allow" 或 "ask"）。

        Returns:
            "deny" 收紧拒绝，"allow" 无实际效果（不可放宽），"passthrough" 不影响。
        """
        ...


# 已注册的 Extension 权限钩子（当前为空，后续 /extension 功能填充）
_extension_permission_hooks: list[ExtensionPermissionHook] = []


def interrupt_on_for_approval_mode(
    approval_mode: ApprovalMode,
    *,
    preflight: Callable[[ToolCallRequest], bool] | None = None,
    extra_interrupt_tools: frozenset[str] | None = None,
    approval_descriptions: Mapping[str, Callable[[dict[str, Any], Any, Any], str]] | None = None,
) -> dict[str, Any] | None:
    """返回应由 HumanInTheLoopMiddleware 拦截的工具集合。

    计划模式的拒绝由 ``PlanModeMiddleware`` 完成，YOLO 则只关闭 Harness
    的人工确认；工作区、Shell 和远端 provider 等硬策略不在这里放宽。

    Args:
        extra_interrupt_tools: 需要一并纳入审批的额外工具名（如 MCP 工具）。
            仅在 default、auto-edit 和 auto 模式下生效；plan 和 yolo 忽略。
        approval_descriptions: 每个工具可选的动态审批描述。文件 mutation 使用它展示
            prepare 阶段生成的有界 diff 预览；完整拟议内容仍由一次性计划固定，其他工具
            继续使用框架默认描述。
    """
    if approval_mode in {"plan", "yolo"}:
        return None
    if approval_mode == "default":
        tool_names = _DEFAULT_HITL_TOOLS
    elif approval_mode == "auto-edit":
        tool_names = _AUTO_EDIT_HITL_TOOLS
    else:  # auto 模式
        tool_names = _AUTO_HITL_TOOLS
    if extra_interrupt_tools:
        tool_names = tool_names | extra_interrupt_tools
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

    interrupt_on: dict[str, InterruptOnConfig] = {}
    for name in tool_names:
        approval = InterruptOnConfig(allowed_decisions=["approve", "reject"])
        if preflight is not None:
            # HumanInTheLoopMiddleware 在实际 ToolNode 之前暂停。把与执行守卫
            # 共用的预检挂在 `when`，越界文件调用就不会产生无法批准的假审批。
            approval["when"] = preflight
        if approval_descriptions is not None and (description := approval_descriptions.get(name)):
            approval["description"] = description
        interrupt_on[name] = approval
    return interrupt_on


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
    if approval_mode == "auto":
        return """

## 审批模式：自动

系统通过多层过滤器自动判断工具调用安全性。只读操作和工作区内编辑自动
执行；破坏性命令被硬拦截；其他操作由 AI 分类器判断安全性。分类器不可用
时安全回退到手动审批。工作区边界和其他安全策略始终有效。
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


class DenyRulesMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """在所有审批模式下强制执行 deny 规则，deny 命中即硬拒绝。

    该中间件注册在所有其他中间件之前，确保 deny 规则不可被任何模式覆盖（包括 yolo）。
    """

    def __init__(self, rules_provider: Callable[[], list[PermissionRule]]) -> None:
        """初始化 deny 规则中间件。

        Args:
            rules_provider: 返回当前所有权限规则的回调函数。
        """
        self._rules_provider = rules_provider

    def _check_deny(self, request: ToolCallRequest) -> ToolMessage | None:
        """检查工具调用是否命中 deny 规则，命中则返回错误消息。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", "unknown"))
        tool_args = tool_call.get("args") or {}
        if not isinstance(tool_args, dict):
            tool_args = {}

        rules = self._rules_provider()
        if not rules:
            return None

        effect = evaluate_tool_rules(tool_name, tool_args, rules)
        if effect == "deny":
            # L2 deny 硬拦截审计：记录拒绝原因，任何模式（包括 yolo）不可覆盖。
            logger.info(
                "approval_deny source=rule tool=%s resource=%s",
                tool_name,
                extract_tool_resource(tool_name, tool_args),
            )
            return ToolMessage(
                content=f"权限规则拒绝 {tool_name}：该操作已被 deny 规则禁止，不可覆盖。",
                name=tool_name,
                tool_call_id=str(tool_call.get("id") or "deny-rule"),
                status="error",
            )
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步调用：deny 规则命中时硬拒绝。"""
        rejection = self._check_deny(request)
        if rejection is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步调用：deny 规则命中时硬拒绝。"""
        rejection = self._check_deny(request)
        if rejection is not None:
            return rejection
        return await handler(request)


class AutoDestructiveGuardMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """AUTO 模式破坏性操作执行层守卫（F3 + F4 deny 兜底）。

    预检对 deny 决策会跳过审批弹窗，本中间件在工具实际执行前兜底硬拒绝，
    保证破坏性命令"直接拒绝、不经过弹窗"。注入了分类器时优先复用其决策
    缓存（模型响应阶段已完成分类），缓存未命中再走确定性过滤器判断。
    """

    def __init__(
        self,
        rules_provider: Callable[[], list[PermissionRule]] | None,
        workspace_root: str | None,
        classifier: SafetyClassifier | None = None,
    ) -> None:
        """初始化 AUTO 模式破坏性命令守卫。

        Args:
            rules_provider: 返回当前合并权限规则的回调；为 None 或返回空列表
                时跳过规则判断，直接进入 AUTO 四层过滤器。
            workspace_root: 工作区根目录，供 AUTO 过滤器判断路径归属。
            classifier: F4 LLM 分类器；提供时其决策缓存作为守卫依据。
        """
        self._rules_provider = rules_provider
        self._workspace_root = workspace_root
        self._classifier = classifier

    def _check(self, request: ToolCallRequest) -> ToolMessage | None:
        """检查工具调用是否应被 AUTO 模式硬拒绝，命中时返回错误消息。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", "unknown"))
        tool_args = tool_call.get("args") or {}
        if not isinstance(tool_args, dict):
            tool_args = {}

        rules = self._rules_provider() if self._rules_provider is not None else []
        if rules:
            effect = evaluate_tool_rules(tool_name, tool_args, rules)
            # deny 由 DenyRulesMiddleware 统一硬拒绝；allow 规则按设计优先于
            # AUTO 过滤器（用户明确批准的操作不再进入过滤器判断）。
            if effect in ("allow", "deny"):
                return None

        # 分类器决策缓存命中时直接执行其结论：deny 硬拒绝；allow/ask 放行
        # （ask 已由 HITL 弹窗处理，用户批准后才会走到执行层）。
        if self._classifier is not None:
            cached = self._classifier.lookup_decision(str(tool_call.get("id") or ""))
            if cached is not None:
                decision, reason = cached
                if decision == "deny":
                    return self._rejection(tool_call, tool_name, reason, source="classifier")
                return None

        decision, reason = evaluate_auto_mode(tool_name, tool_args, self._workspace_root)
        if decision == "deny":
            return self._rejection(
                tool_call, tool_name, reason, source="auto_destructive_guard"
            )
        return None

    @staticmethod
    def _rejection(
        tool_call: dict[str, Any], tool_name: str, reason: str, *, source: str
    ) -> ToolMessage:
        """生成硬拒绝消息并记录审计日志；静默拒绝必须留痕。"""
        logger.info(
            "approval_deny source=%s tool=%s reason=%s", source, tool_name, reason
        )
        return ToolMessage(
            content=f"AUTO 模式拒绝 {tool_name}：{reason}",
            name=tool_name,
            tool_call_id=str(tool_call.get("id") or "auto-deny"),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步调用：AUTO 模式 F3 硬拒绝时不调用底层 handler。"""
        rejection = self._check(request)
        if rejection is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步调用：AUTO 模式 F3 硬拒绝时不调用底层 handler。"""
        rejection = self._check(request)
        if rejection is not None:
            return rejection
        return await handler(request)


class AutoClassifierMiddleware(AgentMiddleware[dict[str, Any], ContextT, ResponseT]):
    """AUTO 模式 F4 分类器中间件：在模型响应阶段完成安全分类。

    HITL 预检（``when`` 回调）是同步的，无法在其中调用 LLM。本中间件挂在
    模型调用链上：模型返回工具调用后、HITL after_model 裁决前，对每个会进入
    F4 的调用执行两阶段分类，并把结论写入分类器决策缓存。预检与执行层守卫
    随后读取缓存决定弹窗与否，保证同一次工具调用最多被分类一次。

    分类前的裁决顺序与预检保持一致：deny/allow 规则、敏感路径和 F1-F3
    确定性过滤器能给出结论的调用不消耗分类器额度。
    """

    def __init__(
        self,
        classifier: SafetyClassifier,
        rules_provider: Callable[[], list[PermissionRule]] | None,
        workspace_root: str | None,
    ) -> None:
        """初始化 F4 分类器中间件。

        Args:
            classifier: 两阶段 LLM 安全分类器，同时充当决策缓存。
            rules_provider: 返回当前合并权限规则的回调。
            workspace_root: 工作区根目录，供 AUTO 过滤器判断路径归属。
        """
        self._classifier = classifier
        self._rules_provider = rules_provider
        self._workspace_root = workspace_root

    def _needs_classifier(self, tool_name: str, tool_args: dict[str, Any]) -> bool:
        """判断调用是否会进入 F4：规则和确定性过滤器已裁决的不需要分类。"""
        rules = self._rules_provider() if self._rules_provider is not None else []
        if rules:
            effect = evaluate_tool_rules(tool_name, tool_args, rules)
            # deny 由 DenyRulesMiddleware 硬拒绝；allow 由预检/守卫处理
            # （敏感路径仍弹窗），两者都不需要分类器参与。
            if effect in ("allow", "deny"):
                return False
        # 敏感路径由预检强制弹窗，结论确定，不进入分类器。
        if requires_safety_check(tool_name, tool_args):
            return False
        # F1/F2 放行与 F3 硬拦截都是确定性结论；仅 ask（F4 未决）需要分类。
        decision, _ = evaluate_auto_mode(tool_name, tool_args, self._workspace_root)
        return decision == "ask"

    async def _classify_response(self, response: Any) -> None:
        """对模型响应中的工具调用逐个分类并记录结论。"""
        for tool_call in _iter_response_tool_calls(response):
            tool_call_id = str(tool_call.get("id") or "")
            tool_name = str(tool_call.get("name") or "")
            tool_args = tool_call.get("args") or {}
            if not isinstance(tool_args, dict):
                tool_args = {}
            if not tool_name or self._classifier.lookup_decision(tool_call_id):
                continue
            if not self._needs_classifier(tool_name, tool_args):
                continue
            decision, reason = await self._classifier.aclassify(tool_name, tool_args)
            self._classifier.record_decision(tool_call_id, decision, reason)

    def _classify_response_sync(self, response: Any) -> None:
        """同步路径分类：阻塞调用模型，仅供同步执行与测试使用。"""
        for tool_call in _iter_response_tool_calls(response):
            tool_call_id = str(tool_call.get("id") or "")
            tool_name = str(tool_call.get("name") or "")
            tool_args = tool_call.get("args") or {}
            if not isinstance(tool_args, dict):
                tool_args = {}
            if not tool_name or self._classifier.lookup_decision(tool_call_id):
                continue
            if not self._needs_classifier(tool_name, tool_args):
                continue
            decision, reason = self._classifier.classify(tool_name, tool_args)
            self._classifier.record_decision(tool_call_id, decision, reason)

    def wrap_model_call(
        self,
        request: ModelRequest[dict[str, Any]],
        handler: Callable[[ModelRequest[dict[str, Any]]], ModelResponse[Any]],
    ) -> Any:
        """同步模型调用：先取响应，再对其中的工具调用分类。"""
        response = handler(request)
        self._classify_response_sync(response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[dict[str, Any]],
        handler: Callable[[ModelRequest[dict[str, Any]]], Awaitable[ModelResponse[Any]]],
    ) -> Any:
        """异步模型调用：先取响应，再对其中的工具调用异步分类。"""
        response = await handler(request)
        await self._classify_response(response)
        return response


def _iter_response_tool_calls(response: Any) -> list[dict[str, Any]]:
    """从模型响应中收集工具调用，兼容 AIMessage 与各类响应包装。"""
    target = getattr(response, "model_response", None) or response
    result = getattr(target, "result", None)
    messages = result if isinstance(result, (list, tuple)) else [target]
    calls: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                if isinstance(tool_call, dict):
                    calls.append(tool_call)
    return calls


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
    *,
    auto_filter: Callable[[str, dict[str, Any]], PermissionDecision] | None = None,
) -> PermissionDecision:
    """多级审批流水线：L1 工具风险声明 → L2 deny 硬拦截 → L3 只读放行 → L3.5 敏感路径 → L4 规则评估 → L5 模式覆盖 → L6 Extension 钩子。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典。
        approval_mode: 当前审批模式。
        rules: 已合并的权限规则列表（session + project + user + system）。
        auto_filter: AUTO 模式四层过滤器回调（仅 auto 模式时调用）。

    Returns:
        "allow" 直接执行，"ask" 需要用户审批，"deny" 硬拒绝。
    """
    # L1: 工具风险声明提取
    kind = get_tool_kind(tool_name)

    # L2: deny 规则硬拦截（任何模式不可覆盖）
    if rules:
        effect = evaluate_tool_rules(tool_name, tool_args, rules)
        if effect == "deny":
            return "deny"

    # L3: 只读工具放行（READ/INTERACT/PLAN 类别）
    if is_read_only(tool_name):
        return "allow"

    # L3.1: Shell 安全命令白名单（default 模式下自动放行只读安全的命令）
    # 仅对 execute 工具生效，且需要检查安全底线。
    # 链式命令必须逐段判定：任一段不在白名单内则整体不走快速放行，
    # 防止 "git status && rm -rf /" 这类危险命令借首段白名单逃逸。
    if tool_name == "execute" and approval_mode == "default":
        command = str(tool_args.get("command", "")).strip()
        if command:
            segments = extract_segments(command)
            if segments and all(is_safe_command(segment) for segment in segments):
                # 白名单命中，但仍需检查安全底线
                floors = evaluate_safety_floors(command)
                if not floors["any_floor_triggered"]:
                    return "allow"
                # 底线触发，强制 ask
                logger.info("安全命令白名单命中但底线触发: %s", command)
                return "ask"

    # L3.5: 敏感路径 safetyCheck（yolo 免疫）
    if approval_mode != "yolo" and requires_safety_check(tool_name, tool_args):
        return "ask"

    # L4: 规则评估（allow/ask 规则，两级优先级）
    if rules:
        effect = evaluate_tool_rules(tool_name, tool_args, rules)
        if effect == "allow":
            # Shell：按规则前缀的剩余部分复核安全底线（ZC-117 约束 B）
            if tool_name in {"execute", "monitor"}:
                from harness_agent.policy.bash_matcher import allow_remainder_triggers_floor

                command = str(tool_args.get("command", "")).strip()
                if command and allow_remainder_triggers_floor(command, rules):
                    return "ask"
            return "allow"
        if effect == "ask":
            return "ask"

    # L5: 审批模式覆盖（按 ToolKind 查表）
    mode_perm = get_mode_permission(kind, approval_mode)

    # auto 模式下 "filter" 表示进入 AUTO 四层过滤器
    if mode_perm == "filter":
        if auto_filter is not None:
            return auto_filter(tool_name, tool_args)
        # 过滤器不可用时回退到 ask（安全优先）
        return "ask"

    decision = mode_perm  # type: ignore[assignment]

    # L6: Extension 权限钩子（预留，当前为空列表直接跳过）
    if decision != "deny" and _extension_permission_hooks:
        for hook in _extension_permission_hooks:
            hook_result = hook.check(tool_name, tool_args, decision)
            if hook_result == "deny":
                return "deny"

    return decision
