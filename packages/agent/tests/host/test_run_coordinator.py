"""RunCoordinator 的领域生命周期测试，不依赖 AgentHost 或 JSON-RPC transport。"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from harness_agent.host.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    RunCoordinator,
    RunError,
    RunPreparation,
    RunRuntime,
    RunState,
    RunTimingLedger,
    StartRun,
)
from harness_agent.runtime.interactions import InteractionRequest


def test_compose_engine_only_mutates_run_lifecycle_through_port() -> None:
    """Compose engine 不直接修改 Host Run 状态或 Transcript 队列。"""
    from harness_agent.compose.work_item_engine import ComposeWorkItemEngine

    source = inspect.getsource(ComposeWorkItemEngine)
    assert "run.status =" not in source
    assert "run.pending_transcript" not in source
from harness_agent.host.run_execution import (
    MAX_TOOL_PAYLOAD_BYTES,
    _capture_transcript_message,
    _extract_interaction,
    _message_text,
    _translate_stream_event,
)
from harness_agent.runtime.execution_binding import ExecutionRef
from harness_agent.compose.models import ThreadMode
from harness_agent.threads.thread_persistence import (
    ThreadPersistence,
    ThreadPersistenceError,
    TranscriptAppend,
)
from tests.support.thread_fixtures import test_binding as make_test_binding


class _NoopInteraction:
    """测试用 InteractionPort；本组用例不需要真正的反向请求。"""

    async def request(self, _owner, _run, _interaction) -> InteractionResult:
        return InteractionResult({"decision": "reject"})


def _coordinator(releases: list[str]) -> RunCoordinator:
    async def persistence_provider():
        return None

    async def preparation_provider(_command, _persistence):
        return RunPreparation()

    async def runtime_provider(run) -> RunRuntime:
        async def release() -> None:
            releases.append(run.ref.run_id)

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    return RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )


async def _events(execution) -> list:
    return [event async for event in execution.events]


class _FakeClock:
    """可手动推进的 monotonic clock，避免时序测试依赖真实等待。"""

    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, milliseconds: int) -> None:
        self.value += milliseconds / 1000


class _RecordingDiagnosticLog:
    """记录结构化日志调用，同时模拟 child context 合并。"""

    def __init__(self, records=None, context=None) -> None:
        self.records = records if records is not None else []
        self.context = dict(context or {})

    def child(self, context):
        return _RecordingDiagnosticLog(self.records, {**self.context, **context})

    def info(self, event, fields) -> None:
        self.records.append(("info", event, self.context, dict(fields)))

    def warn(self, event, fields) -> None:
        self.records.append(("warn", event, self.context, dict(fields)))

    def error(self, event, fields) -> None:
        self.records.append(("error", event, self.context, dict(fields)))

    def debug(self, event, fields) -> None:
        self.records.append(("debug", event, self.context, dict(fields)))


def test_run_timing_ledger_unions_active_intervals_and_separates_waits() -> None:
    """并发 active 区间只计并集，Interaction/retry 等待独立累计。"""
    clock = _FakeClock()
    ledger = RunTimingLedger(clock=clock, started_at=clock())

    first = ledger.begin_active()
    clock.advance_ms(10)
    second = ledger.begin_active()
    clock.advance_ms(10)
    ledger.end_active(first)
    clock.advance_ms(10)
    ledger.end_active(second)
    interaction = ledger.begin_wait()
    clock.advance_ms(7)
    ledger.end_interaction_wait(interaction)
    retry = ledger.begin_wait()
    clock.advance_ms(5)
    ledger.end_retry_wait(retry)
    ledger.mark_first_visible()

    assert ledger.snapshot() == {
        "duration_ms": 42,
        "active_ms": 30,
        "interaction_wait_ms": 7,
        "retry_wait_ms": 5,
        "first_visible_activity_ms": 42,
    }


@pytest.mark.asyncio
async def test_run_coordinator_emits_one_diagnostic_terminal_with_timing() -> None:
    """Run 日志带稳定关联身份、唯一终态和 monotonic 时间账本。"""
    clock = _FakeClock()
    log = _RecordingDiagnosticLog()

    async def runtime_provider(run) -> RunRuntime:
        clock.advance_ms(12)

        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=lambda _command, _persistence: _noop_preparation(),
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
        diagnostic_log=log,
        clock=clock,
    )
    execution = await coordinator.start(
        StartRun(mode="build", thread_id="thread", run_id="run-1", message="hello"),
        ConnectionRef("owner"),
    )

    await _events(execution)

    run_records = [record for record in log.records if record[1].startswith("run.")]
    assert [record[1] for record in run_records] == ["run.started", "run.completed"]
    assert run_records[0][2] == {
        "thread_id": "thread",
        "run_id": "run-1",
        "execution_id": "root-run-1",
        "agent_id": "main",
    }
    terminal = run_records[1][3]
    assert terminal["duration_ms"] == 12
    assert terminal["active_ms"] == 12
    assert terminal["interaction_wait_ms"] == 0
    assert terminal["retry_wait_ms"] == 0
    assert terminal["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": None,
    }


@pytest.mark.asyncio
async def test_run_acceptance_logs_bounded_catalog_projection() -> None:
    """Run 受理记录实际目录计数；超过 32 个 ID 时不写有界列表。"""
    log = _RecordingDiagnosticLog()

    async def prepare(_command, _persistence) -> RunPreparation:
        return RunPreparation(
            catalog_skill_ids=tuple(f"skill-{index}" for index in range(33)),
            catalog_mcp_ids=("filesystem",),
            catalog_plugin_ids=("review-plugin",),
        )

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=prepare,
        runtime_provider=_noop_runtime,
        interaction_port=_NoopInteraction(),
        diagnostic_log=log,
    )
    execution = await coordinator.start(
        StartRun(mode="build", thread_id="thread", run_id="run-catalog", message="hello"),
        ConnectionRef("owner"),
    )
    await _events(execution)

    record = next(record for record in log.records if record[1] == "catalog.bound")
    assert record[2]["execution_id"] == "root-run-catalog"
    assert record[3] == {
        "skill_count": 33,
        "mcp_count": 1,
        "plugin_count": 1,
        "mcp_ids": ["filesystem"],
        "plugin_ids": ["review-plugin"],
    }


@pytest.mark.asyncio
async def test_run_duration_starts_after_preparation_when_run_is_accepted() -> None:
    """Run 总耗时从受理完成起算，不把受理前准备静默计入 Run。"""
    clock = _FakeClock()
    log = _RecordingDiagnosticLog()

    async def prepare(_command, _persistence) -> RunPreparation:
        clock.advance_ms(7000)
        return RunPreparation()

    async def runtime_provider(run) -> RunRuntime:
        clock.advance_ms(12)

        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=prepare,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
        diagnostic_log=log,
        clock=clock,
    )

    execution = await coordinator.start(
        StartRun(mode="build", thread_id="thread", run_id="run-prepared", message="hello"),
        ConnectionRef("owner"),
    )
    await _events(execution)

    terminal = next(record for record in log.records if record[1] == "run.completed")
    assert terminal[3]["duration_ms"] == 12


@pytest.mark.asyncio
async def test_run_diagnostic_failure_does_not_change_terminal() -> None:
    """日志 adapter 抛错时 Run 仍按原语义成功并只产生一个业务终态。"""

    class _FailingLog(_RecordingDiagnosticLog):
        def child(self, context):
            return self

        def info(self, event, fields) -> None:
            raise RuntimeError("diagnostic unavailable")

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=lambda _command, _persistence: _noop_preparation(),
        runtime_provider=_noop_runtime,
        interaction_port=_NoopInteraction(),
        diagnostic_log=_FailingLog(),
    )
    execution = await coordinator.start(
        StartRun(mode="build", thread_id="thread", run_id="run-log-fail", message="hello"),
        ConnectionRef("owner"),
    )

    events = await _events(execution)

    assert [event.type for event in events][-1] == "run.completed"


@pytest.mark.asyncio
async def test_request_interaction_logs_wait_and_omits_payload() -> None:
    """Interaction 等待单独累计，日志不含 payload 或 canary。"""
    clock = _FakeClock()
    log = _RecordingDiagnosticLog()

    class _SlowInteraction:
        async def request(self, _owner, _run, _interaction) -> InteractionResult:
            clock.advance_ms(15)
            return InteractionResult({"decision": "approve"})

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=lambda _command, _persistence: _noop_preparation(),
        runtime_provider=_noop_runtime,
        interaction_port=_SlowInteraction(),
        diagnostic_log=log,
        clock=clock,
    )
    run = RunState(
        start=StartRun(mode="build", thread_id="thread", run_id="run-int", message="hello"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
        started_at=clock(),
        timing=RunTimingLedger(clock=clock, started_at=clock()),
        diagnostic_log=log.child({"thread_id": "thread", "run_id": "run-int"}),
    )

    result = await coordinator._lifecycle_port.request_interaction(
        run,
        InteractionRequest(
            request_id="approval-1",
            type="approval",
            payload={"secret": "CANARY_HC163_INTERACTION"},
            interrupt_id="i-1",
        ),
    )

    assert result.value == {"decision": "approve"}
    interaction = [record for record in log.records if record[1].startswith("interaction.")]
    assert [record[1] for record in interaction] == [
        "interaction.started",
        "interaction.completed",
    ]
    completed = interaction[1][3]
    assert completed["kind"] == "approval"
    assert completed["outcome"] == "approve"
    assert completed["wait_ms"] == 15
    assert run.timing is not None
    assert run.timing.snapshot()["interaction_wait_ms"] == 15
    assert run.timing.snapshot()["active_ms"] == 0
    assert "CANARY_HC163_INTERACTION" not in repr(log.records)


@pytest.mark.asyncio
async def test_run_coordinator_enforces_owner_busy_and_single_terminal_event() -> None:
    """生命周期边界由 Coordinator interface 负责，不需要构造 Server。"""
    releases: list[str] = []
    coordinator = _coordinator(releases)
    owner = ConnectionRef("owner")
    other = ConnectionRef("other")

    execution = await coordinator.start(
        StartRun(mode="build", thread_id="thread", run_id="run-1", message="hello"),
        owner,
    )
    with pytest.raises(RunError, match="THREAD_BUSY") as busy:
        await coordinator.start(
            StartRun(mode="build", thread_id="thread", run_id="run-2", message="busy"),
            owner,
        )
    assert busy.value.code == "THREAD_BUSY"
    with pytest.raises(RunError) as not_owner:
        await coordinator.cancel(execution.ref, other)
    assert not_owner.value.code == "RUN_NOT_OWNER"

    cancelled = await coordinator.cancel(execution.ref, owner)
    assert cancelled.cancelled is True
    events = await _events(execution)
    assert [event.type for event in events] == ["run.cancelled"]
    assert events[0].execution_id == "root-run-1"
    assert await coordinator.execution_registry.list(
        ExecutionRef.root("thread", "run-1")
    ) == ()


@pytest.mark.asyncio
async def test_run_coordinator_releases_runtime_and_completes_once() -> None:
    """正常执行只发一个 completed 终态，并释放本次 Runtime。"""
    releases: list[str] = []
    coordinator = _coordinator(releases)
    execution = await coordinator.start(
        StartRun(mode="build", thread_id="thread", run_id="run-1", message="hello"),
        ConnectionRef("owner"),
    )

    events = await _events(execution)
    assert [event.type for event in events] == [
        "run.started",
        "run.progress",
        "content.delta",
        "run.completed",
    ]
    assert {event.execution_id for event in events} == {"root-run-1"}
    assert {event.agent_id for event in events} == {"main"}
    assert events[0].record()["execution_id"] == "root-run-1"
    assert releases == ["run-1"]
    assert await coordinator.is_active("thread") is False
    assert await coordinator.execution_registry.list(
        ExecutionRef.root("thread", "run-1")
    ) == ()


@pytest.mark.asyncio
async def test_lifecycle_port_assigns_shared_sequence_for_scoped_child_activity() -> None:
    """root/child activity 共用 Run sequence；compose_scope 进入 wire record。"""
    from harness_agent.host.run_execution import COMPOSE_SUMMARY, RUN_PROGRESS

    coordinator = _coordinator([])
    run = RunState(
        start=StartRun(mode="compose", thread_id="thread-scope", run_id="run-scope", message="组合"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(
            execution_binding=make_test_binding("thread-scope", "run-scope")
        ),
    )
    run.root_execution = coordinator._root_execution_binding(run.start, run.preparation)
    port = coordinator._lifecycle_port
    scope = {
        "activity_id": "act-understand-1",
        "stage": "understand",
        "attempt": 1,
    }
    port.emit(
        run,
        RUN_PROGRESS,
        {"phase": "model", "elapsed_ms": 5},
        execution_id="child-understand-1",
        parent_execution_id=run.root_execution_ref.execution_id,
        agent_id="understand",
        compose_scope=scope,
    )
    port.emit(
        run,
        COMPOSE_SUMMARY,
        {"status": "passed", "text": "理解完成"},
        execution_id="child-understand-1",
        parent_execution_id=run.root_execution_ref.execution_id,
        agent_id="understand",
        compose_scope=scope,
    )
    # 终态后迟到事件被拒绝（静默丢弃），sequence 不再前进。
    run.terminal_event_emitted = True
    port.emit(run, RUN_PROGRESS, {"phase": "model", "elapsed_ms": 9})

    events = []
    while not run.events.empty():
        events.append(run.events.get_nowait())
    assert [event.sequence for event in events] == [1, 2]
    assert events[0].execution_id == "child-understand-1"
    assert events[0].compose_scope == scope
    assert events[0].record()["compose_scope"] == scope
    assert events[1].type == COMPOSE_SUMMARY



@pytest.mark.asyncio
async def test_run_coordinator_limits_second_connection_run_without_multithread() -> None:
    """无 run.multithread 时，同一 Connection 的第二个 Run 被拒且不影响他人。"""
    gate = asyncio.Event()

    async def slow_preparation(_command, _persistence):
        await gate.wait()
        return RunPreparation()

    coordinator = RunCoordinator(
        persistence_provider=lambda: _noop_persistence(),
        preparation_provider=slow_preparation,
        runtime_provider=lambda run: _noop_runtime(run),
        interaction_port=_NoopInteraction(),
    )
    owner = ConnectionRef("owner")
    start_task = asyncio.create_task(
        coordinator.start(
            StartRun(mode="build", thread_id="thread-1", run_id="run-1", message="first"),
            owner,
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(RunError) as busy:
        await coordinator.start(
            StartRun(mode="build", thread_id="thread-2", run_id="run-2", message="second"),
            owner,
        )
    assert busy.value.code == "CONNECTION_RUN_BUSY"
    assert busy.value.retryable is True

    # 其他 Connection 不受限制。
    other_task = asyncio.create_task(
        coordinator.start(
            StartRun(mode="build", thread_id="thread-2", run_id="run-2", message="second"),
            ConnectionRef("other"),
        )
    )
    await asyncio.sleep(0)
    gate.set()
    await start_task
    other = await other_task
    await coordinator.cancel(other.ref, ConnectionRef("other"))
    await _events(other)


@pytest.mark.asyncio
async def test_same_run_different_work_mode_conflicts() -> None:
    """同一 thread/run 以不同工作模式重新受理是稳定冲突，不能幂等复用。"""
    gate = asyncio.Event()

    class _BlockingAgent:
        async def astream(self, *_args: Any, **_kwargs: Any):
            await gate.wait()
            if False:
                yield None

    async def blocking_runtime(run):
        async def release() -> None:
            return None

        return RunRuntime(
            agent=_BlockingAgent(),
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    async def noop_preparation(_command, _persistence) -> RunPreparation:
        return RunPreparation()

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=noop_preparation,
        runtime_provider=blocking_runtime,
        interaction_port=_NoopInteraction(),
    )
    owner = ConnectionRef("owner")
    first = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello", mode="build"),
        owner,
    )
    # 相同 mode 的幂等重试仍被受理，不产生事件。
    retried = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello", mode="build"),
        owner,
    )
    assert retried.accepted is True
    with pytest.raises(RunError) as conflict:
        await coordinator.start(
            StartRun(thread_id="thread", run_id="run-1", message="hello", mode="compose"),
            owner,
        )
    assert conflict.value.code == "RUN_ID_CONFLICT"
    gate.set()
    events = await _events(first)
    assert [event.type for event in events][-1] == "run.completed"
    await coordinator.close()


@pytest.mark.asyncio
async def test_adapter_terminal_signal_rejected_by_coordinator() -> None:
    """adapter 通过 lifecycle port 发终态事件必须被拒绝，终态只由 coordinator 产生。"""
    class _MalformedAdapter:
        async def execute(self, run, port):
            # 恶意 adapter 试图自己发出 run.completed，必须被 port 拒绝。
            port.emit(run, "run.completed", {"usage": {"input_tokens": 0, "output_tokens": 0}})

    async def prep(_command, _persistence) -> RunPreparation:
        return RunPreparation()

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=prep,
        runtime_provider=_noop_runtime,
        interaction_port=_NoopInteraction(),
    )
    coordinator._execution_adapters["build"] = _MalformedAdapter()
    execution = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello", mode="build"),
        ConnectionRef("owner"),
    )
    events = await _events(execution)
    assert [event.type for event in events] == ["run.failed"]
    assert events[0].payload["error"]["code"] == "ADAPTER_TERMINAL_VIOLATION"
    await coordinator.close()


@pytest.mark.asyncio
async def test_compose_adapter_stub_has_single_terminal() -> None:
    """Compose 空壳在完整实现前只以稳定错误收敛，不产生第二套生命周期。"""
    coordinator = _coordinator([])
    execution = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello", mode="compose"),
        ConnectionRef("owner"),
    )
    events = await _events(execution)
    assert [event.type for event in events] == ["run.failed"]
    assert events[0].payload["error"]["code"] == "COMPOSE_ADAPTER_NOT_READY"
    await coordinator.close()


@pytest.mark.asyncio
async def test_coordinator_persists_start_mode_as_thread_mode(tmp_path) -> None:
    """真实 StartRun 必须把 mode 传入同一受理事务，不能默认为 build。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)

    async def prepare(command, _persistence) -> RunPreparation:
        return RunPreparation(
            execution_binding=make_test_binding(command.thread_id, command.run_id)
        )

    async def persistence_provider() -> ThreadPersistence:
        return persistence

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=prepare,
        runtime_provider=_noop_runtime,
        interaction_port=_NoopInteraction(),
    )
    try:
        execution = await coordinator.start(
            StartRun(
                mode="build",
                thread_id="thread-mode",
                run_id="run-build",
                message="先以 Build 开始",
            ),
            ConnectionRef("owner"),
        )
        assert (await _events(execution))[-1].type == "run.completed"
        assert (
            await persistence.compose_work_item_store().load_thread_mode("thread-mode")
        ) is ThreadMode.BUILD

        with pytest.raises(RunError, match="THREAD_MODE_LOCKED") as locked:
            await coordinator.start(
                StartRun(
                    mode="compose",
                    thread_id="thread-mode",
                    run_id="run-compose",
                    message="不能切换模式",
                ),
                ConnectionRef("owner"),
            )
        assert locked.value.code == "THREAD_MODE_LOCKED"
    finally:
        await coordinator.close()
        await persistence.close()


