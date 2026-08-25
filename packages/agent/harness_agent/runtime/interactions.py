"""跨 host/compose 共享的进程内 Interaction 契约。

InteractionRequest 描述一次「需要用户审批或回答问题」的请求，由 Run owner
（RunCoordinator）统一派发；Host 与 Compose Workflow 两侧都构造和消费它，
因此不能定义在 host 层（compose 模块反向依赖 host 会破坏模块方向）。
不进入 wire payload：与协议 schema 的映射由 host adapter 负责。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


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
    # Compose child activity 归属；Build root 交互保持为空。
    execution_id: str | None = None
    parent_execution_id: str | None = None
    agent_id: str | None = None
    compose_scope: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class InteractionResult:
    """InteractionPort 返回的语言无关结果。"""

    value: object
    expired: bool = False


@dataclass(frozen=True, slots=True)
class ChildInteractionRecord:
    """一次子 Agent 交互的有界 provenance 与 checkpoint 归属。"""

    request_id: str
    run_id: str
    execution_id: str
    parent_execution_id: str | None
    agent_id: str
    checkpoint_namespace: str


class ChildInteractionRegistry:
    """Run 级子 Agent Interaction 登记表。

    Registry 只保存请求元数据，不持有 transport future；具体反向请求仍由
    ``ProtocolInteractionAdapter`` 复用现有通道。这样断连、取消和父 Run
    终态时可以按 Run 清除 child 记录，同时不会创建第二条交互链路。
    """

    def __init__(self) -> None:
        """建立进程内、生命周期受 Host 控制的登记表。"""
        self._records: dict[str, ChildInteractionRecord] = {}

    def register(
        self,
        *,
        request_id: str,
        run_id: str,
        execution_id: str,
        parent_execution_id: str | None,
        agent_id: str,
        checkpoint_namespace: str,
    ) -> ChildInteractionRecord:
        """登记一条 child 请求；重复 ID 直接失败关闭。"""
        values = (
            request_id,
            run_id,
            execution_id,
            agent_id,
            checkpoint_namespace,
        )
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("CHILD_INTERACTION_INVALID")
        if parent_execution_id is not None and not parent_execution_id:
            raise ValueError("CHILD_INTERACTION_INVALID")
        if request_id in self._records:
            raise ValueError("CHILD_INTERACTION_DUPLICATE")
        record = ChildInteractionRecord(
            request_id=request_id,
            run_id=run_id,
            execution_id=execution_id,
            parent_execution_id=parent_execution_id,
            agent_id=agent_id,
            checkpoint_namespace=checkpoint_namespace,
        )
        self._records[request_id] = record
        return record

    def get(self, request_id: str) -> ChildInteractionRecord | None:
        """读取仍在等待中的 child 请求。"""
        return self._records.get(request_id)

    def resolve(self, request_id: str) -> ChildInteractionRecord | None:
        """移除已经得到响应或失败的 child 请求。"""
        return self._records.pop(request_id, None)

    def cancel_run(self, run_id: str) -> tuple[str, ...]:
        """清理父 Run 的全部 child 请求；重复调用保持幂等。"""
        removed = tuple(
            request_id
            for request_id, record in self._records.items()
            if record.run_id == run_id
        )
        for request_id in removed:
            self._records.pop(request_id, None)
        return removed

    def clear(self) -> None:
        """Host 关闭时清空全部登记。"""
        self._records.clear()
