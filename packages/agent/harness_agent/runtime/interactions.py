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