@pytest.mark.asyncio
async def test_cancellation_propagates_through_adapter_boundary() -> None:
    """取消必须穿透 adapter 的阻塞执行并产生唯一 cancelled 终态。"""
    gate = asyncio.Event()

    class _BlockingAdapter:
        async def execute(self, run, port):
            port.emit(run, "run.started", {"resumed": False, "mode": "build"})
            run.status = "running"
            await gate.wait()

    coordinator = _coordinator([])
    coordinator._execution_adapters["build"] = _BlockingAdapter()
    execution = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello", mode="build"),
        ConnectionRef("owner"),
    )
    await asyncio.sleep(0)
    cancelled = await coordinator.cancel(execution.ref, ConnectionRef("owner"))
    assert cancelled.cancelled is True
    events = await _events(execution)
    assert [event.type for event in events] == ["run.started", "run.cancelled"]
    await coordinator.close()


@pytest.mark.asyncio
async def test_run_coordinator_allows_multithread_connection_runs() -> None:
    """有 run.multithread 时，同一 Connection 可在不同 Thread 并发 starting/active。"""
    gate = asyncio.Event()

    async def slow_preparation(_command, _persistence):
        await gate.wait()
        return RunPreparation()

    coordinator = RunCoordinator(
        persistence_provider=lambda: _noop_persistence(),
        preparation_provider=slow_preparation,
        runtime_provider=lambda run: _noop_runtime(run),
        interaction_port=_NoopInteraction(),
    )
    owner = ConnectionRef("owner")
    first = asyncio.create_task(
        coordinator.start(
            StartRun(mode="build", thread_id="thread-1", run_id="run-1", message="first"),
            owner,
            allow_multithread=True,
        )
    )
    await asyncio.sleep(0)
    second_task = asyncio.create_task(
        coordinator.start(
            StartRun(mode="build", thread_id="thread-2", run_id="run-2", message="second"),
            owner,
            allow_multithread=True,
        )
    )
    await asyncio.sleep(0)
    gate.set()
    await first
    second = await second_task
    await coordinator.cancel(second.ref, owner)
    await _events(second)


