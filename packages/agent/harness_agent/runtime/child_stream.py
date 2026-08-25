"""Inline child 的流式执行：过程事件带 child 身份，不进父 Transcript。

child 子图不再闷跑 ``ainvoke``。本模块按与父 Run 同一套 ExecutionStream
协议投影 child 图：每条 reasoning / tool / content 信号都以 child 的
execution 身份转发到父 RunContext 的 event_port，供表现层组装独立子时间线。
Transcript 只由根 ExecutionStream 的 observer 捕获，本模块的 observer
一律返回 False，child 过程正文不可能进入父 Transcript 或下一轮模型上下文。

child 内的审批（ChildHitlMiddleware）与目录信任仍走 Host Interaction，不经
本模块；若 child 图意外产生 LangGraph interrupt（question / approval），
一律 fail closed，不默认放行也不伪装成空时间线成功。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from harness_agent.runtime.execution_binding import ExecutionMode, ExecutionRef
from harness_agent.runtime.execution_stream import (
    ExecutionStreamError,
    ExecutionStreamPorts,
    ExecutionStreamRequest,
    ExecutionSignal,
    StreamInteractionRequest,
    StreamSession,
    execute,
)
from harness_agent.runtime.run_context import RunContext


class ChildStreamPorts(ExecutionStreamPorts):
    """child stream 对父 RunContext 的 seam：只发事件，Interaction fail closed。"""

    def __init__(
        self,
        *,
        emit: Callable[[str, Mapping[str, object]], None],
    ) -> None:
        self._emit_signal = emit

    def emit(self, signal: ExecutionSignal) -> None:
        self._emit_signal(signal.type, dict(signal.payload))

    async def interact(self, request: StreamInteractionRequest) -> object:
        """child 图意外产生 LangGraph interrupt 时 fail closed，不默认放行。"""
        raise ExecutionStreamError(
            "CHILD_INTERACTION_UNSUPPORTED",
            "child graph must not raise LangGraph interrupts",
        )

    async def observe_message(self, chunk: object, session: StreamSession) -> bool:
        """child 过程正文不进入任何 Transcript。"""
        return False

    async def after_tool_boundary(self) -> None:
        return None

    def on_stream_event(self) -> None:
        return None


def child_context_for(parent: RunContext, *, child_ref: ExecutionRef, agent_id: str) -> RunContext:
    """以 child execution 身份派生 RunContext，只用于 child 图内部读身份。"""
    return RunContext(
        thread_id=parent.thread_id,
        run_id=parent.run_id,
        approval_mode=parent.approval_mode,
        context_snapshot=parent.context_snapshot,
        profile_key=parent.profile_key,
        execution_id=child_ref.execution_id,
        parent_execution_id=child_ref.parent_execution_id,
        agent_id=agent_id,
        execution_mode=ExecutionMode.INLINE,
        cancellation_token=parent.cancellation_token,
        skill_registry=parent.skill_registry,
        delegation_policy=None,
        snapshot_store=parent.snapshot_store,
        approval_presentations=parent.approval_presentations,
        workspace_root_registry=parent.workspace_root_registry,
        model_call_lifecycle=parent.model_call_lifecycle,
    )


async def stream_inline_child(
    *,
    graph: Any,
    parent: RunContext,
    child_ref: ExecutionRef,
    agent_id: str,
    task: str,
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """流式运行 Inline child 图，事件带 child 身份；返回父 task 需要的状态。

    返回的 state 只保留最终 assistant 文本（deepagents task 工具要求
    ``messages`` 键并从最后一条 AIMessage 取正文），child 的中间消息与
    工具结果不回传父图。
    """
    event_port = getattr(parent, "event_port", None)
    child_context = child_context_for(parent, child_ref=child_ref, agent_id=agent_id)

    def emit(event_type: str, payload: Mapping[str, object]) -> None:
        if callable(event_port):
            event_port(
                event_type,
                dict(payload),
                child_ref.execution_id,
                child_ref.parent_execution_id,
                agent_id,
            )

    ports = ChildStreamPorts(emit=emit)
    session = StreamSession(run_id=child_ref.run_id)
    stream_input: object = {"messages": [HumanMessage(content=task)]}
    resume: object | None = None
    final_content = ""
    while True:
        if resume is not None:
            stream_input = Command(resume=resume)
        request = ExecutionStreamRequest(
            agent=graph,
            stream_input=stream_input,
            graph_config={"configurable": {"thread_id": child_ref.thread_id}},
            context=child_context,
            content_visibility="passthrough",
            session=session,
            is_cancelled=cancelled,
        )
        try:
            result = await execute(request, ports)
        except ExecutionStreamError as exc:
            if exc.code == "RUN_CANCELLED":
                raise asyncio.CancelledError from exc
            raise
        final_content = result.final_content or final_content
        if result.resume is None:
            break
        resume = result.resume
    return {"messages": [AIMessage(content=final_content)]}
