"""GoalStore 的投影、幂等、CAS 与 Thread 隔离契约。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness_agent.goals.models import (
    GOAL_MAX_ASSUMPTIONS,
    GOAL_MAX_ASSUMPTION_CHARS,
    GOAL_MAX_CRITERIA,
    GOAL_MAX_CRITERION_CHARS,
    GoalStoreError,
    validate_goal_items,
    validate_goal_payload,
)
from harness_agent.threads.thread_persistence import ThreadPersistence


@pytest.mark.asyncio
async def test_create_review_restore_without_transcript(tmp_path: Path) -> None:
    """首次 Goal 原子建立空 Thread；接受后恢复稳定 criterion ID。"""
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        requested = await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成协议升级",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=10,
        )
        assert requested.disposition == "ready"
        assert requested.pending.status == "ready"
        await store.save_proposal(
            request_id="request-1",
            objective="完成协议升级",
            assumptions=("当前协议尚未发布",),
            criteria=("协议契约通过", "两端类型检查通过"),
            now_ms=20,
        )
        applied = await store.apply_proposal(
            request_id="request-1",
            criteria=("协议契约通过", "两端类型检查通过"),
            now_ms=30,
        )
        assert applied.goal.revision == 1
        assert [item.criterion_id for item in applied.goal.criteria] == [
            "criterion-1",
            "criterion-2",
        ]
        assert applied.continuation.goal_id == applied.goal.goal_id

        opened = await persistence.open_thread("thread-1")
        assert opened is not None
        assert opened.messages == ()
        snapshot = await store.inspect("thread-1")
        assert snapshot.goal == applied.goal
        assert snapshot.pending is None
        assert snapshot.activities[-1].kind == "lifecycle"
    finally:
        await persistence.close()

    reopened = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        assert (await reopened.goal_store().inspect("thread-1")).goal == applied.goal
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_request_is_idempotent_and_rejects_conflict_and_second_pending(
    tmp_path: Path,
) -> None:
    """同 request 可重放，不同参数或第二个 pending fail closed。"""
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        command = dict(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成测试",
            expected_goal_id=None,
            expected_revision=None,
            ready=False,
            now_ms=10,
        )
        first = await store.request(**command)
        assert await store.request(**{**command, "now_ms": 99}) == first
        with pytest.raises(GoalStoreError, match="GOAL_REQUEST_ID_CONFLICT"):
            await store.request(**{**command, "input_text": "另一个目标"})
        with pytest.raises(GoalStoreError, match="GOAL_OPERATION_IN_PROGRESS"):
            await store.request(**{**command, "request_id": "request-2"})
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_limits_cas_and_project_namespace_are_enforced(tmp_path: Path) -> None:
    """文本上限、base identity 与 project namespace 不能被绕过。"""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    first = await ThreadPersistence.open(project=project_a, home=tmp_path)
    second = await ThreadPersistence.open(project=project_b, home=tmp_path)
    try:
        with pytest.raises(GoalStoreError, match="GOAL_OBJECTIVE_INVALID"):
            await first.goal_store().request(
                thread_id="thread-1",
                request_id="request-long",
                kind="create",
                input_text="x" * 8001,
                expected_goal_id=None,
                expected_revision=None,
                ready=True,
                now_ms=1,
            )
        await first.goal_store().request(
            thread_id="thread-long-ok",
            request_id="request-long-ok",
            kind="create",
            input_text="x" * 8000,
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )
        await first.goal_store().cancel_proposal("request-long-ok", now_ms=2)
        await first.goal_store().request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成测试",
            expected_goal_id=None,
            expected_revision=None,
            ready=True,
            now_ms=1,
        )
        assert (await second.goal_store().inspect("thread-1")).pending is None
        with pytest.raises(GoalStoreError, match="GOAL_REVISION_CONFLICT"):
            await first.goal_store().apply_proposal(
                request_id="request-1",
                criteria=("测试通过",),
                expected_goal_id="stale-goal",
                expected_revision=1,
                now_ms=2,
            )
    finally:
        await first.close()
        await second.close()


def test_proposal_item_and_combined_limits_match_spec() -> None:
    assert len(validate_goal_items(
        ("a" * GOAL_MAX_ASSUMPTION_CHARS,) * GOAL_MAX_ASSUMPTIONS,
        allow_empty=True,
        max_items=GOAL_MAX_ASSUMPTIONS,
        max_chars=GOAL_MAX_ASSUMPTION_CHARS,
    )) == GOAL_MAX_ASSUMPTIONS
    assert len(validate_goal_items(
        ("c" * GOAL_MAX_CRITERION_CHARS,) * GOAL_MAX_CRITERIA,
        allow_empty=False,
    )) == GOAL_MAX_CRITERIA
    with pytest.raises(GoalStoreError, match="GOAL_CRITERIA_INVALID"):
        validate_goal_payload("o" * 8_000, ("a" * 1_000, "b" * 1_000), ("c" * 2_001,))


@pytest.mark.asyncio
async def test_goal_activity_restore_is_ordered_and_bounded_to_latest_fifty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        for index in range(55):
            await persistence._connection.execute(  # type: ignore[attr-defined]
                """
                INSERT INTO harness_goal_activities(
                    project_fingerprint, thread_id, activity_id, kind, summary, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    persistence.project_fingerprint,
                    "thread-1",
                    f"activity-{index:02d}",
                    "lifecycle",
                    f"活动 {index}",
                    index,
                ),
            )
        await persistence._connection.commit()  # type: ignore[attr-defined]

        activities = (await persistence.goal_store().inspect("thread-1")).activities

        assert len(activities) == 50
        assert activities[0].activity_id == "activity-05"
        assert activities[-1].activity_id == "activity-54"
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_fresh_database_uses_schema_v19_and_goal_tables(tmp_path: Path) -> None:
    """ThreadPersistence 的 canonical bootstrap 必须建立 Goal 表（当前 schema 含 v20 标题列）。"""
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    database_path = persistence.database_path
    await persistence.close()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'harness_goal%'"
            )
        }
    assert tables == {
        "harness_goals",
        "harness_goal_current",
        "harness_goal_pending",
        "harness_goal_evaluations",
        "harness_goal_activities",
        "harness_goal_operations",
    }


