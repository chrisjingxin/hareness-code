"""用户级 SQLite thread 存储：重启、project 隔离、迁移和损坏诊断回归测试。"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path
from typing import Any, Sequence

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langgraph.checkpoint.base import empty_checkpoint

from harness_agent.thread_store import ThreadStore, ThreadStoreError


class ToolCallingFakeChatModel(GenericFakeChatModel):
    """满足 deepagents 工具绑定契约的最小离线模型。"""

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        """测试不执行工具，只需保持模型可被图编译。"""
        return self


async def test_thread_store_recovers_messages_after_reopen(tmp_path: Path) -> None:
    """相同 project 和 thread_id 重开数据库后必须读取此前 checkpoint 消息。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    first = await ThreadStore.open(project=project, home=home)
    await first.record_message("thread-1", "请检查当前改动")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="请检查当前改动"),
            AIMessage(content="我会先读取变更。"),
        ]
    }
    await first.checkpointer.aput(first.graph_config("thread-1"), checkpoint, {}, {})
    await first.refresh_thread("thread-1")
    first_fingerprint = first.project_fingerprint
    database_path = first.database_path
    await first.close()

    second = await ThreadStore.open(project=project, home=home)
    opened = await second.open_thread("thread-1")
    assert second.project_fingerprint == first_fingerprint
    assert [(message.kind, message.content) for message in opened.messages] == [
        ("user", "请检查当前改动"),
        ("assistant", "我会先读取变更。"),
    ]
    assert opened.summary.first_message == "请检查当前改动"
    assert opened.summary.message_count == 2
    assert second.graph_config("thread-1")["configurable"]["checkpoint_ns"] == first_fingerprint
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(database_path.parent.stat().st_mode) == 0o700
    await second.close()


async def test_thread_store_reuses_langgraph_state_across_graph_restart(tmp_path: Path) -> None:
    """两个独立 Agent 图通过同一 thread_id 和 project namespace 累积 message 状态。"""
    from harness_agent.agent import create_harness_agent

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    first = await ThreadStore.open(project=project, home=home)
    first_model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="第一轮回答")]))
    first_model.profile = {"max_input_tokens": 200000}
    first_agent = create_harness_agent(
        first_model,
        cwd=str(project),
        checkpointer=first.checkpointer,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
    )
    await first.record_message("thread-1", "第一轮请求")
    _ = [
        event
        async for event in first_agent.astream(
            {"messages": [HumanMessage(content="第一轮请求")]},
            config=first.graph_config("thread-1"),
            stream_mode=["messages", "updates"],
        )
    ]
    await first.refresh_thread("thread-1")
    await first.close()

    second = await ThreadStore.open(project=project, home=home)
    second_model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="第二轮回答")]))
    second_model.profile = {"max_input_tokens": 200000}
    second_agent = create_harness_agent(
        second_model,
        cwd=str(project),
        checkpointer=second.checkpointer,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
    )
    await second.record_message("thread-1", "第二轮请求")
    _ = [
        event
        async for event in second_agent.astream(
            {"messages": [HumanMessage(content="第二轮请求")]},
            config=second.graph_config("thread-1"),
            stream_mode=["messages", "updates"],
        )
    ]
    await second.refresh_thread("thread-1")
    opened = await second.open_thread("thread-1")
    assert [message.content for message in opened.messages if message.kind == "user"] == [
        "第一轮请求",
        "第二轮请求",
    ]
    await second.close()


async def test_thread_store_keeps_projects_isolated_without_raw_paths(tmp_path: Path) -> None:
    """同一全局数据库中不同 project 不能列出或打开彼此的 thread。"""
    home = tmp_path / "home"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    first = await ThreadStore.open(project=project_a, home=home)
    await first.record_message("same-thread", "仅属于 project A")
    database = first.database_path
    await first.close()

    second = await ThreadStore.open(project=project_b, home=home)
    assert await second.list_threads() == ()
    with pytest.raises(ThreadStoreError, match="THREAD_NOT_FOUND"):
        await second.open_thread("same-thread")
    await second.close()

    connection = sqlite3.connect(database)
    try:
        fingerprints = [row[0] for row in connection.execute("SELECT project_fingerprint FROM harness_threads")]
    finally:
        connection.close()
    assert fingerprints and str(project_a) not in fingerprints


async def test_thread_store_persists_immutable_prompt_epoch_without_rescan(tmp_path: Path) -> None:
    """恢复 epoch 应逐字返回已保存前缀，并拒绝同一 thread 的后续形状变化。"""
    from harness_agent.prompting import PromptComposer

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    epoch = PromptComposer("core").create_epoch(
        thread_id="thread-epoch",
        execution_boundary="execution",
        environment={"workspace": "logical-workspace"},
        readonly_memory="memory",
        skill_index="<skills />",
        tool_fingerprint="schema",
        now_ms=1,
    )
    first = await ThreadStore.open(project=project, home=home)
    await first.save_prompt_epoch(epoch)
    assert (await first.get_prompt_epoch("thread-epoch")) == epoch
    changed = PromptComposer("different core").create_epoch(
        thread_id="thread-epoch",
        execution_boundary="execution",
        environment={"workspace": "logical-workspace"},
        readonly_memory="memory",
        skill_index="<skills />",
        tool_fingerprint="schema",
        now_ms=1,
    )
    with pytest.raises(ThreadStoreError, match="PROMPT_EPOCH_IMMUTABLE"):
        await first.save_prompt_epoch(changed)
    await first.close()

    second = await ThreadStore.open(project=project, home=home)
    assert (await second.get_prompt_epoch("thread-epoch")) == epoch
    await second.close()


async def test_thread_store_reports_future_schema_and_closed_store(tmp_path: Path) -> None:
    """未来 schema 不能被旧版静默写回，关闭连接后也不得继续读写。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadStore.open(project=project, home=home)
    database = store.database_path
    await store.close()
    with pytest.raises(ThreadStoreError, match="CHECKPOINT_STORE_CLOSED"):
        await store.list_threads()

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version=99")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ThreadStoreError, match="CHECKPOINT_SCHEMA_TOO_NEW"):
        await ThreadStore.open(project=project, home=home)


async def test_thread_store_reports_corrupt_database(tmp_path: Path) -> None:
    """损坏的 SQLite 文件需要返回明确的 checkpoint 损坏诊断。"""
    home = tmp_path / "home"
    database = home / ".harness" / "threads.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")
    with pytest.raises(ThreadStoreError, match="CHECKPOINT_DATABASE_CORRUPT"):
        await ThreadStore.open(project=tmp_path, home=home)
