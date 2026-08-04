"""Run 生命周期 deep module：集中受理、执行、交互、终态和资源清理。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from harness_agent.policy.approval_mode import ApprovalMode
from harness_agent.policy.permission_rules import PermissionRule, save_rule
from harness_agent.runtime.agent_execution import AgentExecutionRegistry, ExecutionRegistryError
from harness_agent.runtime.agent_engine import AgentEnginePoolCapacityError
from harness_agent.runtime.agent_engine_profile import AgentEngineProfile
from harness_agent.threads.context_lifecycle import RunContextSnapshot
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
    ResolvedExecutionBinding,
    RunExecutionBinding,
)
from harness_agent.runtime.run_context import RunCancellationToken, RunContext
from harness_agent.extensions.skills import LoadedSkill
from harness_agent.threads.thread_persistence import (
    AcceptRun,
    ThreadPersistenceError,
    TranscriptAppend,
)

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
    requested_approval_mode: ApprovalMode | None = None

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
            self.requested_approval_mode,
        )


@dataclass(frozen=True, slots=True)
class RunPreparation:
    """模型、Profile、Skill 和 RunContextSnapshot 的一次解析结果。"""

    resolved_execution_binding: ResolvedExecutionBinding | None = None
    execution_binding: RunExecutionBinding | None = None
    agent_engine_profile: AgentEngineProfile | None = None
    skill_snapshot_id: str | None = None
    skill_registry: Any | None = None
    requested_skill: LoadedSkill | None = None
    context_snapshot: RunContextSnapshot | None = None
    idle_duration_ms: int | None = None
    # Default AgentHost may hold this reservation from spec resolution until the
    # corresponding AgentEngine lease is acquired.  It is intentionally opaque
    # here so the coordinator does not own the runtime snapshot protocol.
    snapshot_reservation: Any | None = None

    def __post_init__(self) -> None:
        """拒绝把 requested Skill、Context 和 Profile 拆成不同 snapshot。"""
        registry_id = (
            getattr(self.skill_registry, "snapshot_id", None)
            if self.skill_registry is not None
            else None
        )
        if self.skill_registry is not None and registry_id != self.skill_snapshot_id:
            raise ValueError("RUN_PREPARATION_SKILL_SNAPSHOT_MISMATCH")
        if self.requested_skill is not None and (
            self.skill_registry is None
            or self.requested_skill.snapshot_id != self.skill_snapshot_id
        ):
            raise ValueError("RUN_PREPARATION_REQUESTED_SKILL_SNAPSHOT_MISMATCH")
        if self.context_snapshot is not None and (
            self.context_snapshot.skill_snapshot_id != self.skill_snapshot_id
        ):
            raise ValueError("RUN_PREPARATION_CONTEXT_SKILL_SNAPSHOT_MISMATCH")


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
    execution_id: str
    agent_id: str
    parent_execution_id: str | None = None

    def record(self) -> dict[str, object]:
        """转换成现有 v3 event notification 使用的字段。"""
        record = {
            "event_id": self.event_id,
            "type": self.type,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp_ms": self.timestamp_ms,
            "execution_id": self.execution_id,
            "agent_id": self.agent_id,
            "payload": dict(self.payload),
        }
        if self.parent_execution_id is not None:
            record["parent_execution_id"] = self.parent_execution_id
        return record


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
    root_execution: AgentExecutionBinding | None = None
    message: str = ""
    status: str = "accepted"
    sequence: int = 0
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    tool_stream_ids: dict[str, str] = field(default_factory=dict)
    tool_result_ids: dict[str, str] = field(default_factory=dict)
    tool_names: dict[str, str] = field(default_factory=dict)
    started_tool_ids: set[str] = field(default_factory=set)
    completed_tool_ids: set[str] = field(default_factory=set)
    seen_tool_provider_ids: set[str] = field(default_factory=set)
    allocated_tool_ids: set[str] = field(default_factory=set)
    tool_call_ordinal: int = 0
    last_tool_id: str | None = None
    last_tool_result_id: str | None = None
    last_tool_result_chunk: object | None = None
    last_captured_message: object | None = None
    model_round_active: bool = False
    model_round_has_tool_results: bool = False
    assistant_tool_calls: dict[str, dict[str, object]] = field(default_factory=dict)
    assistant_buffer: list[str] = field(default_factory=list)
    assistant_turn_count: int = 0
    pending_transcript: list[TranscriptAppend] = field(default_factory=list)
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

    @property
    def root_execution_ref(self) -> ExecutionRef:
        """返回根 AgentExecution 的稳定身份。"""
        if self.root_execution is not None:
            return self.root_execution.ref
        return ExecutionRef.root(self.thread_id, self.run_id)


PersistenceProvider = Callable[[], Awaitable[Any | None]]
PreparationProvider = Callable[[StartRun, Any | None], Awaitable[RunPreparation]]
RuntimeProvider = Callable[[RunState], Awaitable[RunRuntime]]
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
        context_updates_provider: ContextUpdatesProvider | None = None,
        execution_registry: AgentExecutionRegistry | None = None,
        project_dir: Path | None = None,
    ) -> None:
        """注入 Project 资源 adapter，保持外部 Run interface 与 Protocol 解耦。"""
        self._persistence_provider = persistence_provider
        self._preparation_provider = preparation_provider
        self._runtime_provider = runtime_provider
        self._interaction_port = interaction_port
        self._context_updates_provider = context_updates_provider or (lambda _thread_id: [])
        self._execution_registry = execution_registry or AgentExecutionRegistry()
        # approve_always 的规则持久化到该目录的 project 层 settings.json
        self._project_dir = project_dir
        # approve_thread 的会话级规则只保存在内存，不落盘
        self._session_rules: list[PermissionRule] = []
        self._runs: dict[str, RunState] = {}
        self._starting_threads: set[str] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def execution_registry(self) -> AgentExecutionRegistry:
        """返回供未来 DelegationDispatcher 复用的执行树 seam。"""
        return self._execution_registry

    @property
    def session_rules(self) -> list[PermissionRule]:
        """返回本会话内审批产生的内存权限规则。"""
        return self._session_rules

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

        preparation: RunPreparation | None = None
        reservation_transferred = False
        try:
            persistence = await self._persistence_provider()
            preparation = await self._preparation_provider(command, persistence)

            if persistence is not None:
                binding = preparation.execution_binding
                if binding is None:
                    raise RunError("RUN_MODEL_BINDING_UNAVAILABLE")
                try:
                    acceptance = await persistence.accept_run(
                        AcceptRun(
                            message=command.message,
                            binding=binding,
                            context_snapshot=preparation.context_snapshot,
                        )
                    )
                except ThreadPersistenceError as exc:
                    if str(exc) == "RUN_EXECUTION_BINDING_CONFLICT":
                        raise RunError("RUN_ID_CONFLICT") from exc
                    raise
                if not acceptance.created:
                    await self._release_snapshot_reservation(preparation)
                    reservation_transferred = True
                    return self._accepted_without_events(command.ref, owner)

            root_execution = self._root_execution_binding(command, preparation)
            run = RunState(
                start=command,
                owner=owner,
                persistence=persistence,
                preparation=preparation,
                root_execution=root_execution,
            )
            await self._execution_registry.accept(root_execution)
            async with self._lock:
                self._runs[command.thread_id] = run
            run.task = asyncio.create_task(
                self._execute(run),
                name=f"harness-run-{command.run_id}",
            )
            reservation_transferred = True
            return RunExecution(command.ref, owner, True, self._read_events(run))
        finally:
            if preparation is not None and not reservation_transferred:
                await self._release_snapshot_reservation(preparation)
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
        await self._execution_registry.cancel_run(active.root_execution_ref)
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
            await self._execution_registry.cancel_run(run.root_execution_ref)
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
            yield AgentEvent("", "", "", "", 0, 0, {}, "root-empty", "main")

    async def _read_events(self, run: RunState) -> AsyncIterator[AgentEvent]:
        while True:
            event = await run.events.get()
            if event is None:
                return
            yield event

    async def _cancel_runs(self, predicate: Callable[[RunState], bool]) -> None:
        async with self._lock:
            runs = tuple(run for run in self._runs.values() if predicate(run))
        for run in runs:
            run.cancel_requested = True
            run.cancellation_token.cancel()
            await self._execution_registry.cancel_run(run.root_execution_ref)
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
        await self._settle_root_execution(run)
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

            try:
                await self._execution_registry.start(run.root_execution_ref)
            except ExecutionRegistryError:
                if run.cancel_requested or run.cancellation_token.cancelled:
                    self._finish(run, "cancelled", {"reason": "Cancelled by client"})
                    return
                raise

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

            loaded = run.preparation.requested_skill
            if loaded is not None:
                if loaded.snapshot_id != run.preparation.skill_snapshot_id:
                    raise RunError("RUN_PREPARATION_REQUESTED_SKILL_SNAPSHOT_MISMATCH")
                self._emit(
                    run,
                    SKILL_LOADED,
                    {
                        "skill_id": loaded.record.skill_id,
                        "source": loaded.record.source,
                        "version": loaded.record.version,
                        "snapshot_id": loaded.snapshot_id,
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
                _queue_assistant_transcript(run, run.message)
            else:
                resume: object | None = None
                while True:
                    resume = await self._stream_agent(run, resume)
                    if resume is None:
                        break

            if run.persistence is not None:
                _flush_assistant_transcript(run)
                await self._flush_transcript(run)
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
            # 已收到终态的 ToolMessage 或已经结束的助手消息属于规范事实；
            # 取消/失败只丢弃仍停留在 assistant_buffer 中的半条流。
            if run.persistence is not None and run.status != "completed":
                try:
                    await self._flush_transcript(run)
                except Exception:
                    logger.exception(
                        "Unable to persist completed transcript records for thread %s",
                        run.ref.thread_id,
                    )
            if run.persistence is not None and run.status != "completed":
                try:
                    await run.persistence.complete_run(run.ref.thread_id)
                except Exception:
                    logger.exception(
                        "Unable to refresh checkpoint index for thread %s",
                        run.ref.thread_id,
                    )
            await self._settle_root_execution(run)
            await self._release_runtime(run)
            async with self._lock:
                if self._runs.get(run.ref.thread_id) is run:
                    self._runs.pop(run.ref.thread_id, None)
            run.events.put_nowait(None)

    async def _release_runtime(self, run: RunState) -> None:
        runtime, run.runtime = run.runtime, None
        try:
            if runtime is not None:
                await runtime.release()
        except Exception:
            logger.exception("Unable to release runtime for run %s", run.ref.run_id)
        finally:
            await self._release_snapshot_reservation(run.preparation)

    async def _flush_transcript(self, run: RunState) -> None:
        """原子追加当前已完成的助手和工具语义边界。"""
        if run.persistence is None or not run.pending_transcript:
            return
        append = getattr(run.persistence, "append_transcript_batch", None)
        if not callable(append):
            raise RunError("TRANSCRIPT_LIFECYCLE_UNAVAILABLE")
        await append(tuple(run.pending_transcript))
        run.pending_transcript.clear()

    @staticmethod
    async def _release_snapshot_reservation(preparation: RunPreparation) -> None:
        """Release a Host-owned snapshot reservation on every startup path."""
        reservation = preparation.snapshot_reservation
        if reservation is None:
            return
        release = getattr(reservation, "release", None)
        if callable(release):
            await release()

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
                execution_id=run.root_execution_ref.execution_id,
                agent_id=(
                    run.root_execution.agent_id
                    if run.root_execution is not None
                    else "main"
                ),
                parent_execution_id=run.root_execution_ref.parent_execution_id,
            )
        )

    @staticmethod
    def _root_execution_binding(
        command: StartRun,
        preparation: RunPreparation,
    ) -> AgentExecutionBinding:
        """从同一 RunPreparation 创建根 execution 的轻量历史事实。"""
        profile = preparation.agent_engine_profile
        model_binding = preparation.execution_binding
        return AgentExecutionBinding(
            ref=ExecutionRef.root(command.thread_id, command.run_id),
            agent_id=profile.agent_id if profile is not None else "main",
            mode=ExecutionMode.MANAGED,
            depth=0,
            model=model_binding.actual_primary if model_binding is not None else None,
            policy_fingerprint=profile.policy_fingerprint if profile is not None else None,
            engine_profile_key=profile.profile_key if profile is not None else None,
            definition_fingerprint=(
                profile.definition_fingerprint if profile is not None else None
            ),
        )

    async def _settle_root_execution(self, run: RunState) -> None:
        """把 Run 终态映射到 root execution，并尝试封口当前执行树。"""
        if run.root_execution is None:
            return
        current = await self._execution_registry.get(run.root_execution.ref)
        try:
            if current is not None and not current.status.terminal:
                desired = {
                    "completed": ExecutionStatus.COMPLETED,
                    "failed": ExecutionStatus.FAILED,
                    "cancelled": ExecutionStatus.CANCELLED,
                }.get(
                    run.completion.status if run.completion is not None else "cancelled",
                    ExecutionStatus.CANCELLED,
                )
                if (
                    current.status is ExecutionStatus.PENDING
                    and desired is not ExecutionStatus.CANCELLED
                ):
                    await self._execution_registry.start(current.ref)
                await self._execution_registry.finalize(
                    current.ref,
                    status=desired,
                    usage=run.usage,
                )
            await self._execution_registry.seal_run(run.root_execution.ref)
            await self._execution_registry.discard_run(run.root_execution.ref)
        except ExecutionRegistryError:
            logger.exception("Unable to settle execution tree for run %s", run.run_id)

    async def _stream_agent(self, run: RunState, resume: object | None) -> object | None:
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        runtime = run.runtime
        if runtime is None or runtime.agent is None:
            return None
        if resume is not None and run.run_context is not None:
            # Interaction resume is an explicit non-initial model phase.  The
            # pressure middleware must not reinterpret the resume as a new
            # idle top-level Run merely because the user took time to answer.
            run.run_context.model_call_lifecycle.schedule("interaction_resume")
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
                self._record_approval_rules(interaction, result.value)
                return _resume_value(interaction, result.value)
            chunk = _message_stream_chunk(event)
            if chunk is not None:
                complete_tool = _capture_transcript_message(run, chunk)
                if complete_tool:
                    # ToolMessage 到达时，前置 assistant 与完整 tool 结果已经
                    # 越过内存 pending 边界；后续模型阶段即使阻塞也不应延迟
                    # 这一个幂等 typed lifecycle 批次的提交。
                    await self._flush_transcript(run)
            for event_type, payload in _translate_stream_event(event, run):
                self._emit(run, event_type, payload)
        _flush_assistant_transcript(run)
        await self._flush_transcript(run)
        _finish_model_round(run)
        return None

    def _drain_context_updates(self, run: RunState) -> None:
        updates = self._context_updates_provider(run.ref.thread_id)
        for update in updates:
            payload = update.payload() if hasattr(update, "payload") else dict(update)
            run.context_summary = payload
            self._emit(run, CONTEXT_UPDATED, payload)

    def _record_approval_rules(self, spec: InteractionRequest, response: object) -> None:
        """审批通过后按决策范围记录或持久化 allow 权限规则。

        - approve_thread：规则只保存在会话内存列表，进程结束即失效；
        - approve_always：规则持久化到 project 层 settings.json。
        工具上下文来自 interrupt payload 中保存的 action_requests。
        """
        if spec.type != "approval" or not isinstance(response, Mapping):
            return
        decision = response.get("decision")
        if decision not in {"approve_thread", "approve_always"}:
            return
        requests = spec.payload.get("requests")
        action_requests = (
            requests.get("action_requests") if isinstance(requests, Mapping) else None
        )
        if not isinstance(action_requests, list):
            return
        for action in action_requests:
            if not isinstance(action, Mapping):
                continue
            tool_name = str(action.get("name") or "")
            tool_args = action.get("args")
            if not tool_name or not isinstance(tool_args, Mapping):
                continue
            rule = _generate_permission_rule(tool_name, tool_args)
            if decision == "approve_thread":
                if rule not in self._session_rules:
                    self._session_rules.append(rule)
            else:
                save_rule(
                    replace(rule, scope="project"),
                    scope="project",
                    project_dir=self._project_dir,
                )


def _extract_interaction(event: tuple[Any, ...]) -> InteractionRequest | None:
    """从 DeepAgents updates 流提取首个 AskUser 或 HITL interrupt。"""
    if len(event) == 3:
        namespace, stream_mode, data = event
        # Protocol v3 has no execution/provenance field for child graph
        # updates.  A non-empty namespace is therefore never a root
        # interaction request; treating it as one would surface a child
        # interrupt as a root approval/question.
        if namespace:
            return None
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
            "decisions": [
                "approve_once",
                "approve_thread",
                "approve_always",
                "reject",
                "reject_with_feedback",
            ],
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
        feedback = response.get("feedback", "")
        if decision in {"approve_once", "approve_thread", "approve_always"}:
            langgraph_decision: dict[str, object] = {"type": "approve"}
        elif decision == "reject_with_feedback" and feedback:
            # 反馈必须落在每个 decision 的 args 中，才符合
            # HumanInTheLoopMiddleware 的 resume 契约；顶层 feedback 会被忽略。
            langgraph_decision = {"type": "reject", "args": {"message": feedback}}
        else:
            langgraph_decision = {"type": "reject"}
        return {
            spec.interrupt_id: {
                "decisions": [langgraph_decision] * spec.action_count
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


def _generate_permission_rule(
    tool_name: str, tool_args: Mapping[str, object]
) -> PermissionRule:
    """从被批准的工具调用上下文生成 allow 权限规则。

    Shell 调用按命令首词收敛；文件及其他工具使用通配资源。工作区边界和
    敏感路径检查仍在实际执行前强制生效，因此规则不会放宽硬性保护。
    """
    if tool_name in {"execute", "monitor"}:
        command = str(tool_args.get("command") or "").strip()
        prefix = command.split()[0] if command else ""
        resource = f"{prefix} *" if prefix else "*"
    else:
        resource = "*"
    return PermissionRule(tool=tool_name, resource=resource, effect="allow")


def _message_stream_chunk(event: tuple[Any, ...]) -> object | None:
    """从 messages stream 取出原始消息块，供 wire 截断前的语义捕获使用。"""
    if len(event) == 3:
        namespace, stream_mode, data = event
        # ``subgraphs=True`` uses a non-empty namespace for child graph
        # messages.  ZC-101 only owns the linear root Transcript; those
        # messages are explicitly suppressed at the v3 adapter boundary until
        # a later execution/provenance projection can represent them safely.
        if namespace:
            return None
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return None
    if stream_mode != "messages" or not isinstance(data, tuple) or not data:
        return None
    return data[0]


def _capture_transcript_message(run: RunState, chunk: object) -> bool:
    """在 1 MiB wire 截断前收集完整助手/工具语义，不收集 delta 事件。"""
    chunk_type = type(chunk).__name__
    if chunk_type in {"AIMessage", "AIMessageChunk"}:
        _ensure_model_round_for_assistant(run)
        full_tool_calls = getattr(chunk, "tool_calls", None)
        if chunk_type == "AIMessage" and isinstance(full_tool_calls, list) and full_tool_calls:
            for index, tool_call in enumerate(full_tool_calls):
                if not isinstance(tool_call, Mapping):
                    continue
                _capture_full_tool_call(run, tool_call, index=index, source_message=chunk)
        else:
            for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
                if not isinstance(tool_chunk, Mapping):
                    continue
                tool_id = _resolve_tool_stream_id(
                    run, tool_chunk, source_message=chunk
                )
                name = tool_chunk.get("name")
                if name:
                    run.tool_names[tool_id] = str(name)
                _merge_assistant_tool_call(
                    run,
                    tool_id,
                    name=name,
                    arguments=tool_chunk.get("args"),
                    is_full=False,
                    call_type=tool_chunk.get("type"),
                )
        text = _message_text(chunk)
        if not text and not run.assistant_tool_calls:
            return False
        # Provider chunk IDs are optional and may change between deltas.  The
        # stream/tool boundary, not an ID, defines one complete assistant turn.
        if text:
            run.assistant_buffer.append(text)
        run.last_captured_message = chunk
        return False
    if chunk_type != "ToolMessage":
        return False
    _flush_assistant_transcript(run)
    tool_id = _resolve_tool_result_id(run, chunk)
    run.pending_transcript.append(
        TranscriptAppend(
            thread_id=run.ref.thread_id,
            record_id=f"run:{run.ref.run_id}:tool:{tool_id}",
            kind="tool",
            content=_content_text(getattr(chunk, "content", None)),
            run_id=run.ref.run_id,
            execution_id=run.root_execution_ref.execution_id,
            tool_call_id=tool_id,
            tool_name=run.tool_names.get(
                tool_id, str(getattr(chunk, "name", None) or "tool")
            ),
            tool_status=(
                "error" if getattr(chunk, "status", None) == "error" else "success"
            ),
        )
    )
    return True


def _queue_assistant_transcript(
    run: RunState,
    content: str,
    tool_calls: tuple[Mapping[str, object], ...] = (),
) -> None:
    """将一个完整助手回答排入当前 Run 的原子提交批次。"""
    if not content and not tool_calls:
        return
    run.assistant_turn_count += 1
    run.pending_transcript.append(
        TranscriptAppend(
            thread_id=run.ref.thread_id,
            record_id=f"run:{run.ref.run_id}:assistant:{run.assistant_turn_count}",
            kind="assistant",
            content=content,
            run_id=run.ref.run_id,
            execution_id=run.root_execution_ref.execution_id,
            tool_calls=tool_calls,
        )
    )


def _flush_assistant_transcript(run: RunState) -> None:
    """结束一个模型消息边界；只把非空完整文本转成助手记录。"""
    content = "".join(run.assistant_buffer)
    tool_calls = _finalize_assistant_tool_calls(run)
    if content or tool_calls:
        _queue_assistant_transcript(run, content, tool_calls)
    run.assistant_buffer.clear()


def _capture_full_tool_call(
    run: RunState,
    tool_call: Mapping[str, object],
    *,
    index: int,
    source_message: object | None = None,
) -> None:
    """捕获完整 AIMessage.tool_calls，不从 ToolMessage 反推参数。"""
    raw_id = tool_call.get("id")
    tool_id = _resolve_tool_stream_id(
        run,
        {
            "index": tool_call.get("index", index),
            "id": raw_id,
            "name": tool_call.get("name"),
            "args": tool_call.get("args", tool_call.get("arguments")),
        },
        source_message=source_message,
    )
    name = tool_call.get("name")
    if name:
        run.tool_names[tool_id] = str(name)
    _merge_assistant_tool_call(
        run,
        tool_id,
        name=name,
        arguments=tool_call.get("args", tool_call.get("arguments")),
        is_full=True,
        call_type=tool_call.get("type"),
    )


def _merge_assistant_tool_call(
    run: RunState,
    tool_id: str,
    *,
    name: object,
    arguments: object,
    is_full: bool,
    call_type: object,
) -> None:
    """在当前模型回合内按稳定调用 ID 合并参数分片。"""
    entry = run.assistant_tool_calls.setdefault(
        tool_id,
        {
            "id": tool_id,
            "name": str(name or run.tool_names.get(tool_id) or "tool"),
            "_argument_fragments": [],
            "_argument_invalid": False,
            "_full_arguments_present": False,
        },
    )
    if name:
        entry["name"] = str(name)
    if call_type:
        entry["type"] = str(call_type)
    if is_full:
        entry["_full_arguments_present"] = True
        entry["_full_arguments"] = arguments
        entry["_argument_fragments"] = []
        return
    if entry.get("_full_arguments_present") or arguments in (None, ""):
        return
    fragments = entry.setdefault("_argument_fragments", [])
    if isinstance(arguments, str):
        fragments.append(arguments)
    else:
        try:
            fragments.append(_canonical_tool_argument(arguments))
        except (TypeError, ValueError):
            # Preserve an explicit invalid marker instead of coercing a
            # provider object to a string that could be mistaken for JSON.
            fragments.append(repr(arguments))
            entry["_argument_invalid"] = True


def _finalize_assistant_tool_calls(
    run: RunState,
) -> tuple[Mapping[str, object], ...]:
    """将当前 assistant 的完整/分片参数定型为可恢复的 typed payload。"""
    calls: list[Mapping[str, object]] = []
    for entry in run.assistant_tool_calls.values():
        call: dict[str, object] = {
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or "tool"),
        }
        if entry.get("type") is not None:
            call["type"] = str(entry["type"])
        if entry.get("_full_arguments_present"):
            _set_tool_call_arguments(call, entry.get("_full_arguments"), partial=False)
        else:
            fragments = entry.get("_argument_fragments")
            raw = "".join(str(fragment) for fragment in fragments or ())
            _set_tool_call_arguments(
                call,
                raw if raw else None,
                partial=not bool(entry.get("_argument_invalid")),
            )
        calls.append(call)
    run.assistant_tool_calls.clear()
    return tuple(calls)


def _set_tool_call_arguments(
    call: dict[str, object],
    value: object,
    *,
    partial: bool,
) -> None:
    """保留参数对象、原文和显式校验状态，供后续恢复层 fail closed。"""
    if value is None:
        call["arguments_status"] = "unavailable"
        return
    if isinstance(value, str):
        call["arguments_raw"] = value
        if not value:
            call["arguments_status"] = "unavailable"
            return
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            call["arguments_status"] = "partial" if partial else "invalid"
            call["arguments_error"] = type(exc).__name__
            return
    else:
        parsed = value
    try:
        encoded = _canonical_tool_argument(parsed)
        normalized = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if not isinstance(value, str):
            call["arguments_raw"] = repr(value)
        call["arguments_status"] = "invalid"
        call["arguments_error"] = type(exc).__name__
        return
    call["arguments"] = normalized
    call["arguments_json"] = encoded
    call["arguments_status"] = "valid" if isinstance(normalized, Mapping) else "invalid"


def _canonical_tool_argument(value: object) -> str:
    """以稳定 JSON 编码工具参数，避免把 provider 对象直接写入 Transcript。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _translate_stream_event(
    event: tuple[Any, ...], run: RunState
) -> Iterable[tuple[str, dict[str, object]]]:
    """把 LangChain message stream 转换为统一领域事件。"""
    if len(event) == 3:
        namespace, stream_mode, data = event
        if namespace:
            # ZC-101 keeps root canonical/tool-correlation state separate from
            # child graph state.  Child messages may still be observed by a
            # future execution/provenance projection, but dropping them here
            # is safer than mutating root IDs while preserving a v3 wire shape.
            return []
    elif len(event) == 2:
        stream_mode, data = event
    else:
        return []
    if stream_mode != "messages" or not isinstance(data, tuple) or not data:
        return []
    chunk = data[0]
    if type(chunk).__name__ in {"AIMessage", "AIMessageChunk"}:
        _ensure_model_round_for_assistant(run)
    _update_usage(run, getattr(chunk, "usage_metadata", None))
    events: list[tuple[str, dict[str, object]]] = []
    content = _message_text(chunk)
    if content and type(chunk).__name__ != "ToolMessage":
        events.append((CONTENT_DELTA, {"text": content}))
    for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
        tool_id = _resolve_tool_stream_id(run, tool_chunk, source_message=chunk)
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
        if run.last_tool_result_chunk is chunk and run.last_tool_result_id:
            tool_id = run.last_tool_result_id
        else:
            tool_id = _resolve_tool_result_id(run, chunk)
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


