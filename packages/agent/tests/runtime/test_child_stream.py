"""HC-162 子代理时间线：Inline child 过程事件、派出绑定与 Transcript 隔离。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import Runnable
from pydantic import Field

from harness_agent.runtime.agent_catalog import DelegationPolicy
from harness_agent.runtime.agent_delegation import (
    AgentDelegator,
    DelegateAgent,
    DelegationCallContext,
    DelegationTarget,
    child_execution_ref,
)
from harness_agent.runtime.agent_delegation import _CURRENT_DELEGATION_CALL
from harness_agent.runtime.agent_execution import AgentExecutionRegistry
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
)
from harness_agent.runtime.run_context import RunCancellationToken, RunContext
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot


class _ToolCallingModel(GenericFakeChatModel):
    """支持 DeepAgents bind_tools 的离线模型。"""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, _tools, **_kwargs) -> Runnable:
        """测试不执行真实 provider。"""
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        """记录主、子 Agent 收到的消息。"""
        self.received.append(list(messages))
        return super()._generate(messages, *args, **kwargs)

    async def _astream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        """离线流式：整条消息作为一个 chunk，保留完整 tool_calls。"""
        self.received.append(list(messages))
        message = next(self.messages)
        message_ = AIMessage(content=message) if isinstance(message, str) else message
        chunk = AIMessageChunk(
            content=message_.content,
            tool_calls=message_.tool_calls,
            id=message_.id,
        )
        chunk.chunk_position = "last"
        yield ChatGenerationChunk(message=chunk)


def _context(*, event_port=None, delegation_policy=None, execution_id: str = "root") -> RunContext:
    """构造带 child 事件通道的父 RunContext。"""
    return RunContext(
        thread_id="thread-1",
        run_id="run-1",
        approval_mode="yolo",
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id="thread-1",
            system_prompt="parent",
            workspace="/tmp",
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        execution_id=execution_id,
        agent_id="main",
        cancellation_token=RunCancellationToken(),
        delegation_policy=delegation_policy,
        event_port=event_port,
    )


def _command(root: ExecutionRef, *, target: str = "general-purpose") -> DelegateAgent:
    """构造允许一个一层子 Agent 的派发命令。"""
    return DelegateAgent(
        parent_ref=root,
        target_agent_id=target,
        task="检查代码并返回结论",
        idempotency_key=f"call-{target}",
        delegation_policy=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose",),
            max_depth=1,
            max_parallelism=1,
        ),
        cancellation_token=RunCancellationToken(),
        timeout_seconds=10,
    )


async def _registry() -> tuple[AgentExecutionRegistry, ExecutionRef]:
    """建立一个 running 根 execution。"""
    registry = AgentExecutionRegistry()
    root = ExecutionRef.root("thread-1", "run-1")
    await registry.accept(
        AgentExecutionBinding(
            ref=root,
            agent_id="main",
            mode=ExecutionMode.MANAGED,
            depth=0,
        )
    )
    await registry.start(root)
    return registry, root


@pytest.mark.asyncio
async def test_delegator_emits_parent_task_binding_delta() -> None:
    """child 创建成功后，父 task 的 tool.delta 带一对一绑定字段。"""
    registry, root = await _registry()
    events: list[tuple[str, Mapping[str, object], str | None, str | None, str | None]] = []

    def event_port(
        event_type: str,
        payload: Mapping[str, object],
        execution_id: str | None,
        parent_execution_id: str | None,
        agent_id: str | None,
    ) -> None:
        events.append((event_type, payload, execution_id, parent_execution_id, agent_id))

    async def inline(command: DelegateAgent):
        return {"messages": [AIMessage(content="CHILD_OK")]}

    delegator = AgentDelegator(
        registry,
        targets=(
            DelegationTarget(
                agent_id="general-purpose",
                mode=ExecutionMode.INLINE,
                runner=inline,
            ),
        ),
    )
    command = _command(root)
    token = _CURRENT_DELEGATION_CALL.set(
        DelegationCallContext(run_context=_context(event_port=event_port), tool_call_id="task-call-1")
    )
    try:
        await delegator.execute(command)
    finally:
        _CURRENT_DELEGATION_CALL.reset(token)

    bindings = [event for event in events if event[0] == "tool.delta"]
    assert len(bindings) == 1
    payload = bindings[0][1]
    assert payload["tool_call_id"] == "task-call-1"
    assert payload["child_execution_id"] == child_execution_ref(command).execution_id
    assert payload["child_agent_id"] == "general-purpose"


@pytest.mark.asyncio
async def test_inline_child_streams_tool_events_with_child_identity(tmp_path: Path) -> None:
    """生产 root stream 中 Inline child 工具只以 child execution 身份出现。"""
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.runtime.agent_catalog import EffectiveExecutionPolicy
    from harness_agent.policy.capability_policy import (
        BUILTIN_TOOL_NAMES,
        resolve_effective_capability_view,
    )
    from harness_agent.threads.snapshots import ThreadSnapshotStore

    registry, root = await _registry()
    policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        tools=None,
        mcp_tools=None,
        skills=None,
        filesystem_read=None,
        filesystem_write=None,
        shell=None,
        network=None,
        isolation="local",
        approval_mode="yolo",
        delegation=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose",),
            max_depth=1,
            max_parallelism=1,
        ),
    )
    view = resolve_effective_capability_view(
        policy,
        available_tools=BUILTIN_TOOL_NAMES,
    )
    snapshots = ThreadSnapshotStore()
    (tmp_path / "answer.txt").write_text("SECRET_CHILD_BODY\n", encoding="utf-8")
    model = _ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "读取 answer.txt 后只返回 CHILD_OK",
                                "subagent_type": "general-purpose",
                            },
                            "id": "task-call-1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/answer.txt", "offset": 0, "limit": 20},
                            "id": "child-read-1",
                        }
                    ],
                ),
                AIMessage(content="CHILD_OK"),
                AIMessage(content="PARENT_OK"),
            ]
        )
    )
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(tmp_path),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        capability_view=view,
        execution_registry=registry,
        snapshot_store=snapshots,
    )
    events: list[tuple[str, Mapping[str, object], str | None, str | None, str | None]] = []

    def event_port(
        event_type: str,
        payload: Mapping[str, object],
        execution_id: str | None,
        parent_execution_id: str | None,
        agent_id: str | None,
    ) -> None:
        events.append((event_type, payload, execution_id, parent_execution_id, agent_id))

    context = _context(
        event_port=event_port,
        delegation_policy=policy.delegation,
        execution_id=root.execution_id,
    )
    from harness_agent.runtime.execution_stream import (
        ExecutionSignal,
        ExecutionStreamRequest,
        StreamInteractionRequest,
        StreamSession,
        execute,
    )

    class RootPorts:
        """模拟 Host root observer，并与 child event_port 汇聚到同一条事件流。"""

        def emit(self, signal: ExecutionSignal) -> None:
            events.append(
                (signal.type, signal.payload, root.execution_id, None, "main")
            )

        async def interact(self, _request: StreamInteractionRequest) -> object:
            raise AssertionError("yolo 测试不应产生 Interaction")

        async def observe_message(
            self, _chunk: object, _session: StreamSession
        ) -> bool:
            return False

        async def after_tool_boundary(self) -> None:
            return None

        def on_stream_event(self) -> None:
            return None

    result = await execute(
        ExecutionStreamRequest(
            agent=graph,
            stream_input={"messages": [HumanMessage(content="delegate")]},
            graph_config={"configurable": {"thread_id": root.thread_id}},
            context=context,
            content_visibility="passthrough",
            session=StreamSession(run_id=root.run_id),
            is_cancelled=lambda: False,
        ),
        RootPorts(),
    )

    assert result.final_content == "PARENT_OK"
    executions = await registry.list(root)
    child = next(item for item in executions if item.agent_id == "general-purpose")
    child_id = child.ref.execution_id
    assert child_id.startswith("child-")

    started = [event for event in events if event[0] == "tool.started" and event[1].get("name") == "read_file"]
    assert started, "child 的 read_file 必须以带身份的 tool.started 流出"
    assert {event[2] for event in started} == {child_id}
    assert {event[3] for event in started} == {root.execution_id}
    assert {event[4] for event in started} == {"general-purpose"}

    child_tool_events = [
        event
        for event in events
        if event[1].get("tool_call_id") == "child-read-1"
        or event[1].get("name") == "read_file"
    ]
    assert child_tool_events
    assert {event[2] for event in child_tool_events} == {child_id}
    assert len(
        [event for event in child_tool_events if event[0] == "tool.completed"]
    ) == 1

    bindings = [event for event in events if event[0] == "tool.delta" and "child_execution_id" in event[1]]
    assert len(bindings) == 1
    assert bindings[0][1]["tool_call_id"] == "task-call-1"
    assert bindings[0][1]["child_execution_id"] == child_id

    root_payloads = "\n".join(
        str(event[1]) for event in events if event[2] == root.execution_id
    )
    assert "SECRET_CHILD_BODY" not in root_payloads


@pytest.mark.asyncio
async def test_child_stream_ports_never_capture_transcript_and_fail_closed() -> None:
    """child 过程不进 Transcript；意外 stream Interaction fail closed。"""
    from harness_agent.runtime.child_stream import ChildStreamPorts
    from harness_agent.runtime.execution_stream import (
        ExecutionStreamError,
        StreamSession,
        StreamInteractionRequest,
    )

    emitted: list[str] = []

    def emit(event_type: str, payload: Mapping[str, object]) -> None:
        emitted.append(event_type)

    ports = ChildStreamPorts(emit=emit)
    session = StreamSession(run_id="run-1")
    assert await ports.observe_message(AIMessage(content="SECRET_CHILD_BODY"), session) is False
    assert ports.on_stream_event() is None

    request = StreamInteractionRequest(
        request_id="r-1",
        type="question",
        payload={},
        interrupt_id="i-1",
    )
    with pytest.raises(ExecutionStreamError) as caught:
        await ports.interact(request)
    assert caught.value.code == "CHILD_INTERACTION_UNSUPPORTED"


def test_translate_stream_event_handles_updates_tool_messages() -> None:
    """ToolNode 在 updates 模式下的 ToolMessage 会被转换为 TOOL_COMPLETED。"""
    from langchain_core.messages import ToolMessage
    from harness_agent.runtime.execution_stream import (
        StreamSession,
        translate_stream_event,
        resolve_tool_stream_id,
    )

    session = StreamSession(run_id="run-1")
    # 模拟首片带 id
    t1_id = resolve_tool_stream_id(session, {"id": "call_1", "name": "ls", "args": ""})
    assert t1_id == "call_1"
    # 模拟续片不带 id
    t1_cont = resolve_tool_stream_id(session, {"args": '{"path":"/"}'})
    assert t1_cont == "call_1"

    update_event = (
        (),
        "updates",
        {"tools": {"messages": [ToolMessage(content="['a.ts', 'b.ts']", tool_call_id="call_1")]}},
    )
    signals = list(translate_stream_event(update_event, session))
    assert len(signals) == 1
    assert signals[0].type == "tool.completed"
    assert signals[0].payload["tool_call_id"] == "call_1"
    assert signals[0].payload["result"]["content"] == "['a.ts', 'b.ts']"
