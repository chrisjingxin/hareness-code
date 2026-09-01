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
from harness_agent.policy.concurrency import is_shell_command_mutating
from harness_agent.tools.file_tool_catalog import FILESYSTEM_WRITE_TOOL_NAMES
from harness_agent.tools.plan_file import PLAN_VIRTUAL_PATH, is_plan_virtual_path

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from harness_agent.policy.classifier import SafetyClassifier

logger = logging.getLogger(__name__)

_DEFAULT_HITL_TOOLS = frozenset(
    {
        "execute",
        *FILESYSTEM_WRITE_TOOL_NAMES,
        "ls",
        "read_file",
        "glob",
        "grep",
        "task",
        "web_fetch",
    }
)
# auto-edit 模式下编辑类工具必须纳入 HITL 集合：敏感路径（如 .git/config）的
# 编辑仍需由预检弹窗确认；工作区内非敏感编辑由预检自动放行，不会真正弹窗。
# 只读工具纳入是为了工作区外目录信任审批；工作区内读取由预检直接放行。
_AUTO_EDIT_HITL_TOOLS = frozenset(
    {
        "execute",
        *FILESYSTEM_WRITE_TOOL_NAMES,
        "ls",
        "read_file",
        "glob",
        "grep",
        "task",
        "web_fetch",
    }
)
# auto 模式下编辑类工具同样必须纳入 HITL 集合：让编辑类调用经过四层过滤器
# 判断——F1 快速通道自动放行，敏感路径与 F4 回退仍走弹窗审批。
_AUTO_HITL_TOOLS = frozenset(
    {
        "execute",
        *FILESYSTEM_WRITE_TOOL_NAMES,
        "ls",
        "read_file",
        "glob",
        "grep",
        "task",
        "web_fetch",
    }
)
_PLAN_DIRECTORY_TRUST_TOOLS = frozenset({"ls", "read_file", "glob", "grep", "lsp"})
_PLAN_ALLOWED_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "ask_user",
        "web_search",
        "web_fetch",
        "lsp",
        "tool_search",
        "memory_search",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)

PlanShellDecision = Literal["allow", "ask", "deny"]
"""Plan 约束下 Shell 命令的三态处置结果。"""


def classify_plan_shell_command(command: object) -> PlanShellDecision:
    """把 Plan Shell 命令分为只读、明确写入和未知三类。

    只读判定复用审批系统现有的跨平台白名单与安全底线；明确文件/Git mutation
    复用并发安全模块的静态写入识别；高危但不属于普通文件写入的命令继续复用
    AUTO 的确定性破坏性规则。无法静态确认的命令保守交给单次人工审批。
    """
    if not isinstance(command, str) or not command.strip():
        return "ask"
    normalized = command.strip()
    segments = extract_segments(normalized)
    floors = evaluate_safety_floors(normalized)
    if (
        segments
        and all(is_safe_command(segment) for segment in segments)
        and not floors["any_floor_triggered"]
    ):
        return "allow"
    if is_shell_command_mutating(normalized):
        return "deny"
    auto_decision, _reason = evaluate_auto_mode("execute", {"command": normalized})
    if auto_decision == "deny":
        return "deny"
    return "ask"


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

    计划模式为目录信任、未知 Shell 和提交计划开启审批通道，明确写入仍由
    ``PlanModeMiddleware`` 硬拒绝。YOLO 图仅在提供预检时保留休眠的 Shell
    通道，供同一 Run 动态进入 Plan 后启用；外部路径仍由边界中间件自动授予
    session 根。

    Args:
        extra_interrupt_tools: 需要一并纳入审批的额外工具名（如 MCP 工具）。
            在 default、auto-edit 和 auto 模式下生效；plan 与 yolo 忽略。
        approval_descriptions: 每个工具可选的动态审批描述。文件 mutation 使用它展示
            prepare 阶段生成的有界 diff 预览；完整拟议内容仍由一次性计划固定，其他工具
            继续使用框架默认描述。
    """
    if approval_mode == "yolo" and preflight is None:
        return None
    if approval_mode == "yolo":
        tool_names = frozenset({"execute"})
    elif approval_mode == "plan":
        tool_names = _PLAN_DIRECTORY_TRUST_TOOLS
    elif approval_mode == "default":
        tool_names = _DEFAULT_HITL_TOOLS
    elif approval_mode == "auto-edit":
        tool_names = _AUTO_EDIT_HITL_TOOLS
    else:  # auto 模式
        tool_names = _AUTO_HITL_TOOLS
    if extra_interrupt_tools and approval_mode in {"default", "auto-edit", "auto"}:
        tool_names = tool_names | extra_interrupt_tools
    if approval_mode == "plan":
        tool_names = tool_names | {"execute", "exit_plan_mode"}
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

    interrupt_on: dict[str, InterruptOnConfig] = {}
    for name in tool_names:
        approval = InterruptOnConfig(allowed_decisions=["approve", "reject"])
        if preflight is not None:
            # HumanInTheLoopMiddleware 在实际 ToolNode 之前暂停。把与执行守卫
            # 共用的预检挂在 `when`：非法路径不弹窗；可信任外部路径弹目录信任卡片；
            # 工作区内读取不弹窗。
            approval["when"] = preflight
        if approval_descriptions is not None and (description := approval_descriptions.get(name)):
            approval["description"] = description
        interrupt_on[name] = approval
    return interrupt_on


def approval_mode_prompt(approval_mode: ApprovalMode) -> str:
    """生成追加到系统提示词的模式事实，不让项目指令改变实际策略。"""
    if approval_mode == "plan":
        return f"""

