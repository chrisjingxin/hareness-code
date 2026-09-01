"""统一管理 Build、Compose 与 Plugin Agent 的 LangGraph 执行生命周期。

调用方只准备已冻结的执行事实与 observer，不接触 Agent graph。这个 module
负责取得一次运行时 lease、构造 checkpoint 配置、复用 execution stream 完成
Interaction resume、归一化 stream 错误，并在所有退出路径释放 runtime。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from harness_agent.runtime.execution_stream import (
    ExecutionStreamError,
    ExecutionStreamPorts,
    ExecutionStreamRequest,
    StreamSession,
    execute as execute_stream,
)
from harness_agent.diagnostic_log.runtime import DiagnosticLog, ensure_log

logger = logging.getLogger(__name__)

ManagedOutputPolicy = Literal["passthrough", "capture_only", "structured"]


class ProviderRetryPolicy(Protocol):
    """provider 错误的可选有界重试策略；无策略时保持原有错误直通。"""

    def should_retry(self, attempt: int, error: BaseException) -> bool: ...

    def retry_delay_seconds(self, error: BaseException) -> float: ...


class ManagedTimingPort(Protocol):
    """把 executor 的主动区间和 retry 等待汇入 Run 时间账本。"""

    def begin_active(self) -> float: ...

    def end_active(self, started_at: float) -> None: ...

    def begin_wait(self) -> float: ...

    def end_retry_wait(self, started_at: float) -> None: ...


_RATE_LIMIT_CODES = frozenset(
    {"RATE_LIMITED", "PROVIDER_RATE_LIMITED", "429", "THROTTLED"}
)


def is_provider_rate_limited(error: BaseException) -> bool:
    """判断异常是否携带稳定 provider 限流事实。"""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.upper() in _RATE_LIMIT_CODES:
        return True
    if getattr(error, "status_code", None) == 429:
        return True
    message = str(error)
    return "429" in message or "rate limit" in message.lower()


class ManagedAgentExecutionError(RuntimeError):
    """Managed executor 对外暴露的稳定执行错误。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存调用方可映射到产品错误码的稳定 code。"""
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class ManagedAgentRuntime(Protocol):
    """一次已取得的 AgentEngine/run lease 的最小视图。

    runtime provider 可以在 Host 内部借助 AgentEnginePool 创建该对象，但只把
    graph、checkpoint 配置和成对 release seam 暴露给 executor，避免 adapter
    触碰 ``engine.graph`` 或分别释放 run/engine lease。
    """

    agent: Any | None
    run_context: object | None

    def graph_config(self, checkpoint_namespace: str) -> Mapping[str, object]:
        """为本次执行生成唯一 checkpoint namespace 配置。"""

    async def release(self) -> None:
        """按 runtime 既定顺序释放 run lease 和 engine lease。"""


class ManagedAgentObserver(ExecutionStreamPorts, Protocol):
    """产品 adapter 注入的观察与 Interaction seam。"""

    def on_model_round(self) -> None:
        """在每个初始或 resume 模型回合开始前投影进度。"""

    async def on_execution_complete(self, result: "ManagedAgentResult") -> None:
        """在 runtime release 前持久化 adapter 的最终投影。"""


class FailClosedManagedObserver:
    """不向用户暴露 child stream 时使用的安全 observer。

    Plugin Agent 的显式 task 入口当前不提供 child Interaction UI。该 observer
    丢弃 capture_only 信号，但任何审批或提问都会 fail closed，绝不默认批准。
    """

    def on_model_round(self) -> None:
        """无 child progress 投影时不产生额外 root event。"""
        return None

    async def on_execution_complete(self, _result: "ManagedAgentResult") -> None:
        """Plugin adapter 直接读取 result，不需要额外持久化。"""
        return None

    def emit(self, _signal: object) -> None:
        """capture_only child signal 不进入父 Agent 的公开事件流。"""
        return None

    async def interact(self, _request: object) -> object:
        """拒绝未显式绑定的 child Interaction，避免绕过 Policy/审批界面。"""
        raise ManagedAgentExecutionError("MANAGED_AGENT_INTERACTION_UNAVAILABLE")

    async def observe_message(self, _chunk: object, _session: StreamSession) -> bool:
        """Plugin child 不写 root Transcript；只返回最终结构化结果。"""
        return False

    async def after_tool_boundary(self) -> None:
        """无 root Transcript 边界需要 flush。"""
        return None

    def on_stream_event(self) -> None:
        """Plugin child 没有额外 context projection。"""
        return None


RuntimeProvider = Callable[[], Awaitable[ManagedAgentRuntime]]
ExecutionStarter = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ManagedFinalOutput:
    """终态 gate 可见的有界 child 输出与执行 provenance。"""

    execution_ref: str
    parent_execution_ref: str | None
    run_id: str
    final_content: str


@dataclass(frozen=True, slots=True)
class FinalOutputGateDecision:
    """终态 gate 对同一 Managed execution 的安全裁决。"""

    action: Literal["allow", "continue"]
    continuation_prompt: str = ""
    skip_once: bool = False
    warning: str = ""

    def __post_init__(self) -> None:
        """只接受 allow/continue，且继续动作必须提供下一回合输入。"""
        if self.action not in {"allow", "continue"}:
            raise ValueError("MANAGED_FINAL_GATE_ACTION_INVALID")
        if not isinstance(self.skip_once, bool):
            raise ValueError("MANAGED_FINAL_GATE_SKIP_INVALID")
        if not isinstance(self.warning, str):
            raise ValueError("MANAGED_FINAL_GATE_WARNING_INVALID")
        if self.action == "continue" and not self.continuation_prompt.strip():
            raise ValueError("MANAGED_FINAL_GATE_PROMPT_REQUIRED")
        if self.action == "continue" and self.skip_once:
            raise ValueError("MANAGED_FINAL_GATE_SKIP_INVALID")
        if self.action == "allow" and self.continuation_prompt:
            raise ValueError("MANAGED_FINAL_GATE_PROMPT_UNEXPECTED")


FinalOutputGate = Callable[
    [ManagedFinalOutput], Awaitable[FinalOutputGateDecision]
]


@dataclass(frozen=True, slots=True)
class ManagedAgentRequest:
    """一次 managed 执行的冻结输入，不允许在运行中重新解析。

    ``execution_starter`` 是 executor 唯一触发 registry running transition 的
    受控 callback。``agent_spec``、``interaction_policy`` 与 required Skill
    snapshot 是执行 provenance；当前 Build tracer bullet 不解释其内容，但它们
    随 request 一起固定，后续 Compose/Plugin adapter 可复用相同 interface。
    """

    execution_ref: str
    parent_execution_ref: str | None
    run_id: str
    input: str | Mapping[str, object]
    checkpoint_namespace: str
    output_policy: ManagedOutputPolicy
    runtime_provider: RuntimeProvider
    is_cancelled: Callable[[], bool]
    idempotency_key: str
    execution_starter: ExecutionStarter | None = None
    agent_spec: object | None = None
    interaction_policy: object | None = None
    timeout_seconds: float | None = None
    required_skill_snapshot_ids: tuple[str, ...] = ()
    usage: dict[str, int] | None = None
    started_at: float = field(default_factory=time.monotonic)
    provider_retry: ProviderRetryPolicy | None = None
    diagnostic_log: DiagnosticLog | None = None
    timing: ManagedTimingPort | None = None
    model_profile_id: str = "default"
    # 目录信任等必须用户决策的中断判定钩子；透传给 shared execution stream。
    needs_user_decision: Callable[[str, Mapping[str, object]], bool] | None = None
    # Managed child 的最终输出提交前门禁；continue 只在同一 runtime/checkpoint
    # 内开始下一模型回合，不创建新的 DelegationTarget 或 execution。
    final_output_gate: FinalOutputGate | None = None

    def __post_init__(self) -> None:
        """在取得昂贵 runtime 前拒绝不完整的执行事实。"""
        if not all(
            isinstance(value, str) and value
            for value in (
                self.execution_ref,
                self.run_id,
                self.checkpoint_namespace,
                self.idempotency_key,
            )
        ):
            raise ValueError("MANAGED_AGENT_REQUEST_INVALID")
        if self.parent_execution_ref is not None and not self.parent_execution_ref:
            raise ValueError("MANAGED_AGENT_REQUEST_INVALID")
        if self.output_policy not in {"passthrough", "capture_only", "structured"}:
            raise ValueError("MANAGED_AGENT_OUTPUT_POLICY_INVALID")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("MANAGED_AGENT_TIMEOUT_INVALID")
        if self.execution_starter is not None and not callable(self.execution_starter):
            raise ValueError("MANAGED_AGENT_EXECUTION_STARTER_INVALID")
        if self.final_output_gate is not None and not callable(self.final_output_gate):
            raise ValueError("MANAGED_AGENT_FINAL_GATE_INVALID")
        if not all(
            isinstance(snapshot_id, str) and snapshot_id
            for snapshot_id in self.required_skill_snapshot_ids
        ):
            raise ValueError("MANAGED_AGENT_REQUIRED_SKILL_SNAPSHOT_INVALID")
        object.__setattr__(self, "diagnostic_log", ensure_log(self.diagnostic_log))
        if not isinstance(self.model_profile_id, str) or not self.model_profile_id:
            raise ValueError("MANAGED_AGENT_MODEL_PROFILE_INVALID")


@dataclass(frozen=True, slots=True)
class ManagedAgentResult:
    """一次 managed 执行的归一化结果；结构化策略由 adapter 解析正文。"""

    final_content: str
    usage: Mapping[str, int]
    output_policy: ManagedOutputPolicy
    used_agent: bool
    warning: str = ""


class ManagedAgentExecutor:
    """统一拥有 Agent runtime、shared stream 与 Interaction resume loop。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """注入 monotonic clock/sleep，使 attempt 与 retry 计时可确定测试。"""
        self._clock = clock
        self._sleep = sleep

    async def execute(
        self,
        request: ManagedAgentRequest,
        observer: ManagedAgentObserver,
    ) -> ManagedAgentResult:
        """执行至最终输出、取消或错误，并保证已取得 runtime 恰好释放一次。"""
        runtime: ManagedAgentRuntime | None = None
        try:
            async def run_with_acquired_runtime() -> ManagedAgentResult:
                """在 timeout 范围内取得 runtime，并把 release 留给外层 finally。"""
                nonlocal runtime
                if request.execution_starter is not None:
                    await request.execution_starter(request.execution_ref)
                acquire_started = self._clock()
                active_started = (
                    request.timing.begin_active() if request.timing is not None else None
                )
                try:
                    runtime = await request.runtime_provider()
                except BaseException as exc:
                    request.diagnostic_log.error(
                        "runtime.acquire.failed",
                        {
                            "duration_ms": _duration_ms(self._clock, acquire_started),
                            **_error_fields(exc, "runtime_acquire", "runtime_acquire_failed"),
                        },
                    )
                    raise
                finally:
                    if request.timing is not None and active_started is not None:
                        request.timing.end_active(active_started)
                request.diagnostic_log.info(
                    "runtime.acquire.completed",
                    {
                        "source": getattr(runtime, "acquire_source", "reused"),
                        "queue_ms": max(0, int(getattr(runtime, "queue_ms", 0))),
                        "build_ms": max(0, int(getattr(runtime, "build_ms", 0))),
                        "duration_ms": _duration_ms(self._clock, acquire_started),
                    },
                )
                return await self._execute_with_runtime(request, observer, runtime)

            if request.timeout_seconds is None:
                return await run_with_acquired_runtime()
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    return await run_with_acquired_runtime()
            except TimeoutError as exc:
                raise ManagedAgentExecutionError(
                    "MANAGED_AGENT_TIMEOUT",
                    "Managed agent execution timed out",
                ) from exc
        finally:
            if runtime is not None:
                release_started = self._clock()
                try:
                    await runtime.release()
                except asyncio.CancelledError:
                    # 保持 Task cancellation 语义；Coordinator 会将它收敛为唯一
                    # cancelled terminal，不能当作普通清理诊断吞掉。
                    raise
                except Exception:
                    # 与原 RunCoordinator release 路径一致：已经完成的 Build
                    # 不能因清理诊断反转为 failed；若有原始异常也不得被遮蔽。
                    logger.exception(
                        "Unable to release managed runtime for execution %s",
                        request.execution_ref,
                    )
                    request.diagnostic_log.warn(
                        "runtime.released",
                        {
                            "outcome": "failed",
                            "duration_ms": _duration_ms(self._clock, release_started),
                        },
                    )
                else:
                    request.diagnostic_log.info(
                        "runtime.released",
                        {
                            "outcome": "released",
                            "duration_ms": _duration_ms(self._clock, release_started),
                        },
                    )

    async def _execute_with_runtime(
        self,
        request: ManagedAgentRequest,
        observer: ManagedAgentObserver,
        runtime: ManagedAgentRuntime,
    ) -> ManagedAgentResult:
        """在已取得 runtime 内完成 echo/stream/resume；不在这里释放 lease。"""
        if request.is_cancelled():
            raise ManagedAgentExecutionError("RUN_CANCELLED", "Run was cancelled")
        if runtime.agent is None:
            result = ManagedAgentResult(
                final_content=_echo_content(request.input),
                usage=dict(request.usage or {}),
                output_policy=request.output_policy,
                used_agent=False,
            )
            await observer.on_execution_complete(result)
            return result

        session = StreamSession(run_id=request.run_id, started_at=request.started_at)
        if request.usage is not None:
            # Build terminal usage 与 Coordinator 的同一 dict 共享，resume 后的
            # 各个模型回合会累积到同一位置，不能生成第二份副本。
            session.usage = request.usage

        resume: object | None = None
        model_round = 0
        stream_input: object = _initial_stream_input(request.input)
        warning = ""
        while True:
            model_round += 1
            observer.on_model_round()
            if resume is not None:
                _schedule_interaction_resume(runtime.run_context)
                stream_input = Command(resume=resume)
            stream_result = await self._execute_round_with_retry(
                request,
                observer,
                runtime,
                session,
                stream_input,
                model_round,
            )
            if stream_result.resume is None:
                if request.is_cancelled():
                    raise ManagedAgentExecutionError("RUN_CANCELLED", "Run was cancelled")
                if request.final_output_gate is not None:
                    gate_decision = await request.final_output_gate(
                        ManagedFinalOutput(
                            execution_ref=request.execution_ref,
                            parent_execution_ref=request.parent_execution_ref,
                            run_id=request.run_id,
                            final_content=stream_result.final_content,
                        )
                    )
                    if not isinstance(gate_decision, FinalOutputGateDecision):
                        raise ManagedAgentExecutionError(
                            "MANAGED_AGENT_FINAL_GATE_INVALID"
                        )
                    warning = gate_decision.warning or warning
                    if request.is_cancelled():
                        raise ManagedAgentExecutionError("RUN_CANCELLED", "Run was cancelled")
                    if gate_decision.action == "continue":
                        # execution_stream 会在下一回合开始时清理本回合正文；
                        # 这里先清理，防止 fake/无正文模型把上一轮结果带回父端。
                        session.content_parts.clear()
                        stream_input = _initial_stream_input(
                            gate_decision.continuation_prompt
                        )
                        resume = None
                        continue
                result = ManagedAgentResult(
                    final_content=stream_result.final_content,
                    usage=dict(stream_result.usage),
                    output_policy=request.output_policy,
                    used_agent=True,
                    warning=warning,
                )
                # Transcript/checkpoint observer 可能仍需使用 runtime 关联的
                # middleware；必须在 finally 释放 lease 前完成最终投影。
                await observer.on_execution_complete(result)
                return result
            resume = stream_result.resume

    async def _execute_round_with_retry(
        self,
        request: ManagedAgentRequest,
        observer: ManagedAgentObserver,
        runtime: ManagedAgentRuntime,
        session: StreamSession,
        stream_input: object,
        model_round: int,
    ):
        """在 provider 预算内重试 stream round；无策略时保持原有错误直通。"""
        attempt = 1
        while True:
            attempt_started = self._clock()
            first_chunk_at: float | None = None
            usage_before = dict(session.usage)
            active_started = (
                request.timing.begin_active() if request.timing is not None else None
            )
            request.diagnostic_log.info(
                "model.started",
                {
                    "model_round": model_round,
                    "provider_attempt": attempt,
                    "profile_id": request.model_profile_id,
                },
            )

            def mark_first_chunk() -> None:
                nonlocal first_chunk_at
                if first_chunk_at is None:
                    first_chunk_at = self._clock()

            try:
                result = await self._execute_stream_round(
                    request,
                    observer,
                    runtime,
                    session,
                    stream_input,
                    mark_first_chunk,
                )
                request.diagnostic_log.info(
                    "model.completed",
                    {
                        "model_round": model_round,
                        "provider_attempt": attempt,
                        "duration_ms": _duration_ms(self._clock, attempt_started),
                        "provider_first_chunk_ms": (
                            None
                            if first_chunk_at is None
                            else max(0, round((first_chunk_at - attempt_started) * 1000))
                        ),
                        "usage": _usage_delta(usage_before, session.usage),
                        "finish_reason": (
                            "interaction" if result.resume is not None else "completed"
                        ),
                    },
                )
                return result
            except asyncio.CancelledError:
                raise
            except ExecutionStreamError as exc:
                _log_model_failure(
                    request,
                    model_round,
                    attempt,
                    _duration_ms(self._clock, attempt_started),
                    exc,
                )
                if (
                    request.provider_retry is not None
                    and request.provider_retry.should_retry(attempt, exc)
                ):
                    if request.timing is not None and active_started is not None:
                        request.timing.end_active(active_started)
                        active_started = None
                    await self._retry_sleep(
                        request, request.provider_retry, exc, model_round, attempt
                    )
                    attempt += 1
                    continue
                raise ManagedAgentExecutionError(exc.code, exc.message) from exc
            except Exception as exc:  # noqa: BLE001 - retry 边界需要判定任何错误
                _log_model_failure(
                    request,
                    model_round,
                    attempt,
                    _duration_ms(self._clock, attempt_started),
                    exc,
                )
                if (
                    request.provider_retry is not None
                    and request.provider_retry.should_retry(attempt, exc)
                ):
                    if request.timing is not None and active_started is not None:
                        request.timing.end_active(active_started)
                        active_started = None
                    await self._retry_sleep(
                        request, request.provider_retry, exc, model_round, attempt
                    )
                    attempt += 1
                    continue
                if is_provider_rate_limited(exc):
                    raise ManagedAgentExecutionError(
                        "PROVIDER_RATE_LIMITED",
                        "Provider rate limit budget exhausted",
                    ) from exc
                raise
            finally:
                if request.timing is not None and active_started is not None:
                    request.timing.end_active(active_started)

    async def _retry_sleep(
        self,
        request: ManagedAgentRequest,
        policy: ProviderRetryPolicy,
        error: BaseException,
        model_round: int,
        provider_attempt: int,
    ) -> None:
        """按策略延迟并在醒来后复核取消；取消语义不被重试吞掉。"""
        delay = max(0.0, policy.retry_delay_seconds(error))
        request.diagnostic_log.warn(
            "model.retry_scheduled",
            {
                "model_round": model_round,
                "provider_attempt": provider_attempt,
                "retry_wait_ms": round(delay * 1000),
                "reason_code": _error_code(error),
            },
        )
        wait_started = (
            request.timing.begin_wait() if request.timing is not None else None
        )
        try:
            await self._sleep(delay)
        finally:
            if request.timing is not None and wait_started is not None:
                request.timing.end_retry_wait(wait_started)
        if request.is_cancelled():
            raise ManagedAgentExecutionError(
                "RUN_CANCELLED",
                "Run was cancelled during provider retry",
            )

    async def _execute_stream_round(
        self,
        request: ManagedAgentRequest,
        observer: ManagedAgentObserver,
        runtime: ManagedAgentRuntime,
        session: StreamSession,
        stream_input: object,
        on_provider_activity: Callable[[], None],
    ):
        """执行一个 initial/resume stream round；structured 复用静默正文策略。"""
        visibility = (
            "passthrough"
            if request.output_policy == "passthrough"
            else "capture_only"
        )
        return await execute_stream(
            ExecutionStreamRequest(
                agent=runtime.agent,
                stream_input=stream_input,
                graph_config=runtime.graph_config(request.checkpoint_namespace),
                context=runtime.run_context,
                content_visibility=visibility,
                session=session,
                is_cancelled=request.is_cancelled,
                needs_user_decision=request.needs_user_decision,
                on_provider_activity=on_provider_activity,
            ),
            observer,
        )


