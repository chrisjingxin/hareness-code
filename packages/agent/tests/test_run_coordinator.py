"""RunCoordinator 的领域生命周期测试，不依赖 AgentHost 或 JSON-RPC transport。"""

from __future__ import annotations

import pytest

from harness_agent.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    RunCoordinator,
    RunError,
    RunPreparation,
    RunRuntime,
    StartRun,
)
from harness_agent.execution_binding import ExecutionRef


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
        skill_registry_provider=lambda: None,  # type: ignore[return-value]
    )


async def _events(execution) -> list:
    return [event async for event in execution.events]


@pytest.mark.asyncio
async def test_run_coordinator_enforces_owner_busy_and_single_terminal_event() -> None:
    """生命周期边界由 Coordinator interface 负责，不需要构造 Server。"""
    releases: list[str] = []
    coordinator = _coordinator(releases)
    owner = ConnectionRef("owner")
    other = ConnectionRef("other")

    execution = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello"),
        owner,
    )
    with pytest.raises(RunError, match="THREAD_BUSY") as busy:
        await coordinator.start(
            StartRun(thread_id="thread", run_id="run-2", message="busy"),
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
        StartRun(thread_id="thread", run_id="run-1", message="hello"),
        ConnectionRef("owner"),
    )

    events = await _events(execution)
    assert [event.type for event in events] == [
        "run.started",
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
