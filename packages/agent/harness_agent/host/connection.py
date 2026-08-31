"""Protocol Connection 状态与反向 Interaction adapter。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from jsonschema.exceptions import ValidationError

from harness_agent.host.run_coordinator import (
    ConnectionRef,
    INTERACTION_TIMEOUT_MS,
    RunRef,
)
from harness_agent.runtime.interactions import InteractionRequest, InteractionResult
from harness_agent.protocol.generated import METHOD, PROTOCOL_MINOR, SERVER_CAPABILITIES

if TYPE_CHECKING:
    from harness_agent.host.agent_host import AgentHost


logger = logging.getLogger(__name__)


def interaction_method(interaction_type: str) -> str:
    """把类型化 Interaction 映射为协议反向请求方法名。"""
    if interaction_type == "question":
        return METHOD["INTERACTION_QUESTION"]
    if interaction_type == "directory_trust":
        return METHOD["INTERACTION_DIRECTORY_TRUST"]
    return METHOD["INTERACTION_APPROVAL"]


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
    # CLI 在 initialize 返回的 Skill snapshot 上计算一次最终 Registry 后，把
    # command id -> resolved slash name 绑定到本连接；Run 只读取这份不可变映射。
    command_binding_snapshot_id: str | None = None
    command_bindings: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
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
        method = interaction_method(interaction.type)
        try:
            params: dict[str, object] = {
                "thread_id": run.thread_id,
                "run_id": run.run_id,
                "timeout_ms": INTERACTION_TIMEOUT_MS,
                "payload": dict(interaction.payload),
            }
            # Compose child activity 归属：与 Event envelope 相同的可选 provenance 与
            # scope，供 Interactive Core 把审批/问答卡归入对应 activity 分组。
            if interaction.execution_id is not None:
                params["execution_id"] = interaction.execution_id
            if interaction.parent_execution_id is not None:
                params["parent_execution_id"] = interaction.parent_execution_id
            if interaction.agent_id is not None:
                params["agent_id"] = interaction.agent_id
            if interaction.compose_scope is not None:
                params["compose_scope"] = dict(interaction.compose_scope)
            await self._host._send_to(
                connection,
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "id": interaction.request_id,
                    "params": params,
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
        if interaction.type == "approval":
            return {"decision": "reject"}
        if interaction.type == "directory_trust":
            return {"decision": "deny"}
        return {"answers": {}}
