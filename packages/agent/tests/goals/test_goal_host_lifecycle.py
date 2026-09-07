"""Goal lifecycle 的 Host 编排、运行中排队与终态收敛测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_agent.host.agent_host import AgentHost


@pytest.mark.asyncio
async def test_goal_mutate_applies_when_idle_and_queues_when_running(tmp_path: Path) -> None:
    host = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=tmp_path)
    try:
        persistence = await host._ensure_thread_persistence()
        goal = await _create_goal(persistence.goal_store())

        async def inactive(_thread_id: str) -> bool:
            return False

        host._run_coordinator.is_active = inactive  # type: ignore[method-assign]
        paused = await host._handle_goal_mutate(
            {
                "thread_id": "thread-1",
                "operation_id": "pause-1",
                "expected_goal_id": goal.goal_id,
                "expected_revision": goal.revision,
                "action": {"kind": "pause"},
            },
            "rpc-1",
        )
        assert paused["disposition"] == "applied"
        assert paused["goal"]["status"] == "paused"

        resumed = await persistence.goal_store().mutate(
            thread_id="thread-1",
            operation_id="resume-setup",
            expected_goal_id=paused["goal"]["goal_id"],
            expected_revision=paused["goal"]["revision"],
            action="resume",
            apply_now=True,
            now_ms=20,
        )
        assert resumed.goal is not None

        async def active(_thread_id: str) -> bool:
            return True

        host._run_coordinator.is_active = active  # type: ignore[method-assign]
        queued = await host._handle_goal_mutate(
            {
                "thread_id": "thread-1",
                "operation_id": "pause-2",
                "expected_goal_id": resumed.goal.goal_id,
                "expected_revision": resumed.goal.revision,
                "action": {"kind": "pause"},
            },
            "rpc-2",
        )
        assert queued["disposition"] == "queued"
        assert queued["goal"]["status"] == "active"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_terminal_reconcile_applies_queued_mutation_before_terminal_context(tmp_path: Path) -> None:
    host = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=tmp_path)
    try:
        persistence = await host._ensure_thread_persistence()
        store = persistence.goal_store()
        goal = await _create_goal(store)
        await store.mutate(
            thread_id="thread-1",
            operation_id="pause-1",
            expected_goal_id=goal.goal_id,
            expected_revision=goal.revision,
            action="pause",
            apply_now=False,
            now_ms=10,
        )
        run = SimpleNamespace(
            persistence=persistence,
            thread_id="thread-1",
            run_id="run-1",
            context_summary={},
        )
        port = _Port()

        await host._reconcile_goal_terminal(run, port)

        snapshot = await store.inspect("thread-1")
        assert snapshot.goal is not None
        assert snapshot.goal.status == "paused"
        assert run.context_summary["goal"]["status"] == "paused"
        assert run.context_summary["goal_reconcile"] == {"reason": "pause"}
        assert port.events == []
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_terminal_reconcile_makes_queued_proposal_ready(tmp_path: Path) -> None:
    host = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=tmp_path)
    try:
        persistence = await host._ensure_thread_persistence()
        await persistence.goal_store().request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成测试",
            expected_goal_id=None,
            expected_revision=None,
            ready=False,
            now_ms=1,
        )
        run = SimpleNamespace(
            persistence=persistence,
            thread_id="thread-1",
            run_id="run-1",
            context_summary={},
        )

        await host._reconcile_goal_terminal(run, _Port())

        assert run.context_summary["goal_proposal_ready"] == {"request_id": "request-1"}
        assert (await persistence.goal_store().inspect("thread-1")).pending.status == "ready"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_terminal_reconcile_cancels_explicitly_cancelled_proposal(tmp_path: Path) -> None:
    host = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=tmp_path)
    try:
        persistence = await host._ensure_thread_persistence()
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成测试",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )
        await store.set_proposal_status("request-1", status="drafting", now_ms=2)
        run = SimpleNamespace(
            persistence=persistence,
            thread_id="thread-1",
            run_id="run-1",
            start=SimpleNamespace(
                input=SimpleNamespace(kind="goal_proposal", request_id="request-1")
            ),
            cancel_requested=True,
            context_summary={},
        )

        await host._reconcile_goal_terminal(run, _Port())

        snapshot = await store.inspect("thread-1")
        assert snapshot.pending is None
        assert "goal_proposal_ready" not in run.context_summary
        assert any(
            activity.summary == "目标审核已取消"
            for activity in snapshot.activities
        )
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_threads_open_reconciles_interrupted_proposal_when_idle(tmp_path: Path) -> None:
    host = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=tmp_path)
    try:
        persistence = await host._ensure_thread_persistence()
        await persistence._connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO harness_threads(
                project_fingerprint, thread_id, created_at_ms, updated_at_ms,
                first_message, latest_message, message_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (persistence.project_fingerprint, "thread-1", 1, 1, "", "", 0),
        )
        await persistence._connection.commit()  # type: ignore[attr-defined]
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成测试",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )
        await store.set_proposal_status("request-1", status="drafting", now_ms=2)

        async def inactive(_thread_id: str) -> bool:
            return False

        host._run_coordinator.is_active = inactive  # type: ignore[method-assign]
        host._require_threads_capability = lambda: None  # type: ignore[method-assign]

        opened = await host._handle_threads_open({"thread_id": "thread-1"}, "rpc-1")

        assert opened["goal_pending"]["status"] == "ready"
        assert opened["goal_activities"][-1]["summary"] == "目标请求已恢复"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_terminal_reconcile_clears_prior_blocker_without_revision_bump(
    tmp_path: Path,
) -> None:
    from harness_agent.goals.context import goal_run_binding

    host = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=tmp_path)
    try:
        persistence = await host._ensure_thread_persistence()
        store = persistence.goal_store()
        goal = await _create_goal(store)
        blocked = await store.block_goal(
            thread_id="thread-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            note="缺少凭据",
            now_ms=10,
        )
        activated = await store.activate_from_blocked(
            thread_id="thread-1",
            goal_id=blocked.goal_id,
            goal_revision=blocked.revision,
            now_ms=11,
        )
        run = SimpleNamespace(
            persistence=persistence,
            thread_id="thread-1",
            run_id="run-1",
            context_summary={},
            preparation=SimpleNamespace(
                goal_binding=goal_run_binding(activated, actual_primary_profile_id="fast")
            ),
        )

        await host._reconcile_goal_terminal(run, _Port())

        snapshot = await store.inspect("thread-1")
        assert snapshot.goal is not None
        assert snapshot.goal.status == "active"
        assert snapshot.goal.prior_blocker is None
        assert snapshot.goal.revision == activated.revision
    finally:
        await host.close()


class _Port:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, _run, event_type: str, payload: dict[str, object]) -> None:
        self.events.append((event_type, payload))


async def _create_goal(store):
    await store.request(
        thread_id="thread-1",
        request_id="request-1",
        kind="create",
        input_text="完成测试",
        expected_goal_id=None,
        expected_revision=None,
        ready=True,
        now_ms=1,
    )
    await store.save_proposal(
        request_id="request-1",
        objective="完成测试",
        assumptions=(),
        criteria=("测试通过",),
        now_ms=2,
    )
    return (await store.apply_proposal(
        request_id="request-1",
        criteria=("测试通过",),
        now_ms=3,
    )).goal
