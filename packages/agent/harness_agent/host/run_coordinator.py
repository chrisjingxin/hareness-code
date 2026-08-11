"""Run 生命周期 deep module：集中受理、执行、交互、终态和资源清理。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from harness_agent.host.run_execution import (
    BuildRunAdapter,
    ComposeRunAdapter,
    CONTEXT_UPDATED,
    INTERACTION_RESOLVED,
    MAX_TOOL_PAYLOAD_BYTES,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RunExecutionAdapter,
    RunLifecyclePort,
    _bounded_json,
)
from harness_agent.policy.approval_mode import ApprovalMode
from harness_agent.policy.bash_parser import extract_command_rule as _extract_command_rule

# 工作模式在 Run 受理时冻结；Compose 是代码状态机驱动的研发流程，Build 保持现有直接协作。
InteractionMode = Literal["build", "compose"]
from harness_agent.policy.permission_rules import (
    PermissionRule,
    evaluate_tool_rules,
    load_rules,
    merge_rules,
    save_rule,
)
from harness_agent.policy.sensitive_paths import requires_safety_check
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

INTERACTION_TIMEOUT_MS = 300_000


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
    mode: InteractionMode
    requested_skill: RequestedSkill | None = None
    requested_primary_profile: str | None = None
    requested_approval_mode: ApprovalMode | None = None

    @property
    def ref(self) -> RunRef:
        """返回本次 Run 的稳定身份。"""
        return RunRef(self.thread_id, self.run_id)

    def fingerprint(self) -> tuple[object, ...]:
        """返回幂等判断所需的请求指纹；工作模式冻结在 Run 身份内。"""
        skill = self.requested_skill
        return (
            self.thread_id,
            self.run_id,
            self.message,
            self.mode,
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
    # 服务端串行审批元数据（完整动作列表与安全/危险索引），仅存内存、
    # 不进入 wire payload：协议 schema 对 payload 附加字段零容忍。
    serial_context: Mapping[str, object] | None = None


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
    # 多工具逐个串行审批队列：存储待审批的工具调用
    pending_approvals: list[dict[str, object]] = field(default_factory=list)
    # 标记是否因用户拒绝而终止同批后续工具
    batch_rejected: bool = False

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


class _CoordinatorLifecyclePort:
    """把 RunCoordinator 的受控能力暴露给 execution adapter 的最小 port。

    adapter 只能发非终态事件、请求 Interaction、刷新 Transcript、读取取消
    状态与解析 Runtime；sequence 分配、终态和资源释放仍只属于 coordinator。
    """

    _TERMINAL_EVENTS = frozenset({RUN_COMPLETED, RUN_CANCELLED, RUN_FAILED})

    def __init__(self, coordinator: RunCoordinator) -> None:
        self._coordinator = coordinator

    def emit(self, run: RunState, event_type: str, payload: Mapping[str, object]) -> None:
        """发非终态事件；adapter 试图自己发终态会被拒绝。"""
        if event_type in self._TERMINAL_EVENTS:
            raise RunError(
                "ADAPTER_TERMINAL_VIOLATION",
                "Terminal events belong to the RunCoordinator",
            )
        self._coordinator._emit(run, event_type, payload)

    def is_cancelled(self, run: RunState) -> bool:
        """返回共享取消 token 与显式取消标记的并集。"""
        return run.cancel_requested or run.cancellation_token.cancelled

    async def resolve_runtime(self, run: RunState) -> RunRuntime:
        """通过 coordinator 注入的 RuntimeProvider 解析本次执行资源。"""
        return await self._coordinator._runtime_provider(run)

    async def request_interaction(
        self, run: RunState, spec: InteractionRequest
    ) -> InteractionResult:
        """请求 owner 回答问题；状态迁移与 resolved 事件由 coordinator 拥有。"""
        run.status = "interacting"
        result = await self._coordinator._interaction_port.request(
            run.owner, run.ref, spec
        )
        run.status = "running"
        self._coordinator._emit(
            run,
            INTERACTION_RESOLVED,
            {"request_id": spec.request_id, "type": spec.type},
        )
        return result

    async def collect_serial_approvals(
        self, run: RunState, spec: InteractionRequest
    ) -> dict[str, object]:
        """把串行工具审批收集委托回 coordinator（规则状态属于 coordinator）。"""
        return await self._coordinator._collect_serial_approvals(run, spec)

    def drain_context_updates(self, run: RunState) -> None:
        """把当前已到达的上下文压缩事实发布为 context.updated。"""
        self._coordinator._drain_context_updates(run)

    async def flush_transcript(self, run: RunState) -> None:
        """原子追加当前已完成语义边界的 Transcript 批次。"""
        await self._coordinator._flush_transcript(run)


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
        # approve_project 的规则持久化到该目录的 project 层 settings.json
        self._project_dir = project_dir
        # approve_thread 的会话级规则只保存在内存，不落盘
        self._session_rules: list[PermissionRule] = []
        # 每个工作模式一个执行 adapter；Compose 在完整实现前是稳定失败空壳。
        self._execution_adapters: dict[InteractionMode, RunExecutionAdapter] = {
            "build": BuildRunAdapter(),
            "compose": ComposeRunAdapter(),
        }
        self._lifecycle_port = _CoordinatorLifecyclePort(self)
        self._runs: dict[str, RunState] = {}
        self._starting_runs: dict[str, ConnectionRef] = {}
        # maintenance 中的 Thread 拒绝受理新 Run，避免 watch/compact 与执行互相踩踏
        self._maintenance_threads: set[str] = set()
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

    async def start(
        self,
        command: StartRun,
        owner: ConnectionRef,
        *,
        allow_multithread: bool = False,
    ) -> RunExecution:
        """受理一次 Run，并在同一锁内判定 Thread 与 Connection 并发限制。"""
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
            if command.thread_id in self._starting_runs:
                raise RunError("THREAD_BUSY", retryable=True)
            if not allow_multithread and self._connection_has_active_run(
                owner.connection_id,
                self._runs,
                self._starting_runs,
            ):
                raise RunError("CONNECTION_RUN_BUSY", retryable=True)
            if command.thread_id in self._maintenance_threads:
                raise RunError("THREAD_BUSY", retryable=True)
            self._starting_runs[command.thread_id] = owner

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
                if self._starting_runs.get(command.thread_id) == owner:
                    self._starting_runs.pop(command.thread_id, None)

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
        """返回 Thread 是否正在受理、执行或被维护操作占用。"""
        async with self._lock:
            return (
                thread_id in self._starting_runs
                or thread_id in self._maintenance_threads
                or self._is_active(self._runs.get(thread_id))
            )

    async def connection_active(self, connection_id: str) -> bool:
        """返回 Connection 是否持有 starting/active Run，供控制租约读取。"""
        async with self._lock:
            return self._connection_has_active_run(
                connection_id,
                self._runs,
                self._starting_runs,
            )

    @asynccontextmanager
    async def idle_thread(self, thread_id: str) -> AsyncIterator[None]:
        """为 watch/compact 保留目标 Thread，不跨耗时 I/O 持有全局锁。"""
        async with self._lock:
            if self._closed:
                raise RunError("HOST_CLOSED", "Host is closed")
            if (
                thread_id in self._starting_runs
                or thread_id in self._maintenance_threads
                or self._is_active(self._runs.get(thread_id))
            ):
                raise RunError("THREAD_BUSY", retryable=True)
                raise RunError("THREAD_BUSY", retryable=True)
            self._maintenance_threads.add(thread_id)
        try:
            yield
        finally:
            async with self._lock:
                self._maintenance_threads.discard(thread_id)

    @staticmethod
    def _connection_has_active_run(connection_id: str, runs: Mapping[str, RunState], starting: Mapping[str, ConnectionRef]) -> bool:
        """判断同一 Connection 是否已有 starting/active Run。"""
        if any(ref.connection_id == connection_id for ref in starting.values()):
            return True
        return any(
            run.owner.connection_id == connection_id and run.completion is None
            for run in runs.values()
        )

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

            adapter = self._execution_adapters[run.start.mode]
            await adapter.execute(run, self._lifecycle_port)
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
        except RunError as exc:
            # 执行路径的领域错误使用稳定错误码收敛，不让模型文本充当终态码。
            self._finish(
                run,
                "failed",
                {
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "retryable": exc.retryable,
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


    def _drain_context_updates(self, run: RunState) -> None:
        updates = self._context_updates_provider(run.ref.thread_id)
        for update in updates:
            payload = update.payload() if hasattr(update, "payload") else dict(update)
            run.context_summary = payload
            self._emit(run, CONTEXT_UPDATED, payload)

    def _record_approval_rule(
        self, tool_name: str, tool_args: Mapping[str, object], decision: str
    ) -> None:
        """单个工具调用审批通过后按决策范围记录或持久化 allow 权限规则。

        - approve_thread：规则只保存在会话内存列表，进程结束即失效；
        - approve_project：规则持久化到 project 层 settings.json。
        """
        if decision not in {"approve_thread", "approve_project"} or not tool_name:
            return
        rules = _generate_permission_rule(tool_name, tool_args)
        for rule in rules:
            if decision == "approve_thread":
                if rule not in self._session_rules:
                    self._session_rules.append(rule)
            else:
                save_rule(
                    replace(rule, scope="project"),
                    scope="project",
                    project_dir=self._project_dir,
                )

    def _evaluate_queued_rule(self, tool_name: str, tool_args: dict[str, object]) -> str | None:
        """合并会话与持久化规则评估排队中的工具调用。

        approve_thread/approve_project 产生的新规则应立即作用于同批后续请求；
        但敏感路径即使命中 allow 规则也不自动放行（保持弹窗）。
        """
        if not tool_name:
            return None
        scoped = load_rules(project_dir=self._project_dir)
        scoped["session"] = list(self._session_rules)
        rules = merge_rules(scoped)
        if not rules:
            return None
        effect = evaluate_tool_rules(tool_name, tool_args, rules)
        if effect == "allow":
            if requires_safety_check(tool_name, tool_args):
                return None
        return effect

    async def _collect_serial_approvals(
        self, run: RunState, first_spec: InteractionRequest
    ) -> dict[str, object]:
        """逐个串行收集一批工具调用的审批决策，最后按原始顺序一次性 resume。

        LangGraph 的 interrupt 恢复时节点会从头重放，因此不能 per-tool
        interrupt；本方法在本地串行循环中逐个弹窗收集决策：

        - 并发安全工具直接 approve；
        - 排队工具先查合并规则：deny（PolicyDeny）按拒绝处理但继续后续工具，
          allow 自动批准（敏感路径除外）；
        - 用户拒绝（UserReject）终止同批后续工具，剩余工具收到带取消原因
          的 reject；已批准/已执行的调用不回滚。
        """
        payload = first_spec.payload
        interrupt_id = str(first_spec.interrupt_id or payload.get("interrupt_id") or "")
        # 串行元数据走服务端 serial_context，wire payload 只保留 schema 字段
        context = first_spec.serial_context or {}
        all_requests = context.get("all_action_requests")
        if not isinstance(all_requests, list):
            all_requests = []
        safe_indices = [
            i for i in context.get("safe_indices", []) if isinstance(i, int)
        ]
        unsafe_indices = [
            i for i in context.get("unsafe_indices", []) if isinstance(i, int)
        ]
        total = len(all_requests)

        decisions: list[dict[str, object]] = [{"type": "reject"} for _ in range(total)]
        for i in safe_indices:
            if 0 <= i < total:
                decisions[i] = {"type": "approve"}

        run.batch_rejected = False
        run.pending_approvals = [
            all_requests[i] for i in unsafe_indices if 0 <= i < total
        ]
        total_unsafe = len(unsafe_indices)
        cancel_message = "cancelled due to earlier permission rejection"

        for position, index in enumerate(unsafe_indices):
            if not 0 <= index < total:
                continue
            action = all_requests[index]
            action_map = action if isinstance(action, Mapping) else {}
            tool_name = str(action_map.get("name") or "")
            raw_args = action_map.get("args")
            tool_args: dict[str, object] = dict(raw_args) if isinstance(raw_args, Mapping) else {}

            # UserReject 已终止同批：剩余工具直接收到取消 reject，不再弹窗
            if run.batch_rejected:
                decisions[index] = {"type": "reject", "args": {"message": cancel_message}}
                continue

            # 规则已明确裁决的排队工具不弹窗：
            # deny（PolicyDeny）继续处理后续工具；allow 自动批准
            effect = self._evaluate_queued_rule(tool_name, tool_args)
            if effect == "deny":
                decisions[index] = {
                    "type": "reject",
                    "args": {"message": "denied by policy rule"},
                }
                continue
            if effect == "allow":
                decisions[index] = {"type": "approve"}
                continue

            base_description = str(
                action_map.get("description") or "A tool execution requires approval"
            )
            # 序号并入 description 展示；payload 严格保持 schema 四字段
            description = (
                f"（第 {position + 1}/{total_unsafe} 个待审批操作）{base_description}"
                if total_unsafe > 1
                else base_description
            )
            spec = InteractionRequest(
                request_id=first_spec.request_id if position == 0 else f"{interrupt_id}-{position}",
                type="approval",
                payload={
                    "interrupt_id": interrupt_id,
                    "description": description,
                    "requests": _bounded_json({"action_requests": [dict(action_map)]}),
                    "decisions": [
                        "approve_once",
                        "approve_thread",
                        "approve_project",
                        "reject",
                        "reject_with_feedback",
                    ],
                },
                interrupt_id=interrupt_id,
                action_count=1,
            )
            run.status = "interacting"
            result = await self._interaction_port.request(run.owner, run.ref, spec)
            run.status = "running"
            self._emit(
                run,
                INTERACTION_RESOLVED,
                {"request_id": spec.request_id, "type": spec.type},
            )
            response = result.value if isinstance(result.value, Mapping) else {}
            decision = str(response.get("decision") or "")
            feedback = str(response.get("feedback") or "")
            self._record_approval_rule(tool_name, tool_args, decision)

            if decision in {"approve_once", "approve_thread", "approve_project"}:
                decisions[index] = {"type": "approve"}
            elif decision == "reject_with_feedback" and feedback:
                decisions[index] = {"type": "reject", "args": {"message": feedback}}
                run.batch_rejected = True
            else:
                decisions[index] = {"type": "reject"}
                run.batch_rejected = True

            run.pending_approvals = run.pending_approvals[1:]

        run.pending_approvals = []
        return {interrupt_id: {"decisions": decisions}}


def _generate_permission_rule(
    tool_name: str, tool_args: Mapping[str, object]
) -> list[PermissionRule]:
    """从被批准的工具调用上下文生成 allow 权限规则列表。

    Shell 工具按链式命令分段逐段生成；其余工具生成单元素列表。
    某段无法生成有效规则（空串 / 裸根禁令）时跳过该段，不写入通配兜底。
    """
    from harness_agent.policy.bash_parser import extract_segments, strip_wrappers

    command = str(tool_args.get("command") or "").strip()
    file_path = str(tool_args.get("file_path") or "").strip()
    url = str(tool_args.get("url") or "").strip()

    if tool_name in {"execute", "monitor"} and command:
        rules: list[PermissionRule] = []
        seen: set[str] = set()
        for raw_segment in extract_segments(command):
            processed = strip_wrappers(raw_segment, max_depth=3)
            resource = _extract_command_rule(processed)
            if not resource or resource in seen:
                continue
            seen.add(resource)
            rules.append(
                PermissionRule(tool=tool_name, resource=resource, effect="allow")
            )
        return rules
    if (
        tool_name in {"write_file", "edit_file", "delete_file"}
        and file_path
    ):
        # 规范：文件写/删工具生成项目级通配规则，用户明确批准后不再反复
        # 弹窗；L3.5 敏感路径与工作区边界预检仍强制裁决，通配不放宽
        # 硬性保护。
        resource = "*"
    elif tool_name == "web_fetch" and url:
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or url
            resource = f"domain:{hostname}"
        except Exception:
            resource = "*"
    else:
        resource = "*"
    return [PermissionRule(tool=tool_name, resource=resource, effect="allow")]


