"""受控 Agent delegation：统一 Inline/Managed 选择、审计、取消和终态。

模型只提交目标 Agent ID 与任务文本。执行模式、模型、Policy、Engine 和资源
租约均来自 Host 注册的可信 target，不能由 Prompt 或 Plugin 请求自行放宽。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from harness_agent.runtime.agent_catalog import DelegationPolicy
from harness_agent.runtime.agent_execution import AgentExecutionRegistry, ExecutionRegistryError
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
    SafeModelProfile,
)
from harness_agent.runtime.execution_stream import TOOL_DELTA
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

_DELEGATION_TIMEOUT_TOOL_MESSAGE = """子代理未在执行时限内完成，已终止。

- status: timed_out
- error_code: DELEGATION_TIMEOUT
- 主对话仍可继续；请根据已有信息决定是否缩小任务后重试。"""


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
        """异步 task 调用保留父上下文，并隔离预期的 child timeout。"""
        token = self._bind(request)
        if token is None:
            return await handler(request)
        try:
            return await handler(request)
        except AgentDelegationError as exc:
            if exc.code != "DELEGATION_TIMEOUT":
                raise
            return ToolMessage(
                content=_DELEGATION_TIMEOUT_TOOL_MESSAGE,
                tool_call_id=str(request.tool_call["id"]),
                status="error",
            )
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


MAX_PARENT_CHILD_PARALLELISM: int = 4
"""每个父 execution 同时运行的子代理最大硬上限。"""

_STABLE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_STABLE_ERROR_CODE_PREFIXES = (
    "DELEGATION_",
    "MANAGED_AGENT_",
    "MCP_",
    "PLUGIN_",
    "PROVIDER_",
    "RUNTIME_",
    "RUN_",
)


def _stable_exception_code(error: BaseException) -> str | None:
    """只透传受控稳定码，不把异常正文中的路径或秘密带到 Tool 输出。"""
    candidate = getattr(error, "code", None)
    if not isinstance(candidate, str) or not candidate:
        candidate = str(error).strip()
    if (
        _STABLE_ERROR_CODE_RE.fullmatch(candidate) is not None
        and candidate.startswith(_STABLE_ERROR_CODE_PREFIXES)
    ):
        return candidate
    return None


class AgentDelegator:
    """验证 delegation envelope 并执行已注册 Inline/Managed target。"""

    def __init__(
        self,
        registry: AgentExecutionRegistry,
        *,
        targets: tuple[DelegationTarget, ...],
        blocked_target_messages: Mapping[str, str] | None = None,
    ) -> None:
        """冻结 target 目录与不可执行门禁，并初始化并发与排队状态。"""
        if len({target.agent_id for target in targets}) != len(targets):
            raise AgentDelegationError("DELEGATION_TARGET_DUPLICATE")
        self._registry = registry
        self._targets = {target.agent_id: target for target in targets}
        self._blocked_target_messages = dict(blocked_target_messages or {})
        self._active_by_parent: dict[str, int] = {}
        self._waiters_by_parent: dict[str, list[asyncio.Future[None]]] = {}
        self._completed: dict[str, tuple[tuple[object, ...], AgentResult]] = {}
        self._lock = asyncio.Lock()

    async def execute(self, command: DelegateAgent) -> AgentResult:
        """执行一次派发；所有成功和失败路径都收敛 child execution。"""
        target = self._targets.get(command.target_agent_id)
        if target is None:
            blocked_message = self._blocked_target_messages.get(command.target_agent_id)
            if blocked_message is not None:
                # Plugin 的加载诊断只决定 target 是否进入可信目录；它不是
                # 用户需要复制的授权凭据。不要把旧诊断正文（可能含 digest、
                # fingerprint 或宿主路径）回传给模型或 CLI。
                raise AgentDelegationError("PLUGIN_LOAD_FAILED")
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

        loop = asyncio.get_running_loop()
        deadline = loop.time() + command.timeout_seconds
        policy_limit = command.delegation_policy.max_parallelism
        effective_limit = (
            min(MAX_PARENT_CHILD_PARALLELISM, policy_limit)
            if policy_limit is not None
            else MAX_PARENT_CHILD_PARALLELISM
        )

        await self._acquire_slot(
            command.parent_ref.execution_id,
            effective_limit,
            command.cancellation_token,
            deadline,
        )

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
            if command.cancellation_token.cancelled:
                raise asyncio.CancelledError
            remaining_timeout = max(0.0, deadline - loop.time())
            if remaining_timeout <= 0:
                raise TimeoutError
            await self._registry.accept(binding)
            await self._registry.start(ref)
            self._emit_child_binding(command, ref, target)
            output = await self._run_with_cancellation(
                target.runner,
                command,
                remaining_timeout,
            )
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
            stable_code = _stable_exception_code(exc)
            if stable_code is not None:
                raise AgentDelegationError(stable_code) from exc
            raise AgentDelegationError(
                "DELEGATION_EXECUTION_FAILED",
                type(exc).__name__,
            ) from exc
        finally:
            await self._release_slot(command.parent_ref.execution_id)

    async def _acquire_slot(
        self,
        parent_execution_id: str,
        limit: int,
        cancellation_token: RunCancellationToken,
        deadline: float,
    ) -> None:
        """获取并发槽位；超额时入队等待，支持取消与排队超时退出。"""
        if cancellation_token.cancelled:
            raise asyncio.CancelledError
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] | None = None
        async with self._lock:
            active = self._active_by_parent.get(parent_execution_id, 0)
            if active < limit:
                self._active_by_parent[parent_execution_id] = active + 1
                return
            future = loop.create_future()
            self._waiters_by_parent.setdefault(parent_execution_id, []).append(future)

        cancel_task = asyncio.create_task(cancellation_token.wait())
        timeout_seconds = max(0.0, deadline - loop.time())
        try:
            done, _ = await asyncio.wait(
                {future, cancel_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if future in done:
                return
            async with self._lock:
                waiters = self._waiters_by_parent.get(parent_execution_id, [])
                if future in waiters:
                    waiters.remove(future)
                elif future.done():
                    self._release_slot_locked(parent_execution_id)
            if cancel_task in done:
                raise asyncio.CancelledError
            raise AgentDelegationError("DELEGATION_TIMEOUT")
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _release_slot(self, parent_execution_id: str) -> None:
        """释放并发槽位并唤醒等待队列中的下一个任务。"""
        async with self._lock:
            self._release_slot_locked(parent_execution_id)

    def _release_slot_locked(self, parent_execution_id: str) -> None:
        """锁内执行槽位释放或交接。"""
        waiters = self._waiters_by_parent.get(parent_execution_id, [])
        while waiters:
            next_waiter = waiters.pop(0)
            if not next_waiter.done():
                next_waiter.set_result(None)
                return
        active = self._active_by_parent.get(parent_execution_id, 0)
        if active <= 1:
            self._active_by_parent.pop(parent_execution_id, None)
            self._waiters_by_parent.pop(parent_execution_id, None)
        else:
            self._active_by_parent[parent_execution_id] = active - 1

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

    def _emit_child_binding(
        self,
        command: DelegateAgent,
        ref: ExecutionRef,
        target: DelegationTarget,
    ) -> None:
        """child 创建成功后，向父 task 的 tool.delta 写入一对一绑定字段。

        只在受控 task handler 内（存在 delegation call context）生效；
        测试或非 UI 路径没有父上下文时静默跳过，派出卡保持不可进入。
        """
        try:
            call = current_delegation_call()
        except AgentDelegationError:
            return
        port = getattr(call.run_context, "event_port", None)
        if not callable(port):
            return
        port(
            TOOL_DELTA,
            {
                "tool_call_id": call.tool_call_id,
                "child_execution_id": ref.execution_id,
                "child_agent_id": target.agent_id,
            },
            None,
            None,
            None,
        )

    async def _run_with_cancellation(
        self,
        runner: DelegationRunner,
        command: DelegateAgent,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """让父取消、timeout 和 runner 终态竞争，并始终回收内部 Task。"""
        if command.cancellation_token.cancelled:
            raise asyncio.CancelledError
        if timeout_seconds <= 0:
            raise TimeoutError
        runner_task = asyncio.create_task(
            runner(command),
            name=f"harness-delegation-{command.target_agent_id}",
        )
        cancel_task = asyncio.create_task(command.cancellation_token.wait())
        try:
            done, _ = await asyncio.wait(
                {runner_task, cancel_task},
                timeout=timeout_seconds,
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
