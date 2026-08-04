"""共享运行资源的所有权、借用租约和定向排空原语。

Host 或 workspace 级资源只能由其上层 owner 关闭。AgentEngine 只持有
借用租约；引擎淘汰时释放租约而不会关闭底层 Provider、MCP 或 sandbox。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

ResourceValue = TypeVar("ResourceValue")
CloseCallback = Callable[[], Awaitable[None] | None]


class ResourceScope(StrEnum):
    """资源的 owner 生命周期层级。"""

    HOST = "host"
    WORKSPACE = "workspace"
    ENGINE = "engine"


class ResourceState(StrEnum):
    """共享资源从可借用到关闭的状态。"""

    READY = "ready"
    DRAINING = "draining"
    CLOSED = "closed"


class SharedResourceError(RuntimeError):
    """共享资源租用或关闭失败。"""


class SharedResourceUnavailableError(SharedResourceError):
    """资源已经排空或关闭，不能产生新的借用。"""


class SharedResourceBusyError(SharedResourceError):
    """仍有借用者时尝试非强制关闭。"""


@dataclass(frozen=True, slots=True)
class SharedResourceSnapshot:
    """不含资源内容的生命周期诊断快照。"""

    name: str
    scope: ResourceScope
    state: ResourceState
    borrowers: int
    drain_reason: str | None
    created_at: float


@dataclass(frozen=True, slots=True)
class SharedResourceCloseFailure:
    """一个共享资源关闭回调的失败摘要。"""

    resource_name: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class SharedResourceCloseReport:
    """共享资源 owner 执行关闭后的结果。"""

    name: str
    scope: ResourceScope
    failures: tuple[SharedResourceCloseFailure, ...] = ()
    duration_ms: float | None = None


class SharedResourceLease(Generic[ResourceValue]):
    """一个 AgentEngine 对 Host/workspace 资源的显式借用。"""

    def __init__(self, resource: "SharedResourceHandle[ResourceValue]") -> None:
        """只由资源 handle 创建，避免调用方伪造借用计数。"""
        self._resource = resource
        self._released = False

    @property
    def name(self) -> str:
        """返回脱敏资源名称。"""
        return self._resource.name

    @property
    def scope(self) -> ResourceScope:
        """返回资源 owner 层级。"""
        return self._resource.scope

    @property
    def value(self) -> ResourceValue:
        """返回构图时使用的共享资源对象。"""
        return self._resource.value

    @property
    def released(self) -> bool:
        """返回该租约是否已经幂等释放。"""
        return self._released

    async def release(self) -> None:
        """释放一次借用；重复调用不会减少底层计数。"""
        if self._released:
            return
        self._released = True
        await self._resource._release(self)


class SharedResourceHandle(Generic[ResourceValue]):
    """由 Host/workspace owner 持有的可定向排空共享资源。"""

    def __init__(
        self,
        *,
        name: str,
        scope: ResourceScope,
        value: ResourceValue,
        close: CloseCallback | None = None,
    ) -> None:
        """创建一个资源 handle；关闭责任只保存在 handle owner。"""
        if not name:
            raise ValueError("SHARED_RESOURCE_NAME_INVALID")
        if scope is ResourceScope.ENGINE:
            raise ValueError("SHARED_RESOURCE_SCOPE_MUST_BE_SHARED")
        self.name = name
        self.scope = scope
        self.value = value
        self._close = close
        self._state = ResourceState.READY
        self._borrowers = 0
        self._drain_reason: str | None = None
        self._created_at = time.monotonic()
        self._close_report: SharedResourceCloseReport | None = None
        self._close_task: asyncio.Task[SharedResourceCloseReport] | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> ResourceState:
        """返回最近状态。"""
        return self._state

    async def acquire(self) -> SharedResourceLease[ResourceValue]:
        """取得一个借用；排空后的资源严格拒绝新使用者。"""
        async with self._lock:
            if self._state is ResourceState.DRAINING:
                raise SharedResourceUnavailableError("SHARED_RESOURCE_DRAINING")
            if self._state is ResourceState.CLOSED:
                raise SharedResourceUnavailableError("SHARED_RESOURCE_CLOSED")
            lease = SharedResourceLease(self)
            self._borrowers += 1
            return lease

    async def begin_draining(self, *, reason: str) -> None:
        """停止新借用，但保留已有 AgentEngine 使用直至释放。"""
        if not reason:
            raise ValueError("SHARED_RESOURCE_DRAIN_REASON_REQUIRED")
        async with self._lock:
            if self._state is ResourceState.CLOSED:
                return
            self._state = ResourceState.DRAINING
            self._drain_reason = reason

    async def snapshot(self) -> SharedResourceSnapshot:
        """读取 owner 诊断所需的计数和排空原因。"""
        async with self._lock:
            return SharedResourceSnapshot(
                name=self.name,
                scope=self.scope,
                state=self._state,
                borrowers=self._borrowers,
                drain_reason=self._drain_reason,
                created_at=self._created_at,
            )

    async def close(self, *, force: bool = False) -> SharedResourceCloseReport:
        """由 owner 关闭资源；非强制模式不允许绕过仍在使用的借用者。"""
        async with self._lock:
            if self._state is ResourceState.CLOSED:
                return self._close_report or SharedResourceCloseReport(self.name, self.scope)
            if self._close_task is not None:
                close_task = self._close_task
            else:
                if self._borrowers and not force:
                    raise SharedResourceBusyError("SHARED_RESOURCE_HAS_BORROWERS")
                self._state = ResourceState.DRAINING
                close_task = asyncio.create_task(
                    self._close_owned_resource(),
                    name=f"harness-resource-close-{self.name[:32]}",
                )
                self._close_task = close_task
        return await asyncio.shield(close_task)

    async def _close_owned_resource(self) -> SharedResourceCloseReport:
        """串行执行 owner 回调，避免并发 close 重复关闭底层对象。"""
        started_at = time.monotonic()
        failures: list[SharedResourceCloseFailure] = []
        if self._close is not None:
            try:
                result = self._close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                failures.append(
                    SharedResourceCloseFailure(
                        self.name,
                        type(exc).__name__,
                        str(exc),
                    )
                )
                logger.warning(
                    "Shared resource close failed resource=%s scope=%s error=%s",
                    self.name,
                    self.scope.value,
                    type(exc).__name__,
                )
        report = SharedResourceCloseReport(
            self.name,
            self.scope,
            tuple(failures),
            duration_ms=(time.monotonic() - started_at) * 1000,
        )
        async with self._lock:
            self._close_report = report
            self._state = ResourceState.CLOSED
            self._borrowers = 0
        return report

    async def _release(self, lease: SharedResourceLease[ResourceValue]) -> None:
        """只接受本 handle 创建的租约，防止跨资源串改借用计数。"""
        if lease._resource is not self:  # noqa: SLF001 - 生命周期内部不变量。
            raise SharedResourceError("SHARED_RESOURCE_LEASE_MISMATCH")
        async with self._lock:
            if self._borrowers < 1:
                raise SharedResourceError("SHARED_RESOURCE_BORROWER_COUNT_UNDERFLOW")
            self._borrowers -= 1
