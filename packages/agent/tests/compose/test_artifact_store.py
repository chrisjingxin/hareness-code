"""Compose artifact 校验与 SQLite 存储测试：round-trip、终态唯一、迁移与并发。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from harness_agent.compose.models import (
    MAX_ARTIFACT_PAYLOAD_BYTES,
    ArtifactKind,
    ComposeArtifact,
    ComposeRunState,
    ComposeStoreError,
    ComposeTask,
    PlanArtifact,
    ReviewFinding,
    ReviewReport,
    UnderstandingArtifact,
    VerificationEvidence,
    find_dag_cycle,
    has_placeholder,
    make_artifact,
    validate_plan_artifact,
    validate_review_report,
    validate_understanding_artifact,
    validate_verification_evidence,
)
from harness_agent.compose.state_machine import ComposeEvent, ComposeStateMachine
from harness_agent.threads.thread_persistence import ThreadPersistence, ThreadPersistenceError
import harness_agent.threads.thread_persistence as tp_module


def _run_state(run_id: str = "run-1", *, terminal: bool = False) -> ComposeRunState:
    state = ComposeStateMachine.initial("thread-1", run_id)
    if terminal:
        return ComposeStateMachine.apply(state, ComposeEvent.CANCEL)
    return state


def _payload(kind: ArtifactKind) -> dict[str, object]:
    if kind is ArtifactKind.UNDERSTANDING:
        return {
            "goal": "实现搜索",
            "constraints": ["不引入新依赖"],
            "acceptance": ["搜索结果可排序"],
            "out_of_scope": ["索引构建"],
            "open_decisions": [],
            "change_kind": "feature",
        }
    if kind is ArtifactKind.PLAN:
        return {
            "solution": "新增 search module",
            "tasks": [
                {
                    "id": "task-1",
                    "title": "实现搜索",
                    "kind": "behavior",
                    "acceptance": "搜索返回结果",
                    "depends_on": [],
                    "verification_commands": ["pytest -q tests/test_search.py"],
                }
            ],
            "relevant_pointers": ["src/search.py"],
        }
    if kind is ArtifactKind.VERIFICATION:
        return {
            "command": "pytest -q tests/test_search.py",
            "working_dir": "packages/agent",
            "started_at_ms": 1,
            "finished_at_ms": 2,
            "exit_code": 0,
            "output_digest": "a" * 64,
            "output_summary": "3 passed",
            "truncated": False,
        }
    return {
        "requirement_verdict": "pass",
        "code_verdict": "pass",
        "findings": [],
    }


# ---------- 校验规则 ----------


def test_understanding_requires_goal_and_bounded_fields() -> None:
    artifact = validate_understanding_artifact(_payload(ArtifactKind.UNDERSTANDING))
    assert artifact.goal == "实现搜索"
    with pytest.raises(ValueError, match="goal"):
        validate_understanding_artifact({**_payload(ArtifactKind.UNDERSTANDING), "goal": ""})
    with pytest.raises(ValueError, match="acceptance"):
        validate_understanding_artifact(
            {**_payload(ArtifactKind.UNDERSTANDING), "acceptance": [""]}
        )


def test_plan_requires_acyclic_tasks_and_no_placeholder() -> None:
    payload = _payload(ArtifactKind.PLAN)
    plan = validate_plan_artifact(payload)
    assert plan.tasks[0].verification_commands == ("pytest -q tests/test_search.py",)

    cyclic = {
        **payload,
        "tasks": [
            {
                "id": "a",
                "title": "A",
                "kind": "behavior",
                "acceptance": "A ok",
                "depends_on": ["b"],
                "verification_commands": ["pytest -q a"],
            },
            {
                "id": "b",
                "title": "B",
                "kind": "behavior",
                "acceptance": "B ok",
                "depends_on": ["a"],
                "verification_commands": ["pytest -q b"],
            },
        ],
    }
    cyclic_tasks = tuple(ComposeTask(**task) for task in cyclic["tasks"])
    assert find_dag_cycle(cyclic_tasks) is not None
    with pytest.raises(ValueError, match="cycle"):
        validate_plan_artifact(cyclic)

    with pytest.raises(ValueError, match="placeholder"):
        validate_plan_artifact({**payload, "solution": "先用 {{TODO}} 实现"})

    with pytest.raises(ValueError, match="acceptance"):
        validate_plan_artifact(
            {
                **payload,
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "实现搜索",
                        "kind": "behavior",
                        "acceptance": "",
                        "depends_on": [],
                        "verification_commands": ["pytest -q tests/test_search.py"],
                    }
                ],
            }
        )


def test_verification_evidence_requires_fresh_exit_code_shape() -> None:
    evidence = validate_verification_evidence(_payload(ArtifactKind.VERIFICATION))
    assert evidence.exit_code == 0
    with pytest.raises(ValueError, match="exit_code"):
        validate_verification_evidence(
            {**_payload(ArtifactKind.VERIFICATION), "exit_code": -1}
        )
    with pytest.raises(ValueError, match="command"):
        validate_verification_evidence(
            {**_payload(ArtifactKind.VERIFICATION), "command": ""}
        )


def test_review_report_rejects_unknown_severity() -> None:
    report = validate_review_report(_payload(ArtifactKind.REVIEW))
    assert report.requirement_verdict == "pass"
    with pytest.raises(ValueError, match="verdict"):
        validate_review_report(
            {
                **_payload(ArtifactKind.REVIEW),
                "requirement_verdict": "maybe",
            }
        )
    with pytest.raises(ValueError, match="severity"):
        validate_review_report(
            {
                **_payload(ArtifactKind.REVIEW),
                "findings": [
                    {
                        "axis": "requirement",
                        "severity": "fatal",
                        "message": "缺需求",
                        "location": "acceptance-1",
                    }
                ],
            }
        )


def test_placeholder_detector_rejects_template_and_todo() -> None:
    assert has_placeholder("用 {{TODO}} 实现")
    assert has_placeholder("方案待定")
    assert has_placeholder("TBD")
    assert not has_placeholder("实现搜索并补充文档")


def test_artifact_payload_is_bounded_and_digested() -> None:
    with pytest.raises(ComposeStoreError, match="payload"):
        make_artifact(
            ArtifactKind.UNDERSTANDING,
            run_id="run-1",
            source_execution_id="root",
            created_at_ms=1,
            payload={"goal": "x" * (MAX_ARTIFACT_PAYLOAD_BYTES + 1)},
        )
    artifact = make_artifact(
        ArtifactKind.UNDERSTANDING,
        run_id="run-1",
        source_execution_id="root",
        created_at_ms=1,
        payload={"goal": "实现搜索"},
    )
    assert artifact.content_digest
    assert artifact.version == 1


# ---------- SQLite 存储 ----------


@pytest.mark.asyncio
async def test_fresh_database_has_compose_tables_at_current_schema(tmp_path: Path) -> None:
    """新库直接到达最新 schema，并包含 Compose 审计表。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    try:
        assert tp_module._SCHEMA_VERSION == 12
        connection = sqlite3.connect(persistence.database_path)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "harness_compose_runs" in tables
            assert "harness_compose_artifacts" in tables
        finally:
            connection.close()
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_run_round_trip_preserves_all_facts(tmp_path: Path) -> None:
    """save_run/load_run 保留 revision、stages、tasks、budget 与终态计数。"""
    persistence, store = await _open_store(tmp_path)
    state = _run_state("run-rt")
    state = ComposeStateMachine.apply(state, ComposeEvent.UNDERSTAND_COMPLETE)
    await store.save_run(state)

    loaded = await store.load_run("run-rt")
    assert loaded is not None
    assert loaded.revision == state.revision
    assert loaded.stage.value == state.stage.value
    assert loaded.stages == state.stages
    assert loaded.stage_attempts == state.stage_attempts
    assert loaded.tasks == state.tasks
    assert await store.terminal_count("run-rt") == 0
    await persistence.close()