def _resolve_tool_stream_id(
    run: RunState,
    chunk: Mapping[str, Any],
    *,
    source_message: object | None = None,
) -> str:
    """为当前模型回合的工具续片建立稳定且不会跨回合复用的 ID。"""
    _ensure_model_round_for_assistant(run)
    index = chunk.get("index")
    raw_id = str(chunk.get("id") or "")
    if raw_id:
        tool_id = run.tool_stream_ids.get(f"id:{raw_id}")
        if tool_id is None and index is not None:
            # 某些 provider 先发无 ID 分片、后发带 ID 分片；同一 index
            # 仍属于同一调用，不能因 ID 后出现而拆成两条记录。
            tool_id = run.tool_stream_ids.get(f"index:{index}")
        if tool_id is None:
            tool_id = _allocate_tool_id(run, raw_id)
        run.tool_result_ids[raw_id] = tool_id
        run.tool_stream_ids[f"id:{raw_id}"] = tool_id
        if index is not None:
            run.tool_stream_ids[f"index:{index}"] = tool_id
    else:
        key = f"index:{index}" if index is not None else "current"
        tool_id = run.tool_stream_ids.get(key)
        if (
            tool_id is not None
            and index is None
            and not raw_id
            and chunk.get("name")
            and run.tool_names.get(tool_id)
            and source_message is not run.last_captured_message
        ):
            raise RunError(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Multiple ID-less tool calls without index cannot be associated safely",
            )
        if tool_id is None:
            tool_id = _allocate_tool_id(run)
            run.tool_stream_ids[key] = tool_id
    run.last_tool_id = tool_id
    return tool_id


