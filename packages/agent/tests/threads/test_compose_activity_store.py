"""Compose activity 有界存储与上限语义测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from harness_agent.threads.compose_activity_store import (
    COMPOSE_ACTIVITY_MAX_RECORDS,
    COMPOSE_ACTIVITY_MAX_TEXT_BYTES,
    ComposeActivityRecord,
    ComposeActivityStore,
    bound_activity_text,
)
from harness_agent.threads.thread_persistence import ThreadPersistence


async def _store(tmp_path: Path) -> ThreadPersistence:
    project = tmp_path / "project"
    project.mkdir()
    return await ThreadPersistence.open(project=project, home=tmp_path / "home")


def _record(**overrides: object) -> ComposeActivityRecord:
    base = dict(
        run_id="run-1",
        event_sequence=1,
        activity_id="act-1",
        stage="understand",
        attempt=1,
        kind="summary",
        label="understand",
        status="passed",
        created_at_ms=1,
        bounded_text="hello",
    )
    base.update(overrides)
    return ComposeActivityRecord(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_compose_activity_append_and_list_for_thread(tmp_path: Path) -> None:
    persistence = await _store(tmp_path)
    try:
        # seed compose run row so join by thread works
        await persistence._connection.execute(
            """
            INSERT INTO harness_compose_runs (
                project_fingerprint, thread_id, run_id, revision, stage, status,
                stages_json, stage_attempts_json, schema_retry_used_json, tasks_json,
                evidence_json, verify_fix_round, review_fix_round, terminal_count, updated_at_ms
            ) VALUES (?, 'thread-1', 'run-1', 1, 'understand', 'completed',
                      '{}', '{}', '{}', '[]', '[]', 0, 0, 1, 1)
            """,
            (persistence._project_fingerprint,),
        )
        await persistence._connection.commit()
        await persistence.append_compose_activity(_record(event_sequence=1, bounded_text="摘要A"))
        await persistence.append_compose_activity(
            _record(
                event_sequence=2,
                kind="tool_terminal",
                label="execute",
                status="completed",
                bounded_text="ok",
            )
        )
        rows = await persistence.compose_activity_store().list_for_thread("thread-1")
        assert [row.kind for row in rows] == ["summary", "tool_terminal"]
        assert rows[0].bounded_text == "摘要A"
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_compose_activity_limits_append_single_truncation(tmp_path: Path) -> None:
    persistence = await _store(tmp_path)
    try:
        store = persistence.compose_activity_store()
        for index in range(COMPOSE_ACTIVITY_MAX_RECORDS):
            await store.append(
                _record(
                    event_sequence=index + 1,
                    activity_id=f"act-{index}",
                    bounded_text=f"r{index}",
                )
            )
        await store.append(_record(event_sequence=COMPOSE_ACTIVITY_MAX_RECORDS + 1, activity_id="overflow"))
        await store.append(_record(event_sequence=COMPOSE_ACTIVITY_MAX_RECORDS + 2, activity_id="overflow-2"))
        rows = await store.list_for_run("run-1")
        kinds = [row.kind for row in rows]
        assert kinds.count("truncation") == 1
        assert kinds[-1] == "truncation"
        assert len(rows) == COMPOSE_ACTIVITY_MAX_RECORDS + 1
    finally:
        await persistence.close()


def test_bound_activity_text_respects_4kib() -> None:
    text = "x" * (COMPOSE_ACTIVITY_MAX_TEXT_BYTES + 50)
    bounded = bound_activity_text(text)
    assert bounded is not None
    assert len(bounded.encode("utf-8")) <= COMPOSE_ACTIVITY_MAX_TEXT_BYTES