def _duration_ms(clock: Callable[[], float], started_at: float) -> int:
    return max(0, round((clock() - started_at) * 1000))


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return str(code) if isinstance(code, str) and code else type(error).__name__


def _error_fields(
    error: BaseException,
    failure_stage: str,
    summary_code: str,
) -> dict[str, object]:
    return {
        "failure_stage": failure_stage,
        "error_code": _error_code(error),
        "error_type": type(error).__name__,
        "retryable": False,
        "summary_code": summary_code,
    }


def _log_model_failure(
    request: ManagedAgentRequest,
    model_round: int,
    provider_attempt: int,
    duration_ms: int,
    error: BaseException,
) -> None:
    fields = {
        "model_round": model_round,
        "provider_attempt": provider_attempt,
        "duration_ms": duration_ms,
        **_error_fields(error, "provider_attempt", "model_attempt_failed"),
    }
    if request.provider_retry is not None:
        request.diagnostic_log.warn("model.failed", fields)
        return
    request.diagnostic_log.error("model.failed", fields)


def _usage_delta(
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for key, aliases in (
        ("input_tokens", ("input_tokens",)),
        ("output_tokens", ("output_tokens",)),
        ("cached_input_tokens", ("cached_input_tokens", "cached_tokens")),
    ):
        found_key = next((k for k in aliases if k in after), None)
        if found_key is None:
            result[key] = None
            continue
        after_val = int(after[found_key])
        before_val = 0
        for k in aliases:
            if k in before:
                before_val = int(before[k])
                break
        delta = max(0, after_val - before_val)
        result[key] = delta if delta > 0 else None
    return result


@dataclass(slots=True)
class _PooledAgentRuntime:
    """由 executor module 持有的 AgentEnginePool lease 适配器。"""

    agent: Any | None
    run_context: object | None
    graph_config: Callable[[str], Mapping[str, object]]
    _release: Callable[[], Awaitable[None]]
    _checkpoint_cleanup: Callable[[], Awaitable[None]] | None = None
    _on_release: Callable[[], None] | None = None

    async def release(self) -> None:
        """按 run lease、engine lease、draining 检查顺序释放资源。"""
        first_error: BaseException | None = None
        try:
            await self._release()
        except BaseException as exc:
            first_error = exc
        try:
            if self._checkpoint_cleanup is not None:
                await self._checkpoint_cleanup()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            if self._on_release is not None:
                self._on_release()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


async def acquire_pooled_agent_runtime(
    *,
    pool: Any,
    profile: Any,
    run_context: object | None,
    graph_config: Callable[[str], Mapping[str, object]],
    checkpoint_cleanup: Callable[[], Awaitable[None]] | None = None,
    on_release: Callable[[], None] | None = None,
) -> ManagedAgentRuntime:
    """从 AgentEnginePool 获取一次运行时，并封装所有 lease 清理。

    Plugin/Compose adapter 只能把这个 function 作为 request 的 runtime provider
    交给 ``ManagedAgentExecutor`` 调用；只有本 module 读取 ``engine.graph``。
    ``checkpoint_cleanup`` 只清理本 execution 的内部 checkpoint；``on_release``
    只负责通知调用方清理与本 execution 绑定的进程内反馈，二者都不能释放
    sibling 或 root 的资源。
    """
    profile_key = getattr(profile, "profile_key", None)
    if not isinstance(profile_key, str) or not profile_key:
        raise ManagedAgentExecutionError("MANAGED_AGENT_PROFILE_INVALID")
    lease: Any | None = None
    run_lease: Any | None = None
    try:
        lease = await pool.acquire(profile)
        run_lease = await lease.run()
        graph = getattr(getattr(lease, "engine", None), "graph", None)
        if graph is None:
            raise ManagedAgentExecutionError("MANAGED_AGENT_GRAPH_UNAVAILABLE")

        async def release() -> None:
            """保证前一个 release 失败时仍继续释放下层资源。"""
            await _release_pooled_leases(pool, profile_key, lease, run_lease)

        return _PooledAgentRuntime(
            agent=graph,
            run_context=run_context,
            graph_config=graph_config,
            _release=release,
            _checkpoint_cleanup=checkpoint_cleanup,
            _on_release=on_release,
        )
    except BaseException:
        try:
            await _release_pooled_leases(pool, profile_key, lease, run_lease)
        except BaseException:
            logger.exception(
                "Unable to clean up pooled runtime after acquire failure profile=%s",
                profile_key,
            )
        if checkpoint_cleanup is not None:
            try:
                await checkpoint_cleanup()
            except BaseException:
                logger.exception(
                    "Unable to clean up execution checkpoint after acquire failure profile=%s",
                    profile_key,
                )
        raise


async def _release_pooled_leases(
    pool: Any,
    profile_key: str,
    lease: Any | None,
    run_lease: Any | None,
) -> None:
    """释放可选 run/engine lease，并始终执行 pool draining 收敛。"""
    try:
        if run_lease is not None:
            await run_lease.release()
    finally:
        try:
            if lease is not None:
                await lease.release()
        finally:
            await pool.finalize_draining(profile_key)


def _initial_stream_input(value: str | Mapping[str, object]) -> object:
    """将用户文本转换为 LangGraph 输入，结构化 ContextPack 原样交给 graph。"""
    if isinstance(value, str):
        return {"messages": [HumanMessage(content=value)]}
    return dict(value)


def _echo_content(value: str | Mapping[str, object]) -> str:
    """为无 Agent 的协议测试路径生成可投影的确定性文本。"""
    if isinstance(value, str):
        return value
    raise ManagedAgentExecutionError("MANAGED_AGENT_ECHO_INPUT_INVALID")


def _schedule_interaction_resume(run_context: object | None) -> None:
    """将 resume 标记为同一 Run 的后续模型回合，避免误判 idle top-level run。"""
    if run_context is None:
        return
    lifecycle = getattr(run_context, "model_call_lifecycle", None)
    schedule = getattr(lifecycle, "schedule", None)
    if not callable(schedule):
        raise ManagedAgentExecutionError("MANAGED_AGENT_CONTEXT_LIFECYCLE_REQUIRED")
    schedule("interaction_resume")
