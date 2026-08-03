"""Host 控制租约：唯一输入 holder、受控操作 permit 与可撤销 attachment 的线性化事实。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass


class ControlLeaseError(Exception):
    """控制权领域错误；AgentHost 负责映射为稳定 JSON-RPC 错误。"""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        """保存稳定错误码与重试语义。"""
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ActivityFacts:
    """acquire/release 时由 AgentHost 提供的 Connection 活动事实。"""

    starting_or_active_runs: int = 0
    pending_interactions: int = 0


@dataclass(frozen=True, slots=True)
class ControlHolder:
    """当前 holder 的不可变身份。"""

    connection_id: str
    role: str
    attachment_id: str | None = None


@dataclass(frozen=True, slots=True)
class ControlStatus:
    """ControlLease 对外暴露的不可变状态快照。"""

    state: str
    holder: ControlHolder

    def to_record(self) -> dict[str, object]:
        """转换为 v3 ControlStatus wire 形状。"""
        return {
            "state": self.state,
            "holder": {
                "connection_id": self.holder.connection_id,
                "role": self.holder.role,
                "attachment_id": self.holder.attachment_id,
            },
        }


@dataclass(slots=True)
class _AttachmentRecord:
    """ControlLease 侧登记的 attachment 身份；不保存 token 或 transport。"""

    connection_id: str | None = None
    revoked: bool = False


class ControlLease:
    """线性化 holder 变更与受控操作受理的唯一输入源。

    host 启动后 holder 是 owner Connection；只有 owner 签发且已认证的
    attached Connection 可以 acquire。控制权转换与受控操作 permit 都在
    同一把锁内登记，因此不存在“先检查 holder 再受理”的 TOCTOU 窗口。
    """

    def __init__(self, owner_connection_id: str) -> None:
        """以 owner Connection 作为初始 holder。"""
        self._owner_connection_id = owner_connection_id
        self._state = "owner"
        self._holder_connection_id = owner_connection_id
        self._holder_attachment_id: str | None = None
        self._permits = 0
        self._attachments: dict[str, _AttachmentRecord] = {}
        self._lock = asyncio.Lock()
        self._idle_condition = asyncio.Condition(self._lock)

    def status(self) -> ControlStatus:
        """返回当前 holder 与状态的不可变快照；只读。"""
        return self._snapshot()

    async def register_attachment(
        self,
        attachment_id: str,
        connection_id: str,
    ) -> bool:
        """在认证完成时绑定 attachment 与其 Connection；已撤销则拒绝。"""
        async with self._lock:
            return self._register_locked(attachment_id, connection_id)

    def register_attachment_sync(
        self,
        attachment_id: str,
        connection_id: str,
    ) -> bool:
        """供同步创建 Connection 的嵌入/测试路径登记已认证 attachment。

        该方法不经过异步锁，只能在事件循环尚未并发运行受控操作的同步
        初始化或单线程测试路径调用；生产 WebSocket 认证统一使用异步
        ``register_attachment``。
        """
        return self._register_locked(attachment_id, connection_id)

    def _register_locked(self, attachment_id: str, connection_id: str) -> bool:
        record = self._attachments.get(attachment_id)
        if record is None:
            self._attachments[attachment_id] = _AttachmentRecord(
                connection_id=connection_id
            )
            return True
        if record.revoked:
            return False
        record.connection_id = connection_id
        return True

    def attachment_id_for(self, connection_id: str) -> str | None:
        """返回 Connection 已登记且未撤销的 attachment_id，供 acquire 使用。"""
        for key, record in self._attachments.items():
            if record.connection_id == connection_id and not record.revoked:
                return key
        return None

    async def acquire(
        self,
        connection_id: str,
        attachment_id: str,
        owner_activity_provider: Callable[[], Awaitable[ActivityFacts]],
    ) -> ControlStatus:
        """把 holder 从 owner 原子转移给一个有效 attached Connection。"""
        async with self._lock:
            record = self._attachments.get(attachment_id)
            if record is None or record.connection_id != connection_id:
                raise ControlLeaseError("ATTACHMENT_NOT_ACTIVE")
            if self._state == "revoking":
                # 瞬态收敛窗口：holder 即将归还 owner，客户端应重试而不是放弃。
                raise ControlLeaseError("CONTROL_BUSY", retryable=True)
            if record.revoked:
                raise ControlLeaseError("ATTACHMENT_NOT_ACTIVE")
            if self._state == "attached":
                if (
                    self._holder_connection_id == connection_id
                    and self._holder_attachment_id == attachment_id
                ):
                    return self._snapshot()
                raise ControlLeaseError("CONTROL_BUSY", retryable=True)
            if connection_id == self._owner_connection_id:
                raise ControlLeaseError("CONTROL_BUSY", retryable=True)
            # 活动事实必须在租约锁内读取，避免与 owner 受理 Run 之间出现 TOCTOU。
            owner_activity = await owner_activity_provider()
            if (
                self._permits > 0
                or owner_activity.starting_or_active_runs > 0
                or owner_activity.pending_interactions > 0
            ):
                raise ControlLeaseError("CONTROL_BUSY", retryable=True)
            self._state = "attached"
            self._holder_connection_id = connection_id
            self._holder_attachment_id = attachment_id
            return self._snapshot()

    async def release(
        self,
        connection_id: str,
        activity_provider: Callable[[], Awaitable[ActivityFacts]],
    ) -> ControlStatus:
        """只允许当前 attached holder 在无未收敛工作时把控制权归还 owner。"""
        async with self._lock:
            if (
                self._state != "attached"
                or self._holder_connection_id != connection_id
            ):
                raise ControlLeaseError("CONTROL_NOT_HOLDER", retryable=True)
            activity = await activity_provider()
            if (
                self._permits > 0
                or activity.starting_or_active_runs > 0
                or activity.pending_interactions > 0
            ):
                raise ControlLeaseError(
                    "CONTROL_RELEASE_BLOCKED",
                    retryable=True,
                )
            self._state = "owner"
            self._holder_connection_id = self._owner_connection_id
            self._holder_attachment_id = None
            return self._snapshot()

    async def begin_revoke(self, attachment_id: str) -> str | None:
        """标记 attachment 撤销并阻止新 permit；返回需收敛的 connection。"""
        async with self._lock:
            record = self._attachments.get(attachment_id)
            if record is None:
                return None
            record.revoked = True
            if (
                self._state == "attached"
                and self._holder_attachment_id == attachment_id
            ):
                self._state = "revoking"
            return record.connection_id

    async def connection_disconnected(self, connection_id: str) -> str | None:
        """Connection 断线时标记其 attachment；返回需要归还的 attachment_id。"""
        async with self._lock:
            attachment_id = next(
                (
                    key
                    for key, record in self._attachments.items()
                    if record.connection_id == connection_id
                ),
                None,
            )
            if attachment_id is None:
                return None
            record = self._attachments[attachment_id]
            record.revoked = True
            if (
                self._state == "attached"
                and self._holder_connection_id == connection_id
            ):
                self._state = "revoking"
            return attachment_id

    async def complete_revoke(self, attachment_id: str) -> ControlStatus:
        """等待进行中 permit 归零后移除 attachment；若它是 holder 则恢复 owner。"""
        async with self._idle_condition:
            await self._idle_condition.wait_for(lambda: self._permits == 0)
            record = self._attachments.pop(attachment_id, None)
            if (
                record is not None
                and self._state == "revoking"
                and self._holder_attachment_id == attachment_id
            ):
                self._state = "owner"
                self._holder_connection_id = self._owner_connection_id
                self._holder_attachment_id = None
            return self._snapshot()

    @asynccontextmanager
    async def permit(self, connection_id: str) -> AsyncIterator[None]:
        """为受控操作发放 permit：同一锁内校验 holder 并登记进行中计数。"""
        async with self._lock:
            if (
                self._state == "revoking"
                or self._holder_connection_id != connection_id
            ):
                raise ControlLeaseError("CONTROL_NOT_HOLDER", retryable=True)
            self._permits += 1
        try:
            yield
        finally:
            # 任务取消时 await 锁会立即抛 CancelledError 导致计数泄漏；
            # 先 uncancel 完成递减，再恢复取消状态。
            task = asyncio.current_task()
            cancelled = task is not None and task.cancelling() > 0
            if cancelled:
                task.uncancel()
            try:
                async with self._idle_condition:
                    self._permits -= 1
                    if self._permits == 0:
                        self._idle_condition.notify_all()
            finally:
                if cancelled:
                    task.cancel()

    def _snapshot(self) -> ControlStatus:
        role = "attached" if self._holder_attachment_id is not None else "owner"
        return ControlStatus(
            state=self._state,
            holder=ControlHolder(
                connection_id=self._holder_connection_id,
                role=role,
                attachment_id=self._holder_attachment_id,
            ),
        )
