"""Host 共享资源的 owner/borrower 租约与延迟关闭原语。

AgentEngine 只持有借用租约，不直接关闭 Provider、MCP manager 或其他 Host
资源。Owner 被替换时先进入 retired；最后一个借用者释放后才执行一次关闭。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar


ResourceT = TypeVar("ResourceT")
CloseCallback = Callable[[ResourceT], Awaitable[None] | None]


class ResourceScope(StrEnum):
    """可关闭资源的稳定生命周期层级。"""

    HOST = "host"
    WORKSPACE = "workspace"
    AGENT_ENGINE = "agent-engine"


class ResourceAccess(StrEnum):
    """资源引用是 owner 还是 borrower。"""

    OWNED = "owned"
    BORROWED = "borrowed"


@dataclass(frozen=True, slots=True)
class SharedResourceSnapshot:
    """不暴露资源内容的借用与关闭状态。"""

    name: str
    scope: ResourceScope
    fingerprint: str
    borrowers: int
    retired: bool
    closed: bool


class SharedResourceOwner(Generic[ResourceT]):
    """拥有一个共享资源，并在 retire 后等待全部 borrower 释放。"""

    def __init__(
        self,
        resource: ResourceT,
        *,
        name: str,
        scope: ResourceScope,
        fingerprint: str,
        close: CloseCallback[ResourceT],
    ) -> None:
        """绑定资源与唯一关闭器；fingerprint 必须是脱敏稳定身份。"""
        if not name or not fingerprint or not callable(close):
            raise ValueError("SHARED_RESOURCE_OWNER_INVALID")
        self.resource = resource
        self.name = name
        self.scope = scope
        self.fingerprint = fingerprint
        self._close = close
        self._borrowers = 0
        self._retired = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def acquire(self) -> "SharedResourceLease[ResourceT]":
        """取得借用租约；retired/closed owner 不再接受新 AgentEngine。"""
        async with self._lock:
            if self._retired or self._closed:
                raise RuntimeError("SHARED_RESOURCE_NOT_ACCEPTING")
            self._borrowers += 1
        return SharedResourceLease(self)

    async def retire(self) -> None:
        """停止新借用；没有 borrower 时立即关闭，否则延迟到最后释放。"""
        async with self._lock:
            self._retired = True
            close_task = self._ensure_close_task_locked()
        if close_task is not None:
            await asyncio.shield(close_task)

    async def aclose(self) -> None:
        """Host 关闭入口；必须在 AgentEnginePool 收敛后调用。"""
        await self.retire()

    async def snapshot(self) -> SharedResourceSnapshot:
        """原子返回脱敏诊断。"""
        async with self._lock:
            return SharedResourceSnapshot(
                name=self.name,
                scope=self.scope,
                fingerprint=self.fingerprint,
                borrowers=self._borrowers,
                retired=self._retired,
                closed=self._closed,
            )

    async def _release(self) -> None:
        """释放一个 borrower，并在 retired 的最后引用退出后关闭。"""
        async with self._lock:
            if self._borrowers > 0:
                self._borrowers -= 1
            close_task = self._ensure_close_task_locked()
        if close_task is not None:
            await asyncio.shield(close_task)

    def _ensure_close_task_locked(self) -> asyncio.Task[None] | None:
        """仅在 retired 且无 borrower 时创建唯一关闭任务。"""
        if not self._retired or self._borrowers or self._closed:
            return self._close_task
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_resource(),
                name=f"harness-shared-resource-close-{self.name}",
            )
        return self._close_task

    async def _close_resource(self) -> None:
        """执行一次 owner 关闭器；失败仍标记 closed 并向调用方传播。"""
        try:
            result = self._close(self.resource)
            if inspect.isawaitable(result):
                await result
        finally:
            async with self._lock:
                self._closed = True


class SharedResourceLease(Generic[ResourceT]):
    """AgentEngine 拥有的 borrower 引用；释放不等于关闭共享资源。"""

    def __init__(self, owner: SharedResourceOwner[ResourceT]) -> None:
        """只能由 owner.acquire 创建。"""
        self.owner = owner
        self.resource = owner.resource
        self._released = False

    async def release(self) -> None:
        """幂等释放 borrower。"""
        if self._released:
            return
        self._released = True
        await self.owner._release()

    async def __aenter__(self) -> ResourceT:
        """返回借用资源。"""
        return self.resource

    async def __aexit__(self, _type: object, _value: object, _traceback: object) -> None:
        """退出作用域时释放借用。"""
        await self.release()
