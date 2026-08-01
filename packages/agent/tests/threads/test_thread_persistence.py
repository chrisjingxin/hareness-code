"""用户级 SQLite thread 存储：重启、project 隔离、迁移和损坏诊断回归测试。"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any, Sequence

import pytest
import aiosqlite
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.checkpoint.base import empty_checkpoint

import harness_agent.thread_persistence as thread_persistence_module
from harness_agent.execution_binding import (
    RunExecutionBinding,
    SafeModelProfile,
    SelectionOrigin,
    ThreadExecutionSelection,
)
from harness_agent.threads.prompting import canonical_json
from harness_agent.threads.thread_persistence import (
    AcceptRun,
    CommitContextRewrite,
    ContextArtifactDraft,
    ContextState,
    ContextSummaryDraft,
    TranscriptAppend,
    ThreadPersistence,
    ThreadPersistenceError,
)
from thread_fixtures import accept_thread, test_binding as make_test_binding


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


def _downgrade_to_v6(database: Path, *, drop_artifact_metadata: bool = False) -> None:
    """把当前测试库还原为可迁移的 v6 形状，不触碰生产数据。"""
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE IF EXISTS harness_thread_history_metadata")
        connection.execute("DROP TABLE IF EXISTS harness_thread_transcript")
        if drop_artifact_metadata:
            connection.execute(
                "ALTER TABLE harness_context_artifacts DROP COLUMN content_sha256"
            )
            connection.execute(
                "ALTER TABLE harness_context_artifacts DROP COLUMN byte_length"
            )
        connection.execute("PRAGMA user_version=6")
        connection.commit()
    finally:
        connection.close()


def test_thread_persistence_exposes_lifecycle_interface_only() -> None:
    """表级读写不再成为业务调用方可见的 ThreadPersistence 方法。"""
    assert hasattr(ThreadPersistence, "accept_run")
    assert hasattr(ThreadPersistence, "load_run_state")
    assert hasattr(ThreadPersistence, "commit_context")
    assert hasattr(ThreadPersistence, "complete_run")
    assert hasattr(ThreadPersistence, "load_context")
    for method in (
        "record_message",
        "record_run_start",
        "get_latest_run_execution_binding",
        "load_execution_binding_state",
        "load_context_messages",
        "archive_context",
        "save_context_summary",
        "context_state",
        "set_context_state",
        "ensure_thread",
        "refresh_thread",
        "get_prompt_epoch",
        "save_prompt_epoch",
        "get_agent_engine_profile",
        "save_agent_engine_profile",
        "read_context_artifact",
        "load_context_state",
    ):
        assert not hasattr(ThreadPersistence, method)


async def test_thread_persistence_recovers_messages_after_reopen(tmp_path: Path) -> None:
    """相同 project 和 thread_id 重开数据库后必须读取此前 checkpoint 消息。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    first = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(first, "thread-1", "请检查当前改动")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="请检查当前改动"),
            AIMessage(content="我会先读取变更。"),
        ]
    }
    await first.checkpointer.aput(first.graph_config("thread-1"), checkpoint, {}, {})
    await first.append_transcript(
        TranscriptAppend(
            thread_id="thread-1",
            record_id="run:fixture-thread-1:assistant:1",
            kind="assistant",
            content="我会先读取变更。",
            run_id="fixture-thread-1",
            execution_id="root-fixture-thread-1",
        )
    )
    await first.complete_run("thread-1")
    first_fingerprint = first.project_fingerprint
    database_path = first.database_path
    await first.close()

    second = await ThreadPersistence.open(project=project, home=home)
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