async def _noop_persistence():
    return None


async def _noop_preparation() -> RunPreparation:
    """为直接 adapter 测试提供不读取外部资源的准备结果。"""
    return RunPreparation()


async def _accept_direct_adapter_run(coordinator: RunCoordinator, run: RunState) -> None:
    """模拟 Coordinator 已受理 root execution，供 adapter-only 测试使用。"""
    binding = coordinator._root_execution_binding(run.start, run.preparation)
    run.root_execution = binding
    await coordinator.execution_registry.accept(binding)


async def _noop_runtime(run):
    async def release() -> None:
        return None

    return RunRuntime(
        agent=None,
        run_context=None,
        graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
        release=release,
    )



@pytest.mark.asyncio
async def test_idle_thread_reserves_only_target_thread_and_releases_after_error() -> None:
    """长耗时 compact/watch 只占用目标 Thread，不阻断其他 Thread 受理。"""
    coordinator = _coordinator([])
    owner = ConnectionRef("owner")

    with pytest.raises(RuntimeError, match="injected maintenance failure"):
        async with coordinator.idle_thread("thread-maintenance"):
            assert await coordinator.is_active("thread-maintenance") is True
            with pytest.raises(RunError, match="THREAD_BUSY"):
                await coordinator.start(
                    StartRun(mode="build", 
                        thread_id="thread-maintenance",
                        run_id="run-blocked",
                        message="must wait",
                    ),
                    owner,
                )

            other = await coordinator.start(
                StartRun(mode="build", 
                    thread_id="thread-other",
                    run_id="run-other",
                    message="may proceed",
                ),
                owner,
            )
            assert [event.type for event in await _events(other)] == [
                    "run.started",
                    "run.progress",
                    "content.delta",
                "run.completed",
            ]
            raise RuntimeError("injected maintenance failure")

    assert await coordinator.is_active("thread-maintenance") is False
    recovered = await coordinator.start(
        StartRun(mode="build", 
            thread_id="thread-maintenance",
            run_id="run-recovered",
            message="continue",
        ),
        owner,
    )
    assert (await _events(recovered))[-1].type == "run.completed"


