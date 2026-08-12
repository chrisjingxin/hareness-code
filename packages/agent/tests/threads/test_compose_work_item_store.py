"""ComposeWorkItem SQLite 事实层测试。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from harness_agent.compose.models import ComposeWorkItemStatus, ThreadMode
from harness_agent.runtime.execution_binding import RunExecutionBinding
from harness_agent.threads.compose_work_item_store import (
    BindRunToWorkItem,
    ComposeWorkItemStoreError,
    CreateComposeWorkItem,
    TerminalizeComposeWorkItem,
)
from harness_agent.threads.thread_persistence import (
    AcceptRun,
    ThreadPersistence,
    ThreadPersistenceError,
)
from tests.support.thread_fixtures import test_binding as make_test_binding


async def _persistence(tmp_path: Path) -> ThreadPersistence:
    """建立隔离 project/home 的真实 SQLite ThreadPersistence。"""
    project = tmp_path / "project"
    project.mkdir()
    return await ThreadPersistence.open(project=project, home=tmp_path / "home")


def _accept(thread_id: str, run_id: str, mode: ThreadMode) -> AcceptRun:
    """构造具有固定模式的 Run 受理命令。"""
    binding: RunExecutionBinding = make_test_binding(thread_id, run_id)
    return AcceptRun(message="实现 Compose Work Item", binding=binding, mode=mode)


def _create(work_item_id: str, *, slug: str = "compose-store") -> CreateComposeWorkItem:
    """构造一个最小可恢复的 Work Item 创建命令。"""
    return CreateComposeWorkItem(
        thread_id="thread-compose",
        work_item_id=work_item_id,
        slug=slug,
        goal="将 Compose 持久化为 Work Item",
        created_at_ms=1_700_000_000_000,
    )


async def _prepare_compose_thread(persistence: ThreadPersistence) -> None:
    """通过真实 Run 受理冻结 Work Item 所在 Thread 的 Compose mode。"""
    await persistence.accept_run(
        _accept("thread-compose", "run-compose", ThreadMode.COMPOSE)
    )


async def test_first_accepted_run_freezes_thread_mode_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    """首个有效 Run 原子冻结模式；后续 mode 不同不能写入 Thread。"""
    persistence = await _persistence(tmp_path)
    try:
        await persistence.accept_run(_accept("thread-mode", "run-build", ThreadMode.BUILD))
        store = persistence.compose_work_item_store()

        assert await store.load_thread_mode("thread-mode") is ThreadMode.BUILD
        with pytest.raises(ThreadPersistenceError, match="THREAD_MODE_LOCKED"):
            await persistence.accept_run(
                _accept("thread-mode", "run-compose", ThreadMode.COMPOSE)
            )
        assert await store.load_thread_mode("thread-mode") is ThreadMode.BUILD
    finally:
        await persistence.close()


@pytest.mark.parametrize(
    "terminal_status",
    (ComposeWorkItemStatus.COMPLETED, ComposeWorkItemStatus.ABANDONED),
)
async def test_work_item_terminal_cas_allows_next_item_but_rejects_stale_update(
    tmp_path: Path,
    terminal_status: ComposeWorkItemStatus,
) -> None:
    """同 Thread 仅一项未终结，terminal CAS 后才能创建下一项。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        first = await store.create(_create("work-1"))

        assert first.status is ComposeWorkItemStatus.ACTIVE
        assert first.revision == 0
        with pytest.raises(ComposeWorkItemStoreError, match="COMPOSE_WORK_ITEM_CONFLICT"):
            await store.create(_create("work-2", slug="another-item"))

        with pytest.raises(
            ComposeWorkItemStoreError,
            match="COMPOSE_WORK_ITEM_REVISION_CONFLICT",
        ):
            await store.terminalize(
                TerminalizeComposeWorkItem(
                    work_item_id=first.work_item_id,
                    expected_revision=1,
                    status=terminal_status,
                    terminal_at_ms=1_700_000_000_050,
                )
            )
        assert (await store.load(first.work_item_id)) == first

        completed = await store.terminalize(
            TerminalizeComposeWorkItem(
                work_item_id=first.work_item_id,
                expected_revision=0,
                status=terminal_status,
                terminal_at_ms=1_700_000_000_100,
            )
        )
        assert completed.status is terminal_status
        assert completed.revision == 1
        with pytest.raises(ComposeWorkItemStoreError, match="COMPOSE_WORK_ITEM_TERMINAL"):
            await store.terminalize(
                TerminalizeComposeWorkItem(
                    work_item_id=first.work_item_id,
                    expected_revision=1,
                    status=ComposeWorkItemStatus.ABANDONED,
                    terminal_at_ms=1_700_000_000_200,
                )
            )

        second = await store.create(_create("work-2", slug="another-item"))
        assert second.status is ComposeWorkItemStatus.ACTIVE
        assert await store.load_active("thread-compose") == second
    finally:
        await persistence.close()