async def test_checkpoint_and_transcript_writes_share_connection_operation_lock(
    tmp_path: Path,
) -> None:
    """同一连接的 checkpoint 与 Transcript 写入必须串行而不交错事务。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-lock", "开始")
    assert store.checkpointer.lock is store._lock

    for index in range(4):
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {
            "messages": [HumanMessage(content=f"checkpoint-{index}")]
        }
        await asyncio.gather(
            store.checkpointer.aput(store.graph_config("thread-lock"), checkpoint, {}, {}),
            store.append_transcript(
                TranscriptAppend(
                    thread_id="thread-lock",
                    record_id=f"run:lock:assistant:{index}",
                    kind="assistant",
                    content=f"assistant-{index}",
                    run_id="run-lock",
                    execution_id="root-run-lock",
                )
            ),
        )

    records = await store.load_transcript("thread-lock")
    assert [record.payload["content"] for record in records] == [
        "开始",
        "assistant-0",
        "assistant-1",
        "assistant-2",
        "assistant-3",
    ]
    assert await store.checkpointer.aget_tuple(store.graph_config("thread-lock")) is not None
    await store.close()


async def test_thread_persistence_reuses_langgraph_state_across_graph_restart(tmp_path: Path) -> None:
    """共享图重建后通过持久化 RunContext 恢复同一 thread 的消息和 PromptEpoch。"""
    from harness_agent.runtime.agent import create_harness_agent, create_prompt_epoch
    from harness_agent.runtime.run_context import RunContext

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    first = await ThreadPersistence.open(project=project, home=home)
    first_model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="第一轮回答")]))
    first_model.profile = {"max_input_tokens": 200000}
    epoch = create_prompt_epoch(
        thread_id="thread-1",
        system_prompt="持久化前缀",
        workspace=str(project),
        sandboxed=False,
        provider=None,
        approval_mode="yolo",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
    )
    await first.persist_prompt_epoch(epoch)
    first_agent = create_harness_agent(
        first_model,
        cwd=str(project),
        checkpointer=first.checkpointer,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
        shared_engine=True,
    )
    await accept_thread(first, "thread-1", "第一轮请求")
    _ = [
        event
        async for event in first_agent.astream(
            {"messages": [HumanMessage(content="第一轮请求")]},
            config=first.graph_config("thread-1"),
            context=RunContext(
                thread_id="thread-1",
                run_id="run-1",
                prompt_epoch=epoch,
                approval_mode="yolo",
            ),
            stream_mode=["messages", "updates"],
        )
    ]
    await first.complete_run("thread-1")
    await first.close()

    second = await ThreadPersistence.open(project=project, home=home)
    second_model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="第二轮回答")]))
    second_model.profile = {"max_input_tokens": 200000}
    restored_epoch = await second.load_prompt_epoch("thread-1")
    assert restored_epoch == epoch
    second_agent = create_harness_agent(
        second_model,
        cwd=str(project),
        checkpointer=second.checkpointer,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
        shared_engine=True,
    )
    await accept_thread(second, "thread-1", "第二轮请求")
    _ = [
        event
        async for event in second_agent.astream(
            {"messages": [HumanMessage(content="第二轮请求")]},
            config=second.graph_config("thread-1"),
            context=RunContext(
                thread_id="thread-1",
                run_id="run-2",
                prompt_epoch=restored_epoch,
                approval_mode="yolo",
            ),
            stream_mode=["messages", "updates"],
        )
    ]
    await second.complete_run("thread-1")
    opened = await second.open_thread("thread-1")
    assert [message.content for message in opened.messages if message.kind == "user"] == [
        "第一轮请求",
        "第二轮请求",
    ]
    await second.close()


async def test_thread_persistence_keeps_projects_isolated_without_raw_paths(tmp_path: Path) -> None:
    """同一全局数据库中不同 project 不能列出或打开彼此的 thread。"""
    home = tmp_path / "home"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    first = await ThreadPersistence.open(project=project_a, home=home)
    await accept_thread(first, "same-thread", "仅属于 project A")
    database = first.database_path
    await first.close()

    second = await ThreadPersistence.open(project=project_b, home=home)
    assert await second.list_threads() == ()
    with pytest.raises(ThreadPersistenceError, match="THREAD_NOT_FOUND"):
        await second.open_thread("same-thread")
    await second.close()

    connection = sqlite3.connect(database)
    try:
        fingerprints = [row[0] for row in connection.execute("SELECT project_fingerprint FROM harness_threads")]
    finally:
        connection.close()
    assert fingerprints and str(project_a) not in fingerprints


async def test_thread_persistence_persists_immutable_prompt_epoch_without_rescan(tmp_path: Path) -> None:
    """恢复 epoch 应逐字返回已保存前缀，并拒绝同一 thread 的后续形状变化。"""
    from harness_agent.threads.prompting import PromptComposer

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
    first = await ThreadPersistence.open(project=project, home=home)
    await first.persist_prompt_epoch(epoch)
    assert (await first.load_prompt_epoch("thread-epoch")) == epoch
    changed = PromptComposer("different core").create_epoch(
        thread_id="thread-epoch",
        execution_boundary="execution",
        environment={"workspace": "logical-workspace"},
        readonly_memory="memory",
        skill_index="<skills />",
        tool_fingerprint="schema",
        now_ms=1,
    )
    with pytest.raises(ThreadPersistenceError, match="PROMPT_EPOCH_IMMUTABLE"):
        await first.persist_prompt_epoch(changed)
    await first.close()

    second = await ThreadPersistence.open(project=project, home=home)
    assert (await second.load_prompt_epoch("thread-epoch")) == epoch
    await second.close()


async def test_thread_persistence_migrates_and_reads_legacy_model_bindings(tmp_path: Path) -> None:
    """v5 模型快照升级后保持只读可见，并转换为类型化状态。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    project_fingerprint = initial.project_fingerprint
    await initial.close()

    binding = {
        "roles": {
            "executor": {
                "id": "fast",
                "model": "fast-model",
                "provider_label": "Gateway",
                "context_window_tokens": 128000,
                "capabilities": ["tool-calling", "streaming"],
                "is_default": True,
                "available": True,
                "unavailable_reason": None,
                "source": "user",
            }
        }
    }
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE harness_run_execution_bindings")
        connection.execute(
            """
            INSERT INTO harness_thread_model_bindings (
                project_fingerprint, thread_id, binding_record, bound_at_ms
            ) VALUES (?, ?, ?, ?)
            """,
            (project_fingerprint, "thread-model", canonical_json(binding), 1),
        )
        connection.execute("PRAGMA user_version=5")
        connection.commit()
    finally:
        connection.close()

    store = await ThreadPersistence.open(project=project, home=home)
    state = await store.load_run_state("thread-model")
    assert state.legacy_models is not None
    assert state.legacy_models.executor_profile_id() == "fast"
    assert state.legacy_models.protocol_roles()["executor"]["model"] == "fast-model"  # type: ignore[index]
    await store.close()


