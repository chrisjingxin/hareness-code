"""ManagedAgentExecutor 的共享执行契约测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from harness_agent.runtime.execution_stream import ExecutionSignal, StreamSession
from harness_agent.runtime.managed_agent_executor import (
    ManagedAgentExecutionError,
    ManagedAgentExecutor,
    ManagedAgentRequest,
    acquire_pooled_agent_runtime,
)


class _FakeAgent:
    """按调用轮次提供预设事件的 LangGraph fake。"""

    def __init__(self, rounds: list[list[object] | BaseException]) -> None:
        self._rounds = list(rounds)
        self.calls: list[dict[str, object]] = []

    async def astream(self, stream_input: object, **kwargs: object):
        """记录调用参数，并产出当前模型回合的事件。"""
        self.calls.append({"input": stream_input, **kwargs})
        outcome = self._rounds.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        for event in outcome:
            yield event


class _Lifecycle:
    """记录 Interaction resume 是否显式标记为非初始模型回合。"""

    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule(self, reason: str) -> None:
        """记录 Context 生命周期原因。"""
        self.scheduled.append(reason)


class _Runtime:
    """将 Agent、checkpoint 配置和 lease release 聚合为测试 runtime。"""

    def __init__(self, agent: object | None, *, context: object | None = None) -> None:
        self.agent = agent
        self.run_context = context
        self.checkpoint_namespaces: list[str] = []
        self.release_calls = 0

    def graph_config(self, namespace: str) -> dict[str, object]:
        """生成可断言的 checkpoint namespace。"""
        self.checkpoint_namespaces.append(namespace)
        return {"configurable": {"thread_id": namespace}}

    async def release(self) -> None:
        """模拟同时释放 AgentEngine run lease 和 engine lease。"""
        self.release_calls += 1


class _Observer:
    """记录 shared execution stream 与模型回合回调。"""

    def __init__(self) -> None:
        self.signals: list[ExecutionSignal] = []
        self.interactions: list[object] = []
        self.messages: list[object] = []
        self.model_rounds = 0
        self.stream_events = 0

    def on_model_round(self) -> None:
        """记录 executor 开始一个模型回合。"""
        self.model_rounds += 1

    async def on_execution_complete(self, _result: object) -> None:
        """满足 runtime release 前的最终投影 seam。"""
        return None

    def emit(self, signal: ExecutionSignal) -> None:
        """接收共享 stream 产生的领域信号。"""
        self.signals.append(signal)

    async def interact(self, request: object) -> object:
        """返回一个确定性的问答结果。"""
        self.interactions.append(request)
        return {"answers": {"question-1": ["继续"]}}

    async def observe_message(self, chunk: object, _session: StreamSession) -> bool:
        """记录 raw chunk；本组不需要 Tool flush。"""
        self.messages.append(chunk)
        return False

    async def after_tool_boundary(self) -> None:
        """满足 shared stream observer 契约。"""
        return None

    def on_stream_event(self) -> None:
        """记录每个 LangGraph event 的 context drain 边界。"""
        self.stream_events += 1


class _PoolRunLease:
    """记录每次 managed execution 的 run lease release。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def release(self) -> None:
        """按 runtime 清理顺序记录 run lease。"""
        self._events.append("run")


class _PoolLease:
    """提供 graph 与 Engine lease 的最小 fake。"""

    def __init__(self, graph: object, events: list[str], *, cancel_on_run: bool = False) -> None:
        self.engine = type("Engine", (), {"graph": graph})()
        self._events = events
        self._cancel_on_run = cancel_on_run

    async def run(self) -> _PoolRunLease:
        """创建 run lease，或模拟刚取得 engine lease 后的取消。"""
        if self._cancel_on_run:
            raise asyncio.CancelledError
        return _PoolRunLease(self._events)

    async def release(self) -> None:
        """记录 Engine lease release。"""
        self._events.append("engine")


class _Pool:
    """供 pooled runtime contract 使用的 AgentEnginePool fake。"""

    def __init__(self, lease: _PoolLease, events: list[str]) -> None:
        self._lease = lease
        self._events = events

    async def acquire(self, _profile: object) -> _PoolLease:
        """返回预设 Engine lease。"""
        return self._lease

    async def finalize_draining(self, _profile_key: str) -> None:
        """记录 release 后的 pool 排空检查。"""
        self._events.append("finalize")