@pytest.mark.asyncio
async def test_terminal_save_is_unique_and_counted(tmp_path: Path) -> None:
    """终态只能保存一次；重复保存被拒绝并保持唯一终态计数。"""
    persistence, store = await _open_store(tmp_path)
    await store.save_run(_run_state("run-term"))
    assert await store.terminal_count("run-term") == 0
    terminal = _run_state("run-term", terminal=True)
    await store.save_run(terminal)
    assert await store.terminal_count("run-term") == 1
    with pytest.raises(ComposeStoreError, match="TERMINAL_DUPLICATE"):
        await store.save_run(terminal)
    assert await store.terminal_count("run-term") == 1
    loaded = await store.load_run("run-term")
    assert loaded is not None and loaded.status.value == "cancelled"
    # 非终态不允许覆盖已终态行（终态是不可逆审计事实）。
    with pytest.raises(ComposeStoreError, match="COMPOSE_TERMINAL_OVERWRITE"):
        await store.save_run(_run_state("run-term"))
    assert await store.terminal_count("run-term") == 1
    await persistence.close()


@pytest.mark.asyncio
async def test_artifact_round_trip_with_digest(tmp_path: Path) -> None:
    """artifact 保存/读取保留 payload 与 digest。"""
    persistence, store = await _open_store(tmp_path)
    artifact = make_artifact(
        ArtifactKind.UNDERSTANDING,
        run_id="run-art",
        source_execution_id="root-exec-1",
        created_at_ms=42,
        payload=_payload(ArtifactKind.UNDERSTANDING),
    )
    await store.save_artifact(artifact)
    loaded = await store.load_artifact("run-art", artifact.artifact_id)
    assert loaded == artifact
    listed = await store.list_artifacts("run-art")
    assert [a.artifact_id for a in listed] == [artifact.artifact_id]
    await persistence.close()