async def test_thread_persistence_records_run_binding_and_recovers_latest_selection(tmp_path: Path) -> None:
    """Run 选择和实际模型须与首条 Thread 索引同事务写入，并可按时间恢复。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    primary = {
        "profile": {
            "id": "fast",
            "model": "fast-model",
            "provider_label": "Gateway",
            "context_window_tokens": 128000,
            "capabilities": ["streaming", "tool-calling"],
            "is_default": True,
            "available": True,
            "unavailable_reason": None,
            "source": "user",
        },
        "source": "thread-primary",
    }
    run_binding = RunExecutionBinding(
        thread_id="thread-run",
        run_id="run-1",
        requested_selection=ThreadExecutionSelection("fast"),
        actual_primary=SafeModelProfile.from_record(primary["profile"]),
        selection_origin=SelectionOrigin.REQUEST,
        runtime_profile_id="123456789abc",
        created_at_ms=1,
    )
    assert (
        await store.accept_run(AcceptRun(message="使用 fast", binding=run_binding))
    ).created
    # 同一请求重试不重复索引或绑定；不同内容则 fail closed。
    assert not (
        await store.accept_run(AcceptRun(message="使用 fast", binding=run_binding))
    ).created
    with pytest.raises(ThreadPersistenceError, match="RUN_EXECUTION_BINDING_CONFLICT"):
        await store.accept_run(AcceptRun(message="使用 pro", binding=run_binding))
    latest = (await store.load_run_state("thread-run")).latest_run
    assert latest is not None
    assert latest.requested_selection.to_record() == {"primary_profile": "fast"}
    assert latest.actual_primary_record() == primary
    assert latest.runtime_profile_id == "123456789abc"
    assert (await store.list_threads())[0].first_message == "使用 fast"
    await store.close()


async def test_transcript_is_ordered_idempotent_and_artifacts_keep_full_tool_text(
    tmp_path: Path,
) -> None:
    """规范记录不随 checkpoint 改写，工具大正文只在 Artifact 中保存一次。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-transcript", "先检查")

    assistant = TranscriptAppend(
        thread_id="thread-transcript",
        record_id="run:assistant:assistant:1",
        kind="assistant",
        content="检查完成。",
        run_id="run:assistant",
        execution_id="root-run:assistant",
    )
    tool_content = "工具原文-" * 20_000
    tool = TranscriptAppend(
        thread_id="thread-transcript",
        record_id="run:assistant:tool:call-1",
        kind="tool",
        content=tool_content,
        run_id="run:assistant",
        execution_id="root-run:assistant",
        tool_call_id="call-1",
        tool_name="read_file",
    )
    await store.append_transcript_batch((assistant, tool))
    duplicate = await store.append_transcript(tool)
    records = await store.load_transcript("thread-transcript")

    assert [record.sequence for record in records] == [1, 2, 3]
    assert records[2].record_id == tool.record_id
    assert records[2].artifact_id is not None
    assert duplicate.sequence == records[2].sequence
    artifact = await store.load_context_artifact("thread-transcript", records[2].artifact_id)
    assert artifact is not None
    assert artifact.content == tool_content
    assert artifact.content_sha256 == records[2].content_sha256
    assert artifact.byte_length == len(tool_content.encode("utf-8"))

    compressed_checkpoint = empty_checkpoint()
    compressed_checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="摘要：先检查"),
            AIMessage(content="压缩后的最近回答"),
        ]
    }
    await store.checkpointer.aput(
        store.graph_config("thread-transcript"), compressed_checkpoint, {}, {}
    )
    opened = await store.open_thread("thread-transcript")
    assert [(message.kind, message.content) for message in opened.messages] == [
        ("user", "先检查"),
        ("assistant", "检查完成。"),
        ("tool", "工具原文-" * 32),
    ]
    assert opened.messages[-1].tool_name == "read_file"
    assert (await store.list_threads())[0].message_count == 3
    await store.close()


