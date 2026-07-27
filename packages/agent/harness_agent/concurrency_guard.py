"""并发守卫中间件：通过异步读写锁保证写操作不与任何操作并发执行。

ToolNode 由 langchain factory 内部创建并使用 asyncio.gather 无差别并行，
本中间件在 awrap_tool_call 层拦截每次工具调用：
- 只读工具获取共享读锁，多个只读调用可同时执行；
- 写工具获取独占写锁，阻塞所有其他读者和写者。
效果等价于分区执行，但无需替换 ToolNode。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest

from harness_agent.concurrency import AsyncRWLock, is_concurrency_safe

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class ConcurrencyGuardMiddleware(AgentMiddleware):
    """并发安全守卫：利用读写锁串行化写操作、并行化只读操作。

    需要 HITL 审批的工具（interrupt_on 集合中的工具）跳过锁获取，
    避免写锁阻塞 HITL 中间件的 action 打包，导致审批后死锁。
    """

    def __init__(self, interrupt_on: frozenset[str] | None = None) -> None:
        """初始化内部读写锁实例。

        参数：
            interrupt_on: 需要 HITL 审批的工具名称集合；这些工具不获取锁。
        """
        super().__init__()
        self._rwlock = AsyncRWLock()
        self._interrupt_on: frozenset[str] = interrupt_on or frozenset()

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步路径直接委托：本项目仅使用异步执行，同步无需并发控制。"""
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步路径：按工具并发安全性获取对应锁后执行。

        需要 HITL 审批的工具跳过锁获取——写锁会阻塞其他协程到达
        HITL 中间件，破坏 action 打包机制，导致审批恢复后死锁。
        """
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        args = tool_call.get("args") or {}

        # 需要 HITL 审批的工具：不获取锁，让 HITL 正常打包多个 action。
        # 审批通过后工具由 ToolNode gather 并行执行；安全性由人工审批保证。
        if tool_name in self._interrupt_on:
            return await handler(request)

        if is_concurrency_safe(tool_name, args):
            # 只读工具：共享读锁，多个读者可并行
            await self._rwlock.acquire_read()
            try:
                return await handler(request)
            finally:
                await self._rwlock.release_read()
        else:
            # 写工具或未知工具：独占写锁，阻塞一切并发
            await self._rwlock.acquire_write()
            try:
                return await handler(request)
            finally:
                await self._rwlock.release_write()