@pytest.mark.asyncio
async def test_concurrent_run_writes_are_linearized(tmp_path: Path) -> None:
    """并发 save_run 不产生部分事实；最后提交的写完整可见。"""
    persistence, store = await _open_store(tmp_path)

    async def write(index: int) -> None:
        state = ComposeStateMachine.initial("thread-c", "run-c")
        state = ComposeStateMachine.apply(state, ComposeEvent.UNDERSTAND_COMPLETE)
        if index % 2:
            state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_COMPLETE)
        await store.save_run(state)

    await asyncio.gather(*(write(i) for i in range(8)))
    loaded = await store.load_run("run-c")
    assert loaded is not None
    # 最后提交的是某一完整状态：要么 plan running（revision 1），要么 waiting（revision 2）。
    assert (loaded.revision == 1 and loaded.stage.value == "plan" and loaded.status.value == "running") or (
        loaded.revision == 2 and loaded.status.value == "waiting_user"
    )
    assert loaded.understanding_artifact_id is None  # 没有半写字段
    assert await store.terminal_count("run-c") == 0
    await persistence.close()


@pytest.mark.asyncio
async def test_v11_database_migrates_to_v12_preserving_old_data(tmp_path: Path) -> None:
    """旧库升级到 v12 保留既有 Thread 数据，并新建 Compose 表。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    await initial.close()

    # 把当前库人为降级成 v11：删除 Compose 表并把版本号改回 11。
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE harness_compose_artifacts")
        connection.execute("DROP TABLE harness_compose_runs")
        connection.execute(
            "INSERT INTO harness_threads"
            " (project_fingerprint, thread_id, created_at_ms, updated_at_ms,"
            "  first_message, latest_message, message_count)"
            " VALUES ('old-fp', 'old-thread', 1, 1, '旧消息', '旧消息', 1)"
        )
        connection.execute("PRAGMA user_version=11")
        connection.commit()
    finally:
        connection.close()

    migrated = await ThreadPersistence.open(project=project, home=home)
    try:
        connection = sqlite3.connect(database)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='harness_compose_runs'"
            ).fetchone() is not None
            assert connection.execute(
                "SELECT first_message FROM harness_threads WHERE thread_id='old-thread'"
            ).fetchone()[0] == "旧消息"
        finally:
            connection.close()
    finally:
        await migrated.close()


@pytest.mark.asyncio
async def test_too_new_schema_is_rejected_without_mutation(tmp_path: Path) -> None:
    """比当前更新的 schema 必须原样拒绝，不开放数据库。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    await initial.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version=99")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_SCHEMA_TOO_NEW"):
        await ThreadPersistence.open(project=project, home=home)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        connection.close()


async def _open_store(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    store = persistence.compose_artifact_store()
    return persistence, store