async def test_transcript_rejects_duplicate_tool_call_ids_in_one_assistant(
    tmp_path: Path,
) -> None:
    """持久层拒绝同一 assistant 内重复 ID，避免后续投影关联歧义。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    try:
        await accept_thread(store, "thread-duplicate-calls", "执行")
        with pytest.raises(ThreadPersistenceError, match="TRANSCRIPT_TOOL_CALL_ID_DUPLICATE"):
            await store.append_transcript(
                TranscriptAppend(
                    thread_id="thread-duplicate-calls",
                    record_id="run:duplicate:assistant:1",
                    kind="assistant",
                    content="",
                    run_id="run-duplicate",
                    execution_id="root-run-duplicate",
                    tool_calls=(
                        {
                            "id": "same-call",
                            "name": "one",
                            "arguments": {},
                            "arguments_status": "valid",
                        },
                        {
                            "id": "same-call",
                            "name": "two",
                            "arguments": {},
                            "arguments_status": "valid",
                        },
                    ),
                )
            )
        records = await store.load_transcript("thread-duplicate-calls")
        assert [(record.kind, record.payload["content"]) for record in records] == [
            ("user", "执行")
        ]
    finally:
        await store.close()


async def test_open_thread_reads_summary_transcript_and_legacy_flag_from_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """另一连接并发追加时，open_thread 返回同一 SQLite 读快照。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    writer = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(writer, "thread-snapshot", "初始消息")
    reader = await ThreadPersistence.open(project=project, home=home)

    paused = asyncio.Event()
    release = asyncio.Event()
    writer_started = asyncio.Event()
    original_execute = reader._connection.execute
    original_append = writer._append_transcript_in_transaction

    class CursorProxy:
        def __init__(self, cursor: Any) -> None:
            self._cursor = cursor

        async def fetchone(self) -> Any:
            row = await self._cursor.fetchone()
            if not paused.is_set():
                paused.set()
                await release.wait()
            return row

        async def fetchall(self) -> Any:
            return await self._cursor.fetchall()

        async def close(self) -> None:
            await self._cursor.close()

    async def hooked_execute(sql: str, *parameters: Any) -> Any:
        cursor = await original_execute(sql, *parameters)
        if "FROM harness_threads" in sql and not paused.is_set():
            return CursorProxy(cursor)
        return cursor

    async def hooked_append(*args: Any, **kwargs: Any) -> Any:
        writer_started.set()
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(reader._connection, "execute", hooked_execute)
    monkeypatch.setattr(writer, "_append_transcript_in_transaction", hooked_append)
    open_task = asyncio.create_task(reader.open_thread("thread-snapshot"))
    await asyncio.wait_for(paused.wait(), 5)

    append_task = asyncio.create_task(
        writer.append_transcript(
            TranscriptAppend(
                thread_id="thread-snapshot",
                record_id="run:snapshot:assistant:1",
                kind="assistant",
                content="并发追加",
                run_id="run-snapshot",
                execution_id="root-run-snapshot",
            )
        )
    )
    await asyncio.wait_for(writer_started.wait(), 5)
    release.set()
    await asyncio.wait_for(append_task, 5)
    opened = await open_task

    assert opened.summary.message_count == 1
    assert [message.content for message in opened.messages] == ["初始消息"]
    after = await reader.open_thread("thread-snapshot")
    assert after.summary.message_count == 2
    assert [message.content for message in after.messages] == ["初始消息", "并发追加"]
    assert opened.legacy_incomplete_history is False
    assert after.legacy_incomplete_history is False
    await reader.close()
    await writer.close()


async def test_transcript_batch_rolls_back_artifact_and_record_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcript 与大型工具 Artifact 必须同成同败。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-rollback", "开始")
    original = store._append_transcript_in_transaction

    async def fail_after_artifact(command: TranscriptAppend):
        if command.kind == "tool":
            await original(command)
            raise aiosqlite.OperationalError("injected transcript failure")
        return await original(command)

    monkeypatch.setattr(store, "_append_transcript_in_transaction", fail_after_artifact)
    with pytest.raises(ThreadPersistenceError, match="TRANSCRIPT_WRITE_FAILED"):
        await store.append_transcript(
            TranscriptAppend(
                thread_id="thread-rollback",
                record_id="run:rollback:tool:call-1",
                kind="tool",
                content="x" * 100_000,
                run_id="run:rollback",
                execution_id="root-run:rollback",
                tool_call_id="call-1",
            )
        )
    monkeypatch.undo()
    assert len(await store.load_transcript("thread-rollback")) == 1
    connection = sqlite3.connect(store.database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM harness_context_artifacts WHERE thread_id = ?",
            ("thread-rollback",),
        ).fetchone()[0] == 0
    finally:
        connection.close()
    await store.close()


