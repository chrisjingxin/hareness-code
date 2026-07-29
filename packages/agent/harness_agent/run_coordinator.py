"""Run 生命周期 deep module：集中受理、执行、交互、终态和资源清理。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness_agent.agent_engine import AgentEnginePoolCapacityError
from harness_agent.agent_engine_profile import AgentEngineProfile
from harness_agent.execution_binding import (
    ResolvedExecutionBinding,
    RunExecutionBinding,
)
from harness_agent.run_context import RunCancellationToken, RunContext
from harness_agent.skills import SkillError, SkillRegistry
from harness_agent.thread_persistence import AcceptRun, ThreadPersistenceError

logger = logging.getLogger(__name__)

MAX_TOOL_PAYLOAD_BYTES = 1 * 1024 * 1024
INTERACTION_TIMEOUT_MS = 300_000

RUN_STARTED = "run.started"
SKILL_LOADED = "skill.loaded"
CONTENT_DELTA = "content.delta"
TOOL_STARTED = "tool.started"
TOOL_DELTA = "tool.delta"
TOOL_COMPLETED = "tool.completed"
CONTEXT_UPDATED = "context.updated"
INTERACTION_RESOLVED = "interaction.resolved"
RUN_COMPLETED = "run.completed"
RUN_CANCELLED = "run.cancelled"
RUN_FAILED = "run.failed"


class RunError(RuntimeError):
    """Run 领域错误；Protocol adapter 负责把它转换为 JSON-RPC 错误。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retryable: bool = False,
        details: object | None = None,
    ) -> None:
        """保存稳定错误码、诊断文案和可选详情。"""
        self.code = code
        self.retryable = retryable
        self.details = details
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class RunRef:
    """一次 Run 的稳定身份。"""

    thread_id: str
    run_id: str

    def __post_init__(self) -> None:
        """拒绝空身份，避免把错误推迟到 registry 查找阶段。"""
        if not self.thread_id or not self.run_id:
            raise ValueError("RUN_REFERENCE_INVALID")


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    """连接的轻量身份引用，不携带 JSON-RPC transport 状态。"""

    connection_id: str

    def __post_init__(self) -> None:
        """拒绝空连接身份。"""
        if not self.connection_id:
            raise ValueError("CONNECTION_REFERENCE_INVALID")


@dataclass(frozen=True, slots=True)
class RequestedSkill:
    """用户在 run.start 中显式选择的 Skill。"""

    skill_id: str
    args: str = ""


@dataclass(frozen=True, slots=True)
class StartRun:
    """RunCoordinator 受理所需的类型化输入。"""

    thread_id: str
    run_id: str
    message: str
    requested_skill: RequestedSkill | None = None
    requested_primary_profile: str | None = None

    @property
    def ref(self) -> RunRef:
        """返回本次 Run 的稳定身份。"""
        return RunRef(self.thread_id, self.run_id)

    def fingerprint(self) -> tuple[object, ...]:
        """返回幂等判断所需的请求指纹。"""
        skill = self.requested_skill
        return (
            self.thread_id,
            self.run_id,
            self.message,
            skill.skill_id if skill else None,
            skill.args if skill else None,
            self.requested_primary_profile,
        )