@pytest.mark.asyncio
async def test_close_before_run_task_starts_releases_snapshot_reservation_once() -> None:
    """尚未取得首个时间片的 Run 也必须释放 Host reservation，且只释放一次。"""
    release_calls = 0
    runtime_started = False

    class Reservation:
        async def release(self) -> None:
            nonlocal release_calls
            release_calls += 1

    async def persistence_provider():
        return None

    async def preparation_provider(_command, _persistence):
        return RunPreparation(snapshot_reservation=Reservation())

    async def runtime_provider(_run) -> RunRuntime:
        nonlocal runtime_started
        runtime_started = True
        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=lambda: _noop_async(),
        )

    async def _noop_async() -> None:
        return None

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )

    await coordinator.start(
        StartRun(mode="build", thread_id="thread-not-started", run_id="run-1", message="hello"),
        ConnectionRef("owner"),
    )
    await coordinator.close()

    assert runtime_started is False
    assert release_calls == 1
    await coordinator.close()
    assert release_calls == 1


@pytest.mark.asyncio
async def test_transcript_capture_keeps_full_tool_text_before_wire_truncation() -> None:
    """语义层先捕获完整 ToolMessage，wire 仍可独立按 1 MiB 截断。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-transcript", run_id="run-raw", message="读取"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        AIMessageChunk(
            content="完整助手回答",
            id="assistant-1",
            tool_call_chunks=[
                {"index": 0, "id": "call-1", "name": "read_file", "args": ""}
            ],
        ),
    )
    full_tool_text = "x" * (MAX_TOOL_PAYLOAD_BYTES + 1)
    tool = ToolMessage(
        content=full_tool_text,
        tool_call_id="call-1",
        name="read_file",
    )

    _capture_transcript_message(run, tool)
    assert [record.kind for record in run.pending_transcript] == ["assistant", "tool"]
    assert run.pending_transcript[-1].content == full_tool_text

    wire_events = list(_translate_stream_event(("messages", (tool, {})), run))
    result = next(payload["result"] for kind, payload in wire_events if kind == "tool.completed")
    assert result["truncated"] is True
    assert result["original_bytes"] == len(full_tool_text.encode("utf-8"))
    assert result["content"] != full_tool_text


def test_reasoning_content_is_not_captured_as_transcript_content() -> None:
    """Chat Completions reasoning 内容不能进入助手正文或 Transcript。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-summary-transcript", run_id="run-summary-transcript", message="检查"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        AIMessageChunk(
            content=[
                {"type": "reasoning", "reasoning": "内部思考"},
            ],
        ),
    )

    assert run.assistant_buffer == []
    assert run.pending_transcript == []