async def test_run_binding_is_idempotent_but_cannot_change_work_item(
    tmp_path: Path,
) -> None:
    """Run 一旦绑定 Work Item，重试可复用，改绑必须 fail closed。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        first = await store.create(_create("work-1"))

        first_binding = BindRunToWorkItem(
            thread_id=first.thread_id,
            run_id="run-1",
            work_item_id=first.work_item_id,
            created_at_ms=1_700_000_000_100,
        )
        assert await store.bind_run(first_binding)
        assert not await store.bind_run(first_binding)
        assert await store.load_run_binding("thread-compose", "run-1") == "work-1"

        await store.terminalize(
            TerminalizeComposeWorkItem(
                work_item_id=first.work_item_id,
                expected_revision=0,
                status=ComposeWorkItemStatus.COMPLETED,
                terminal_at_ms=1_700_000_000_200,
            )
        )
        second = await store.create(_create("work-2", slug="another-item"))
        with pytest.raises(ComposeWorkItemStoreError, match="RUN_WORK_ITEM_BINDING_CONFLICT"):
            await store.bind_run(
                BindRunToWorkItem(
                    thread_id=second.thread_id,
                    run_id="run-1",
                    work_item_id=second.work_item_id,
                    created_at_ms=1_700_000_000_300,
                )
            )
    finally:
        await persistence.close()


async def test_concurrent_create_allows_exactly_one_nonterminal_work_item(
    tmp_path: Path,
) -> None:
    """共享事务锁与数据库约束共同阻止同一 Thread 产生两个 active Work Item。"""
    persistence = await _persistence(tmp_path)
    try:
        await _prepare_compose_thread(persistence)
        store = persistence.compose_work_item_store()
        results = await asyncio.gather(
            store.create(_create("work-1")),
            store.create(_create("work-2", slug="another-item")),
            return_exceptions=True,
        )

        created = [item for item in results if not isinstance(item, BaseException)]
        conflicts = [item for item in results if isinstance(item, ComposeWorkItemStoreError)]
        assert len(created) == 1
        assert len(conflicts) == 1
        assert conflicts[0].code == "COMPOSE_WORK_ITEM_CONFLICT"
        assert await store.load_active("thread-compose") == created[0]
    finally:
        await persistence.close()


async def test_v13_database_rebuilds_work_item_schema_without_old_compose_fallback(
    tmp_path: Path,
) -> None:
    """v13 数据库升级时只建立 Work Item schema，不把旧 ComposeRun 当新事实。"""
    persistence = await _persistence(tmp_path)
    database = persistence.database_path
    await persistence.close()

    connection = sqlite3.connect(database)
    try:
        for table in (
            "harness_thread_modes",
            "harness_compose_work_item_run_bindings",
            "harness_compose_work_item_evidence",
            "harness_compose_work_item_effects",
            "harness_compose_work_item_activities",
            "harness_compose_work_item_confirmations",
            "harness_compose_work_item_documents",
            "harness_compose_work_items",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("PRAGMA user_version=13")
        connection.commit()
    finally:
        connection.close()

    rebuilt = await ThreadPersistence.open(project=tmp_path / "project", home=tmp_path / "home")
    try:
        store = rebuilt.compose_work_item_store()
        assert await store.load_active("thread-compose") is None
        assert await store.load_thread_mode("thread-compose") is None
    finally:
        await rebuilt.close()


async def test_v14_database_missing_work_item_table_is_rejected(tmp_path: Path) -> None:
    """已标为 v14 却缺 Work Item 表时不能静默启动或回落旧 ComposeRun。"""
    persistence = await _persistence(tmp_path)
    database = persistence.database_path
    await persistence.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE harness_compose_work_items")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ThreadPersistenceError, match="harness_compose_work_items"):
        await ThreadPersistence.open(project=tmp_path / "project", home=tmp_path / "home")


async def test_v14_database_with_nonunique_active_index_is_rejected(tmp_path: Path) -> None:
    """唯一 nonterminal 约束的索引被篡改后必须在 open 时 fail closed。"""
    persistence = await _persistence(tmp_path)
    database = persistence.database_path
    await persistence.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP INDEX harness_compose_work_items_one_active")
        connection.execute(
            """
            CREATE INDEX harness_compose_work_items_one_active
                ON harness_compose_work_items(project_fingerprint, thread_id)
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ThreadPersistenceError,
        match="harness_compose_work_items_one_active",
    ):
        await ThreadPersistence.open(project=tmp_path / "project", home=tmp_path / "home")


async def test_v14_database_missing_work_item_terminal_column_is_rejected(
    tmp_path: Path,
) -> None:
    """已标为 v14 的 Work Item 表不能缺少 terminal 事实字段。"""
    persistence = await _persistence(tmp_path)
    database = persistence.database_path
    await persistence.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "ALTER TABLE harness_compose_work_items DROP COLUMN terminal_at_ms"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ThreadPersistenceError, match="harness_compose_work_items"):
        await ThreadPersistence.open(project=tmp_path / "project", home=tmp_path / "home")


async def test_corrupt_work_item_record_fails_closed_on_reopen(tmp_path: Path) -> None:
    """SQLite 行违反 Work Item invariant 时不能被重新解释为可恢复目标。"""
    persistence = await _persistence(tmp_path)
    database = persistence.database_path
    try:
        await _prepare_compose_thread(persistence)
        await persistence.compose_work_item_store().create(_create("work-1"))
    finally:
        await persistence.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE harness_compose_work_items SET revision = -1 WHERE work_item_id = 'work-1'"
        )
        connection.commit()
    finally:
        connection.close()

    reopened = await ThreadPersistence.open(project=tmp_path / "project", home=tmp_path / "home")
    try:
        with pytest.raises(ComposeWorkItemStoreError, match="COMPOSE_WORK_ITEM_RECORD_INVALID"):
            await reopened.compose_work_item_store().load_active("thread-compose")
    finally:
        await reopened.close()
