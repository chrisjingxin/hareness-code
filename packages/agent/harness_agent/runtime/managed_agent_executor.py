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

logger = logging.getLogger(__name__)

ManagedOutputPolicy = Literal["passthrough", "capture_only", "structured"]


class ManagedAgentExecutionError(RuntimeError):
    """Managed executor 对外暴露的稳定执行错误。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存调用方可映射到产品错误码的稳定 code。"""
        self.code = code
        super().__init__(message or code)


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


RuntimeProvider = Callable[[], Awaitable[ManagedAgentRuntime]]
ExecutionStarter = Callable[[str], Awaitable[None]]


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
        if not all(
            isinstance(snapshot_id, str) and snapshot_id
            for snapshot_id in self.required_skill_snapshot_ids
        ):
            raise ValueError("MANAGED_AGENT_REQUIRED_SKILL_SNAPSHOT_INVALID")


@dataclass(frozen=True, slots=True)
class ManagedAgentResult:
    """一次 managed 执行的归一化结果；结构化策略由 adapter 解析正文。"""

    final_content: str
    usage: Mapping[str, int]
    output_policy: ManagedOutputPolicy
    used_agent: bool


class ManagedAgentExecutor:
    """统一拥有 Agent runtime、shared stream 与 Interaction resume loop。"""

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
                runtime = await request.runtime_provider()
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
        while True:
            observer.on_model_round()
            if resume is not None:
                _schedule_interaction_resume(runtime.run_context)
            stream_input = (
                Command(resume=resume)
                if resume is not None
                else _initial_stream_input(request.input)
            )
            try:
                stream_result = await self._execute_stream_round(
                    request,
                    observer,
                    runtime,
                    session,
                    stream_input,
                )
            except ExecutionStreamError as exc:
                raise ManagedAgentExecutionError(exc.code, exc.message) from exc
            if stream_result.resume is None:
                result = ManagedAgentResult(
                    final_content=stream_result.final_content,
                    usage=dict(stream_result.usage),
                    output_policy=request.output_policy,
                    used_agent=True,
                )
                # Transcript/checkpoint observer 可能仍需使用 runtime 关联的
                # middleware；必须在 finally 释放 lease 前完成最终投影。
                await observer.on_execution_complete(result)
                return result
            resume = stream_result.resume

    async def _execute_stream_round(
        self,
        request: ManagedAgentRequest,
        observer: ManagedAgentObserver,
        runtime: ManagedAgentRuntime,
        session: StreamSession,
        stream_input: object,
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
            ),
            observer,
        )


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