async def test_transcript_cancelled_transaction_releases_lock_for_next_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消追加必须锁内 rollback，后续事务不能继承半成品。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-cancel", "开始")
    started = asyncio.Event()
    release = asyncio.Event()
    original = store._append_transcript_in_transaction

    async def block(command: TranscriptAppend):
        started.set()
        await release.wait()
        return await original(command)

    monkeypatch.setattr(store, "_append_transcript_in_transaction", block)
    task = asyncio.create_task(
        store.append_transcript(
            TranscriptAppend(
                thread_id="thread-cancel",
                record_id="run:cancel:assistant:1",
                kind="assistant",
                content="不应提交",
                run_id="run:cancel",
                execution_id="root-run:cancel",
            )
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread-cancel",
            record_id="run:cancel:assistant:2",
            kind="assistant",
            content="后续可写",
            run_id="run:cancel",
            execution_id="root-run:cancel",
        )
    )
    assert [record.payload["content"] for record in await store.load_transcript("thread-cancel")] == [
        "开始",
        "后续可写",
    ]
    await store.close()


async def test_accept_run_cancelled_transaction_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受理事务取消后释放锁和事务，原 binding 可安全重试。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    binding = make_test_binding("thread-accept-cancel", "run-accept-cancel")
    started = asyncio.Event()
    release = asyncio.Event()
    original = store._append_transcript_in_transaction

    async def block(command: TranscriptAppend):
        started.set()
        await release.wait()
        return await original(command)

    monkeypatch.setattr(store, "_append_transcript_in_transaction", block)
    task = asyncio.create_task(
        store.accept_run(AcceptRun(message="会取消", binding=binding))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()

    assert (
        await store.accept_run(AcceptRun(message="会取消", binding=binding))
    ).created
    assert [record.kind for record in await store.load_transcript("thread-accept-cancel")] == [
        "user"
    ]
    await store.close()


async def test_v6_bootstrap_is_project_scoped_and_preserves_incomplete_boundary(
    tmp_path: Path,
) -> None:
    """v6 legacy bootstrap 不混用相同 thread_id 的两个 project，也不伪造 Run 身份。"""
    home = tmp_path / "home"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    first = await ThreadPersistence.open(project=project_a, home=home)
    await accept_thread(first, "same-thread", "A 首条")
    checkpoint_a = empty_checkpoint()
    checkpoint_a["channel_values"] = {
        "messages": [
            HumanMessage(content="A 首条"),
            AIMessage(content="A 回答"),
        ]
    }
    await first.checkpointer.aput(first.graph_config("same-thread"), checkpoint_a, {}, {})
    database = first.database_path
    await first.close()

    second = await ThreadPersistence.open(project=project_b, home=home)
    await accept_thread(second, "same-thread", "B 首条")
    checkpoint_b = empty_checkpoint()
    checkpoint_b["channel_values"] = {
        "messages": [
            HumanMessage(content="B 首条"),
            AIMessage(content="B 回答"),
        ]
    }
    await second.checkpointer.aput(second.graph_config("same-thread"), checkpoint_b, {}, {})
    await second.close()

    _downgrade_to_v6(database, drop_artifact_metadata=True)

    migrated_a = await ThreadPersistence.open(project=project_a, home=home)
    opened_a = await migrated_a.open_thread("same-thread")
    assert [(message.kind, message.content) for message in opened_a.messages] == [
        ("user", "A 首条"),
        ("assistant", "A 回答"),
    ]
    assert opened_a.legacy_incomplete_history is True
    records_a = await migrated_a.load_transcript("same-thread")
    assert all(record.run_id is None and record.execution_id is None for record in records_a)
    await migrated_a.close()

    migrated_b = await ThreadPersistence.open(project=project_b, home=home)
    opened_b = await migrated_b.open_thread("same-thread")
    assert [(message.kind, message.content) for message in opened_b.messages] == [
        ("user", "B 首条"),
        ("assistant", "B 回答"),
    ]
    assert opened_b.legacy_incomplete_history is True
    await migrated_b.close()

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT project_fingerprint, thread_id, sequence, run_id, execution_id
            FROM harness_thread_transcript
            WHERE thread_id = ?
            ORDER BY project_fingerprint, sequence
            """,
            ("same-thread",),
        ).fetchall()
        metadata = connection.execute(
            """
            SELECT project_fingerprint, legacy_incomplete_history, source_schema_version
            FROM harness_thread_history_metadata
            WHERE thread_id = ?
            ORDER BY project_fingerprint
            """,
            ("same-thread",),
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 4
    assert len({row[0] for row in rows}) == 2
    assert all(row[3] is None and row[4] is None for row in rows)
    assert [row[1:] for row in metadata] == [(1, 6), (1, 6)]


async def test_v6_bootstrap_preserves_ai_tool_calls_and_tool_result_ids(
    tmp_path: Path,
) -> None:
    """v6 可见 checkpoint 的 tool calls、tool-only assistant 和 invalid raw 均不丢失。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-tool-calls", "执行工具")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="执行工具"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "legacy-call",
                        "name": "execute",
                        "args": {"command": "pwd"},
                    }
                ],
                invalid_tool_calls=[
                    {
                        "id": "legacy-invalid",
                        "name": "execute",
                        "args": '{"command":',
                    }
                ],
            ),
            ToolMessage(
                content="/workspace",
                tool_call_id="legacy-call",
                name="execute",
            ),
        ]
    }
    await initial.checkpointer.aput(
        initial.graph_config("legacy-tool-calls"), checkpoint, {}, {}
    )
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database, drop_artifact_metadata=True)

    migrated = await ThreadPersistence.open(project=project, home=home)
    try:
        records = await migrated.load_transcript("legacy-tool-calls")
        assert [record.kind for record in records] == ["user", "assistant", "tool"]
        assistant = records[1]
        assert assistant.payload["content"] == ""
        assert assistant.payload["tool_calls"] == [
            {
                "id": "legacy-call",
                "name": "execute",
                "type": "tool_call",
                "arguments": {"command": "pwd"},
                "arguments_json": '{"command":"pwd"}',
                "arguments_status": "valid",
            },
            {
                "id": "legacy-invalid",
                "name": "execute",
                "type": "invalid_tool_call",
                "arguments_raw": '{"command":',
                "arguments_status": "invalid",
                "arguments_error": "JSONDecodeError",
            },
        ]
        assert records[2].payload["tool_call_id"] == "legacy-call"
        opened = await migrated.open_thread("legacy-tool-calls")
        assert opened.legacy_incomplete_history is True
    finally:
        await migrated.close()


