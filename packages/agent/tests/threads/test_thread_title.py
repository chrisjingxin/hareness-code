"""Thread 短标题：schema v20、规范化与用户写入覆盖自动占位。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import harness_agent.threads.thread_persistence as thread_persistence_module
from harness_agent.threads.thread_persistence import ThreadPersistence, ThreadPersistenceError
from tests.support.thread_fixtures import accept_thread


@pytest.mark.asyncio
async def test_fresh_database_uses_schema_v20_and_null_title(tmp_path: Path) -> None:
    """新库是 v20；尚未改名的 thread 摘要 title 为 null。"""
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=tmp_path)
    await accept_thread(store, "thread-1", "请检查当前改动")
    listed = await store.list_threads()
    opened = await store.open_thread("thread-1")
    database_path = store.database_path
    await store.close()

    assert listed[0].title is None
    assert opened.summary.title is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(harness_threads)")
        }
    assert {"title", "title_origin"} <= columns


@pytest.mark.asyncio
async def test_v19_database_migrates_title_columns_as_null(tmp_path: Path) -> None:
    """现有 v19 库升到 v20 后旧行没有标题。"""
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=tmp_path)
    await accept_thread(initial, "old-thread", "历史会话")
    database_path = initial.database_path
    await initial.close()
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(harness_threads)")
        }
        if "title" in columns:
            connection.execute("ALTER TABLE harness_threads DROP COLUMN title")
        if "title_origin" in columns:
            connection.execute("ALTER TABLE harness_threads DROP COLUMN title_origin")
        connection.execute("PRAGMA user_version=19")
        connection.commit()

    migrated = await ThreadPersistence.open(project=project, home=tmp_path)
    try:
        summary = (await migrated.list_threads())[0]
        assert summary.thread_id == "old-thread"
        assert summary.title is None
        assert summary.first_message == "历史会话"
    finally:
        await migrated.close()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20


@pytest.mark.asyncio
async def test_set_user_title_normalizes_and_does_not_bump_updated_at(tmp_path: Path) -> None:
    """用户标题折叠空白、去掉包裹引号、截到 20 码点，且不改 updated_at。"""
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=tmp_path)
    await accept_thread(store, "thread-1", "请检查当前改动")
    before = (await store.list_threads())[0]
    updated = await store.set_user_title("thread-1", '  "一二三四五六七八九十一二三四五六七八九十超出"  \n')
    after = (await store.list_threads())[0]
    await store.close()

    assert updated.title == "一二三四五六七八九十一二三四五六七八九十"
    assert len(updated.title) == 20
    assert after.title == updated.title
    assert after.updated_at_ms == before.updated_at_ms


@pytest.mark.asyncio
async def test_set_user_title_rejects_empty_and_missing_thread(tmp_path: Path) -> None:
    """空标题与不存在的 thread 返回稳定错误码。"""
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=tmp_path)
    await accept_thread(store, "thread-1", "请检查当前改动")
    with pytest.raises(ThreadPersistenceError, match="TITLE_EMPTY"):
        await store.set_user_title("thread-1", "   \n")
    with pytest.raises(ThreadPersistenceError, match="THREAD_NOT_FOUND"):
        await store.set_user_title("missing", "修索引")
    assert (await store.list_threads())[0].title is None
    await store.close()


@pytest.mark.asyncio
async def test_set_user_title_overwrites_auto_placeholder(tmp_path: Path) -> None:
    """用户写入覆盖已有 auto 标题。"""
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=tmp_path)
    await accept_thread(store, "thread-1", "请检查当前改动")
    auto = await store.apply_auto_title("thread-1", "自动短标题")
    assert auto is not None
    assert auto.title == "自动短标题"
    user = await store.set_user_title("thread-1", "修索引")
    second_auto = await store.apply_auto_title("thread-1", "迟到的自动名")
    await store.close()

    assert user.title == "修索引"
    assert second_auto is None


@pytest.mark.asyncio
async def test_apply_auto_title_does_not_overwrite_existing_auto(tmp_path: Path) -> None:
    """已被 auto 占用时，迟到的自动写入必须返回 None。"""
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=tmp_path)
    await accept_thread(store, "thread-1", "请检查当前改动")
    first = await store.apply_auto_title("thread-1", "自动短标题")
    second = await store.apply_auto_title("thread-1", "另一自动名")
    listed = await store.list_threads()
    await store.close()

    assert first is not None
    assert first.title == "自动短标题"
    assert second is None
    assert listed[0].title == "自动短标题"