def test_reasoning_only_chunk_emits_reasoning_delta_without_content() -> None:
    """reasoning-only chunk 产生 reasoning.delta，原始思维不进入正文。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-reasoning", run_id="run-reasoning", message="检查"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    chunk = AIMessageChunk(content=[{"type": "reasoning", "reasoning": "正在检查代码路径"}])

    events = list(_translate_stream_event(("messages", (chunk, {})), run))

    assert [event_type for event_type, _ in events] == ["reasoning.delta"]
    assert events[0][1] == {"text": "正在检查代码路径"}
    assert _message_text(chunk) == ""


def test_chat_completions_reasoning_content_emits_reasoning_delta() -> None:
    """Chat Completions 的 reasoning_content 产生独立 reasoning.delta。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-completions-reasoning", run_id="run-completions-reasoning", message="检查"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={"reasoning_content": "正在检查代码路径"},
    )

    events = list(_translate_stream_event(("messages", (chunk, {})), run))

    assert [event_type for event_type, _ in events] == ["reasoning.delta"]
    assert events[0][1] == {"text": "正在检查代码路径"}
    assert _message_text(chunk) == ""


def test_reasoning_block_and_text_emit_separate_deltas() -> None:
    """reasoning 与正文分别投影为独立增量事件，互不污染。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-summary", run_id="run-summary", message="检查"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "reasoning": "内部思考"},
            {"type": "text", "text": "结论"},
        ],
    )

    events = list(_translate_stream_event(("messages", (chunk, {})), run))

    assert [event_type for event_type, _ in events] == ["content.delta", "reasoning.delta"]
    assert events[0][1] == {"text": "结论"}
    assert events[1][1] == {"text": "内部思考"}


def test_reasoning_text_is_shown_but_private_fields_never_leak() -> None:
    """reasoning 字段文本可显示；加密/供应商私有字段绝不进入事件或正文。"""
    for content, expected_reasoning in (
        ([{"type": "reasoning", "reasoning": "检查", "encrypted_content": "secret"}], "检查"),
        ([{"type": "reasoning", "reasoning": "私有", "reasoning_details": "private"}], "私有"),
        ([{"type": "reasoning", "reasoning": "公开", "vendor_private": "私有"}], "公开"),
    ):
        run = RunState(
            start=StartRun(mode="build", thread_id="thread-safe", run_id="run-safe", message="检查"),
            owner=ConnectionRef("owner"),
            persistence=None,
            preparation=RunPreparation(),
        )
        events = list(_translate_stream_event(("messages", (AIMessageChunk(content=content), {})), run))
        assert [event_type for event_type, _ in events] == ["reasoning.delta"]
        assert events[0][1] == {"text": expected_reasoning}
        assert "secret" not in str(events)
        assert "private" not in str(events)
        assert "vendor_private" not in str(events)


def test_non_string_text_block_is_not_promoted_to_assistant_text() -> None:
    """不符合标准 text block 的对象不能经字符串化泄露到正文事件。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-text-shape", run_id="run-text-shape", message="检查"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    chunk = AIMessageChunk(content=[{"type": "text", "text": {"private": "secret"}}])

    events = list(_translate_stream_event(("messages", (chunk, {})), run))

    assert events == []
    assert "secret" not in str(events)


@pytest.mark.asyncio
async def test_transcript_capture_groups_chunks_without_stable_ids_into_one_assistant() -> None:
    """无 ID 或变 ID 的 assistant delta 仍属于同一完整助手消息。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-chunks", run_id="run-chunks", message="分片"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    for chunk in (
        AIMessageChunk(
            content="第一段",
            tool_call_chunks=[
                {"index": 0, "id": "call-1", "name": "read_file", "args": ""}
            ],
        ),
        AIMessageChunk(content="第二段"),
        AIMessageChunk(content="第三段", id="provider-id-changed"),
    ):
        _capture_transcript_message(run, chunk)

    _capture_transcript_message(
        run,
        ToolMessage(content="边界", tool_call_id="call-1", name="read_file"),
    )
    assert [(record.kind, record.content) for record in run.pending_transcript] == [
        ("assistant", "第一段第二段第三段"),
        ("tool", "边界"),
    ]


def test_idless_tool_call_index_is_reset_between_model_rounds() -> None:
    """同一 index 的无 ID 工具调用跨回合必须使用不同的 Run ordinal。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-rounds", run_id="run-rounds", message="连续执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    first_call = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": None, "name": "execute", "args": "pwd"}
            ],
        },
    )()
    second_call = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": None, "name": "execute", "args": "ls"}
            ],
        },
    )()
    first_result = type(
        "ToolMessage",
        (),
        {"content": "first", "tool_call_id": "", "name": "execute", "status": "success"},
    )()
    second_result = type(
        "ToolMessage",
        (),
        {"content": "second", "tool_call_id": "", "name": "execute", "status": "success"},
    )()

    _capture_transcript_message(run, first_call)
    _capture_transcript_message(run, first_result)
    _capture_transcript_message(run, second_call)
    _capture_transcript_message(run, second_result)

    tool_records = [record for record in run.pending_transcript if record.kind == "tool"]
    assert [record.content for record in tool_records] == ["first", "second"]
    assert [record.tool_call_id for record in tool_records] == [
        "tool-run-rounds-1",
        "tool-run-rounds-2",
    ]
    assert [record.record_id for record in tool_records] == [
        "run:run-rounds:tool:tool-run-rounds-1",
        "run:run-rounds:tool:tool-run-rounds-2",
    ]