@pytest.mark.asyncio
async def test_v18_database_migrates_through_canonical_worker(tmp_path: Path) -> None:
    """现有 v18 数据库经统一备份/child 路径新增 Goal 表。"""
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=tmp_path)
    database_path = initial.database_path
    await initial.close()
    with sqlite3.connect(database_path) as connection:
        for table in (
            "harness_goal_operations",
            "harness_goal_activities",
            "harness_goal_evaluations",
            "harness_goal_pending",
            "harness_goal_current",
            "harness_goals",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=18")
        connection.commit()

    migrated = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        assert (await migrated.goal_store().inspect("unknown")).goal is None
    finally:
        await migrated.close()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20


@pytest.mark.asyncio
async def test_pause_resume_and_clear_preserve_history(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    database_path = persistence.database_path
    try:
        store = persistence.goal_store()
        goal = await _create_goal(store)

        paused = await store.mutate(
            thread_id="thread-1",
            operation_id="pause-1",
            expected_goal_id=goal.goal_id,
            expected_revision=goal.revision,
            action="pause",
            apply_now=True,
            now_ms=10,
        )
        assert paused.disposition == "applied"
        assert paused.goal is not None
        assert paused.goal.status == "paused"
        assert paused.goal.revision == 2

        idempotent = await store.mutate(
            thread_id="thread-1",
            operation_id="pause-2",
            expected_goal_id=paused.goal.goal_id,
            expected_revision=paused.goal.revision,
            action="pause",
            apply_now=True,
            now_ms=11,
        )
        assert idempotent.goal == paused.goal

        resumed = await store.mutate(
            thread_id="thread-1",
            operation_id="resume-1",
            expected_goal_id=paused.goal.goal_id,
            expected_revision=paused.goal.revision,
            action="resume",
            apply_now=True,
            now_ms=12,
        )
        assert resumed.goal is not None
        assert resumed.goal.status == "active"
        assert resumed.goal.revision == 3
        assert resumed.continuation is not None
        assert resumed.continuation.reason == "resumed"

        cleared = await store.mutate(
            thread_id="thread-1",
            operation_id="clear-1",
            expected_goal_id=resumed.goal.goal_id,
            expected_revision=resumed.goal.revision,
            action="clear",
            apply_now=True,
            now_ms=13,
        )
        assert cleared.goal is None
        assert (await store.inspect("thread-1")).goal is None
    finally:
        await persistence.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM harness_goals").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_mutation_queues_during_run_and_reconciles_exactly_once(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        goal = await _create_goal(store)
        queued = await store.mutate(
            thread_id="thread-1",
            operation_id="pause-1",
            expected_goal_id=goal.goal_id,
            expected_revision=goal.revision,
            action="pause",
            apply_now=False,
            now_ms=10,
        )
        assert queued.disposition == "queued"
        assert queued.goal == goal
        with pytest.raises(GoalStoreError, match="GOAL_OPERATION_IN_PROGRESS"):
            await store.mutate(
                thread_id="thread-1",
                operation_id="clear-1",
                expected_goal_id=goal.goal_id,
                expected_revision=goal.revision,
                action="clear",
                apply_now=False,
                now_ms=11,
            )

        reconciled = await store.reconcile("thread-1", now_ms=20)
        assert reconciled.changed is True
        assert reconciled.goal is not None
        assert reconciled.goal.status == "paused"
        assert reconciled.goal.revision == 2
        assert (await store.reconcile("thread-1", now_ms=21)).changed is False

        replay = await store.mutate(
            thread_id="thread-1",
            operation_id="pause-1",
            expected_goal_id=goal.goal_id,
            expected_revision=goal.revision,
            action="pause",
            apply_now=False,
            now_ms=99,
        )
        assert replay.disposition == "applied"
        assert replay.goal == reconciled.goal
    finally:
        await persistence.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "drafting", "clarifying"])
