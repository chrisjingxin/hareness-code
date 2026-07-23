"""按 Runtime Profile 共享的 AgentRuntime 生命周期原语。

本模块不接入 JSON-RPC；它定义共享编译图的资源所有权、租约、容量/空闲淘汰、
状态机与脱敏诊断，使 Sidecar 能安全复用、观测或关闭运行时。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from harness_agent.runtime_profile import RuntimeProfile

logger = logging.getLogger(__name__)


class AgentRuntimeState(StrEnum):
    """一个 Profile Runtime 的生命周期状态。"""

    MISSING = "missing"
    BUILDING = "building"
    READY = "ready"
    ACTIVE = "active"
    IDLE = "idle"
    DRAINING = "draining"
    CLOSED = "closed"


class AgentRuntimeError(RuntimeError):
    """Runtime 构建、租用或关闭中的可预期错误。"""


class RuntimeUnavailableError(AgentRuntimeError):
    """Runtime 已进入 DRAINING/CLOSED，不能接受新的租约时抛出。"""


class RuntimeBusyError(AgentRuntimeError):
    """Runtime 仍有租约或活动 run，不能安全关闭时抛出。"""


class RuntimePoolClosedError(AgentRuntimeError):
    """Pool 已关闭，不能继续 acquire 时抛出。"""


class RuntimePoolCapacityError(AgentRuntimeError):
    """Pool 没有可安全淘汰的空闲 Runtime，不能继续扩容时抛出。"""


@dataclass(frozen=True, slots=True)
class RuntimeStateTransition:
    """一次可观测的状态转换，不记录 Prompt、路径或凭据。"""

    state: AgentRuntimeState
    reason: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Runtime 的脱敏生命周期快照，供池策略和诊断读取。"""

    profile_key: str
    state: AgentRuntimeState
    active_leases: int
    active_runs: int
    queued_runs: int
    pinned: bool
    created_at: float
    last_used_at: float


@dataclass(frozen=True, slots=True)
class RuntimeCloseFailure:
    """单个关闭步骤的失败记录；后续资源仍必须继续释放。"""

    resource_name: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class RuntimeCloseReport:
    """幂等关闭的结果，只包含资源名称和错误分类。"""

    profile_key: str
    failures: tuple[RuntimeCloseFailure, ...] = ()
    duration_ms: float | None = None

    @property
    def closed_cleanly(self) -> bool:
        """返回资源关闭过程中是否没有记录失败。"""
        return not self.failures


