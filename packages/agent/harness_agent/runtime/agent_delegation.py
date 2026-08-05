"""受控 Agent delegation：统一 Inline/Managed 选择、审计、取消和终态。

模型只提交目标 Agent ID 与任务文本。执行模式、模型、Policy、Engine 和资源
租约均来自 Host 注册的可信 target，不能由 Prompt 或 Plugin 请求自行放宽。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest

from harness_agent.runtime.agent_catalog import DelegationPolicy
from harness_agent.runtime.agent_execution import AgentExecutionRegistry, ExecutionRegistryError
from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool
from harness_agent.runtime.agent_engine_profile import AgentEngineProfile
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
    SafeModelProfile,
)
from harness_agent.runtime.run_context import RunCancellationToken


class AgentDelegationError(RuntimeError):
    """派发目标、权限、并发、超时或执行失败的稳定错误。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存可审计错误码，不携带任务正文或资源路径。"""
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class DelegateAgent:
    """一次受控派发的领域输入。"""

    parent_ref: ExecutionRef
    target_agent_id: str
    task: str
    idempotency_key: str
    delegation_policy: DelegationPolicy
    cancellation_token: RunCancellationToken
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        """拒绝空任务、空幂等键和无效超时。"""
        if (
            not self.target_agent_id
            or not self.task.strip()
            or not self.idempotency_key
            or self.timeout_seconds <= 0
        ):
            raise AgentDelegationError("DELEGATION_COMMAND_INVALID")


DelegationRunner = Callable[[DelegateAgent], Awaitable[Mapping[str, Any]]]
ManagedInvocation = Callable[
    [AgentEngine, DelegateAgent],
    Awaitable[Mapping[str, Any]],
]


@dataclass(frozen=True, slots=True)
class DelegationTarget:
    """由 Host 注册的可信 Agent target；执行模式不接受模型覆盖。"""

    agent_id: str
    mode: ExecutionMode
    runner: DelegationRunner
    description: str | None = None
    model: SafeModelProfile | None = None
    policy_fingerprint: str | None = None
    engine_profile_key: str | None = None
    definition_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """Managed target 必须提供独立 Engine 身份；Inline 不得伪装持有它。"""
        if not self.agent_id or not callable(self.runner):
            raise AgentDelegationError("DELEGATION_TARGET_INVALID")
        if self.mode is ExecutionMode.MANAGED and not self.engine_profile_key:
            raise AgentDelegationError("DELEGATION_MANAGED_PROFILE_REQUIRED")
        if self.mode is ExecutionMode.INLINE and self.engine_profile_key is not None:
            raise AgentDelegationError("DELEGATION_INLINE_PROFILE_FORBIDDEN")


@dataclass(frozen=True, slots=True)
class AgentResult:
    """子 execution 的结构化结果；私有图状态不进入父 Agent。"""

    ref: ExecutionRef
    agent_id: str
    mode: ExecutionMode
    status: ExecutionStatus
    output: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DelegationCallContext:
    """从父 `task` ToolRuntime 提取的当前 Run 与 tool call ID。"""

    run_context: Any
    tool_call_id: str


_CURRENT_DELEGATION_CALL: ContextVar[DelegationCallContext | None] = ContextVar(
    "harness_current_delegation_call",
    default=None,
)


def current_delegation_call() -> DelegationCallContext:
    """返回受控 task handler 绑定的调用上下文，缺失时 fail closed。"""
    current = _CURRENT_DELEGATION_CALL.get()
    if current is None:
        raise AgentDelegationError("DELEGATION_RUN_CONTEXT_REQUIRED")
    return current


class DelegationContextMiddleware(AgentMiddleware):
    """只在 `task` handler 期间绑定父 RunContext，避免使用全局可变状态。"""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步 task 调用绑定 contextvar，其他工具直接委托。"""
        token = self._bind(request)
        if token is None:
            return handler(request)
        try:
            return handler(request)
        finally:
            _CURRENT_DELEGATION_CALL.reset(token)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步 task 调用在整个子图执行期间保留父上下文。"""
        token = self._bind(request)
        if token is None:
            return await handler(request)
        try:
            return await handler(request)
        finally:
            _CURRENT_DELEGATION_CALL.reset(token)

    def _bind(
        self,
        request: ToolCallRequest,
    ) -> Token[DelegationCallContext | None] | None:
        """从 ToolCallRequest.runtime 读取 RunContext，不接受参数伪造。"""
        if request.tool_call.get("name") != "task":
            return None
        from harness_agent.runtime.run_context import require_run_context

        context = require_run_context(request.runtime)
        tool_call_id = str(request.tool_call.get("id") or "")
        if not tool_call_id:
            raise AgentDelegationError("DELEGATION_TOOL_CALL_ID_REQUIRED")
        return _CURRENT_DELEGATION_CALL.set(
            DelegationCallContext(
                run_context=context,
                tool_call_id=tool_call_id,
            )
        )


class AgentDelegator:
    """验证 delegation envelope 并执行已注册 Inline/Managed target。"""

    def __init__(
        self,
        registry: AgentExecutionRegistry,
        *,
        targets: tuple[DelegationTarget, ...],
    ) -> None:
        """冻结 target 目录并初始化父 execution 并发计数。"""
        if len({target.agent_id for target in targets}) != len(targets):
            raise AgentDelegationError("DELEGATION_TARGET_DUPLICATE")
        self._registry = registry
        self._targets = {target.agent_id: target for target in targets}
        self._active_by_parent: dict[str, int] = {}
        self._completed: dict[str, tuple[tuple[object, ...], AgentResult]] = {}
        self._lock = asyncio.Lock()

    async def execute(self, command: DelegateAgent) -> AgentResult:
        """执行一次派发；所有成功和失败路径都收敛 child execution。"""
        target = self._targets.get(command.target_agent_id)
        if target is None:
            raise AgentDelegationError("DELEGATION_TARGET_NOT_FOUND")
        parent = await self._registry.get(command.parent_ref)
        if parent is None:
            raise AgentDelegationError("DELEGATION_PARENT_NOT_FOUND")
        self._validate_policy(command, parent.depth)
        fingerprint = (
            command.parent_ref.execution_id,
            command.target_agent_id,
            command.task,
            command.timeout_seconds,
        )
        async with self._lock:
            completed = self._completed.get(command.idempotency_key)
            if completed is not None:
                if completed[0] != fingerprint:
                    raise AgentDelegationError("DELEGATION_IDEMPOTENCY_CONFLICT")
                return completed[1]
            active = self._active_by_parent.get(command.parent_ref.execution_id, 0)
            limit = command.delegation_policy.max_parallelism
            if limit is not None and active >= limit:
                raise AgentDelegationError("DELEGATION_PARALLELISM_EXCEEDED")
            self._active_by_parent[command.parent_ref.execution_id] = active + 1

        ref = child_execution_ref(command)
        binding = AgentExecutionBinding(
            ref=ref,
            agent_id=target.agent_id,
            mode=target.mode,
            depth=parent.depth + 1,
            model=target.model,
            policy_fingerprint=target.policy_fingerprint,
            engine_profile_key=target.engine_profile_key,
            definition_fingerprint=target.definition_fingerprint,
        )
        try:
            await self._registry.accept(binding)
            await self._registry.start(ref)
            output = await self._run_with_cancellation(target.runner, command)
            await self._registry.finalize(ref, status=ExecutionStatus.COMPLETED)
            result = AgentResult(
                ref=ref,
                agent_id=target.agent_id,
                mode=target.mode,
                status=ExecutionStatus.COMPLETED,
                output=dict(output),
            )
            async with self._lock:
                self._completed[command.idempotency_key] = (fingerprint, result)
            return result
        except asyncio.CancelledError:
            await self._finalize_if_running(ref, ExecutionStatus.CANCELLED)
            raise
        except TimeoutError as exc:
            await self._finalize_if_running(ref, ExecutionStatus.FAILED)
            raise AgentDelegationError("DELEGATION_TIMEOUT") from exc
        except (AgentDelegationError, ExecutionRegistryError):
            await self._finalize_if_running(ref, ExecutionStatus.FAILED)
            raise
        except Exception as exc:
            await self._finalize_if_running(ref, ExecutionStatus.FAILED)
            raise AgentDelegationError(
                "DELEGATION_EXECUTION_FAILED",
                type(exc).__name__,
            ) from exc
        finally:
            async with self._lock:
                active = self._active_by_parent.get(command.parent_ref.execution_id, 0)
                if active <= 1:
                    self._active_by_parent.pop(command.parent_ref.execution_id, None)
                else:
                    self._active_by_parent[command.parent_ref.execution_id] = active - 1

    def _validate_policy(self, command: DelegateAgent, parent_depth: int) -> None:
        """验证目标、深度和显式 enabled；所有缺失上限只表示该层不新增限制。"""
        policy = command.delegation_policy
        if not policy.enabled:
            raise AgentDelegationError("DELEGATION_DISABLED")
        if (
            policy.allowed_agents is not None
            and command.target_agent_id not in policy.allowed_agents
        ):
            raise AgentDelegationError("DELEGATION_TARGET_FORBIDDEN")
        if policy.max_depth is not None and parent_depth + 1 > policy.max_depth:
            raise AgentDelegationError("DELEGATION_DEPTH_EXCEEDED")

    async def _run_with_cancellation(
        self,
        runner: DelegationRunner,
        command: DelegateAgent,
    ) -> Mapping[str, Any]:
        """让父取消、timeout 和 runner 终态竞争，并始终回收内部 Task。"""
        if command.cancellation_token.cancelled:
            raise asyncio.CancelledError
        runner_task = asyncio.create_task(
            runner(command),
            name=f"harness-delegation-{command.target_agent_id}",
        )
        cancel_task = asyncio.create_task(command.cancellation_token.wait())
        try:
            done, _ = await asyncio.wait(
                {runner_task, cancel_task},
                timeout=command.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)
                raise TimeoutError
            if cancel_task in done:
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)
                raise asyncio.CancelledError
            return await runner_task
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _finalize_if_running(
        self,
        ref: ExecutionRef,
        status: ExecutionStatus,
    ) -> None:
        """仅在 child 已成功登记且尚未终结时收敛状态。"""
        current = await self._registry.get(ref)
        if current is None or current.status.terminal:
            return
        if current.status is ExecutionStatus.PENDING and status is not ExecutionStatus.CANCELLED:
            await self._registry.start(ref)
        await self._registry.finalize(ref, status=status)


def managed_engine_runner(
    pool: AgentEnginePool,
    profile: AgentEngineProfile,
    invoke: ManagedInvocation,
) -> DelegationRunner:
    """创建 Managed adapter：复用 Profile 图并在所有终态释放 run/engine lease。"""
    if not callable(invoke):
        raise AgentDelegationError("DELEGATION_MANAGED_INVOKER_INVALID")

    async def run(command: DelegateAgent) -> Mapping[str, Any]:
        lease = await pool.acquire(profile)
        run_lease = await lease.run()
        try:
            return await invoke(lease.engine, command)
        finally:
            await run_lease.release()
            await lease.release()
            await pool.finalize_draining(profile.profile_key)

    return run


def child_execution_ref(command: DelegateAgent) -> ExecutionRef:
    """返回 Delegator 与 Managed adapter 共用的稳定 child execution 引用。"""
    digest = hashlib.sha256(
        "\0".join(
            (
                command.parent_ref.run_id,
                command.parent_ref.execution_id,
                command.target_agent_id,
                command.idempotency_key,
            )
        ).encode()
    ).hexdigest()[:20]
    return ExecutionRef(
        thread_id=command.parent_ref.thread_id,
        run_id=command.parent_ref.run_id,
        execution_id=f"child-{digest}",
        parent_execution_id=command.parent_ref.execution_id,
    )