async def test_v6_bootstrap_binds_single_idless_tool_result_and_marks_ambiguous(
    tmp_path: Path,
) -> None:
    """legacy 无 ID 结果只绑定唯一 pending call，多候选显式 unmatched。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-single-idless", "单个工具")
    await accept_thread(initial, "legacy-ambiguous-idless", "多个工具")

    single_checkpoint = empty_checkpoint()
    single_checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="单个工具"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": None, "name": "execute", "args": {"command": "pwd"}}
                ],
            ),
            ToolMessage(content="done", tool_call_id="", name="execute"),
        ]
    }
    await initial.checkpointer.aput(
        initial.graph_config("legacy-single-idless"), single_checkpoint, {}, {}
    )

    ambiguous_checkpoint = empty_checkpoint()
    ambiguous_checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="多个工具"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": None, "name": "one", "args": {}},
                    {"id": None, "name": "two", "args": {}},
                ],
            ),
            ToolMessage(content="ambiguous", tool_call_id="", name="tool"),
        ]
    }
    await initial.checkpointer.aput(
        initial.graph_config("legacy-ambiguous-idless"), ambiguous_checkpoint, {}, {}
    )
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database, drop_artifact_metadata=True)

    migrated = await ThreadPersistence.open(project=project, home=home)
    try:
        single = await migrated.load_transcript("legacy-single-idless")
        single_call_id = single[1].payload["tool_calls"][0]["id"]
        assert single[2].payload["tool_call_id"] == single_call_id
        assert "tool_call_id_status" not in single[2].payload

        ambiguous = await migrated.load_transcript("legacy-ambiguous-idless")
        declared_ids = {call["id"] for call in ambiguous[1].payload["tool_calls"]}
        assert len(declared_ids) == 2
        assert ambiguous[2].payload["tool_call_id_status"] == "unmatched"
        assert ambiguous[2].payload["tool_call_id"] not in declared_ids
    finally:
        await migrated.close()


async def test_transcript_rejects_non_json_and_nonfinite_tool_arguments(
    tmp_path: Path,
) -> None:
    """typed Transcript command 不能把非 JSON 或 NaN/Infinity 伪装成 valid。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-invalid-arguments", "执行")
    try:
        for index, value in enumerate((object(), math.nan, math.inf, -math.inf)):
            with pytest.raises(ThreadPersistenceError, match="TRANSCRIPT_TOOL_CALL_INVALID"):
                await store.append_transcript(
                    TranscriptAppend(
                        thread_id="thread-invalid-arguments",
                        record_id=f"run:invalid:assistant:{index}",
                        kind="assistant",
                        content="",
                        run_id="run-invalid",
                        execution_id="root-run-invalid",
                        tool_calls=(
                            {
                                "id": f"invalid-{index}",
                                "name": "execute",
                                "arguments": value,
                                "arguments_status": "valid",
                            },
                        ),
                    )
                )
        assert [record.kind for record in await store.load_transcript("thread-invalid-arguments")] == [
            "user"
        ]
    finally:
        await store.close()