@dataclass(frozen=True, slots=True)
class RuntimePoolEvent:
    """Pool 的脱敏生命周期事件；仅保留短 Profile ID 与可诊断元数据。"""

    event: str
    profile_id: str
    reason: str | None
    duration_ms: float | None
    close_failures: int
    occurred_at: float

    def payload(self) -> dict[str, object]:
        """返回可安全写入本地诊断 JSON 的事件摘要。"""
        result: dict[str, object] = {
            "event": self.event,
            "profile_id": self.profile_id,
            "close_failures": self.close_failures,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        if self.duration_ms is not None:
            result["duration_ms"] = round(self.duration_ms, 3)
        return result


@dataclass(frozen=True, slots=True)
class RuntimePoolRuntimeDiagnostic:
    """单个 Runtime 的脱敏诊断；不含完整 Profile Key 或原始配置。"""

    profile_id: str
    state: AgentRuntimeState
    active_leases: int
    active_runs: int
    queued_runs: int
    pinned: bool

    def payload(self) -> dict[str, object]:
        """转换为稳定 JSON 形状，供 config.show 和未来指标适配器复用。"""
        return {
            "profile_id": self.profile_id,
            "state": self.state.value,
            "active_leases": self.active_leases,
            "active_runs": self.active_runs,
            "queued_runs": self.queued_runs,
            "pinned": self.pinned,
        }


@dataclass(frozen=True, slots=True)
class RuntimePoolDiagnosticSnapshot:
    """RuntimePool 的脱敏可观测性快照，不包含 Prompt、路径、模型或凭据。"""

    pool_size: int
    max_profiles: int
    idle_ttl_seconds: float
    close_timeout_seconds: float
    closed: bool
    state_counts: dict[str, int]
    active_leases: int
    active_runs: int
    queued_runs: int
    hits: int
    misses: int
    build_successes: int
    build_failures: int
    build_duration_ms_total: float
    capacity_rejections: int
    eviction_reasons: dict[str, int]
    close_reports: int
    close_failures: int
    close_duration_ms_total: float
    runtimes: tuple[RuntimePoolRuntimeDiagnostic, ...]
    recent_events: tuple[RuntimePoolEvent, ...]

    def payload(self) -> dict[str, object]:
        """返回已有 JSON-RPC 配置查询可直接携带的安全诊断摘要。"""
        return {
            "available": True,
            "pool_size": self.pool_size,
            "limits": {
                "max_profiles": self.max_profiles,
                "idle_ttl_seconds": self.idle_ttl_seconds,
                "close_timeout_seconds": self.close_timeout_seconds,
            },
            "closed": self.closed,
            "state_counts": dict(self.state_counts),
            "active": {
                "leases": self.active_leases,
                "runs": self.active_runs,
                "queued_runs": self.queued_runs,
            },
            "metrics": {
                "hits": self.hits,
                "misses": self.misses,
                "build_successes": self.build_successes,
                "build_failures": self.build_failures,
                "build_duration_ms_total": round(self.build_duration_ms_total, 3),
                "capacity_rejections": self.capacity_rejections,
                "eviction_reasons": dict(self.eviction_reasons),
                "close_reports": self.close_reports,
                "close_failures": self.close_failures,
                "close_duration_ms_total": round(self.close_duration_ms_total, 3),
            },
            # 真实 RSS 需跨平台基线和校准；先保留稳定接口，CI 以 Runtime 数验证上界。
            "memory": {"estimated_bytes": None, "rss_bytes": None, "status": "not_collected"},
            "runtimes": [runtime.payload() for runtime in self.runtimes],
            "recent_events": [event.payload() for event in self.recent_events],
        }


CloseCallback = Callable[[], Awaitable[None] | None]
"""可同步或异步执行的资源关闭/刷新回调。"""


@dataclass(frozen=True, slots=True)
class RuntimeCloseAdapter:
    """把 MCP、Sandbox、工具或模型客户端适配为稳定关闭步骤。"""

    name: str
    close: CloseCallback

    def __post_init__(self) -> None:
        """拒绝无法在诊断中定位的空名称或不可调用关闭器。"""
        if not self.name or not callable(self.close):
            raise ValueError("RUNTIME_CLOSE_ADAPTER_INVALID")

    async def invoke(self) -> None:
        """执行关闭器，并同时兼容当前本地实现的同步无资源对象。"""
        result = self.close()
        if inspect.isawaitable(result):
            await result


@dataclass(frozen=True, slots=True)
class RuntimeResourceBundle:
    """Runtime 独占资源及其确定性关闭顺序。

    进程级 Provider HTTP client 不属于该 Bundle；它未来应由 ProviderClientPool
    持有。当前本地运行时可以使用空 Bundle，不需要伪造 MCP 或 Sandbox 资源。
    """

    flushers: tuple[RuntimeCloseAdapter, ...] = ()
    tool_resources: tuple[RuntimeCloseAdapter, ...] = ()
    mcp_resources: tuple[RuntimeCloseAdapter, ...] = ()
    sandbox_resources: tuple[RuntimeCloseAdapter, ...] = ()
    model_resources: tuple[RuntimeCloseAdapter, ...] = ()

    @classmethod
    def from_sequences(
        cls,
        *,
        flushers: Sequence[RuntimeCloseAdapter] = (),
        tool_resources: Sequence[RuntimeCloseAdapter] = (),
        mcp_resources: Sequence[RuntimeCloseAdapter] = (),
        sandbox_resources: Sequence[RuntimeCloseAdapter] = (),
        model_resources: Sequence[RuntimeCloseAdapter] = (),
    ) -> "RuntimeResourceBundle":
        """把可变输入规范化为不可变 Bundle，防止构建后被调用方篡改。"""
        return cls(
            flushers=tuple(flushers),
            tool_resources=tuple(tool_resources),
            mcp_resources=tuple(mcp_resources),
            sandbox_resources=tuple(sandbox_resources),
            model_resources=tuple(model_resources),
        )


class AgentRuntime:
    """一个 Runtime Profile 拥有的共享编译图及其资源生命周期。"""

    def __init__(
        self,
        *,
        profile: RuntimeProfile,
        graph: Any,
        resources: RuntimeResourceBundle | None = None,
        pinned: bool = False,
    ) -> None:
        """创建已构建但尚未被租用的 Runtime。"""
        now = time.monotonic()
        self.profile = profile
        self.graph: Any | None = graph
        self.resources = resources or RuntimeResourceBundle()
        self._state = AgentRuntimeState.READY
        self._pinned = pinned
        self._active_leases = 0
        self._active_runs = 0
        self._queued_runs = 0
        self._created_at = now
        self._last_used_at = now
        self._lock = asyncio.Lock()
        self._close_task: asyncio.Task[RuntimeCloseReport] | None = None
        self._close_report: RuntimeCloseReport | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._transitions: list[RuntimeStateTransition] = [
            RuntimeStateTransition(AgentRuntimeState.READY, "built", now)
        ]

    @property
    def profile_key(self) -> str:
        """返回该 Runtime 绑定的稳定 Profile Key。"""
        return self.profile.profile_key

    @property
    def state(self) -> AgentRuntimeState:
        """返回最近状态；需要一致计数时使用 ``snapshot``。"""
        return self._state

    @property
    def transitions(self) -> tuple[RuntimeStateTransition, ...]:
        """返回状态转换的只读副本。"""
        return tuple(self._transitions)

    @property
    def close_report(self) -> RuntimeCloseReport | None:
        """返回已完成关闭的报告；未关闭时为 ``None``。"""
        return self._close_report

    async def snapshot(self) -> RuntimeSnapshot:
        """原子读取资源租用计数和最后使用时间。"""
        async with self._lock:
            return RuntimeSnapshot(
                profile_key=self.profile_key,
                state=self._state,
                active_leases=self._active_leases,
                active_runs=self._active_runs,
                queued_runs=self._queued_runs,
                pinned=self._pinned,
                created_at=self._created_at,
                last_used_at=self._last_used_at,
            )

    async def set_pinned(self, pinned: bool) -> None:
        """设置是否允许未来的池策略主动淘汰该 Runtime。"""
        async with self._lock:
            if self._state == AgentRuntimeState.CLOSED:
                raise RuntimeUnavailableError("RUNTIME_CLOSED")
            self._pinned = pinned

    async def acquire_lease(self) -> "AgentRuntimeLease":
        """获取一个 Runtime 租约，并阻止 DRAINING/CLOSED Runtime 接受新调用。"""
        async with self._lock:
            self._require_accepting_locked()
            self._active_leases += 1
            self._last_used_at = time.monotonic()
            self._set_state_locked(AgentRuntimeState.ACTIVE, "lease_acquired")
        return AgentRuntimeLease(self)

    async def begin_draining(self, *, reason: str = "draining") -> None:
        """停止接受新租约，但允许已有 lease 按其取消/完成策略收尾。"""
        async with self._lock:
            if self._state == AgentRuntimeState.CLOSED:
                return
            self._set_state_locked(AgentRuntimeState.DRAINING, reason)

    async def is_closeable(self) -> bool:
        """返回是否不存在 lease、排队 run 或活动 run，适合无强制关闭。"""
        async with self._lock:
            return self._is_closeable_locked()

    async def register_background_task(self, task: asyncio.Task[Any]) -> None:
        """登记 Runtime 自己创建的后台任务，关闭时会取消并等待它们。"""
        async with self._lock:
            if self._state in {AgentRuntimeState.DRAINING, AgentRuntimeState.CLOSED}:
                task.cancel()
                return
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def aclose(self, *, force: bool = False) -> RuntimeCloseReport:
        """幂等释放 Runtime 独占资源；单项失败不会阻断后续关闭。

        非强制关闭只接受无 lease/run 的 Runtime。Pool shutdown 可传 ``force``，
        此时已登记的后台任务会被取消；外部调用方仍应先释放其 lease。
        """
        close_task: asyncio.Task[RuntimeCloseReport]
        async with self._lock:
            if self._state == AgentRuntimeState.CLOSED:
                return self._close_report or RuntimeCloseReport(self.profile_key)
            if self._close_task is not None:
                close_task = self._close_task
            else:
                if not force and not self._is_closeable_locked():
                    raise RuntimeBusyError("RUNTIME_HAS_ACTIVE_LEASE_OR_RUN")
                self._set_state_locked(AgentRuntimeState.DRAINING, "closing")
                close_task = asyncio.create_task(
                    self._close_owned_resources(),
                    name=f"harness-runtime-close-{self.profile_key[:12]}",
                )
                self._close_task = close_task
        return await asyncio.shield(close_task)

    async def _queue_run(self) -> None:
        """让已持有 lease 的调用方登记一个待启动 run。"""
        async with self._lock:
            if self._state == AgentRuntimeState.CLOSED:
                raise RuntimeUnavailableError("RUNTIME_CLOSED")
            self._queued_runs += 1
            self._last_used_at = time.monotonic()
            if self._state != AgentRuntimeState.DRAINING:
                self._set_state_locked(AgentRuntimeState.ACTIVE, "run_queued")

    async def _start_queued_run(self) -> None:
        """将一个已登记的 run 从队列转为活动状态。"""
        async with self._lock:
            if self._queued_runs < 1:
                raise AgentRuntimeError("RUNTIME_RUN_NOT_QUEUED")
            if self._state == AgentRuntimeState.CLOSED:
                raise RuntimeUnavailableError("RUNTIME_CLOSED")
            self._queued_runs -= 1
            self._active_runs += 1
            self._last_used_at = time.monotonic()
            if self._state != AgentRuntimeState.DRAINING:
                self._set_state_locked(AgentRuntimeState.ACTIVE, "run_started")

    async def _release_queued_run(self) -> None:
        """取消尚未启动的 queued run，并恢复可用状态。"""
        async with self._lock:
            if self._queued_runs < 1:
                return
            self._queued_runs -= 1
            self._last_used_at = time.monotonic()
            self._update_activity_state_locked("queued_run_released")

    async def _release_active_run(self) -> None:
        """结束活动 run；最后一个活动项释放后进入 IDLE 或保留 DRAINING。"""
        async with self._lock:
            if self._active_runs < 1:
                return
            self._active_runs -= 1
            self._last_used_at = time.monotonic()
            self._update_activity_state_locked("run_released")

    async def _release_lease(self) -> None:
        """释放租约，保证重复 release 不会让计数变为负数。"""
        async with self._lock:
            if self._active_leases < 1:
                return
            self._active_leases -= 1
            self._last_used_at = time.monotonic()
            self._update_activity_state_locked("lease_released")

    async def _close_owned_resources(self) -> RuntimeCloseReport:
        """按固定顺序取消后台任务、刷新状态、关闭资源并清除图引用。"""
        started_at = time.monotonic()
        failures: list[RuntimeCloseFailure] = []
        try:
            async with self._lock:
                tasks = tuple(task for task in self._background_tasks if not task.done())
            for task in tasks:
                task.cancel()
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        failures.append(
                            RuntimeCloseFailure("background_task", type(result).__name__, str(result))
                        )
            await self._run_adapters("flush", self.resources.flushers, failures)
            await self._run_adapters("tool", self.resources.tool_resources, failures)
            await self._run_adapters("mcp", self.resources.mcp_resources, failures)
            await self._run_adapters("sandbox", self.resources.sandbox_resources, failures)
            await self._run_adapters("model", self.resources.model_resources, failures)
        finally:
            async with self._lock:
                self.graph = None
                report = RuntimeCloseReport(
                    self.profile_key,
                    tuple(failures),
                    duration_ms=(time.monotonic() - started_at) * 1000,
                )
                self._close_report = report
                self._set_state_locked(AgentRuntimeState.CLOSED, "resources_released")
            return report

    async def _run_adapters(
        self,
        category: str,
        adapters: Sequence[RuntimeCloseAdapter],
        failures: list[RuntimeCloseFailure],
    ) -> None:
        """执行一个资源类别；失败只记录，不跳过同类或后续资源。"""
        for adapter in adapters:
            try:
                await adapter.invoke()
            except Exception as exc:
                failure = RuntimeCloseFailure(
                    resource_name=f"{category}:{adapter.name}",
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                failures.append(failure)
                logger.warning(
                    "Runtime resource close failed profile=%s resource=%s error=%s",
                    self.profile_key[:12],
                    failure.resource_name,
                    failure.error_type,
                )

    def _require_accepting_locked(self) -> None:
        """在锁内拒绝 DRAINING/CLOSED Runtime 的新租约。"""
        if self._state == AgentRuntimeState.DRAINING:
            raise RuntimeUnavailableError("RUNTIME_DRAINING")
        if self._state == AgentRuntimeState.CLOSED or self.graph is None:
            raise RuntimeUnavailableError("RUNTIME_CLOSED")

    def _is_closeable_locked(self) -> bool:
        """判断当前是否没有任何会继续使用图或资源的活动项。"""
        return self._active_leases == 0 and self._active_runs == 0 and self._queued_runs == 0

    def _update_activity_state_locked(self, reason: str) -> None:
        """根据租约和 run 计数切换 ACTIVE/IDLE，不覆盖 DRAINING/CLOSED。"""
        if self._state in {AgentRuntimeState.DRAINING, AgentRuntimeState.CLOSED}:
            return
        target = AgentRuntimeState.IDLE if self._is_closeable_locked() else AgentRuntimeState.ACTIVE
        self._set_state_locked(target, reason)

    def _set_state_locked(self, state: AgentRuntimeState, reason: str) -> None:
        """记录真正发生的状态转换，避免重复状态污染诊断历史。"""
        if self._state == state:
            return
        self._state = state
        self._transitions.append(RuntimeStateTransition(state, reason, time.monotonic()))


class AgentRuntimeLease:
    """对一个 AgentRuntime 的租约；释放前它阻止无强制淘汰。"""

    def __init__(self, runtime: AgentRuntime) -> None:
        """由 ``AgentRuntime.acquire_lease`` 创建，外部不能伪造活动计数。"""
        self.runtime = runtime
        self._released = False

    async def __aenter__(self) -> "AgentRuntimeLease":
        """异步上下文进入时返回租约自身。"""
        return self

    async def __aexit__(self, _type: object, _value: object, _traceback: object) -> None:
        """退出上下文时总是释放租约。"""
        await self.release()

    async def reserve_run(self) -> "AgentRuntimeRunLease":
        """登记一个 run；调用方可稍后 ``start``，用于显式观察 queued 状态。"""
        if self._released:
            raise RuntimeUnavailableError("RUNTIME_LEASE_RELEASED")
        await self.runtime._queue_run()
        return AgentRuntimeRunLease(self.runtime)

    async def run(self) -> "AgentRuntimeRunLease":
        """登记并立即启动一个 run，适合 ``async with await lease.run()``。"""
        run_lease = await self.reserve_run()
        await run_lease.start()
        return run_lease

    async def release(self) -> None:
        """幂等释放租约。"""
        if self._released:
            return
        self._released = True
        await self.runtime._release_lease()


class AgentRuntimeRunLease:
    """一个已登记或正在执行的 run 对图资源的活动引用。"""

    def __init__(self, runtime: AgentRuntime) -> None:
        """由活动 RuntimeLease 预留；初始状态为 queued。"""
        self.runtime = runtime
        self._started = False
        self._released = False

    async def __aenter__(self) -> AgentRuntime:
        """进入上下文时确保 run 已启动并暴露 Runtime 图。"""
        await self.start()
        return self.runtime

    async def __aexit__(self, _type: object, _value: object, _traceback: object) -> None:
        """退出上下文时结束该 run 的活动引用。"""
        await self.release()

    async def start(self) -> None:
        """将 queued run 转成 active；重复调用不会重复增加计数。"""
        if self._released:
            raise RuntimeUnavailableError("RUNTIME_RUN_LEASE_RELEASED")
        if self._started:
            return
        await self.runtime._start_queued_run()
        self._started = True

    async def release(self) -> None:
        """结束 active run 或取消 queued run，保证重复调用安全。"""
        if self._released:
            return
        self._released = True
        if self._started:
            await self.runtime._release_active_run()
        else:
            await self.runtime._release_queued_run()


RuntimeBuilder = Callable[[RuntimeProfile], Awaitable[AgentRuntime] | AgentRuntime]
"""由 Profile 构建完整 Runtime 的工厂；图及资源关闭责任一并返回。"""


@dataclass(slots=True)
class _RuntimeEntry:
    """Pool 内部条目；BUILDING 时只有 Future，READY 后才持有 Runtime。"""

    profile: RuntimeProfile
    state: AgentRuntimeState
    build_task: asyncio.Task[AgentRuntime] | None = None
    runtime: AgentRuntime | None = None
    build_started_at: float = field(default_factory=time.monotonic)
    drain_reason: str | None = None


class RuntimePool:
    """按 RuntimeProfileKey 管理共享 Runtime 的构建、有界缓存和显式排空。"""

    def __init__(
        self,
        builder: RuntimeBuilder,
        *,
        max_profiles: int = 8,
        idle_ttl_seconds: float = 1_800,
        close_timeout_seconds: float = 15,
    ) -> None:
        """保存工厂与 Pool 策略；只淘汰无租约、无 run 的未固定 Runtime。"""
        if not callable(builder):
            raise ValueError("RUNTIME_BUILDER_INVALID")
        if not isinstance(max_profiles, int) or isinstance(max_profiles, bool) or max_profiles < 1:
            raise ValueError("RUNTIME_POOL_MAX_PROFILES_INVALID")
        if (
            not isinstance(idle_ttl_seconds, (int, float))
            or isinstance(idle_ttl_seconds, bool)
            or idle_ttl_seconds < 0
        ):
            raise ValueError("RUNTIME_POOL_IDLE_TTL_INVALID")
        if (
            not isinstance(close_timeout_seconds, (int, float))
            or isinstance(close_timeout_seconds, bool)
            or close_timeout_seconds <= 0
        ):
            raise ValueError("RUNTIME_POOL_CLOSE_TIMEOUT_INVALID")
        self._builder = builder
        self._max_profiles = max_profiles
        self._idle_ttl_seconds = idle_ttl_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._entries: dict[str, _RuntimeEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[tuple[RuntimeCloseReport, ...]] | None = None
        self._hits = 0
        self._misses = 0
        self._build_successes = 0
        self._build_failures = 0
        self._build_duration_ms_total = 0.0
        self._capacity_rejections = 0
        self._eviction_reasons: dict[str, int] = {}
        self._close_reports = 0
        self._close_failures = 0
        self._close_duration_ms_total = 0.0
        self._recent_events: list[RuntimePoolEvent] = []

    async def acquire(self, profile: RuntimeProfile) -> AgentRuntimeLease:
        """按 Profile 获取共享 Runtime 租约，并对同 Key 首建执行 single-flight。"""
        key = profile.profile_key
        accounted = False
        while True:
            runtime: AgentRuntime | None = None
            build_task: asyncio.Task[AgentRuntime] | None = None
            requires_eviction = False
            async with self._lock:
                if self._closed:
                    raise RuntimePoolClosedError("RUNTIME_POOL_CLOSED")
                entry = self._entries.get(key)
                if entry is None:
                    if not accounted:
                        self._misses += 1
                        accounted = True
                    requires_eviction = len(self._entries) >= self._max_profiles
                    if not requires_eviction:
                        entry = _RuntimeEntry(profile=profile, state=AgentRuntimeState.BUILDING)
                        build_task = asyncio.create_task(
                            self._build_entry(key, entry),
                            name=f"harness-runtime-build-{key[:12]}",
                        )
                        entry.build_task = build_task
                        self._entries[key] = entry
                elif entry.state == AgentRuntimeState.BUILDING:
                    if not accounted:
                        self._hits += 1
                        accounted = True
                    build_task = entry.build_task
                else:
                    if not accounted:
                        self._hits += 1
                        accounted = True
                    runtime = entry.runtime
                    if runtime is None:
                        # 不允许损坏条目变成永久缓存；删除后让当前 acquire 重新构建。
                        self._entries.pop(key, None)
                        continue
            if requires_eviction:
                if not await self._evict_lru_idle():
                    self._capacity_rejections += 1
                    self._record_event(
                        "capacity_rejected",
                        profile_key=key,
                        reason="no_idle_runtime",
                    )
                    raise RuntimePoolCapacityError("RUNTIME_POOL_CAPACITY_EXHAUSTED")
                continue
            if build_task is not None:
                await asyncio.shield(build_task)
                continue
            if runtime is None:
                continue
            return await runtime.acquire_lease()

    async def size(self) -> int:
        """返回包括 BUILDING/DRAINING 在内的 Pool 条目数，供诊断与测试使用。"""
        async with self._lock:
            return len(self._entries)

    async def diagnostics(self) -> RuntimePoolDiagnosticSnapshot:
        """读取 Pool 的脱敏快照；不会暴露完整 Key、Prompt、路径或模型配置。"""
        async with self._lock:
            entries = tuple(self._entries.values())
            closed = self._closed
            counters = {
                "hits": self._hits,
                "misses": self._misses,
                "build_successes": self._build_successes,
                "build_failures": self._build_failures,
                "build_duration_ms_total": self._build_duration_ms_total,
                "capacity_rejections": self._capacity_rejections,
                "eviction_reasons": dict(self._eviction_reasons),
                "close_reports": self._close_reports,
                "close_failures": self._close_failures,
                "close_duration_ms_total": self._close_duration_ms_total,
                "recent_events": tuple(self._recent_events),
            }
        diagnostics: list[RuntimePoolRuntimeDiagnostic] = []
        for entry in entries:
            if entry.state == AgentRuntimeState.BUILDING or entry.runtime is None:
                diagnostics.append(
                    RuntimePoolRuntimeDiagnostic(
                        profile_id=_profile_id(entry.profile.profile_key),
                        state=AgentRuntimeState.BUILDING,
                        active_leases=0,
                        active_runs=0,
                        queued_runs=0,
                        pinned=False,
                    )
                )
                continue
            snapshot = await entry.runtime.snapshot()
            diagnostics.append(
                RuntimePoolRuntimeDiagnostic(
                    profile_id=_profile_id(snapshot.profile_key),
                    state=snapshot.state,
                    active_leases=snapshot.active_leases,
                    active_runs=snapshot.active_runs,
                    queued_runs=snapshot.queued_runs,
                    pinned=snapshot.pinned,
                )
            )
        diagnostics.sort(key=lambda item: (item.state.value, item.profile_id))
        state_counts = {state.value: 0 for state in AgentRuntimeState}
        for runtime in diagnostics:
            state_counts[runtime.state.value] += 1
        return RuntimePoolDiagnosticSnapshot(
            pool_size=len(diagnostics),
            max_profiles=self._max_profiles,
            idle_ttl_seconds=self._idle_ttl_seconds,
            close_timeout_seconds=self._close_timeout_seconds,
            closed=closed,
            state_counts={name: count for name, count in state_counts.items() if count},
            active_leases=sum(runtime.active_leases for runtime in diagnostics),
            active_runs=sum(runtime.active_runs for runtime in diagnostics),
            queued_runs=sum(runtime.queued_runs for runtime in diagnostics),
            hits=int(counters["hits"]),
            misses=int(counters["misses"]),
            build_successes=int(counters["build_successes"]),
            build_failures=int(counters["build_failures"]),
            build_duration_ms_total=float(counters["build_duration_ms_total"]),
            capacity_rejections=int(counters["capacity_rejections"]),
            eviction_reasons=dict(counters["eviction_reasons"]),
            close_reports=int(counters["close_reports"]),
            close_failures=int(counters["close_failures"]),
            close_duration_ms_total=float(counters["close_duration_ms_total"]),
            runtimes=tuple(diagnostics),
            recent_events=tuple(counters["recent_events"]),
        )

    async def sweep(self, *, now: float | None = None) -> tuple[str, ...]:
        """按空闲 TTL 淘汰安全候选，并返回实际进入排空的 Profile Key。"""
        current = time.monotonic() if now is None else now
        runtimes = await self._idle_runtimes()
        expired: list[tuple[float, str]] = []
        for runtime in runtimes:
            snapshot = await runtime.snapshot()
            if current - snapshot.last_used_at >= self._idle_ttl_seconds:
                expired.append((snapshot.last_used_at, snapshot.profile_key))
        evicted: list[str] = []
        for _, profile_key in sorted(expired):
            if await self.evict(profile_key, reason="idle_ttl"):
                evicted.append(profile_key)
        return tuple(evicted)

    async def state_for(self, profile_key: str) -> AgentRuntimeState:
        """返回 Pool 观察到的状态；不存在或构建失败后统一为 MISSING。"""
        async with self._lock:
            entry = self._entries.get(profile_key)
            if entry is None:
                return AgentRuntimeState.MISSING
            if entry.state == AgentRuntimeState.BUILDING:
                return AgentRuntimeState.BUILDING
            runtime = entry.runtime
        return runtime.state if runtime is not None else AgentRuntimeState.MISSING

    async def runtime_for(self, profile_key: str) -> AgentRuntime | None:
        """返回已构建 Runtime 的只读引用，仅供生命周期协调和测试。"""
        async with self._lock:
            entry = self._entries.get(profile_key)
            return entry.runtime if entry is not None else None

    async def evict(
        self,
        profile_key: str,
        *,
        reason: str = "evicted",
        force: bool = False,
    ) -> bool:
        """开始排空 Runtime；force 仅供配置失效/关闭绕过 pin，不中断已有 run。"""
        runtime = await self.runtime_for(profile_key)
        if runtime is None:
            return False
        snapshot = await runtime.snapshot()
        if snapshot.pinned and not force:
            return False
        if snapshot.state == AgentRuntimeState.CLOSED:
            return False
        await runtime.begin_draining(reason=reason)
        if snapshot.state != AgentRuntimeState.DRAINING:
            async with self._lock:
                entry = self._entries.get(profile_key)
                if entry is not None and entry.runtime is runtime and entry.drain_reason is None:
                    entry.drain_reason = reason
                    self._eviction_reasons[reason] = self._eviction_reasons.get(reason, 0) + 1
                    self._record_event("eviction_started", profile_key=profile_key, reason=reason)
        return await self.finalize_draining(profile_key)

    async def finalize_draining(self, profile_key: str) -> bool:
        """在最后一个 lease/run 释放后关闭 DRAINING Runtime 并移出 Pool。"""
        runtime = await self.runtime_for(profile_key)
        if runtime is None or runtime.state != AgentRuntimeState.DRAINING:
            return False
        if not await runtime.is_closeable():
            return False
        report = await runtime.aclose()
        async with self._lock:
            entry = self._entries.get(profile_key)
            if entry is not None and entry.runtime is runtime:
                self._record_close_report(
                    profile_key,
                    report,
                    reason=entry.drain_reason or "draining",
                )
                self._entries.pop(profile_key, None)
        return True

    async def aclose(self) -> tuple[RuntimeCloseReport, ...]:
        """关闭 Pool；等待首建结束后强制关闭其创建的全部 Runtime。"""
        close_task: asyncio.Task[tuple[RuntimeCloseReport, ...]]
        async with self._lock:
            if self._close_task is not None:
                close_task = self._close_task
            else:
                self._closed = True
                close_task = asyncio.create_task(self._close_all(), name="harness-runtime-pool-close")
                self._close_task = close_task
        return await asyncio.shield(close_task)

    async def _build_entry(self, key: str, entry: _RuntimeEntry) -> AgentRuntime:
        """执行工厂并只在成功时发布 READY Runtime；失败后删除条目以允许重试。"""
        try:
            created = self._builder(entry.profile)
            runtime = await created if inspect.isawaitable(created) else created
            if not isinstance(runtime, AgentRuntime):
                raise AgentRuntimeError("RUNTIME_BUILDER_RESULT_INVALID")
            if runtime.profile.profile_key != key:
                raise AgentRuntimeError("RUNTIME_PROFILE_KEY_MISMATCH")
            async with self._lock:
                current = self._entries.get(key)
                if current is entry:
                    entry.runtime = runtime
                    entry.state = AgentRuntimeState.READY
                    entry.build_task = None
                    duration_ms = (time.monotonic() - entry.build_started_at) * 1000
                    self._build_successes += 1
                    self._build_duration_ms_total += duration_ms
                    self._record_event("build_completed", profile_key=key, duration_ms=duration_ms)
            return runtime
        except Exception:
            async with self._lock:
                if self._entries.get(key) is entry:
                    self._entries.pop(key, None)
                    duration_ms = (time.monotonic() - entry.build_started_at) * 1000
                    self._build_failures += 1
                    self._build_duration_ms_total += duration_ms
                    self._record_event("build_failed", profile_key=key, duration_ms=duration_ms)
            raise

    async def _close_all(self) -> tuple[RuntimeCloseReport, ...]:
        """等待 BUILDING 条目收敛，再继续关闭每个已发布 Runtime。"""
        async with self._lock:
            build_tasks = tuple(
                entry.build_task
                for entry in self._entries.values()
                if entry.build_task is not None
            )
        if build_tasks:
            for build_task in build_tasks:
                build_task.cancel()
            await asyncio.gather(*build_tasks, return_exceptions=True)
        async with self._lock:
            closing_entries = tuple(
                (entry.runtime, entry.drain_reason or "pool_shutdown")
                for entry in self._entries.values()
                if entry.runtime is not None
            )
        reports = tuple([await self._close_with_timeout(runtime) for runtime, _ in closing_entries])
        async with self._lock:
            for (runtime, reason), report in zip(closing_entries, reports, strict=True):
                self._record_close_report(runtime.profile_key, report, reason=reason)
            self._entries.clear()
        return reports

    async def _evict_lru_idle(self) -> bool:
        """淘汰最久未使用的安全候选；并发重新租用时重试下一候选。"""
        candidates = await self._idle_runtimes()
        snapshots = [(await runtime.snapshot(), runtime) for runtime in candidates]
        for snapshot, _ in sorted(snapshots, key=lambda item: item[0].last_used_at):
            if await self.evict(snapshot.profile_key, reason="lru_capacity"):
                return True
        return False

    async def _idle_runtimes(self) -> tuple[AgentRuntime, ...]:
        """快照当前可供策略检查的 Runtime；不在 Pool 锁内等待 Runtime 锁。"""
        async with self._lock:
            runtimes = tuple(
                entry.runtime
                for entry in self._entries.values()
                if entry.runtime is not None
            )
        result: list[AgentRuntime] = []
        for runtime in runtimes:
            snapshot = await runtime.snapshot()
            if (
                snapshot.state == AgentRuntimeState.IDLE
                and snapshot.active_leases == 0
                and snapshot.active_runs == 0
                and snapshot.queued_runs == 0
                and not snapshot.pinned
            ):
                result.append(runtime)
        return tuple(result)

    async def _close_with_timeout(self, runtime: AgentRuntime) -> RuntimeCloseReport:
        """限制 Pool shutdown 等待时间；超时后记录失败并让关闭任务自行收尾。"""
        try:
            return await asyncio.wait_for(
                runtime.aclose(force=True), timeout=self._close_timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "Runtime close timed out profile=%s timeout_seconds=%s",
                runtime.profile_key[:12],
                self._close_timeout_seconds,
            )
            return RuntimeCloseReport(
                runtime.profile_key,
                (
                    RuntimeCloseFailure(
                        "runtime_close_timeout",
                        "TimeoutError",
                        f"close exceeded {self._close_timeout_seconds} seconds",
                    ),
                ),
                duration_ms=self._close_timeout_seconds * 1000,
            )

    def _record_close_report(
        self,
        profile_key: str,
        report: RuntimeCloseReport,
        *,
        reason: str,
    ) -> None:
        """汇总一个关闭结果，并以短 Profile ID 记录结构化日志与有限事件历史。"""
        self._close_reports += 1
        self._close_failures += len(report.failures)
        if report.duration_ms is not None:
            self._close_duration_ms_total += report.duration_ms
        self._record_event(
            "runtime_closed",
            profile_key=profile_key,
            reason=reason,
            duration_ms=report.duration_ms,
            close_failures=len(report.failures),
            level=logging.WARNING if report.failures else logging.INFO,
        )

    def _record_event(
        self,
        event: str,
        *,
        profile_key: str,
        reason: str | None = None,
        duration_ms: float | None = None,
        close_failures: int = 0,
        level: int = logging.INFO,
    ) -> None:
        """写入最多 64 条脱敏事件；日志字段不携带完整 Profile Key 或用户输入。"""
        item = RuntimePoolEvent(
            event=event,
            profile_id=_profile_id(profile_key),
            reason=reason,
            duration_ms=duration_ms,
            close_failures=close_failures,
            occurred_at=time.monotonic(),
        )
        self._recent_events.append(item)
        if len(self._recent_events) > 64:
            del self._recent_events[:-64]
        logger.log(
            level,
            "RuntimePool event=%s profile=%s reason=%s duration_ms=%s close_failures=%s",
            item.event,
            item.profile_id,
            item.reason or "-",
            f"{item.duration_ms:.3f}" if item.duration_ms is not None else "-",
            item.close_failures,
        )


def _profile_id(profile_key: str) -> str:
    """返回诊断可关联但不可反向作为完整 Profile Key 使用的短标识。"""
    return profile_key[:12]