async def test_reconcile_returns_interrupted_proposal_to_ready(
    tmp_path: Path,
    status: str,
) -> None:
    project = tmp_path / status
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        await store.request(
            thread_id="thread-1",
            request_id="request-1",
            kind="create",
            input_text="完成测试",
            expected_goal_id=None,
            expected_revision=None,
            ready=status != "queued",
            now_ms=1,
        )
        if status != "queued":
            await store.set_proposal_status(
                "request-1",
                status=status,
                now_ms=2,
            )

        reconciled = await store.reconcile("thread-1", now_ms=3)

        assert reconciled.pending is not None
        assert reconciled.pending.status == "ready"
        assert reconciled.proposal_ready is True
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_reconcile_preserves_complete_review_draft(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
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
        await store.save_proposal(
            request_id="request-1",
            objective="完成测试",
            assumptions=(),
            criteria=("测试通过",),
            now_ms=2,
        )

        reconciled = await store.reconcile("thread-1", now_ms=3)

        assert reconciled.changed is False
        assert reconciled.pending is not None
        assert reconciled.pending.status == "reviewing"
        assert reconciled.proposal_ready is False
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_amend_keeps_identity_and_replace_switches_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        store = persistence.goal_store()
        original = await _create_goal(store)
        await store.request(
            thread_id="thread-1",
            request_id="amend-1",
            kind="amend",
            input_text="增加错误路径",
            expected_goal_id=original.goal_id,
            expected_revision=original.revision,
            ready=True,
            now_ms=10,
        )
        await store.save_proposal(
            request_id="amend-1",
            objective="完成测试并覆盖错误路径",
            assumptions=(),
            criteria=("成功和失败路径都通过",),
            now_ms=11,
        )
        amended = await store.apply_proposal(
            request_id="amend-1",
            criteria=("成功和失败路径都通过",),
            now_ms=12,
        )
        assert amended.goal.goal_id == original.goal_id
        assert amended.goal.revision == 2

        await store.request(
            thread_id="thread-1",
            request_id="replace-1",
            kind="replace",
            input_text="改做文档",
            expected_goal_id=amended.goal.goal_id,
            expected_revision=amended.goal.revision,
            ready=True,
            now_ms=20,
        )
        assert (await store.inspect("thread-1")).goal == amended.goal
        await store.save_proposal(
            request_id="replace-1",
            objective="完成文档",
            assumptions=(),
            criteria=("文档可读",),
            now_ms=21,
        )
        replaced = await store.apply_proposal(
            request_id="replace-1",
            criteria=("文档可读",),
            now_ms=22,
        )
        assert replaced.goal.goal_id != original.goal_id
        assert replaced.goal.revision == 1
    finally:
        await persistence.close()


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
