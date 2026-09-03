"""ManagedAgentExecutor 的共享执行契约测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from harness_agent.runtime.execution_stream import ExecutionSignal, StreamSession
from harness_agent.runtime.managed_agent_executor import (
    FailClosedManagedObserver,
    ManagedAgentExecutionError,
    ManagedAgentExecutor,
    ManagedAgentResult,
    ManagedAgentRequest,
    ManagedChildObserver,
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

    def __init__(
        self,
        agent: object | None,
        *,
        context: object | None = None,
        acquire_source: str = "reused",
        queue_ms: int = 0,
        build_ms: int = 0,
    ) -> None:
        self.agent = agent
        self.run_context = context
        self.acquire_source = acquire_source
        self.queue_ms = queue_ms
        self.build_ms = build_ms
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
    diagnostic_log=None,
    timing=None,
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
        diagnostic_log=diagnostic_log,
        timing=timing,
        model_profile_id="profile-main",
    )


class _FakeClock:
    """Managed executor 计时测试使用的 monotonic clock。"""

    def __init__(self) -> None:
        self.value = 1.0

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, milliseconds: int) -> None:
        self.value += milliseconds / 1000


class _RecordingLog:
    """记录 executor 发出的结构化 Diagnostic Event。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, fields: dict[str, object]) -> None:
        self.records.append(("info", event, dict(fields)))

    def warn(self, event: str, fields: dict[str, object]) -> None:
        self.records.append(("warn", event, dict(fields)))

    def error(self, event: str, fields: dict[str, object]) -> None:
        self.records.append(("error", event, dict(fields)))

    def debug(self, event: str, fields: dict[str, object]) -> None:
        self.records.append(("debug", event, dict(fields)))


class _Timing:
    """只记录 retry 等待累计的最小 timing port。"""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.retry_wait_ms = 0

    def begin_active(self) -> float:
        return self.clock()

    def end_active(self, _started_at: float) -> None:
        return None

    def begin_wait(self) -> float:
        return self.clock()

    def end_retry_wait(self, started_at: float) -> None:
        self.retry_wait_ms += round((self.clock() - started_at) * 1000)


@pytest.mark.asyncio
async def test_executor_logs_runtime_model_attempts_retry_and_nullable_usage() -> None:
    """每个 provider attempt 独立成对，retry 等待不计入 attempt duration。"""

    class _ProviderFailure(RuntimeError):
        code = "PROVIDER_BUSY"

    class _RetryOnce:
        def should_retry(self, attempt: int, _error: BaseException) -> bool:
            return attempt == 1

        def retry_delay_seconds(self, _error: BaseException) -> float:
            return 0.02

    clock = _FakeClock()

    async def sleep(seconds: float) -> None:
        clock.advance_ms(round(seconds * 1000))

    runtime = _Runtime(
        _FakeAgent(
            [
                _ProviderFailure("CANARY_EXCEPTION_MESSAGE"),
                [("updates", {"heartbeat": True})],
            ]
        ),
        acquire_source="new",
        build_ms=4,
    )
    log = _RecordingLog()
    timing = _Timing(clock)
    request = _request(runtime, diagnostic_log=log, timing=timing)
    object.__setattr__(request, "provider_retry", _RetryOnce())

    result = await ManagedAgentExecutor(clock=clock, sleep=sleep).execute(
        request,
        _Observer(),
    )

    assert result.used_agent is True
    assert [event for _, event, _ in log.records] == [
        "runtime.acquire.completed",
        "model.started",
        "model.failed",
        "model.retry_scheduled",
        "model.started",
        "model.completed",
        "runtime.released",
    ]
    attempts = [fields for _, event, fields in log.records if event == "model.started"]
    assert [(item["model_round"], item["provider_attempt"]) for item in attempts] == [
        (1, 1),
        (1, 2),
    ]
    completed = next(fields for _, event, fields in log.records if event == "model.completed")
    assert completed["provider_first_chunk_ms"] is None
    assert completed["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
    }
    assert timing.retry_wait_ms == 20
    assert "CANARY_EXCEPTION_MESSAGE" not in repr(log.records)


