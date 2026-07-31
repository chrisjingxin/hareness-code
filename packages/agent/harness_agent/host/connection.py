"""Protocol Connection 状态与反向 Interaction adapter。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jsonschema.exceptions import ValidationError

from harness_agent.host.run_coordinator import (
    ConnectionRef,
    INTERACTION_TIMEOUT_MS,
    InteractionRequest,
    InteractionResult,
    RunRef,
)
from harness_agent.protocol.generated import METHOD, PROTOCOL_MINOR, SERVER_CAPABILITIES

if TYPE_CHECKING:
    from harness_agent.host.agent_host import AgentHost


logger = logging.getLogger(__name__)


class RpcError(Exception):
    """可安全返回给客户端的预期 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        """保存错误码、文案和可选结构化详情。"""
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(slots=True)
class ProtocolConnection:
    """一个前端连接的协议状态，不拥有任何 Agent 运行资源。"""

    connection_id: str
    role: str
    sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    capability_ceiling: frozenset[str] = field(
        default_factory=lambda: frozenset(SERVER_CAPABILITIES)
    )
    initialized: bool = False
    protocol_minor: int = PROTOCOL_MINOR
    enabled_capabilities: set[str] = field(default_factory=set)
    interaction_handles: set[str] = field(default_factory=set)
    watched_threads: set[str] = field(default_factory=set)
    pending_requests: dict[str, asyncio.Future[object]] = field(default_factory=dict)
    interaction_specs: dict[str, InteractionRequest] = field(default_factory=dict)
    closed: bool = False


class ProtocolInteractionAdapter:
    """把类型化 Interaction 映射为 owner Connection 上的 JSON-RPC reverse request。"""

    def __init__(self, host: AgentHost) -> None:
        """保存 Host 引用；RunCoordinator 不会看到 transport 实现。"""
        self._host = host

    async def request(
        self,
        owner: ConnectionRef,
        run: RunRef,
        interaction: InteractionRequest,
    ) -> InteractionResult:
        """向 owner 请求审批/问答，超时、断开和缺少 capability 均安全降级。"""
        connection = self._host._connections.get(owner.connection_id)
        if (
            connection is None
            or connection.closed
            or interaction.type not in self._host._connection_handles(connection)
        ):
            logger.info("Interaction %s disabled by capability negotiation", interaction.request_id)
            return InteractionResult(self._default_value(interaction), expired=True)

        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        connection.pending_requests[interaction.request_id] = future
        connection.interaction_specs[interaction.request_id] = interaction
        method = (
            METHOD["INTERACTION_APPROVAL"]
            if interaction.type == "approval"
            else METHOD["INTERACTION_QUESTION"]
        )
        try:
            await self._host._send_to(
                connection,
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "id": interaction.request_id,
                    "params": {
                        "thread_id": run.thread_id,
                        "run_id": run.run_id,
                        "timeout_ms": INTERACTION_TIMEOUT_MS,
                        "payload": dict(interaction.payload),
                    },
                },
            )
            return InteractionResult(
                await asyncio.wait_for(future, timeout=INTERACTION_TIMEOUT_MS / 1000)
            )
        except (TimeoutError, RpcError, ValidationError) as exc:
            logger.warning("Interaction %s failed closed: %s", interaction.request_id, exc)
            return InteractionResult(self._default_value(interaction), expired=True)
        finally:
            connection.pending_requests.pop(interaction.request_id, None)
            connection.interaction_specs.pop(interaction.request_id, None)

    @staticmethod
    def _default_value(interaction: InteractionRequest) -> dict[str, object]:
        """返回 LangGraph 可接受的安全默认交互结果。"""
        return {"decision": "reject"} if interaction.type == "approval" else {"answers": {}}