def test_parallel_idless_tool_results_fail_closed() -> None:
    """并行无 ID 结果无法可靠归属时不能静默合并到一个记录。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-parallel", run_id="run-parallel", message="并行执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        type(
            "AIMessageChunk",
            (),
            {
                "content": "",
                "usage_metadata": None,
                "tool_call_chunks": [
                    {"index": 0, "id": None, "name": "one", "args": ""},
                    {"index": 1, "id": None, "name": "two", "args": ""},
                ],
            },
        )(),
    )
    with pytest.raises(RunError, match="cannot be associated safely"):
        _capture_transcript_message(
            run,
            type(
                "ToolMessage",
                (),
                {
                    "content": "ambiguous",
                    "tool_call_id": "",
                    "name": "tool",
                    "status": "success",
                },
            )(),
        )


def test_tool_result_provider_id_mismatch_does_not_guess_unique_stable_call() -> None:
    """已有 provider ID=A 时，结果 ID=B 不能借唯一候选猜配。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-id-mismatch", run_id="run-id-mismatch", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        AIMessage(
            content="",
            tool_calls=[{"id": "provider-a", "name": "execute", "args": {"cmd": "pwd"}}],
        ),
    )
    with pytest.raises(RunError, match="does not match"):
        _capture_transcript_message(
            run,
            ToolMessage(content="结果", tool_call_id="provider-b", name="execute"),
        )
    assert [record.kind for record in run.pending_transcript] == ["assistant"]
    assert run.pending_transcript[0].tool_calls[0]["id"] == "provider-a"


def test_orphan_tool_result_does_not_create_transcript_call() -> None:
    """没有前置 assistant tool call 时，结果不能凭空生成孤儿 ID。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-orphan-result", run_id="run-orphan-result", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    with pytest.raises(RunError, match="preceding assistant"):
        _capture_transcript_message(
            run,
            ToolMessage(content="孤儿结果", tool_call_id="provider-b", name="execute"),
        )
    assert run.pending_transcript == []


def test_idless_assistant_allows_late_provider_result_id_binding() -> None:
    """无 provider ID 的唯一 assistant 候选允许结果 ID 到达时绑定内部 ID。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-late-id", run_id="run-late-id", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": None, "name": "execute", "args": '{"cmd":"pwd"}'}
            ],
        ),
    )
    _capture_transcript_message(
        run,
        ToolMessage(content="结果", tool_call_id="late-provider-id", name="execute"),
    )
    assistant = next(record for record in run.pending_transcript if record.kind == "assistant")
    tool = next(record for record in run.pending_transcript if record.kind == "tool")
    assert assistant.tool_calls[0]["id"] == tool.tool_call_id
    assert assistant.tool_calls[0]["id"] != "late-provider-id"


def test_idless_unindexed_second_named_call_fails_closed() -> None:
    """无 ID、无 index 的第二个明确 call start 不能静默并入 current。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-unindexed-calls", run_id="run-unindexed-calls", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    chunk = lambda name, args: type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [{"index": None, "id": None, "name": name, "args": args}],
        },
    )()
    _capture_transcript_message(run, chunk("execute", "pwd"))
    with pytest.raises(RunError, match="without index"):
        _capture_transcript_message(run, chunk("execute", "ls"))


def test_tool_chunk_id_change_with_same_index_stays_one_call() -> None:
    """同一回合的 provider ID 变化不能把一个 index 拆成两次工具调用。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-id-change", run_id="run-id-change", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    first = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": "provider-a", "name": "execute", "args": ""}
            ],
        },
    )()
    changed = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": "provider-b", "name": None, "args": "pwd"}
            ],
        },
    )()
    result = type(
        "ToolMessage",
        (),
        {
            "content": "done",
            "tool_call_id": "provider-b",
            "name": "execute",
            "status": "success",
        },
    )()

    _capture_transcript_message(run, first)
    _capture_transcript_message(run, changed)
    _capture_transcript_message(run, result)

    tools = [record for record in run.pending_transcript if record.kind == "tool"]
    assert len(tools) == 1
    assert tools[0].tool_call_id == "provider-a"


def test_tool_call_chunks_round_trip_json_arguments_and_invalid_raw() -> None:
    """AIMessageChunk 参数分片聚合后可恢复，半条 JSON 保留 raw/status。"""
    complete = RunState(
        start=StartRun(mode="build", thread_id="thread-chunk-args", run_id="run-chunk-args", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    for chunk in (
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": "chunk-call", "name": "execute", "args": '{"cmd":'}
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": None, "name": None, "args": '"pwd"}'}
            ],
        ),
    ):
        _capture_transcript_message(complete, chunk)
    _capture_transcript_message(
        complete,
        ToolMessage(content="done", tool_call_id="chunk-call", name="execute"),
    )
    assert complete.pending_transcript[0].tool_calls[0]["arguments"] == {"cmd": "pwd"}
    assert complete.pending_transcript[0].tool_calls[0]["arguments_status"] == "valid"
    assert complete.pending_transcript[0].tool_calls[0]["arguments_raw"] == '{"cmd":"pwd"}'

    invalid = RunState(
        start=StartRun(mode="build", thread_id="thread-chunk-invalid", run_id="run-chunk-invalid", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        invalid,
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": "invalid-call", "name": "execute", "args": '{"cmd":'}
            ],
        ),
    )
    _capture_transcript_message(
        invalid,
        ToolMessage(content="done", tool_call_id="invalid-call", name="execute"),
    )
    assert invalid.pending_transcript[0].tool_calls[0]["arguments_status"] == "partial"
    assert invalid.pending_transcript[0].tool_calls[0]["arguments_raw"] == '{"cmd":'