def test_managed_agent_execution_error_exposes_stable_message() -> None:
    """Host 转换 managed 错误时可直接读取原始信息。"""
    error = ManagedAgentExecutionError(
        "TOOL_CALL_ID_UNAVAILABLE",
        "Tool result cannot be associated safely",
    )

    assert error.code == "TOOL_CALL_ID_UNAVAILABLE"
    assert error.message == "Tool result cannot be associated safely"
    assert str(error) == error.message


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
async def test_fail_closed_observer_rejects_unbound_interaction() -> None:
    """无 UI/Interaction adapter 的 Plugin execution 不能静默自动批准。"""
    observer = FailClosedManagedObserver()

    with pytest.raises(ManagedAgentExecutionError) as error:
        await observer.interact(object())

    assert error.value.code == "MANAGED_AGENT_INTERACTION_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plugin_format",
    ["qwen-code", "claude-code", "agent-plugins-1.0"],
)
async def test_managed_child_observer_shares_identity_and_signal_contract(
    plugin_format: str,
) -> None:
    """Qwen/Claude/portable Managed adapter 共用同一 child observer 契约。"""
    events: list[tuple[str, dict[str, object], str | None, str | None, str | None]] = []
    execution_id = f"child-{plugin_format}"
    parent_execution_id = "root-parent"
    agent_id = f"{plugin_format}-agent"

    def event_port(
        event_type: str,
        payload: Mapping[str, object],
        child_id: str | None,
        parent_id: str | None,
        emitted_agent_id: str | None,
    ) -> None:
        events.append((event_type, payload, child_id, parent_id, emitted_agent_id))

    observer = ManagedChildObserver(
        event_port=event_port,
        execution_ref=execution_id,
        parent_execution_ref=parent_execution_id,
        agent_id=agent_id,
    )
    observer.emit(ExecutionSignal("content.delta", {"text": "private stream content"}))
    observer.emit(ExecutionSignal("reasoning.delta", {"text": "reasoning"}))
    observer.emit(ExecutionSignal("tool.started", {"tool_call_id": "tool-1", "name": "glob"}))
    observer.emit(ExecutionSignal("tool.delta", {"tool_call_id": "tool-1", "arguments_delta": "{}"}))
    observer.emit(ExecutionSignal("tool.completed", {"tool_call_id": "tool-1", "result": {}}))
    await observer.on_execution_complete(
        ManagedAgentResult(
            final_content="PLUGIN_FINAL",
            usage={},
            output_policy="capture_only",
            used_agent=True,
        )
    )
    await observer.on_execution_complete(
        ManagedAgentResult(
            final_content="DUPLICATE",
            usage={},
            output_policy="capture_only",
            used_agent=True,
        )
    )

    assert [event[0] for event in events] == [
        "reasoning.delta",
        "tool.started",
        "tool.delta",
        "tool.completed",
        "content.delta",
    ]
    assert all(event[2:] == (execution_id, parent_execution_id, agent_id) for event in events)
    assert events[-1][1] == {"text": "PLUGIN_FINAL"}
    assert await observer.observe_message(object(), StreamSession(run_id="child-run")) is False
    with pytest.raises(ManagedAgentExecutionError) as error:
        await observer.interact(object())
    assert error.value.code == "MANAGED_AGENT_INTERACTION_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_content", ["", "   "])
async def test_managed_child_observer_does_not_fake_empty_final_content(
    final_content: str,
) -> None:
    """Managed child 空正文不生成假的 content.delta。"""
    events: list[str] = []

    def event_port(
        event_type: str,
        _payload: Mapping[str, object],
        _execution_id: str | None,
        _parent_execution_id: str | None,
        _agent_id: str | None,
    ) -> None:
        events.append(event_type)

    observer = ManagedChildObserver(
        event_port=event_port,
        execution_ref="child-empty",
        parent_execution_ref="root-parent",
        agent_id="portable-agent",
    )
    await observer.on_execution_complete(
        ManagedAgentResult(
            final_content=final_content,
            usage={},
            output_policy="capture_only",
            used_agent=True,
        )
    )

    assert events == []


def test_managed_child_observers_keep_sibling_event_ports_isolated() -> None:
    """并发 sibling 各自冻结 event port，不能按 agent id 抢占彼此通道。"""
    destinations: dict[str, list[tuple[str, str | None, str | None, str | None]]] = {
        "a": [],
        "b": [],
    }

    def make_port(destination: str):
        def event_port(
            event_type: str,
            _payload: Mapping[str, object],
            execution_id: str | None,
            parent_execution_id: str | None,
            agent_id: str | None,
        ) -> None:
            destinations[destination].append(
                (event_type, execution_id, parent_execution_id, agent_id)
            )

        return event_port

    observer_a = ManagedChildObserver(
        event_port=make_port("a"),
        execution_ref="child-a",
        parent_execution_ref="root-parent",
        agent_id="same-agent-id",
    )
    observer_b = ManagedChildObserver(
        event_port=make_port("b"),
        execution_ref="child-b",
        parent_execution_ref="root-parent",
        agent_id="same-agent-id",
    )

    observer_a.emit(ExecutionSignal("tool.started", {"name": "glob"}))
    observer_b.emit(ExecutionSignal("tool.started", {"name": "grep"}))

    assert destinations["a"] == [("tool.started", "child-a", "root-parent", "same-agent-id")]
    assert destinations["b"] == [("tool.started", "child-b", "root-parent", "same-agent-id")]


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