## 审批模式：计划（Plan Mode）

当前处于严格的计划模式。用户要求你先制定方案，不要执行修改。你必须遵守以下规划工作流：

1. **只读调研**：使用只读工具（`ls`、`read_file`、`glob`、`grep`、`lsp`、只读 Shell 命令）充分调查代码库，理解现有架构，主动寻找可复用的现有函数与模块，不要重复发明轮子。
2. **主动澄清**：遇到需求歧义、边界情况或技术选型权衡时，使用 `ask_user` 提问，不要自行盲目假设。
3. **规划期无需维护 Todo**：在此阶段不要调用 `write_todos` 创建任务清单（任务清单将在用户批准计划后的实现阶段使用）。规划阶段你的唯一产物是计划文件。
4. **撰写方案**：使用 `write_file` 将完整方案覆盖写入 `{PLAN_VIRTUAL_PATH}`（包含：改动背景、影响文件列表、可复用的现有代码、具体实现步骤及端到端验证方案）。
5. **提交审阅**：计划写完后，调用 `exit_plan_mode` 弹出审阅窗口等待用户批准。

【工具拦截原则】：
- 项目文件写入、修改系统的 Shell、子 Agent 与会写的 MCP 会被服务端直接拒绝。
- 若工具被拦截，不要重试或尝试绕过，直接把该操作作为执行步骤写进 `{PLAN_VIRTUAL_PATH}`。
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
    """按初始档位或同一 Run 的运行时 flag 强制计划模式只读。"""

    def __init__(self, approval_mode: ApprovalMode = "plan") -> None:
        """保存非共享图的构图期档位；共享图优先读取 RunContext。"""
        super().__init__()
        self._approval_mode = approval_mode

    def _constraint_active(self, request: ToolCallRequest) -> bool:
        """统一读取 Run 计划约束；无 RunContext 时只接受构图期 plan。"""
        from harness_agent.runtime.run_context import (
            RunContextError,
            plan_constraint_active,
            require_run_context,
        )

        try:
            context = require_run_context(getattr(request, "runtime", None))
        except RunContextError:
            return self._approval_mode == "plan"
        return plan_constraint_active(context)

    def _rejection(self, request: ToolCallRequest) -> ToolMessage:
        """返回可指导模型继续调研并把动作写进计划的错误结果。"""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", "unknown"))
        extra = ""
        if tool_name == "write_todos":
            extra = "计划模式下无需维护任务清单。"
        elif tool_name in {"write_file", "edit_file", "delete_file"}:
            extra = f"唯一可写路径是 `{PLAN_VIRTUAL_PATH}`。"
        return ToolMessage(
            content=(
                f"计划模式拒绝 {tool_name}：当前是计划模式，不能执行此操作。"
                f"{extra}请使用只读调查，或把动作写进 `{PLAN_VIRTUAL_PATH}` 后调用 exit_plan_mode。"
            ),
            name=tool_name,
            tool_call_id=str(tool_call.get("id") or "plan-mode"),
            status="error",
        )

    def _is_allowed(self, request: ToolCallRequest) -> bool:
        """白名单、会话计划文件写入、以及声明只读的 MCP 可以放行。"""
        tool_name = str(request.tool_call.get("name", ""))
        if tool_name in _PLAN_ALLOWED_TOOLS:
            return True
        if tool_name == "execute":
            args = request.tool_call.get("args") or {}
            command = args.get("command") if isinstance(args, dict) else None
            # ask 已经由 HITL 完成单次批准；这里只硬拒明确 mutation。
            return classify_plan_shell_command(command) != "deny"
        if tool_name in {"write_file", "edit_file"} and is_plan_virtual_path(_tool_file_path(request)):
            return True
        return _declared_read_only_tool(request, tool_name)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步调用：处理进入/提交计划，未受约束时透明放行。"""
        tool_name = request.tool_call.get("name")
        if tool_name == "enter_plan_mode":
            from harness_agent.tools.tools_mode import request_plan_entry

            return request_plan_entry(request)
        if not self._constraint_active(request):
            return handler(request)
        if tool_name == "exit_plan_mode":
            from harness_agent.tools.tools_mode import submit_plan

            return submit_plan(request)
        if not self._is_allowed(request):
            return self._rejection(request)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步调用沿用相同运行时判定。"""
        tool_name = request.tool_call.get("name")
        if tool_name == "enter_plan_mode":
            from harness_agent.tools.tools_mode import request_plan_entry

            return request_plan_entry(request)
        if not self._constraint_active(request):
            return await handler(request)
        if tool_name == "exit_plan_mode":
            from harness_agent.tools.tools_mode import submit_plan

            return submit_plan(request)
        if not self._is_allowed(request):
            return self._rejection(request)
        return await handler(request)