@pytest.mark.parametrize("value", [{1}, float("nan"), float("inf"), -float("inf")])
def test_non_json_provider_chunk_arguments_are_invalid_not_stringified_valid(
    value,
) -> None:
    """非 JSON/非有限 provider 参数不能由字符串化伪装成 valid。"""
    run = RunState(
        start=StartRun(mode="build", thread_id="thread-non-json", run_id="run-non-json", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        type(
            "AIMessageChunk",
            (),
            {
                "content": "",
                "usage_metadata": None,
                "tool_call_chunks": [
                    {"index": 0, "id": "non-json", "name": "execute", "args": value}
                ],
            },
        )(),
    )
    _capture_transcript_message(
        run,
        ToolMessage(content="done", tool_call_id="non-json", name="execute"),
    )
    call = run.pending_transcript[0].tool_calls[0]
    assert call["arguments_status"] == "invalid"
    assert "arguments_raw" in call
    assert "arguments" not in call


def test_child_namespace_interrupt_does_not_trigger_root_interaction() -> None:
    """Protocol v3 无 provenance 时，child interrupt 不能提升为 root request。"""
    child_event = (
        ("child", "approval-node"),
        "updates",
        {
            "__interrupt__": [
                {
                    "id": "child-interrupt",
                    "value": {
                        "type": "ask_user",
                        "questions": [{"question": "子图问题", "choices": []}],
                    },
                }
            ]
        },
    )
    result, auto_resume = _extract_interaction(child_event)
    assert result is None
    assert auto_resume is None

    root_event = (
        "updates",
        {
            "__interrupt__": [
                {
                    "id": "root-interrupt",
                    "value": {
                        "type": "ask_user",
                        "questions": [{"question": "根问题", "choices": []}],
                    },
                }
            ]
        },
    )
    result, auto_resume = _extract_interaction(root_event)
    assert result is not None


@pytest.mark.asyncio
async def test_tool_call_only_assistant_round_trips_arguments_and_conflicts_on_change(
    tmp_path,
) -> None:
    """无正文 assistant 也持久化完整参数，幂等重试不能吞掉参数差异。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-tool-calls", "读取 README", run_id="run-tool-calls")
    try:
        run = RunState(
            start=StartRun(mode="build", thread_id="thread-tool-calls", run_id="run-tool-calls", message="读取 README"),
            owner=ConnectionRef("owner"),
            persistence=store,
            preparation=RunPreparation(),
        )
        _capture_transcript_message(
            run,
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-read",
                        "name": "read_file",
                        "args": {"path": "README.md"},
                    }
                ],
            ),
        )
        _capture_transcript_message(
            run,
            ToolMessage(content="README 内容", tool_call_id="call-read", name="read_file"),
        )
        await _coordinator([])._flush_transcript(run)

        records = await store.load_transcript("thread-tool-calls")
        assistant = records[1]
        assert assistant.kind == "assistant"
        assert assistant.payload["content"] == ""
        assert assistant.payload["tool_calls"] == [
            {
                "id": "call-read",
                "name": "read_file",
                "type": "tool_call",
                "arguments": {"path": "README.md"},
                "arguments_json": '{"path":"README.md"}',
                "arguments_status": "valid",
            }
        ]
        assert records[2].payload["tool_call_id"] == "call-read"

        with pytest.raises(ThreadPersistenceError, match="TRANSCRIPT_RECORD_CONFLICT"):
            await store.append_transcript(
                TranscriptAppend(
                    thread_id="thread-tool-calls",
                    record_id=assistant.record_id,
                    kind="assistant",
                    content="",
                    run_id="run-tool-calls",
                    execution_id="root-run-tool-calls",
                    tool_calls=(
                        {
                            "id": "call-read",
                            "name": "read_file",
                            "arguments": {"path": "OTHER.md"},
                            "arguments_json": '{"path":"OTHER.md"}',
                            "arguments_status": "valid",
                        },
                    ),
                )
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_parallel_stable_tool_calls_keep_result_pairing(tmp_path) -> None:
    """并行稳定调用 ID 按结果到达顺序也保持各自的 Transcript 归属。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-parallel-calls", "并行读取", run_id="run-parallel-calls")
    try:
        run = RunState(
            start=StartRun(mode="build", 
                thread_id="thread-parallel-calls",
                run_id="run-parallel-calls",
                message="并行读取",
            ),
            owner=ConnectionRef("owner"),
            persistence=store,
            preparation=RunPreparation(),
        )
        _capture_transcript_message(
            run,
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-a", "name": "read_file", "args": {"path": "a"}},
                    {"id": "call-b", "name": "read_file", "args": {"path": "b"}},
                ],
            ),
        )
        coordinator = _coordinator([])
        _capture_transcript_message(
            run,
            ToolMessage(content="A", tool_call_id="call-a", name="read_file"),
        )
        await coordinator._flush_transcript(run)
        partial_records = await store.load_transcript("thread-parallel-calls")
        partial_assistant = next(
            record for record in partial_records if record.kind == "assistant"
        )
        partial_tool_ids = [
            record.payload["tool_call_id"]
            for record in partial_records
            if record.kind == "tool"
        ]
        declared_ids = {
            call["id"] for call in partial_assistant.payload["tool_calls"]
        }
        assert declared_ids == {"call-a", "call-b"}
        assert partial_tool_ids == ["call-a"]
        assert declared_ids - set(partial_tool_ids) == {"call-b"}

        _capture_transcript_message(
            run,
            ToolMessage(content="B", tool_call_id="call-b", name="read_file"),
        )
        await coordinator._flush_transcript(run)

        records = await store.load_transcript("thread-parallel-calls")
        assert [record.payload["tool_call_id"] for record in records if record.kind == "tool"] == [
            "call-a",
            "call-b",
        ]
        assistant = next(record for record in records if record.kind == "assistant")
        assert [call["id"] for call in assistant.payload["tool_calls"]] == ["call-a", "call-b"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_subgraph_messages_are_not_flattened_into_root_transcript(tmp_path) -> None:
    """无 provenance 时显式抑制非空 namespace 的 child live 事件，不写 root 事实。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-root", "根请求", run_id="run-root")
    try:
        class RootAndSubgraphAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    ("child", "tool-node"),
                    "messages",
                    (
                        AIMessageChunk(
                            content="子图回答",
                            tool_call_chunks=[
                                {
                                    "index": 0,
                                    "id": "child-call",
                                    "name": "child_tool",
                                    "args": '{"secret":true}',
                                }
                            ],
                        ),
                        {},
                    ),
                )
                yield (
                    ("child", "tool-node"),
                    "messages",
                    (ToolMessage(content="子图工具结果", tool_call_id="child-call"), {}),
                )
                yield (
                    "messages",
                    (
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "root-call",
                                    "name": "read_file",
                                    "args": {"path": "root.txt"},
                                }
                            ],
                        ),
                        {},
                    ),
                )
                yield (
                    "messages",
                    (
                        ToolMessage(
                            content="根工具结果",
                            tool_call_id="root-call",
                            name="read_file",
                        ),
                        {},
                    ),
                )

        run = RunState(
            start=StartRun(mode="build", thread_id="thread-root", run_id="run-root", message="根请求"),
            owner=ConnectionRef("owner"),
            persistence=store,
            preparation=RunPreparation(),
        )
        runtime = RunRuntime(
            agent=RootAndSubgraphAgent(),
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=lambda: _noop_async(),
        )

        async def runtime_provider(_run) -> RunRuntime:
            return runtime

        coordinator = RunCoordinator(
            persistence_provider=_noop_persistence,
            preparation_provider=lambda _command, _persistence: _noop_preparation(),
            runtime_provider=runtime_provider,
            interaction_port=_NoopInteraction(),
        )
        await _accept_direct_adapter_run(coordinator, run)
        await coordinator._execution_adapters["build"].execute(run, coordinator._lifecycle_port)

        records = await store.load_transcript("thread-root")
        assert [record.kind for record in records] == ["user", "assistant", "tool"]
        assert records[1].payload["tool_calls"][0]["id"] == "root-call"
        assert records[2].payload["tool_call_id"] == "root-call"
        assert all("子图" not in str(record.payload) for record in records)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_completed_tool_batch_is_readable_while_run_waits_for_next_model_stage(
    tmp_path,
) -> None:
    """Tool 边界提交后，独立连接可在后续模型阶段阻塞时恢复已完成语义。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-durable", "读取文件", run_id="run-durable")

    blocked = asyncio.Event()
    release = asyncio.Event()

    class BlockingAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    type(
                        "AIMessageChunk",
                        (),
                        {
                            "content": "正在读取",
                            "tool_call_chunks": [
                                {"index": 0, "id": None, "name": "read_file", "args": ""}
                            ],
                            "usage_metadata": None,
                        },
                    )(),
                    {},
                ),
            )
            yield (
                "messages",
                (
                    type(
                        "ToolMessage",
                        (),
                        {
                            "content": "完整工具结果",
                            "tool_call_id": "",
                            "name": "read_file",
                            "status": "success",
                        },
                    )(),
                    {},
                ),
            )
            blocked.set()
            await release.wait()
            yield (
                "messages",
                (
                    type(
                        "AIMessageChunk",
                        (),
                        {
                            "content": "最终回答",
                            "tool_call_chunks": [],
                            "usage_metadata": None,
                        },
                    )(),
                    {},
                ),
            )

    run = RunState(
        start=StartRun(mode="build", thread_id="thread-durable", run_id="run-durable", message="读取文件"),
        owner=ConnectionRef("owner"),
        persistence=store,
        preparation=RunPreparation(execution_binding=make_test_binding("thread-durable", "run-durable")),
    )
    runtime = RunRuntime(
        agent=BlockingAgent(),
        run_context=None,
        graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
        release=lambda: _noop_async(),
    )

    async def runtime_provider(_run) -> RunRuntime:
        return runtime

    coordinator = RunCoordinator(
        persistence_provider=_noop_persistence,
        preparation_provider=lambda _command, _persistence: _noop_preparation(),
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )
    await _accept_direct_adapter_run(coordinator, run)
    stream_task = asyncio.create_task(
        coordinator._execution_adapters["build"].execute(run, coordinator._lifecycle_port)
    )
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        independent = await ThreadPersistence.open(project=project, home=home)
        try:
            records = await independent.load_transcript("thread-durable")
            assert [(record.kind, record.payload["content"]) for record in records] == [
                ("user", "读取文件"),
                ("assistant", "正在读取"),
                ("tool", "完整工具结果"),
            ]
        finally:
            await independent.close()
    finally:
        release.set()
        try:
            await stream_task
            final_records = await store.load_transcript("thread-durable")
            assert final_records[-1].payload["content"] == "最终回答"
        finally:
            await store.close()