def _resolve_tool_result_id(run: RunState, chunk: object) -> str:
    """把 ToolMessage 归属到当前回合，无法可靠关联时明确失败。"""
    if not run.model_round_active:
        _start_model_round(run)
    result_id = str(getattr(chunk, "tool_call_id", "") or "")
    if result_id:
        tool_id = run.tool_result_ids.get(result_id)
        if tool_id is None:
            tool_id = run.tool_stream_ids.get(f"id:{result_id}")
        if tool_id is None:
            candidates = _current_tool_candidates(run)
            if len(candidates) == 1:
                candidate = next(iter(candidates))
                if _candidate_has_provider_id(run, candidate):
                    raise RunError(
                        "TOOL_CALL_ID_UNAVAILABLE",
                        "Tool result ID does not match the known provider call ID",
                    )
                # The only safe late-binding case is an assistant chunk whose
                # call had no provider ID at all. Bind the result ID to that
                # existing internal call, without inventing a call when no
                # assistant declaration was observed.
                tool_id = candidate
            elif len(candidates) > 1:
                raise RunError(
                    "TOOL_CALL_ID_UNAVAILABLE",
                    "Tool result cannot be associated with parallel calls without stable IDs",
                )
            else:
                raise RunError(
                    "TOOL_CALL_ID_UNAVAILABLE",
                    "Tool result has no preceding assistant tool call",
                )
            run.tool_result_ids[result_id] = tool_id
            run.tool_stream_ids[f"id:{result_id}"] = tool_id
        run.seen_tool_provider_ids.add(result_id)
    else:
        candidates = _current_tool_candidates(run)
        if len(candidates) != 1:
            raise RunError(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Tool result has no stable ID and cannot be associated safely",
            )
        tool_id = next(iter(candidates))
        if tool_id in run.completed_tool_ids:
            raise RunError(
                "TOOL_CALL_ID_UNAVAILABLE",
                "Multiple ID-less tool results cannot be associated safely",
            )
    run.completed_tool_ids.add(tool_id)
    run.model_round_has_tool_results = True
    run.last_tool_id = tool_id
    run.last_tool_result_id = tool_id
    run.last_tool_result_chunk = chunk
    return tool_id