@dataclass(frozen=True, slots=True)
class RunPreparation:
    """模型选择、实际绑定和 AgentEngine Profile 的一次解析结果。"""

    resolved_execution_binding: ResolvedExecutionBinding | None = None
    execution_binding: RunExecutionBinding | None = None
    agent_engine_profile: AgentEngineProfile | None = None
    skill_snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunCompletion:
    """Run 的唯一终态事实。"""

    status: str
    usage: Mapping[str, int]
    duration_ms: int
    finish_reason: str
    context: Mapping[str, object] = field(default_factory=dict)
    error: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """与 transport 无关的 Agent 事件；由 Host 负责 fanout。"""

    event_id: str
    type: str
    thread_id: str
    run_id: str
    sequence: int
    timestamp_ms: int
    payload: Mapping[str, object]

    def record(self) -> dict[str, object]:
        """转换成现有 v3 event notification 使用的字段。"""
        return {
            "event_id": self.event_id,
            "type": self.type,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp_ms": self.timestamp_ms,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """Agent 请求 owner 审批或回答问题。"""

    request_id: str
    type: str
    payload: Mapping[str, object]
    interrupt_id: str
    questions: tuple[Mapping[str, object], ...] = ()
    action_count: int = 1


@dataclass(frozen=True, slots=True)
class InteractionResult:
    """InteractionPort 返回的语言无关结果。"""

    value: object
    expired: bool = False


@dataclass(frozen=True, slots=True)
class CancelResult:
    """取消请求的结果。"""

    cancelled: bool
    run_id: str


@dataclass(frozen=True, slots=True)
class RunExecution:
    """已受理 Run 的结果和后续事件流。"""

    ref: RunRef
    owner: ConnectionRef
    accepted: bool
    events: AsyncIterator[AgentEvent]


class InteractionPort(Protocol):
    """向 Run owner 发起类型化 Interaction 的 seam。"""

    async def request(
        self,
        owner: ConnectionRef,
        run: RunRef,
        interaction: InteractionRequest,
    ) -> InteractionResult:
        """等待 owner 返回审批或问答结果。"""


@dataclass(slots=True)
class RunRuntime:
    """一次 Run 的 Agent 图、Context 和共享资源 lease。"""

    agent: Any | None
    run_context: RunContext | None
    graph_config: Callable[[str], dict[str, dict[str, str]]]
    release: Callable[[], Awaitable[None]]


@dataclass(slots=True)
class RunState:
    """Coordinator 内部保存的单次 Run 状态；不属于 ProtocolConnection。"""

    start: StartRun
    owner: ConnectionRef
    persistence: Any | None
    preparation: RunPreparation
    skill_record: Any | None = None
    message: str = ""
    status: str = "accepted"
    sequence: int = 0
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    tool_stream_ids: dict[str, str] = field(default_factory=dict)
    tool_result_ids: dict[str, str] = field(default_factory=dict)
    started_tool_ids: set[str] = field(default_factory=set)
    last_tool_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    context_summary: dict[str, object] = field(default_factory=dict)
    cancellation_token: RunCancellationToken = field(default_factory=RunCancellationToken)
    run_context: RunContext | None = None
    agent_engine_lease: Any | None = None
    agent_engine_run_lease: Any | None = None
    agent_engine_profile_key: str | None = None
    runtime: RunRuntime | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    completion: RunCompletion | None = None
    terminal_event_emitted: bool = False
    events: asyncio.Queue[AgentEvent | None] = field(default_factory=asyncio.Queue)

    def __post_init__(self) -> None:
        """默认使用受理请求中的原始消息。"""
        if not self.message:
            self.message = self.start.message

    @property
    def ref(self) -> RunRef:
        """返回当前 Run 的身份。"""
        return self.start.ref

    @property
    def thread_id(self) -> str:
        """兼容资源 adapter 读取 Run 身份的便捷属性。"""
        return self.ref.thread_id

    @property
    def run_id(self) -> str:
        """兼容资源 adapter 读取 Run 身份的便捷属性。"""
        return self.ref.run_id

    @property
    def resolved_execution_binding(self) -> ResolvedExecutionBinding | None:
        """返回受理阶段解析出的 Thread 绑定。"""
        return self.preparation.resolved_execution_binding

    @property
    def execution_binding(self) -> RunExecutionBinding | None:
        """返回本次 Run 实际持久化的模型绑定。"""
        return self.preparation.execution_binding

    @property
    def resolved_agent_engine_profile(self) -> AgentEngineProfile | None:
        """返回受理阶段计算出的共享 AgentEngine Profile。"""
        return self.preparation.agent_engine_profile


PersistenceProvider = Callable[[], Awaitable[Any | None]]
PreparationProvider = Callable[[StartRun, Any | None], Awaitable[RunPreparation]]
RuntimeProvider = Callable[[RunState], Awaitable[RunRuntime]]
SkillRegistryProvider = Callable[[], SkillRegistry]
ContextUpdatesProvider = Callable[[str], list[Any]]


class RunCoordinator:
    """集中拥有 Run registry、执行任务、Interaction 和终态清理。"""

    def __init__(
        self,
        *,
        persistence_provider: PersistenceProvider,
        preparation_provider: PreparationProvider,
        runtime_provider: RuntimeProvider,
        interaction_port: InteractionPort,
        skill_registry_provider: SkillRegistryProvider,
        context_updates_provider: ContextUpdatesProvider | None = None,
    ) -> None:
        """注入 Project 资源 adapter，保持外部 Run interface 与 Protocol 解耦。"""
        self._persistence_provider = persistence_provider
        self._preparation_provider = preparation_provider
        self._runtime_provider = runtime_provider
        self._interaction_port = interaction_port
        self._skill_registry_provider = skill_registry_provider
        self._context_updates_provider = context_updates_provider or (lambda _thread_id: [])
        self._runs: dict[str, RunState] = {}
        self._starting_threads: set[str] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self, command: StartRun, owner: ConnectionRef) -> RunExecution:
        """受理一次 Run，并在受理成功后创建唯一执行任务。"""
        if not command.message.strip():
            raise RunError("INVALID_MESSAGE")
        async with self._lock:
            if self._closed:
                raise RunError("HOST_CLOSED", "Host is closed")
            existing = self._runs.get(command.thread_id)
            if existing is not None and existing.status not in {
                "completed",
                "failed",
                "cancelled",
            }:
                if existing.ref.run_id == command.run_id:
                    if existing.start.fingerprint() == command.fingerprint():
                        return self._accepted_without_events(existing.ref, existing.owner)
                    raise RunError(
                        "RUN_ID_CONFLICT",
                        retryable=False,
                    )
                raise RunError("THREAD_BUSY", retryable=True)
            if command.thread_id in self._starting_threads:
                raise RunError("THREAD_BUSY", retryable=True)
            self._starting_threads.add(command.thread_id)

        try:
            persistence = await self._persistence_provider()
            skill_record = self._resolve_requested_skill(command.requested_skill)
            preparation = await self._preparation_provider(command, persistence)

            if persistence is not None:
                binding = preparation.execution_binding
                if binding is None:
                    raise RunError("RUN_MODEL_BINDING_UNAVAILABLE")
                try:
                    acceptance = await persistence.accept_run(
                        AcceptRun(message=command.message, binding=binding)
                    )
                except ThreadPersistenceError as exc:
                    if str(exc) == "RUN_EXECUTION_BINDING_CONFLICT":
                        raise RunError("RUN_ID_CONFLICT") from exc
                    raise
                if not acceptance.created:
                    return self._accepted_without_events(command.ref, owner)

            run = RunState(
                start=command,
                owner=owner,
                persistence=persistence,
                preparation=preparation,
                skill_record=skill_record,
            )
            async with self._lock:
                self._runs[command.thread_id] = run
            run.task = asyncio.create_task(
                self._execute(run),
                name=f"harness-run-{command.run_id}",
            )
            return RunExecution(command.ref, owner, True, self._read_events(run))
        finally:
            async with self._lock:
                self._starting_threads.discard(command.thread_id)

    async def cancel(self, run: RunRef, requester: ConnectionRef) -> CancelResult:
        """只允许 owner 取消 Run，并让执行路径产生唯一取消终态。"""
        active = await self._lookup(run)
        if active.owner != requester:
            raise RunError("RUN_NOT_OWNER")
        if active.completion is not None:
            return CancelResult(False, run.run_id)

        active.cancel_requested = True
        active.cancellation_token.cancel()
        task = active.task
        if task is not None and not task.done() and active.status != "accepted":
            task.cancel()
        return CancelResult(True, run.run_id)

    async def owner_disconnected(self, connection: ConnectionRef) -> None:
        """取消指定 owner 拥有的 Run；其他 Connection 的 Run 不受影响。"""
        await self._cancel_runs(
            lambda run: run.owner == connection and run.completion is None
        )

    async def close(self) -> None:
        """停止所有 Run，并在关闭持久化和 AgentEngine 前完成清理。"""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            runs = tuple(self._runs.values())
        for run in runs:
            run.cancel_requested = True
            run.cancellation_token.cancel()
            if run.task is not None and not run.task.done():
                run.task.cancel()
        tasks = [run.task for run in runs if run.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run in runs:
            if run.completion is None:
                await self._force_cancel(run)

    async def is_active(self, thread_id: str) -> bool:
        """返回 Thread 是否正在受理或执行 Run。"""
        async with self._lock:
            return thread_id in self._starting_threads or self._is_active(
                self._runs.get(thread_id)
            )

    @asynccontextmanager
    async def idle_thread(self, thread_id: str) -> AsyncIterator[None]:
        """在 registry 锁内暂时保持 Thread 空闲，供 watch/compact 保证原子性。"""
        await self._lock.acquire()
        try:
            if self._closed:
                raise RunError("HOST_CLOSED", "Host is closed")
            if thread_id in self._starting_threads or self._is_active(self._runs.get(thread_id)):
                raise RunError("THREAD_BUSY", retryable=True)
            yield
        finally:
            self._lock.release()

    async def _lookup(self, ref: RunRef) -> RunState:
        async with self._lock:
            run = self._runs.get(ref.thread_id)
        if run is None or run.ref.run_id != ref.run_id or run.completion is not None:
            raise RunError("RUN_NOT_FOUND")
        return run

    @staticmethod
    def _is_active(run: RunState | None) -> bool:
        return run is not None and run.completion is None

    def _accepted_without_events(self, ref: RunRef, owner: ConnectionRef) -> RunExecution:
        return RunExecution(ref, owner, True, self._empty_events())

    async def _empty_events(self) -> AsyncIterator[AgentEvent]:
        if False:
            yield AgentEvent("", "", "", "", 0, 0, {})

    async def _read_events(self, run: RunState) -> AsyncIterator[AgentEvent]:
        while True:
            event = await run.events.get()
            if event is None:
                return
            yield event

    def _resolve_requested_skill(self, requested: RequestedSkill | None) -> Any | None:
        if requested is None:
            return None
        try:
            skill = self._skill_registry_provider().resolve(requested.skill_id)
        except SkillError:
            raise
        if not skill.user_invocable:
            raise SkillError(f'Skill "{skill.skill_id}" is not user-invocable')
        return skill

    async def _cancel_runs(self, predicate: Callable[[RunState], bool]) -> None:
        async with self._lock:
            runs = tuple(run for run in self._runs.values() if predicate(run))
        for run in runs:
            run.cancel_requested = True
            run.cancellation_token.cancel()
            if run.task is not None and not run.task.done():
                run.task.cancel()
        tasks = [run.task for run in runs if run.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run in runs:
            if run.completion is None:
                await self._force_cancel(run)

    async def _force_cancel(self, run: RunState) -> None:
        """补偿任务尚未取得首个时间片时的取消，避免 Run 永久悬挂。"""
        if run.completion is None:
            self._finish(run, "cancelled", {"reason": "Cancelled by client"})
        if run.runtime is not None:
            await self._release_runtime(run)
        async with self._lock:
            if self._runs.get(run.ref.thread_id) is run:
                self._runs.pop(run.ref.thread_id, None)
        run.events.put_nowait(None)

    async def _execute(self, run: RunState) -> None:
        try:
            if run.cancel_requested or run.cancellation_token.cancelled:
                self._finish(run, "cancelled", {"reason": "Cancelled by client"})
                return

            started_payload: dict[str, object] = {
                "resumed": False,
                "skills_snapshot_id": run.preparation.skill_snapshot_id,
            }
            binding = run.preparation.execution_binding
            if binding is not None:
                started_payload["primary_model"] = binding.protocol_primary_model()
                started_payload["runtime_profile_id"] = binding.runtime_profile_id
            self._emit(run, RUN_STARTED, started_payload)
            run.status = "running"

            if run.skill_record is not None:
                registry = self._skill_registry_provider()
                requested = run.start.requested_skill
                assert requested is not None
                loaded = registry.load(requested.skill_id, requested.args)
                self._emit(
                    run,
                    SKILL_LOADED,
                    {
                        "skill_id": loaded.record.skill_id,
                        "source": loaded.record.source,
                        "version": loaded.record.version,
                        "snapshot_id": registry.snapshot_id,
                    },
                )
                run.message = (
                    f"The user explicitly selected Skill `{loaded.record.skill_id}`. "
                    f"Read `/.harness/skills/{loaded.record.skill_id}/SKILL.md` with read_file before using it.\n\n"
                    f"User request:\n{run.message}"
                )

            run.runtime = await self._runtime_provider(run)
            if run.cancel_requested or run.cancellation_token.cancelled:
                raise asyncio.CancelledError
            if run.runtime.agent is None:
                self._emit(run, CONTENT_DELTA, {"text": run.message})
            else:
                resume: object | None = None
                while True:
                    resume = await self._stream_agent(run, resume)
                    if resume is None:
                        break

            if run.persistence is not None:
                await run.persistence.complete_run(run.ref.thread_id)
            self._drain_context_updates(run)
            self._finish(
                run,
                "completed",
                {
                    "usage": run.usage,
                    "duration_ms": round((time.monotonic() - run.started_at) * 1000),
                    "finish_reason": "completed",
                    "context": run.context_summary,
                },
            )
        except asyncio.CancelledError:
            self._finish(run, "cancelled", {"reason": "Cancelled by client"})
        except AgentEnginePoolCapacityError as exc:
            self._finish(
                run,
                "failed",
                {
                    "error": {
                        "code": "RUNTIME_POOL_CAPACITY_EXHAUSTED",
                        "message": str(exc),
                        "retryable": True,
                    }
                },
            )
        except Exception as exc:
            logger.exception("Agent run failed: %s", run.ref.run_id)
            self._finish(
                run,
                "failed",
                {
                    "error": {
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "retryable": False,
                    }
                },
            )
        finally:
            if run.persistence is not None and run.status != "completed":
                try:
                    await run.persistence.complete_run(run.ref.thread_id)
                except Exception:
                    logger.exception(
                        "Unable to refresh checkpoint index for thread %s",
                        run.ref.thread_id,
                    )
            await self._release_runtime(run)
            async with self._lock:
                if self._runs.get(run.ref.thread_id) is run:
                    self._runs.pop(run.ref.thread_id, None)
            run.events.put_nowait(None)

    async def _release_runtime(self, run: RunState) -> None:
        runtime, run.runtime = run.runtime, None
        if runtime is None:
            return
        try:
            await runtime.release()
        except Exception:
            logger.exception("Unable to release runtime for run %s", run.ref.run_id)

    def _finish(self, run: RunState, status: str, payload: dict[str, object]) -> None:
        if run.completion is not None:
            return
        run.status = status
        duration_ms = round((time.monotonic() - run.started_at) * 1000)
        if status == "completed":
            completion = RunCompletion(
                status=status,
                usage=dict(run.usage),
                duration_ms=duration_ms,
                finish_reason="completed",
                context=dict(run.context_summary),
            )
            event_type = RUN_COMPLETED
        elif status == "cancelled":
            completion = RunCompletion(
                status=status,
                usage=dict(run.usage),
                duration_ms=duration_ms,
                finish_reason="cancelled",
                context=dict(run.context_summary),
            )
            event_type = RUN_CANCELLED
        else:
            raw_error = payload.get("error")
            error = raw_error if isinstance(raw_error, Mapping) else None
            completion = RunCompletion(
                status=status,
                usage=dict(run.usage),
                duration_ms=duration_ms,
                finish_reason="failed",
                context=dict(run.context_summary),
                error=error,
            )
            event_type = RUN_FAILED
        run.terminal_event_emitted = True
        self._emit(run, event_type, payload, terminal=True)
        run.completion = completion

    def _emit(
        self,
        run: RunState,
        event_type: str,
        payload: Mapping[str, object],
        *,
        terminal: bool = False,
    ) -> None:
        if run.terminal_event_emitted and not terminal:
            return
        run.sequence += 1
        run.events.put_nowait(
            AgentEvent(
                event_id=str(uuid.uuid4()),
                type=event_type,
                thread_id=run.ref.thread_id,
                run_id=run.ref.run_id,
                sequence=run.sequence,
                timestamp_ms=int(time.time() * 1000),
                payload=dict(payload),
            )
        )

    async def _stream_agent(self, run: RunState, resume: object | None) -> object | None:
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        runtime = run.runtime
        if runtime is None or runtime.agent is None:
            return None
        stream_input: Any = (
            Command(resume=resume)
            if resume is not None
            else {"messages": [HumanMessage(content=run.message)]}
        )
        stream_kwargs: dict[str, Any] = {
            "config": runtime.graph_config(run.ref.thread_id),
            "stream_mode": ["messages", "updates"],
            "subgraphs": True,
        }
        if runtime.run_context is not None:
            stream_kwargs["context"] = runtime.run_context
        async for event in runtime.agent.astream(stream_input, **stream_kwargs):
            self._drain_context_updates(run)
            interaction = _extract_interaction(event)
            if interaction is not None:
                run.status = "interacting"
                result = await self._interaction_port.request(
                    run.owner,
                    run.ref,
                    interaction,
                )
                run.status = "running"
                self._emit(
                    run,
                    INTERACTION_RESOLVED,
                    {"request_id": interaction.request_id, "type": interaction.type},
                )
                return _resume_value(interaction, result.value)
            for event_type, payload in _translate_stream_event(event, run):
                self._emit(run, event_type, payload)
        return None

    def _drain_context_updates(self, run: RunState) -> None:
        updates = self._context_updates_provider(run.ref.thread_id)
        for update in updates:
            payload = update.payload() if hasattr(update, "payload") else dict(update)
            run.context_summary = payload
            self._emit(run, CONTEXT_UPDATED, payload)


def _extract_interaction(event: tuple[Any, ...]) -> InteractionRequest | None:
    """从 DeepAgents updates 流提取首个 AskUser 或 HITL interrupt。"""
    if len(event) == 3:
        _namespace, stream_mode, data = event
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return None
    if stream_mode != "updates" or not isinstance(data, Mapping):
        return None
    interrupts = data.get("__interrupt__")
    if not interrupts:
        return None
    interrupt = (interrupts if isinstance(interrupts, (list, tuple)) else [interrupts])[0]
    value = getattr(interrupt, "value", interrupt)
    interrupt_id = str(getattr(interrupt, "id", uuid.uuid4()))
    if isinstance(value, Mapping) and value.get("type") == "ask_user":
        raw_questions = value.get("questions")
        questions = tuple(q for q in raw_questions or [] if isinstance(q, Mapping))
        normalized = []
        for index, question in enumerate(questions):
            options = [
                {
                    "label": str(choice.get("value", "")),
                    "value": str(choice.get("value", "")),
                    "description": "",
                }
                for choice in question.get("choices", [])
                if isinstance(choice, Mapping) and choice.get("value")
            ]
            normalized.append(
                {
                    "id": f"question-{index + 1}",
                    "question": str(question.get("question", "Agent needs input")),
                    "header": "",
                    "body": "",
                    "options": options,
                    "multi_select": False,
                    "allow_other": True,
                }
            )
        return InteractionRequest(
            request_id=interrupt_id,
            type="question",
            payload={"interrupt_id": interrupt_id, "questions": normalized},
            interrupt_id=interrupt_id,
            questions=questions,
        )

    description = "A tool execution requires approval"
    action_count = 1
    if isinstance(value, Mapping):
        action_requests = value.get("action_requests", [])
        action_count = len(action_requests) if isinstance(action_requests, list) else 1
        descriptions = [
            str(request.get("description"))
            for request in action_requests
            if isinstance(request, Mapping) and request.get("description")
        ]
        if descriptions:
            description = "\n\n".join(descriptions)
    return InteractionRequest(
        request_id=interrupt_id,
        type="approval",
        payload={
            "interrupt_id": interrupt_id,
            "description": description,
            "requests": _bounded_json(value),
            "decisions": ["approve_once", "reject"],
        },
        interrupt_id=interrupt_id,
        action_count=action_count,
    )


def _resume_value(spec: InteractionRequest, response: object) -> dict[str, object]:
    """将语言无关交互结果映射回 LangGraph interrupt resume 契约。"""
    if not isinstance(response, dict):
        response = {}
    if spec.type == "approval":
        decision = response.get("decision")
        langgraph_decision = (
            "approve"
            if decision in {"approve_once", "approve_thread", "approve_always"}
            else "reject"
        )
        return {
            spec.interrupt_id: {
                "decisions": [{"type": langgraph_decision}] * spec.action_count
            }
        }
    answers_by_id = response.get("answers", {})
    answers: list[str] = []
    if isinstance(answers_by_id, Mapping):
        for index, _question in enumerate(spec.questions):
            values = answers_by_id.get(f"question-{index + 1}", [])
            answers.append(str(values[0]) if isinstance(values, list) and values else "")
    status = "answered" if any(answers) else "cancelled"
    return {spec.interrupt_id: {"status": status, "answers": answers}}


def _translate_stream_event(
    event: tuple[Any, ...], run: RunState
) -> Iterable[tuple[str, dict[str, object]]]:
    """把 LangChain message stream 转换为统一领域事件。"""
    if len(event) == 3:
        _namespace, stream_mode, data = event
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return []
    if stream_mode != "messages" or not isinstance(data, tuple) or not data:
        return []
    chunk = data[0]
    _update_usage(run, getattr(chunk, "usage_metadata", None))
    events: list[tuple[str, dict[str, object]]] = []
    content = _message_text(chunk)
    if content and type(chunk).__name__ != "ToolMessage":
        events.append((CONTENT_DELTA, {"text": content}))
    for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
        tool_id = _resolve_tool_stream_id(run, tool_chunk)
        if tool_chunk.get("name") and tool_id not in run.started_tool_ids:
            run.started_tool_ids.add(tool_id)
            events.append((TOOL_STARTED, {"tool_call_id": tool_id, "name": str(tool_chunk["name"])}))
        if tool_chunk.get("args"):
            arguments = _truncate_text(str(tool_chunk["args"]))
            events.append(
                (
                    TOOL_DELTA,
                    {
                        "tool_call_id": tool_id,
                        "arguments_delta": arguments[0],
                        "truncated": arguments[1],
                        "original_bytes": arguments[2],
                    },
                )
            )
    if type(chunk).__name__ == "ToolMessage":
        result = _truncate_text(_content_text(getattr(chunk, "content", None)))
        result_id = str(getattr(chunk, "tool_call_id", "") or "")
        tool_id = run.tool_result_ids.get(result_id, result_id) or run.last_tool_id or f"tool-{run.ref.run_id}"
        events.append(
            (
                TOOL_COMPLETED,
                {
                    "tool_call_id": tool_id,
                    "result": {
                        "content": result[0],
                        "is_error": getattr(chunk, "status", None) == "error",
                        "truncated": result[1],
                        "original_bytes": result[2],
                    },
                },
            )
        )
    return events


def _resolve_tool_stream_id(run: RunState, chunk: Mapping[str, Any]) -> str:
    """为缺失调用 ID 的续片建立稳定的工具事件 ID。"""
    index = chunk.get("index")
    raw_id = str(chunk.get("id") or "")
    if raw_id:
        tool_id = run.tool_result_ids.get(raw_id, raw_id)
        run.tool_result_ids[raw_id] = tool_id
        run.tool_stream_ids[f"id:{raw_id}"] = tool_id
        if index is not None:
            run.tool_stream_ids[f"index:{index}"] = tool_id
    else:
        key = f"index:{index}" if index is not None else "current"
        tool_id = run.tool_stream_ids.get(key)
        if tool_id is None:
            tool_id = f"tool-{run.ref.run_id}-{len(run.tool_stream_ids)}"
            run.tool_stream_ids[key] = tool_id
    run.last_tool_id = tool_id
    return tool_id


def _update_usage(run: RunState, usage: Any) -> None:
    """合并流式 usage，避免分片计数回退。"""
    if not isinstance(usage, Mapping):
        return
    run.usage["input_tokens"] = max(
        run.usage["input_tokens"], int(usage.get("input_tokens", 0) or 0)
    )
    run.usage["output_tokens"] = max(
        run.usage["output_tokens"], int(usage.get("output_tokens", 0) or 0)
    )


def _truncate_text(value: str) -> tuple[str, bool, int]:
    """按 UTF-8 字节安全截断工具输出，并保留原始大小。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return value, False, len(encoded)
    clipped = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return clipped, True, len(encoded)


def _content_text(content: object) -> str:
    """提取 LangChain 内容字段中的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        )
    return "" if content is None else str(content)


def _message_text(message: object) -> str:
    """优先从标准 content_blocks 提取正文，兼容旧式 content。"""
    blocks = getattr(message, "content_blocks", None)
    if isinstance(blocks, list):
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
        if text:
            return text
    return _content_text(getattr(message, "content", None))


def _json_safe(value: object) -> object:
    """确保中断详情可 JSON 编码，复杂对象降级为字符串。"""
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _bounded_json(value: object) -> object:
    """限制交互详情的 JSON 大小，避免工具参数撑爆 stdio。"""
    safe = _json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return safe
    preview = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return {"truncated": True, "original_bytes": len(encoded), "preview": preview}