def test_legacy_tool_argument_encoding_rejects_nonfinite_values() -> None:
    """legacy 参数编码也必须拒绝 NaN/Infinity。"""
    for value in (math.nan, math.inf, -math.inf):
        payload: dict[str, object] = {}
        thread_persistence_module._set_legacy_tool_call_arguments(payload, value)
        assert payload["arguments_status"] == "invalid"
        assert "arguments_raw" in payload


async def test_concurrent_v6_openers_serialize_migration_without_downgrade(
    tmp_path: Path,
) -> None:
    """两个 project Host 同时打开共享 v6 库时最终只提交一次后继 schema。"""
    home = tmp_path / "home"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    first = await ThreadPersistence.open(project=project_a, home=home)
    await accept_thread(first, "same-thread", "并发 A")
    checkpoint_a = empty_checkpoint()
    checkpoint_a["channel_values"] = {
        "messages": [HumanMessage(content="并发 A"), AIMessage(content="A 回答")]
    }
    await first.checkpointer.aput(first.graph_config("same-thread"), checkpoint_a, {}, {})
    database = first.database_path
    await first.close()

    second = await ThreadPersistence.open(project=project_b, home=home)
    await accept_thread(second, "same-thread", "并发 B")
    checkpoint_b = empty_checkpoint()
    checkpoint_b["channel_values"] = {
        "messages": [HumanMessage(content="并发 B"), AIMessage(content="B 回答")]
    }
    await second.checkpointer.aput(second.graph_config("same-thread"), checkpoint_b, {}, {})
    await second.close()
    _downgrade_to_v6(database, drop_artifact_metadata=True)

    migrated_a, migrated_b = await asyncio.gather(
        ThreadPersistence.open(project=project_a, home=home),
        ThreadPersistence.open(project=project_b, home=home),
    )
    try:
        opened_a = await migrated_a.open_thread("same-thread")
        opened_b = await migrated_b.open_thread("same-thread")
        assert [message.content for message in opened_a.messages] == ["并发 A", "A 回答"]
        assert [message.content for message in opened_b.messages] == ["并发 B", "B 回答"]
        assert opened_a.legacy_incomplete_history is True
        assert opened_b.legacy_incomplete_history is True
    finally:
        await asyncio.gather(migrated_a.close(), migrated_b.close())

    connection = sqlite3.connect(database)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute(
            """
            SELECT project_fingerprint, thread_id, sequence, run_id, execution_id
            FROM harness_thread_transcript
            WHERE thread_id = ?
            ORDER BY project_fingerprint, sequence
            """,
            ("same-thread",),
        ).fetchall()
    finally:
        connection.close()
    assert version == thread_persistence_module._SCHEMA_VERSION
    assert len(rows) == 4
    assert len({row[0] for row in rows}) == 2
    assert all(row[3] is None and row[4] is None for row in rows)