def _tool_file_path(request: ToolCallRequest) -> str:
    """从文件工具参数取出目标路径。"""
    args = request.tool_call.get("args") or {}
    if not isinstance(args, dict):
        return ""
    for key in ("file_path", "path", "target_path"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def _declared_read_only_tool(request: ToolCallRequest, tool_name: str) -> bool:
    """MCP 等外部工具只有显式声明只读才放行；未声明 fail-closed。"""
    candidates: list[object] = []
    tool = getattr(request, "tool", None)
    if tool is not None:
        candidates.append(tool)
    runtime = getattr(request, "runtime", None)
    for item in getattr(runtime, "tools", None) or ():
        candidates.append(item)
    for candidate in candidates:
        if getattr(candidate, "name", None) != tool_name:
            continue
        if getattr(candidate, "is_read_only", False) is True:
            return True
        metadata = getattr(candidate, "metadata", None) or {}
        if isinstance(metadata, dict) and metadata.get("readOnlyHint") is True:
            return True
        annotations = getattr(candidate, "annotations", None)
        if annotations is not None and getattr(annotations, "read_only_hint", None) is True:
            return True
        if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
            return True
    return False


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

    # L3.1: Shell 安全命令白名单（非 plan 模式下自动放行只读安全的命令）
    # 仅对 execute 工具生效，且需要检查安全底线。
    # 链式命令必须逐段判定：任一段不在白名单内则整体不走快速放行，
    # 防止 "git status && rm -rf /" 这类危险命令借首段白名单逃逸。
    if tool_name == "execute" and approval_mode != "plan":
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
            if tool_name == "execute":
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