async def _noop_async() -> None:
    return None


@pytest.mark.asyncio
async def test_failed_run_flushes_completed_semantics_but_discards_partial_assistant() -> None:
    """失败收尾保留已完成 assistant/tool，流式半条 assistant 不进入 Transcript。"""

    class Persistence:
        def __init__(self) -> None:
            self.records = []
            self.completed = False

        async def accept_run(self, _command):
            return type("Acceptance", (), {"created": True})()

        async def append_transcript_batch(self, records):
            self.records.extend(records)

        async def complete_run(self, _thread_id: str) -> None:
            self.completed = True

    class FailingAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="已完成回答",
                        id="assistant-1",
                        tool_call_chunks=[
                            {"index": 0, "id": "call-1", "name": "read_file", "args": ""}
                        ],
                    ),
                    {},
                ),
            )
            yield (
                "messages",
                (
                    ToolMessage(content="工具已完成", tool_call_id="call-1", name="read_file"),
                    {},
                ),
            )
            yield ("messages", (AIMessageChunk(content="流式半条", id="assistant-2"), {}))
            raise RuntimeError("injected agent failure")

    persistence = Persistence()

    async def persistence_provider():
        return persistence

    async def preparation_provider(_command, _persistence):
        return RunPreparation(
            execution_binding=make_test_binding("thread-failed", "run-failed")
        )

    async def runtime_provider(_run) -> RunRuntime:
        async def release() -> None:
            return None

        return RunRuntime(
            agent=FailingAgent(),
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )
    execution = await coordinator.start(
        StartRun(mode="build", thread_id="thread-failed", run_id="run-failed", message="失败测试"),
        ConnectionRef("owner"),
    )
    events = await _events(execution)

    assert events[-1].type == "run.failed"
    assert [(record.kind, record.content) for record in persistence.records] == [
        ("assistant", "已完成回答"),
        ("tool", "工具已完成"),
    ]
    assert persistence.completed is True
