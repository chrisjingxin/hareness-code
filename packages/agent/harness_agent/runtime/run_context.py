"""单次 Agent Run 的显式上下文和冻结 RunContextSnapshot 注入。

本模块只承载一次调用的 thread、run、提示词与取消状态。它不会写入
LangGraph checkpoint，也不会被 Agent 图长期持有，因此同一编译图可安全
服务多个 thread。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Mapping
from typing import TYPE_CHECKING

from langchain.agents.middleware.types import AgentMiddleware, ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from harness_agent.policy.approval_mode import ApprovalMode
from harness_agent.policy.approval_policy import approval_mode_prompt
from harness_agent.runtime.approval_presentation import ApprovalPresentationStore
from harness_agent.runtime.execution_binding import ExecutionMode
from harness_agent.threads.context_lifecycle import RunContextSnapshot
from harness_agent.threads.context_pressure import ModelCallLifecycle

_LEGACY_APPROVAL_MODE_MARKER = "\n\n## 审批模式："

if TYPE_CHECKING:
    from harness_agent.runtime.agent_catalog import DelegationPolicy


class RunContextError(ValueError):
    """Run Context 缺失、归属不一致或提示词不合法时抛出。"""


@dataclass(slots=True)
class RunPlanConstraint:
    """同一 Run 内可单向开启的计划约束；不进入 checkpoint 或持久化。"""

    _active: bool = field(default=False, repr=False)

    @property
    def active(self) -> bool:
        """返回本 Run 是否已由用户点头进入计划约束。"""
        return self._active

    def activate(self) -> None:
        """幂等开启计划约束；当前 Run 内不提供模型侧关闭入口。"""
        self._active = True


@dataclass(slots=True)
class RunCancellationToken:
    """一次 run 的协作式取消标记，供后续 scheduler 或 worker 安全观察。"""

    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def cancel(self) -> None:
        """标记取消；实际 Agent task 仍由 server 立即取消以保持现有语义。"""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """返回当前 run 是否已收到取消请求。"""
        return self._event.is_set()

    async def wait(self) -> None:
        """等待取消信号，供未来后台 worker 在安全边界处退出。"""
        await self._event.wait()


@dataclass(frozen=True, slots=True)
class RunContext:
    """传给单次图调用的 thread 私有状态，不属于共享 AgentEngine。"""

    thread_id: str
    run_id: str
    approval_mode: ApprovalMode
    # 根图 checkpoint 的内部身份；公开 thread_id 仍绑定 Transcript、Skill、
    # 诊断日志和 UI provenance，不能用内部摘要伪造对外执行来源。
    checkpoint_thread_id: str | None = None
    context_snapshot: RunContextSnapshot | None = None
    profile_key: str | None = None
    execution_id: str = "root"
    parent_execution_id: str | None = None
    agent_id: str = "main"
    execution_mode: ExecutionMode = ExecutionMode.MANAGED
    cancellation_token: RunCancellationToken = field(default_factory=RunCancellationToken)
    model_call_lifecycle: ModelCallLifecycle = field(default_factory=ModelCallLifecycle)
    # 用户可在同一 Run 内批准 enter_plan_mode；该 flag 只会从 False 变 True。
    plan_constraint: RunPlanConstraint = field(default_factory=RunPlanConstraint, repr=False)
    # 共享图只能从当前 Run 取得对应的 immutable Skill snapshot；不写入持久化记录。
    skill_registry: Any | None = field(default=None, repr=False)
    delegation_policy: DelegationPolicy | None = None
    # Host-owned 的进程内 Snapshot store；文件 contract 只能从本字段取得当前
    # Run 的 Thread，不能把构图期 thread 捕获进共享 graph 闭包。
    snapshot_store: Any | None = field(default=None, repr=False)
    # 文件审批展示只在当前 Run 内短暂存活；它不参与授权或提交，也不进入持久化。
    approval_presentations: ApprovalPresentationStore = field(
        default_factory=ApprovalPresentationStore,
        repr=False,
    )
    # 额外工作目录 registry：可变、按引用共享，不进入执行资源池 fingerprint。
    workspace_root_registry: Any | None = field(default=None, repr=False)
    # Host-owned 的进程内 deferred 工具 reveal 存储；按 thread_id 隔离已激活的低频/MCP 工具。
    deferred_tool_store: Any | None = field(default=None, repr=False)
    # child HITL 把 Interaction 交给 Host 的回调；None 时询问 fail closed。
    interaction_port: Callable[[Any], Awaitable[Any]] | None = field(default=None, repr=False)
    # child 过程事件转发到 Host 的回调；None 时 child 过程不对外可见。
    event_port: (
        Callable[
            [str, Mapping[str, object], str | None, str | None, str | None], None
        ]
        | None
    ) = field(default=None, repr=False)
    # 把 approve_thread / approve_project 暂存到父 Run 的 rule store 边界。
    record_approval: Callable[[str, dict[str, Any], str], None] | None = field(default=None, repr=False)
    # 当前 Run 的 Diagnostic Log；共享图在调用时读取，不在构图期捕获。
    diagnostic_log: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """在执行前验证 thread 与 snapshot 的绑定，阻止跨 project 注入。"""
        if not self.thread_id or not self.run_id:
            raise RunContextError("RUN_CONTEXT_ID_INVALID")
        if self.checkpoint_thread_id is not None and (
            not isinstance(self.checkpoint_thread_id, str)
            or not self.checkpoint_thread_id
        ):
            raise RunContextError("RUN_CONTEXT_CHECKPOINT_ID_INVALID")
        if self.context_snapshot is None:
            raise RunContextError("RUN_CONTEXT_SNAPSHOT_REQUIRED")
        if self.context_snapshot.thread_id != self.thread_id:
            raise RunContextError("RUN_CONTEXT_SNAPSHOT_THREAD_MISMATCH")
        snapshot_skill_id = self.context_snapshot.skill_snapshot_id
        if snapshot_skill_id is not None:
            if self.skill_registry is None or getattr(self.skill_registry, "snapshot_id", None) != snapshot_skill_id:
                raise RunContextError("RUN_CONTEXT_SKILL_SNAPSHOT_MISMATCH")
        if not self.execution_id or not self.agent_id:
            raise RunContextError("RUN_CONTEXT_EXECUTION_ID_INVALID")
        if self.parent_execution_id == self.execution_id:
            raise RunContextError("RUN_CONTEXT_PARENT_SELF_REFERENCE")


def require_run_context(runtime: object) -> RunContext:
    """从 LangGraph runtime 读取已验证的 RunContext，缺失时 fail closed。"""
    context = getattr(runtime, "context", None)
    if not isinstance(context, RunContext):
        raise RunContextError("RUN_CONTEXT_REQUIRED")
    return context


def plan_constraint_active(context: object) -> bool:
    """统一判定当前 Run 的计划约束：初始 plan 档位或运行时已点头。"""
    if getattr(context, "approval_mode", None) == "plan":
        return True
    constraint = getattr(context, "plan_constraint", None)
    return bool(getattr(constraint, "active", False))


def thread_id_for_runtime(runtime: object) -> str | None:
    """从显式 Context 优先取 thread ID，并校验 configurable 不会串线。"""
    context = getattr(runtime, "context", None)
    if isinstance(context, RunContext):
        config = getattr(runtime, "config", {})
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        configured_thread = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
        expected_thread = context.checkpoint_thread_id or context.thread_id
        if configured_thread is not None and str(configured_thread) != expected_thread:
            raise RunContextError("RUN_CONTEXT_CONFIG_THREAD_MISMATCH")
        return context.thread_id
    return None


class RunContextSnapshotMiddleware(AgentMiddleware):
    """在模型调用边界注入本 Run 冻结的 system prompt。"""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """保留基础提示词，并注入快照及本 Run 的实际审批模式。

        旧快照可能内嵌创建时的模式小节，必须先剥离，再根据当前
        ``RunContext`` 追加，保证 TUI 切换模式后提示与强制策略一致。
        """
        context = require_run_context(request.runtime)
        base_prompt = _system_message_text(request.system_message)
        prompt = _without_legacy_approval_mode_section(context.context_snapshot.system_prompt)
        effective_mode: ApprovalMode = (
            "plan" if plan_constraint_active(context) else context.approval_mode
        )
        prompt = f"{prompt}{approval_mode_prompt(effective_mode)}"
        # 已信任额外根是会话内可变事实，必须在模型调用边界动态追加。
        extra_roots = _extra_roots_prompt(request.runtime)
        if extra_roots:
            prompt = f"{prompt}{extra_roots}"
        system_prompt = f"{prompt}\n\n{base_prompt}" if base_prompt else prompt
        return await handler(request.override(system_message=SystemMessage(content=system_prompt)))


def _extra_roots_prompt(runtime: object) -> str:
    """从 RunContext / Host 侧 registry 生成已信任额外根提示；缺失时返回空串。"""
    try:
        context = require_run_context(runtime)
    except Exception:  # noqa: BLE001
        return ""
    registry = getattr(context, "workspace_root_registry", None)
    if registry is None:
        registry = getattr(getattr(runtime, "context", None), "workspace_root_registry", None)
    if registry is None or not hasattr(registry, "display_extra_roots"):
        return ""
    roots = registry.display_extra_roots()
    if not roots:
        return ""
    lines = "\n".join(f"- `{path}`" for path in roots)
    return f"""

## 已信任的额外工作目录

以下目录已获用户授权，可使用真实绝对路径访问，与主工作区等价：
{lines}
"""


def _without_legacy_approval_mode_section(prompt: str) -> str:
    """剥离历史迁移快照末尾可能内嵌的审批模式小节。"""
    index = prompt.find(_LEGACY_APPROVAL_MODE_MARKER)
    if index == -1:
        return prompt
    return prompt[:index]


def _system_message_text(message: object | None) -> str:
    """将 DeepAgents 生成的基础 system message 转为文本，保持现有顺序。"""
    if message is None:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        values: list[str] = []
        for item in content:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                values.append(str(item["text"]))
        return "".join(values).strip()
    return str(content).strip()