def _current_tool_candidates(run: RunState) -> set[str]:
    """返回当前模型回合去重后的工具调用候选。"""
    return set(run.tool_stream_ids.values())


def _candidate_has_provider_id(run: RunState, tool_id: str) -> bool:
    """判断候选是否已经由 assistant 明确声明过 provider tool-call ID。"""
    return any(
        key.startswith("id:") and candidate == tool_id
        for key, candidate in run.tool_stream_ids.items()
    )


def _allocate_tool_id(run: RunState, preferred: str | None = None) -> str:
    """分配 Run 内单调唯一 ID，稳定 provider ID 只在首次出现时直接复用。"""
    run.tool_call_ordinal += 1
    if preferred and preferred not in run.seen_tool_provider_ids:
        candidate = preferred
    else:
        candidate = f"tool-{run.ref.run_id}-{run.tool_call_ordinal}"
    while candidate in run.allocated_tool_ids:
        run.tool_call_ordinal += 1
        candidate = f"tool-{run.ref.run_id}-{run.tool_call_ordinal}"
    run.allocated_tool_ids.add(candidate)
    if preferred:
        run.seen_tool_provider_ids.add(preferred)
    return candidate


def _start_model_round(run: RunState) -> None:
    """清理只属于当前模型/工具回合的临时映射。"""
    run.model_round_active = True
    run.model_round_has_tool_results = False
    run.tool_stream_ids.clear()
    run.tool_result_ids.clear()
    run.tool_names.clear()
    run.started_tool_ids.clear()
    run.completed_tool_ids.clear()
    run.assistant_tool_calls.clear()
    run.last_tool_id = None
    run.last_tool_result_id = None
    run.last_tool_result_chunk = None
    run.last_captured_message = None


def _ensure_model_round_for_assistant(run: RunState) -> None:
    """模型在收到上一回合工具结果后开始新回合并重置临时索引。"""
    if not run.model_round_active or run.model_round_has_tool_results:
        _start_model_round(run)


def _finish_model_round(run: RunState) -> None:
    """流正常结束后丢弃回合映射，但保留 Run 级 ordinal 和去重事实。"""
    run.model_round_active = False
    run.model_round_has_tool_results = False
    run.tool_stream_ids.clear()
    run.tool_result_ids.clear()
    run.tool_names.clear()
    run.started_tool_ids.clear()
    run.completed_tool_ids.clear()
    run.assistant_tool_calls.clear()
    run.last_tool_id = None
    run.last_tool_result_id = None
    run.last_tool_result_chunk = None
    run.last_captured_message = None


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