async def test_v6_legacy_artifact_reads_explicit_default_metadata(tmp_path: Path) -> None:
    """真正缺少 v7 两列的旧 Artifact 迁移后读回空摘要和零字节默认值。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-artifact", "旧 Artifact")
    committed = await initial.commit_context(
        CommitContextRewrite(
            thread_id="legacy-artifact",
            artifacts=(ContextArtifactDraft(kind="history", content="旧原文"),),
        )
    )
    database = initial.database_path
    artifact_id = committed.artifacts[0].artifact_id
    await initial.close()

    _downgrade_to_v6(database, drop_artifact_metadata=True)
    migrated = await ThreadPersistence.open(project=project, home=home)
    artifact = await migrated.load_context_artifact("legacy-artifact", artifact_id)
    assert artifact is not None
    assert artifact.content == "旧原文"
    assert artifact.content_sha256 == ""
    assert artifact.byte_length == 0
    await migrated.close()


async def test_v6_migration_failure_restores_backup_without_half_v7_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移遇到解码/类型异常时恢复 v6 主库，不能留下半套 v7 表。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-failure", "旧记录")
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)

    async def fail_bootstrap(_self: ThreadPersistence, _source_version: int) -> None:
        raise ValueError("invalid legacy payload")

    monkeypatch.setattr(ThreadPersistence, "_bootstrap_legacy_transcripts", fail_bootstrap)
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_MIGRATION_FAILED"):
        await ThreadPersistence.open(project=project, home=home)
    monkeypatch.undo()

    connection = sqlite3.connect(database)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        transcript_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'harness_thread_transcript'
            """
        ).fetchone()
        metadata_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'harness_thread_history_metadata'
            """
        ).fetchone()
    finally:
        connection.close()
    assert version == 6
    assert transcript_table is None
    assert metadata_table is None
    backup = database.with_name(f"{database.name}.pre-v6-migration.bak")
    assert backup.exists()

    recovered = await ThreadPersistence.open(project=project, home=home)
    assert (await recovered.open_thread("legacy-failure")).legacy_incomplete_history is True
    await recovered.close()


async def test_v6_migration_cancellation_restores_original_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移取消也关闭旧连接并恢复 v6，下一次打开仍可继续迁移。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-cancel", "取消迁移")
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)
    started = asyncio.Event()
    release = asyncio.Event()

    async def block_bootstrap(_self: ThreadPersistence, _source_version: int) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(ThreadPersistence, "_bootstrap_legacy_transcripts", block_bootstrap)
    task = asyncio.create_task(ThreadPersistence.open(project=project, home=home))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'harness_thread_transcript'"
        ).fetchone() is None
    finally:
        connection.close()
    recovered = await ThreadPersistence.open(project=project, home=home)
    assert (await recovered.open_thread("legacy-cancel")).legacy_incomplete_history is True
    await recovered.close()


async def test_migration_backup_uses_distinct_temporary_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一用户库的两次备份创建不会互删固定临时文件。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    temporary_paths: list[Path] = []
    original_replace = thread_persistence_module.os.replace

    def capture_replace(source: Any, destination: Any) -> None:
        temporary_paths.append(Path(source))
        original_replace(source, destination)

    monkeypatch.setattr(thread_persistence_module.os, "replace", capture_replace)
    await store._create_migration_backup(thread_persistence_module._SCHEMA_VERSION)
    await store._create_migration_backup(thread_persistence_module._SCHEMA_VERSION)
    assert len(temporary_paths) == 2
    assert len(set(temporary_paths)) == 2
    assert all(path.name.endswith(".tmp") for path in temporary_paths)
    assert all(not path.exists() for path in temporary_paths)
    await store.close()


async def test_thread_persistence_commits_context_rewrite_as_one_typed_operation(tmp_path: Path) -> None:
    """Context 的 artifact、summary 和熔断状态通过一个生命周期操作提交。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)

    committed = await store.commit_context(
        CommitContextRewrite(
            thread_id="thread-context",
            artifacts=(
                ContextArtifactDraft(
                    kind="history",
                    content="旧消息",
                    source_start=0,
                    source_end=1,
                ),
            ),
            summary=ContextSummaryDraft(
                rewrite_version="v1",
                content="摘要",
                source_start=0,
                source_end=1,
                artifact_indexes=(0,),
            ),
            state=ContextState(failures=1, last_action="summary"),
        )
    )

    artifact = committed.artifacts[0]
    assert committed.summary is not None
    assert committed.summary.artifact_ids == (artifact.artifact_id,)
    assert await store.load_context_artifact("thread-context", artifact.artifact_id) == artifact
    assert (await store.load_context("thread-context")).state == ContextState(
        failures=1,
        circuit_open=False,
        last_action="summary",
    )
    await store.close()


async def test_context_rewrite_rolls_back_all_rows_when_summary_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摘要写入失败时 artifact 和状态也必须保持未提交。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)

    monkeypatch.setattr(
        "harness_agent.threads.thread_persistence.uuid.uuid4",
        lambda: uuid.UUID(int=1),
    )
    original_execute = store._connection.execute

    def fail_summary(sql: str, *args: object, **kwargs: object):
        if "harness_context_summaries" in sql:
            raise aiosqlite.OperationalError("injected summary failure")
        return original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(store._connection, "execute", fail_summary)
    with pytest.raises(ThreadPersistenceError, match="CONTEXT_REWRITE_WRITE_FAILED"):
        await store.commit_context(
            CommitContextRewrite(
                thread_id="rollback",
                artifacts=(ContextArtifactDraft(kind="history", content="未提交"),),
                summary=ContextSummaryDraft(
                    rewrite_version="v1",
                    content="摘要",
                    source_start=0,
                    source_end=0,
                    artifact_indexes=(0,),
                ),
                state=ContextState(failures=1, last_action="summary"),
            )
        )

    artifact_id = "history-" + ("0" * 32)
    snapshot = await store.load_context("rollback")
    assert snapshot.state == ContextState()
    assert await store.load_context_artifact("rollback", artifact_id) is None
    await store.close()


async def test_thread_persistence_reports_future_schema_and_closed_persistence(tmp_path: Path) -> None:
    """未来 schema 不能被旧版静默写回，关闭连接后也不得继续读写。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    database = store.database_path
    await store.close()
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_STORE_CLOSED"):
        await store.list_threads()

    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version=99")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_SCHEMA_TOO_NEW"):
        await ThreadPersistence.open(project=project, home=home)


async def test_thread_persistence_reports_corrupt_database(tmp_path: Path) -> None:
    """损坏的 SQLite 文件需要返回明确的 checkpoint 损坏诊断。"""
    home = tmp_path / "home"
    database = home / ".harness" / "threads.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_DATABASE_CORRUPT"):
        await ThreadPersistence.open(project=tmp_path, home=home)