def _request(
    runtime: _Runtime,
    *,
    output_policy: str = "passthrough",
    is_cancelled=lambda: False,
    usage: dict[str, int] | None = None,
    timeout_seconds: float | None = None,
    execution_starter=None,
) -> ManagedAgentRequest:
    """创建包含稳定 execution/checkpoint/idempotency 事实的请求。"""

    async def acquire_runtime() -> _Runtime:
        return runtime

    return ManagedAgentRequest(
        execution_ref="root-run-1",
        parent_execution_ref=None,
        run_id="run-1",
        input="修复当前问题",
        checkpoint_namespace="thread-1",
        output_policy=output_policy,
        runtime_provider=acquire_runtime,
        is_cancelled=is_cancelled,
        idempotency_key="run:run-1",
        agent_spec={"role": "main"},
        required_skill_snapshot_ids=("snapshot-1",),
        usage=usage,
        timeout_seconds=timeout_seconds,
        execution_starter=execution_starter,
    )


@pytest.mark.asyncio
async def test_pooled_runtime_hides_engine_graph_and_releases_run_then_engine() -> None:
    """Pooled runtime 只向 executor 暴露 graph seam，并收敛两层 lease。"""
    events: list[str] = []
    graph = _FakeAgent([[("messages", (AIMessageChunk(content="完成"), {}))]])
    pool = _Pool(_PoolLease(graph, events), events)
    profile = type("Profile", (), {"profile_key": "profile-1"})()

    runtime = await acquire_pooled_agent_runtime(
        pool=pool,
        profile=profile,
        run_context=None,
        graph_config=lambda namespace: {"configurable": {"thread_id": namespace}},
    )
    result = await ManagedAgentExecutor().execute(
        _request(runtime), _Observer()
    )

    assert result.final_content == "完成"
    assert events == ["run", "engine", "finalize"]
    assert graph.calls[0]["config"] == {"configurable": {"thread_id": "thread-1"}}


@pytest.mark.asyncio
async def test_pooled_runtime_releases_engine_when_run_lease_acquire_is_cancelled() -> None:
    """Engine lease 已取得但 run lease 被取消时也必须立即回收。"""
    events: list[str] = []
    pool = _Pool(_PoolLease(object(), events, cancel_on_run=True), events)
    profile = type("Profile", (), {"profile_key": "profile-1"})()

    with pytest.raises(asyncio.CancelledError):
        await acquire_pooled_agent_runtime(
            pool=pool,
            profile=profile,
            run_context=None,
            graph_config=lambda _namespace: {},
        )

    assert events == ["engine", "finalize"]


@pytest.mark.asyncio
async def test_executor_owns_runtime_release_checkpoint_and_passthrough_stream() -> None:
    """成功路径由 executor 创建 checkpoint 配置、转发 stream 并释放 lease。"""
    agent = _FakeAgent(
        [
            [
                (
                    "messages",
                    (
                        AIMessageChunk(
                            content="已完成",
                            usage_metadata={
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "total_tokens": 5,
                            },
                        ),
                        {},
                    ),
                )
            ]
        ]
    )
    runtime = _Runtime(agent)
    observer = _Observer()
    usage = {"input_tokens": 0, "output_tokens": 0}
    started: list[str] = []

    async def start_execution(execution_ref: str) -> None:
        started.append(execution_ref)

    result = await ManagedAgentExecutor().execute(
        _request(runtime, usage=usage, execution_starter=start_execution), observer
    )

    assert started == ["root-run-1"]
    assert runtime.checkpoint_namespaces == ["thread-1"]
    assert runtime.release_calls == 1
    assert observer.model_rounds == 1
    assert [signal.type for signal in observer.signals] == ["content.delta"]
    assert result.final_content == "已完成"
    assert result.usage == {"input_tokens": 3, "output_tokens": 2}
    assert usage == {"input_tokens": 3, "output_tokens": 2}
    first_input = agent.calls[0]["input"]
    assert first_input["messages"][0].content == "修复当前问题"
    assert agent.calls[0]["config"] == {"configurable": {"thread_id": "thread-1"}}


