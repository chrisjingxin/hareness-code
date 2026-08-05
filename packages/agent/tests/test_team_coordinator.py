"""固定 Agent Team 的 DAG、并发、取消、失败和恢复测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from harness_agent.runtime.agent_catalog import DelegationPolicy
from harness_agent.runtime.agent_delegation import AgentDelegator, DelegateAgent, DelegationTarget
from harness_agent.runtime.agent_execution import AgentExecutionRegistry
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
)
from harness_agent.runtime.run_context import RunCancellationToken
from harness_agent.runtime.team_coordinator import (
    InMemoryTeamStateStore,
    SqliteTeamStateStore,
    TeamCoordinator,
    TeamDefinition,
    TeamError,
    TeamFailurePolicy,
    TeamRun,
    TeamRunStatus,
    TeamTaskAccess,
    TeamTaskDefinition,
    TeamTaskState,
    TeamTaskStatus,
    generate_fanout_team,
)


async def _runtime(
    runners: dict[str, object],
) -> tuple[TeamCoordinator, InMemoryTeamStateStore, ExecutionRef]:
    """建立 running 根 execution 和只包含指定角色的 Delegator。"""
    registry = AgentExecutionRegistry()
    root = ExecutionRef.root("thread-team", "run-root")
    await registry.accept(
        AgentExecutionBinding(
            ref=root,
            agent_id="main",
            mode=ExecutionMode.MANAGED,
            depth=0,
        )
    )
    await registry.start(root)
    targets = tuple(
        DelegationTarget(
            agent_id=agent_id,
            mode=ExecutionMode.INLINE,
            runner=runner,  # type: ignore[arg-type]
        )
        for agent_id, runner in runners.items()
    )
    store = InMemoryTeamStateStore()
    return TeamCoordinator(AgentDelegator(registry, targets=targets), store=store), store, root


def _policy(*agents: str, parallelism: int = 4) -> DelegationPolicy:
    """构造允许测试角色的一层派发上限。"""
    return DelegationPolicy(
        enabled=True,
        allowed_agents=agents,
        max_depth=1,
        max_parallelism=parallelism,
    )


def test_team_rejects_cycle_before_execution() -> None:
    """循环依赖必须在 TeamDefinition 受理时被拒绝。"""
    with pytest.raises(TeamError) as caught:
        TeamDefinition(
            team_id="cycle",
            tasks=(
                TeamTaskDefinition("a", "worker", "a", depends_on=("b",)),
                TeamTaskDefinition("b", "worker", "b", depends_on=("a",)),
            ),
        )
    assert caught.value.code == "TEAM_DEPENDENCY_CYCLE"


def test_generate_team_from_agent_definitions_creates_previewable_dag() -> None:
    """明确选择 lead/workers 后可从 AgentDefinition 生成固定 fanout DAG。"""
    from types import SimpleNamespace

    agents = (
        SimpleNamespace(agent_id="security", description="Security findings"),
        SimpleNamespace(agent_id="tests", description="Test findings"),
        SimpleNamespace(agent_id="lead", description="Synthesize"),
    )
    definition = generate_fanout_team(
        team_id="generated-review",
        agents=agents,  # type: ignore[arg-type]
        lead_agent_id="lead",
        worker_agent_ids=("security", "tests"),
        max_parallelism=2,
    )

    assert [task.task_id for task in definition.tasks] == [
        "security",
        "tests",
        "synthesis",
    ]
    assert definition.tasks[-1].depends_on == ("security", "tests")
    assert "{{tasks.security.result}}" in definition.tasks[-1].input_template
    assert definition.failure_policy is TeamFailurePolicy.CONTINUE_TO_SYNTHESIS


async def test_read_tasks_parallel_then_synthesis_receives_results() -> None:
    """无依赖只读任务并行，汇总任务只在两者完成后启动。"""
    active = 0
    peak = 0
    workers_done = asyncio.Event()

    async def worker(command: DelegateAgent):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if active == 0:
            workers_done.set()
        return {"value": command.target_agent_id}

    async def lead(command: DelegateAgent):
        assert workers_done.is_set()
        assert "security" in command.task and "tests" in command.task
        return {"report": "ok"}

    coordinator, _store, root = await _runtime(
        {"security": worker, "tests": worker, "lead": lead}
    )
    definition = TeamDefinition(
        team_id="review",
        max_parallelism=2,
        tasks=(
            TeamTaskDefinition("security", "security", "{{request}}"),
            TeamTaskDefinition("tests", "tests", "{{request}}"),
            TeamTaskDefinition(
                "synthesis",
                "lead",
                "{{tasks.security.result}}\n{{tasks.tests.result}}",
                depends_on=("security", "tests"),
            ),
        ),
    )
    result = await coordinator.run(
        definition,
        run_id="team-1",
        parent_ref=root,
        request="review",
        delegation_policy=_policy("security", "tests", "lead", parallelism=2),
        cancellation_token=RunCancellationToken(),
    )

    assert peak == 2
    assert result.status is TeamRunStatus.COMPLETED
    assert result.terminal_count == 1
    assert result.task("synthesis").result == {"report": "ok"}


async def test_write_tasks_are_serialized_at_team_boundary() -> None:
    """两个写任务即使没有依赖也不能同时占用 Team 写阶段。"""
    active = 0
    peak = 0

    async def writer(_command: DelegateAgent):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"written": True}

    coordinator, _store, root = await _runtime({"one": writer, "two": writer})
    result = await coordinator.run(
        TeamDefinition(
            team_id="writers",
            max_parallelism=2,
            tasks=(
                TeamTaskDefinition(
                    "one",
                    "one",
                    "write one",
                    access=TeamTaskAccess.WRITE,
                ),
                TeamTaskDefinition(
                    "two",
                    "two",
                    "write two",
                    access=TeamTaskAccess.WRITE,
                ),
            ),
        ),
        run_id="team-writes",
        parent_ref=root,
        request="write",
        delegation_policy=_policy("one", "two", parallelism=2),
        cancellation_token=RunCancellationToken(),
    )

    assert result.status is TeamRunStatus.COMPLETED
    assert peak == 1


async def test_fail_fast_cancels_siblings_and_blocks_dependents() -> None:
    """fail-fast 成员失败后收敛其他成员并只发布一个 failed 终态。"""
    started = asyncio.Event()

    async def failing(_command: DelegateAgent):
        await started.wait()
        raise RuntimeError("boom")

    async def waiting(_command: DelegateAgent):
        started.set()
        await asyncio.Event().wait()
        return {}

    coordinator, _store, root = await _runtime({"bad": failing, "slow": waiting})
    result = await coordinator.run(
        TeamDefinition(
            team_id="fail-fast",
            max_parallelism=2,
            failure_policy=TeamFailurePolicy.FAIL_FAST,
            tasks=(
                TeamTaskDefinition("bad", "bad", "bad"),
                TeamTaskDefinition("slow", "slow", "slow"),
                TeamTaskDefinition("after", "slow", "after", depends_on=("bad",)),
            ),
        ),
        run_id="team-fail",
        parent_ref=root,
        request="fail",
        delegation_policy=_policy("bad", "slow", parallelism=2),
        cancellation_token=RunCancellationToken(),
    )

    assert result.status is TeamRunStatus.FAILED
    assert result.terminal_count == 1
    assert result.task("bad").status is TeamTaskStatus.FAILED
    assert result.task("slow").status is TeamTaskStatus.CANCELLED
    assert result.task("after").status in {
        TeamTaskStatus.CANCELLED,
        TeamTaskStatus.BLOCKED,
    }


async def test_parent_cancellation_converges_member_tasks() -> None:
    """父 token 取消后 Team 与正在运行的 child 都收敛为 cancelled。"""
    started = asyncio.Event()

    async def waiting(_command: DelegateAgent):
        started.set()
        await asyncio.Event().wait()
        return {}

    coordinator, _store, root = await _runtime({"worker": waiting})
    token = RunCancellationToken()
    task = asyncio.create_task(
        coordinator.run(
            TeamDefinition(
                team_id="cancel",
                tasks=(TeamTaskDefinition("work", "worker", "work"),),
            ),
            run_id="team-cancel",
            parent_ref=root,
            request="cancel",
            delegation_policy=_policy("worker"),
            cancellation_token=token,
        )
    )
    await started.wait()
    token.cancel()
    result = await task

    assert result.status is TeamRunStatus.CANCELLED
    assert result.task("work").status is TeamTaskStatus.CANCELLED


async def test_recovery_retries_read_but_never_repeats_unknown_write() -> None:
    """恢复时只读 running 可重试，结果未知的写任务必须 fail closed。"""
    calls: list[str] = []

    async def worker(command: DelegateAgent):
        calls.append(command.target_agent_id)
        return {"ok": True}

    coordinator, store, root = await _runtime({"reader": worker, "writer": worker})
    definition = TeamDefinition(
        team_id="recover",
        failure_policy=TeamFailurePolicy.CONTINUE,
        tasks=(
            TeamTaskDefinition("read", "reader", "read"),
            TeamTaskDefinition(
                "write",
                "writer",
                "write",
                access=TeamTaskAccess.WRITE,
            ),
        ),
    )
    await store.save(
        TeamRun(
            run_id="team-recover",
            team_id="recover",
            parent_ref=root,
            status=TeamRunStatus.RUNNING,
            tasks=(
                TeamTaskState("read", TeamTaskStatus.RUNNING, attempts=1),
                TeamTaskState("write", TeamTaskStatus.RUNNING, attempts=1),
            ),
        )
    )

    result = await coordinator.run(
        definition,
        run_id="team-recover",
        parent_ref=root,
        request="recover",
        delegation_policy=_policy("reader", "writer"),
        cancellation_token=RunCancellationToken(),
    )

    assert calls == ["reader"]
    assert result.status is TeamRunStatus.FAILED
    assert result.task("read").status is TeamTaskStatus.COMPLETED
    assert result.task("write").error_code == "TEAM_WRITE_OUTCOME_UNKNOWN"
    assert result.terminal_count == 1
    assert await coordinator.run(
        definition,
        run_id="team-recover",
        parent_ref=root,
        request="recover",
        delegation_policy=_policy("reader", "writer"),
        cancellation_token=RunCancellationToken(),
    ) == result


async def test_sqlite_store_recovers_team_state_after_connection_reopen(
    tmp_path: Path,
) -> None:
    """SQLite adapter 跨连接恢复状态，并拒绝改写已经发布的终态。"""
    import aiosqlite

    database = tmp_path / "teams.sqlite3"
    root = ExecutionRef.root("thread-persisted", "root-run")
    run = TeamRun(
        run_id="team-persisted",
        team_id="review",
        parent_ref=root,
        status=TeamRunStatus.COMPLETED,
        tasks=(
            TeamTaskState(
                "review",
                TeamTaskStatus.COMPLETED,
                execution_id="child-review",
                result={"final": "ok"},
                attempts=1,
            ),
        ),
        terminal_count=1,
    )
    first_connection = await aiosqlite.connect(database)
    first = SqliteTeamStateStore(
        first_connection,
        project_fingerprint="project-a",
    )
    await first.save(run)
    await first_connection.close()

    second_connection = await aiosqlite.connect(database)
    second = SqliteTeamStateStore(
        second_connection,
        project_fingerprint="project-a",
    )
    assert await second.load(run.run_id) == run
    with pytest.raises(TeamError) as caught:
        await second.save(replace(run, status=TeamRunStatus.FAILED))
    assert caught.value.code == "TEAM_TERMINAL_ALREADY_PUBLISHED"
    isolated = SqliteTeamStateStore(
        second_connection,
        project_fingerprint="project-b",
    )
    assert await isolated.load(run.run_id) is None
    await second_connection.close()