@pytest.mark.asyncio
async def test_executor_resumes_interaction_in_same_runtime_and_marks_context_lifecycle() -> None:
    """Interaction 只复用本次 runtime/session，并标记 resume 模型回合。"""
    interrupt = type(
        "Interrupt",
        (),
        {
            "id": "question-1",
            "value": {
                "type": "ask_user",
                "questions": [{"question": "是否继续？", "choices": []}],
            },
        },
    )()
    agent = _FakeAgent(
        [
            [("updates", {"__interrupt__": [interrupt]})],
            [("messages", (AIMessageChunk(content="继续完成"), {}))],
        ]
    )
    lifecycle = _Lifecycle()
    runtime = _Runtime(agent, context=type("RunContext", (), {"model_call_lifecycle": lifecycle})())
    observer = _Observer()

    result = await ManagedAgentExecutor().execute(_request(runtime), observer)

    assert result.final_content == "继续完成"
    assert runtime.release_calls == 1
    assert observer.model_rounds == 2
    assert len(observer.interactions) == 1
    assert lifecycle.scheduled == ["interaction_resume"]
    assert type(agent.calls[1]["input"]).__name__ == "Command"


@pytest.mark.asyncio
async def test_executor_maps_stream_errors_and_releases_runtime_on_every_failure() -> None:
    """stream 错误、取消和 task cancel 都不能遗留 run/engine lease。"""
    cases: list[tuple[object, object]] = [
        (RuntimeError("boom"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ]
    for failure, expected in cases:
        runtime = _Runtime(_FakeAgent([failure]))
        with pytest.raises(expected):
            await ManagedAgentExecutor().execute(_request(runtime), _Observer())
        assert runtime.release_calls == 1

    cancelled_runtime = _Runtime(_FakeAgent([[]]))
    with pytest.raises(ManagedAgentExecutionError) as error:
        await ManagedAgentExecutor().execute(
            _request(cancelled_runtime, is_cancelled=lambda: True), _Observer()
        )
    assert error.value.code == "RUN_CANCELLED"
    assert cancelled_runtime.release_calls == 1


@pytest.mark.asyncio
async def test_executor_normalizes_timeout_and_releases_runtime() -> None:
    """超时覆盖整个 managed execution，并在取消 stream 后释放 lease。"""

    class _SlowAgent:
        async def astream(self, *_args: object, **_kwargs: object):
            await asyncio.sleep(0.02)
            yield ("messages", (AIMessageChunk(content="too late"), {}))

    runtime = _Runtime(_SlowAgent())
    with pytest.raises(ManagedAgentExecutionError) as error:
        await ManagedAgentExecutor().execute(
            _request(runtime, timeout_seconds=0.001), _Observer()
        )

    assert error.value.code == "MANAGED_AGENT_TIMEOUT"
    assert runtime.release_calls == 1


@pytest.mark.asyncio
async def test_executor_preserves_success_when_runtime_release_reports_error() -> None:
    """release 诊断失败不能倒置已经完成的 Build 终态。"""

    class _ReleaseFailureRuntime(_Runtime):
        async def release(self) -> None:
            self.release_calls += 1
            raise RuntimeError("release failed")

    runtime = _ReleaseFailureRuntime(
        _FakeAgent([[("messages", (AIMessageChunk(content="完成"), {}))]])
    )

    result = await ManagedAgentExecutor().execute(_request(runtime), _Observer())

    assert result.final_content == "完成"
    assert runtime.release_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output_policy", ["capture_only", "structured"])
async def test_nonpassthrough_output_captures_final_content_without_content_delta(
    output_policy: str,
) -> None:
    """capture_only/structured 复用静默正文流，保留结果给 adapter 处理。"""
    runtime = _Runtime(
        _FakeAgent([[("messages", (AIMessageChunk(content='{"ready":true}'), {}))]])
    )
    observer = _Observer()

    result = await ManagedAgentExecutor().execute(
        _request(runtime, output_policy=output_policy), observer
    )

    assert result.output_policy == output_policy
    assert result.final_content == '{"ready":true}'
    assert observer.signals == []
    assert runtime.release_calls == 1


@pytest.mark.asyncio
async def test_executor_returns_echo_content_when_runtime_has_no_agent() -> None:
    """无 Agent 的协议测试路径不进入 graph，调用方仍可投影原消息。"""
    runtime = _Runtime(None)

    result = await ManagedAgentExecutor().execute(_request(runtime), _Observer())

    assert result.used_agent is False
    assert result.final_content == "修复当前问题"
    assert runtime.checkpoint_namespaces == []
    assert runtime.release_calls == 1
