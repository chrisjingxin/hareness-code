"""用户级 SQLite thread 存储：重启、project 隔离、迁移和损坏诊断回归测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence

import pytest
import aiosqlite
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.checkpoint.base import empty_checkpoint

import harness_agent.threads.thread_persistence as thread_persistence_module
from harness_agent.threads.context_projection import ContextProjectionError, ContextProjector
from harness_agent.runtime.execution_binding import (
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
from tests.support.thread_fixtures import accept_thread, test_binding as make_test_binding

# Windows 上 migration child 的冷启动（解释器 + 模块导入）约需 2.5~3 秒，且
# 受杀毒软件/磁盘负载影响波动明显，POSIX 约 0.5 秒。测试压缩的截止时间必须
# 按平台放宽，否则 child 还没进入迁移就被误判超时。Windows 取生产默认值 30s，
# 覆盖 pytest 全量运行时磁盘/杀软争用导致的冷启动劣化。
_MIGRATION_CHILD_TEST_DEADLINE = 30.0 if os.name == "nt" else 3.0


async def _await_migration_state(
    state_path: Path,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float,
    description: str,
    opening_task: asyncio.Task | None = None,
) -> None:
    """在预算内轮询迁移状态文件，直到 predicate 成立。

    计数式轮询受 asyncio.sleep 粒度的平台差异影响（Windows 单次 sleep 实际
    约 4~16ms），同样次数覆盖的真实窗口可能相差数倍，因此以墙钟时间为准。
    状态文件可能短暂处于半写状态，读取/解析失败需容忍并重试。

    ``opening_task`` 传入发起迁移的 open 任务；若它在状态达标前就带着异常
    结束，说明 child 提前崩溃或被拒，直接上抛原始错误以便定位，而不是等到
    轮询预算耗尽才报笼统断言。
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if opening_task is not None and opening_task.done():
            exception = opening_task.exception()
            if exception is not None:
                raise AssertionError(
                    f"migration child {description}: open() exited early"
                ) from exception
            raise AssertionError(
                f"migration child {description}: open() returned early"
            )
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
            if isinstance(state, dict) and predicate(state):
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"migration child {description}")


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
    """把当前测试库还原为精确 v6 形状，不触碰生产数据。

    ``drop_artifact_metadata`` 保留为旧测试调用的兼容参数；v6 fixture 现在
    始终移除所有后续版本对象，避免测试继续依赖开放式 source fallback。
    """
    del drop_artifact_metadata
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE IF EXISTS harness_compression_checkpoints")
        connection.execute("DROP TABLE IF EXISTS harness_run_context_snapshots")
        connection.execute("DROP TABLE IF EXISTS harness_thread_history_metadata")
        connection.execute("DROP TABLE IF EXISTS harness_thread_transcript")
        connection.execute(
            "ALTER TABLE harness_run_execution_bindings DROP COLUMN context_snapshot_id"
        )
        connection.execute("ALTER TABLE harness_context_state DROP COLUMN runtime_state")
        connection.execute(
            "ALTER TABLE harness_context_artifacts DROP COLUMN content_sha256"
        )
        connection.execute("ALTER TABLE harness_context_artifacts DROP COLUMN byte_length")
        connection.execute("PRAGMA user_version=6")
        connection.commit()
    finally:
        connection.close()


async def _new_v6_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """创建一个仅用于迁移回归的真实 v6 主库。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "migration-fixture", "迁移 fixture")
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)
    return home, project, database


def test_thread_persistence_exposes_lifecycle_interface_only() -> None:
    """表级读写不再成为业务调用方可见的 ThreadPersistence 方法。"""
    assert hasattr(ThreadPersistence, "accept_run")
    assert hasattr(ThreadPersistence, "load_run_state")
    assert hasattr(ThreadPersistence, "commit_context")
    assert hasattr(ThreadPersistence, "complete_run")
    assert hasattr(ThreadPersistence, "load_context")
    assert hasattr(ThreadPersistence, "load_context_state")
    assert hasattr(ThreadPersistence, "load_latest_valid_compression_checkpoint")
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
        "load_prompt_epoch",
        "persist_prompt_epoch",
        "get_agent_engine_profile",
        "save_agent_engine_profile",
        "read_context_artifact",
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
    if os.name != "nt":
        # Windows 的 chmod 只映射只读属性，文件默认模式位固定为 0o666。
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
    """共享图重建后通过持久化 RunContextSnapshot 恢复同一 thread 的消息。"""
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.threads.context_lifecycle import (
        ContextAuthority,
        ContextBlock,
        ContextStability,
        RunContextSnapshot,
    )
    import harness_agent.threads.context_lifecycle as context_lifecycle_module
    from harness_agent.runtime.run_context import RunContext
    from harness_agent.threads.prompting import sha256_text

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    first = await ThreadPersistence.open(project=project, home=home)
    first_model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="第一轮回答")]))
    first_model.profile = {"max_input_tokens": 200000}
    block = ContextBlock(
        key="core-policy",
        authority=ContextAuthority.CORE_POLICY,
        stability=ContextStability.IMMUTABLE,
        content="持久化前缀",
    )
    snapshot = RunContextSnapshot(
        project_fingerprint=first.project_fingerprint,
        thread_id="thread-1",
        snapshot_id=context_lifecycle_module._snapshot_id(
            project_fingerprint=first.project_fingerprint,
            thread_id="thread-1",
            blocks=(block,),
            system_prompt=block.content,
            skill_snapshot_id=None,
            legacy=False,
        ),
        blocks=(block,),
        system_prompt=block.content,
        system_fingerprint=sha256_text(block.content),
        created_at_ms=1,
    )
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
    first_binding = replace(
        make_test_binding("thread-1", "run-1"),
        context_snapshot_id=snapshot.snapshot_id,
    )
    await first.accept_run(
        AcceptRun(
            message="第一轮请求",
            binding=first_binding,
            context_snapshot=snapshot,
        )
    )
    _ = [
        event
        async for event in first_agent.astream(
            {"messages": [HumanMessage(content="第一轮请求")]},
            config=first.graph_config("thread-1"),
            context=RunContext(
                thread_id="thread-1",
                run_id="run-1",
                context_snapshot=snapshot,
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
    restored_snapshot = await second.load_context_snapshot(
        snapshot.snapshot_id,
        thread_id="thread-1",
    )
    assert restored_snapshot == snapshot
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
    second_binding = replace(make_test_binding("thread-1", "run-2"), context_snapshot_id=snapshot.snapshot_id)
    await second.accept_run(
        AcceptRun(
            message="第二轮请求",
            binding=second_binding,
            context_snapshot=restored_snapshot,
        )
    )
    _ = [
        event
        async for event in second_agent.astream(
            {"messages": [HumanMessage(content="第二轮请求")]},
            config=second.graph_config("thread-1"),
            context=RunContext(
                thread_id="thread-1",
                run_id="run-2",
                context_snapshot=restored_snapshot,
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
        connection.execute("DROP TABLE harness_compression_checkpoints")
        connection.execute("DROP TABLE harness_run_context_snapshots")
        connection.execute("DROP TABLE harness_thread_history_metadata")
        connection.execute("DROP TABLE harness_thread_transcript")
        connection.execute("ALTER TABLE harness_context_state DROP COLUMN runtime_state")
        connection.execute(
            "ALTER TABLE harness_context_artifacts DROP COLUMN content_sha256"
        )
        connection.execute("ALTER TABLE harness_context_artifacts DROP COLUMN byte_length")
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
    backup = database.with_name(f"{database.name}.pre-v6-migration.bak")
    assert backup.exists()
    backup_connection = sqlite3.connect(backup)
    try:
        assert backup_connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        backup_connection.close()

    continued = await ThreadPersistence.open(project=project_a, home=home)
    try:
        assert (
            await continued.accept_run(
                AcceptRun(
                    message="迁移后继续",
                    binding=make_test_binding("same-thread", "after-migration"),
                )
            )
        ).created
        assert [
            message.content for message in (await continued.open_thread("same-thread")).messages
        ] == ["A 首条", "A 回答", "迁移后继续"]
    finally:
        await continued.close()


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


async def test_v6_bootstrap_keeps_malformed_tool_identity_invalid_and_unmatched(
    tmp_path: Path,
) -> None:
    """非字符串 legacy 工具身份只保留 invalid 事实，不能被洗成有效关联。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-malformed-tool", "执行畸形工具")
    malformed_call = AIMessage.model_construct(
        content="",
        tool_calls=[
            {
                "id": 123,
                "name": ["execute"],
                "type": 7,
                "args": {},
                "error": {"reason": "bad"},
            }
        ],
        invalid_tool_calls=[],
    )
    malformed_result = ToolMessage.model_construct(
        content="result",
        tool_call_id=456,
        name={"tool": "execute"},
        status=["success"],
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="执行畸形工具"),
            malformed_call,
            malformed_result,
        ]
    }
    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        await initial.checkpointer.aput(
            initial.graph_config("legacy-malformed-tool"), checkpoint, {}, {}
        )
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database, drop_artifact_metadata=True)

    migrated = await ThreadPersistence.open(project=project, home=home)
    try:
        records = await migrated.load_transcript("legacy-malformed-tool")
        assert [record.kind for record in records] == ["user", "assistant", "tool"]
        call = records[1].payload["tool_calls"][0]
        assert "id" not in call and "name" not in call and "type" not in call
        assert call["legacy_invalid_fields"] == ["id", "name", "type", "error"]
        assert call["arguments_status"] == "invalid"
        assert call["arguments_error_type"] == "dict"

        result = records[2].payload
        assert result["tool_call_id_status"] == "unmatched"
        assert result["legacy_invalid_fields"] == [
            "name",
            "status",
            "tool_call_id",
        ]
        assert "name" not in result and "status" not in result
        assert result["tool_call_id"] == records[2].record_id

        opened = await migrated.open_thread("legacy-malformed-tool")
        assert opened.legacy_incomplete_history is True
        assert opened.messages[-1].kind == "tool"
        assert opened.messages[-1].tool_name is None
        with pytest.raises(
            ContextProjectionError, match="PROJECTION_TOOL_CALL_IDENTITY_INVALID"
        ):
            await ContextProjector(migrated).project("legacy-malformed-tool")
    finally:
        await migrated.close()


async def test_v6_invalid_arguments_never_enter_pending_tool_correlation(
    tmp_path: Path,
) -> None:
    """合法 ID/name 不能掩盖 error 或非 valid 参数事实，结果必须 unmatched。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-invalid-arguments", "执行")
    calls = AIMessage.model_construct(
        content="",
        tool_calls=[
            {
                "id": "call-error",
                "name": "execute",
                "args": {},
                "error": {"reason": "bad"},
            },
            {
                "id": "call-unavailable",
                "name": "execute",
                "args": {},
                "arguments_status": "unavailable",
            },
        ],
        invalid_tool_calls=[],
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="执行"),
            calls,
            ToolMessage(
                content="error-result",
                tool_call_id="call-error",
                name="execute",
            ),
            ToolMessage(
                content="unavailable-result",
                tool_call_id="call-unavailable",
                name="execute",
            ),
        ]
    }
    await initial.checkpointer.aput(
        initial.graph_config("legacy-invalid-arguments"), checkpoint, {}, {}
    )
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database, drop_artifact_metadata=True)

    migrated = await ThreadPersistence.open(project=project, home=home)
    try:
        records = await migrated.load_transcript("legacy-invalid-arguments")
        imported_calls = records[1].payload["tool_calls"]
        assert imported_calls[0]["id"] == "call-error"
        assert imported_calls[0]["arguments_status"] == "invalid"
        assert imported_calls[0]["legacy_invalid_fields"] == ["error"]
        assert imported_calls[0]["arguments_error_type"] == "dict"
        assert imported_calls[1]["id"] == "call-unavailable"
        assert imported_calls[1]["arguments_status"] == "unavailable"
        assert "legacy_invalid_fields" not in imported_calls[1]
        assert records[2].payload["tool_call_id"] == "call-error"
        assert records[2].payload["tool_call_id_status"] == "unmatched"
        assert records[3].payload["tool_call_id"] == "call-unavailable"
        assert records[3].payload["tool_call_id_status"] == "unmatched"
    finally:
        await migrated.close()


@pytest.mark.parametrize(
    "raw_error",
    ({}, [], 0, False),
    ids=("empty-dict", "empty-list", "zero", "false"),
)
async def test_v6_falsey_non_string_error_never_matches_result(
    tmp_path: Path, raw_error: object
) -> None:
    """字段存在的 falsey 非字符串 error 仍是 invalid，不能被 truthiness 漏掉。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "legacy-falsey-error", "执行")
    call = AIMessage.model_construct(
        content="",
        tool_calls=[
            {
                "id": "call",
                "name": "execute",
                "args": {},
                "error": raw_error,
            }
        ],
        invalid_tool_calls=[],
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(content="执行"),
            call,
            ToolMessage(content="result", tool_call_id="call", name="execute"),
        ]
    }
    await initial.checkpointer.aput(
        initial.graph_config("legacy-falsey-error"), checkpoint, {}, {}
    )
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database, drop_artifact_metadata=True)

    migrated = await ThreadPersistence.open(project=project, home=home)
    try:
        records = await migrated.load_transcript("legacy-falsey-error")
        imported_call = records[1].payload["tool_calls"][0]
        assert imported_call["arguments_status"] == "invalid"
        assert imported_call["legacy_invalid_fields"] == ["error"]
        assert imported_call["arguments_error_type"] == type(raw_error).__name__
        assert records[2].payload["tool_call_id"] == "call"
        assert records[2].payload["tool_call_id_status"] == "unmatched"
    finally:
        await migrated.close()


def test_legacy_error_missing_none_and_strings_keep_existing_semantics() -> None:
    """缺失/None/空字符串等同无错误；非空字符串保留为可证明错误。"""
    base = {"id": "call", "name": "execute", "args": {}}
    for extra in ({}, {"error": None}, {"error": ""}):
        message = AIMessage.model_construct(
            content="",
            tool_calls=[{**base, **extra}],
            invalid_tool_calls=[],
        )
        imported = thread_persistence_module._legacy_tool_calls(
            message,
            project_fingerprint="project",
            thread_id="thread",
            sequence=1,
        )[0]
        assert imported["arguments_status"] == "valid"
        assert "legacy_invalid_fields" not in imported
        assert "arguments_error" not in imported

    string_error = AIMessage.model_construct(
        content="",
        tool_calls=[{**base, "error": "provider error"}],
        invalid_tool_calls=[],
    )
    imported_error = thread_persistence_module._legacy_tool_calls(
        string_error,
        project_fingerprint="project",
        thread_id="thread",
        sequence=1,
    )[0]
    assert imported_error["arguments_error"] == "provider error"
    assert imported_error["arguments_status"] == "invalid"
    assert "legacy_invalid_fields" not in imported_error


async def test_public_transcript_append_cannot_enable_legacy_leniency(
    tmp_path: Path,
) -> None:
    """普通写入不能伪造迁移能力或注入 legacy invalid marker。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread", "u")
    with pytest.raises(TypeError):
        TranscriptAppend(
            thread_id="thread",
            record_id="legacy-flag",
            kind="assistant",
            content="",
            legacy_import=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ThreadPersistenceError, match="TRANSCRIPT_TOOL_CALL_INVALID"):
        await store.append_transcript(
            TranscriptAppend(
                thread_id="thread",
                record_id="nested-marker",
                kind="assistant",
                content="",
                tool_calls=(
                    {
                        "legacy_invalid_fields": ["id"],
                        "arguments_status": "invalid",
                    },
                ),
            )
        )
    with pytest.raises(
        ThreadPersistenceError, match="TRANSCRIPT_LEGACY_MARKER_FORBIDDEN"
    ):
        await store.append_transcript_batch(
            (
                TranscriptAppend(
                    thread_id="thread",
                    record_id="would-be-partial",
                    kind="user",
                    content="later",
                ),
                TranscriptAppend(
                    thread_id="thread",
                    record_id="result-marker",
                    kind="tool",
                    content="result",
                    legacy_invalid_fields=("name",),
                ),
            )
        )
    assert [record.kind for record in await store.load_transcript("thread")] == [
        "user"
    ]
    await store.close()


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


async def test_v6_backup_captures_committed_wal_and_is_standalone(
    tmp_path: Path,
) -> None:
    """已提交但仍在 WAL 的 v6 数据进入 backup，脱离原 WAL/SHM 仍可恢复。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(initial, "seed", "seed")
    project_fingerprint = initial.project_fingerprint
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)

    writer = sqlite3.connect(database, timeout=5.0)
    try:
        assert str(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
        writer.execute(
            """
            INSERT INTO harness_threads (
                project_fingerprint, thread_id, created_at_ms, updated_at_ms,
                first_message, latest_message, message_count
            ) VALUES (?, ?, 10, 10, ?, ?, 0)
            """,
            (project_fingerprint, "wal-thread", "WAL committed", "WAL committed"),
        )
        writer.commit()
        assert database.with_name(database.name + "-wal").exists()
        migrated = await ThreadPersistence.open(project=project, home=home)
    finally:
        writer.close()

    await migrated.close()
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        sidecar.unlink(missing_ok=True)
        sidecar.write_bytes(b"stale sidecar")

    backup = database.with_name(f"{database.name}.pre-v6-migration.bak")
    assert backup.exists()
    assert not backup.with_name(backup.name + "-wal").exists()
    assert not backup.with_name(backup.name + "-shm").exists()
    restored = tmp_path / "restored.sqlite3"
    source = sqlite3.connect(backup)
    target = sqlite3.connect(restored)
    try:
        source.backup(target)
        target.commit()
    finally:
        source.close()
        target.close()
    check = sqlite3.connect(restored)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("PRAGMA user_version").fetchone()[0] == 6
        assert check.execute(
            "SELECT first_message FROM harness_threads WHERE thread_id = 'wal-thread'"
        ).fetchone()[0] == "WAL committed"
    finally:
        check.close()


async def test_migration_backup_creation_failure_is_typed_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backup API 创建失败不得进入迁移或留下半套 schema。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)

    async def fail_backup(
        _self: ThreadPersistence,
        _source_version: int,
        _source_fingerprint: Any = None,
    ) -> Path:
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_FAILED")

    monkeypatch.setattr(ThreadPersistence, "_create_migration_backup", fail_backup)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "backup_failure")
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_MIGRATION_BACKUP_FAILED"):
        await ThreadPersistence.open(project=project, home=home)
    assert not database.with_name(database.name + ".migration-state.json").exists()
    check = sqlite3.connect(database)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 6
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'harness_thread_transcript'"
        ).fetchone() is None
    finally:
        check.close()


@pytest.mark.parametrize("invalid_field", ("integrity_check", "schema_digest", "data_digest"))
async def test_migration_backup_rejects_integrity_schema_and_data_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
) -> None:
    """backup 验证必须同时覆盖 integrity、schema 和 data 证明字段。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    original_fingerprint = thread_persistence_module._migration_database_fingerprint_sync

    def corrupt_fingerprint(connection: sqlite3.Connection) -> Any:
        actual = original_fingerprint(connection)
        if invalid_field == "integrity_check":
            return replace(actual, integrity_check="not ok")
        if invalid_field == "schema_digest":
            return replace(actual, schema_digest="schema-corrupted")
        return replace(actual, data_digest="data-corrupted")

    monkeypatch.setattr(
        thread_persistence_module,
        "_migration_database_fingerprint_sync",
        corrupt_fingerprint,
    )
    await store._connection.execute("BEGIN IMMEDIATE")
    fingerprint = await store._database_fingerprint_async()
    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_BACKUP_VALIDATION_FAILED",
    ):
        await store._create_migration_backup(
            thread_persistence_module._SCHEMA_VERSION,
            fingerprint,
        )
    await store._connection.rollback()
    await store.close()


async def test_v6_incomplete_source_schema_is_rejected_before_backup(tmp_path: Path) -> None:
    """源 v6 缺关键表时在 backup 前 fail closed，不把半套库当可迁移库。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE harness_run_execution_bindings")
        connection.execute("PRAGMA user_version=6")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:harness_run_execution_bindings",
    ):
        await ThreadPersistence.open(project=project, home=home)
    assert not database.with_name(f"{database.name}.pre-v6-migration.bak").exists()


async def test_v6_migration_boundary_blocks_second_connection_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二连接在 backup/迁移排他边界内只能被阻止，不能交错写入。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)

    writer_started = threading.Event()
    writer_finished = threading.Event()
    writer_result: dict[str, str] = {}

    def concurrent_writer() -> None:
        connection = sqlite3.connect(database, timeout=0.2)
        try:
            writer_started.set()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO harness_threads (
                        project_fingerprint, thread_id, created_at_ms, updated_at_ms,
                        first_message, latest_message, message_count
                    ) VALUES ('writer', 'interleaved', 1, 1, 'bad', 'bad', 0)
                    """
                )
                connection.commit()
                writer_result["outcome"] = "committed"
            except sqlite3.OperationalError as exc:
                writer_result["outcome"] = str(exc)
        finally:
            connection.close()
            writer_finished.set()

    original_fingerprint = ThreadPersistence._database_fingerprint_async
    fingerprint_calls = 0
    writer_thread: threading.Thread | None = None

    async def observe_boundary(self: ThreadPersistence) -> Any:
        nonlocal fingerprint_calls, writer_thread
        result = await original_fingerprint(self)
        if fingerprint_calls == 0:
            fingerprint_calls += 1
            writer_thread = threading.Thread(target=concurrent_writer)
            writer_thread.start()
            assert await asyncio.to_thread(writer_started.wait, 1.0)
            await asyncio.sleep(0.05)
        return result

    original_backup = ThreadPersistence._create_migration_backup

    async def hold_after_backup(
        self: ThreadPersistence,
        source_version: int,
        source_fingerprint: Any = None,
    ) -> Path:
        result = await original_backup(self, source_version, source_fingerprint)
        await asyncio.sleep(0.35)
        return result

    del observe_boundary, hold_after_backup, original_fingerprint, original_backup
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "before_commit")
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        _MIGRATION_CHILD_TEST_DEADLINE,
    )
    state_path = database.with_name(database.name + ".migration-state.json")
    opening = asyncio.create_task(ThreadPersistence.open(project=project, home=home))
    await _await_migration_state(
        state_path,
        lambda state: state.get("status") == "committing",
        _MIGRATION_CHILD_TEST_DEADLINE + 5.0,
        "did not reach committing state",
    )

    writer_thread = threading.Thread(target=concurrent_writer)
    writer_thread.start()
    assert await asyncio.to_thread(writer_finished.wait, 1.0)
    writer_thread.join(timeout=1.0)
    assert writer_result["outcome"] != "committed"
    assert "locked" in writer_result["outcome"].lower()
    with pytest.raises(ThreadPersistenceError, match="WORKER_TIMEOUT"):
        await opening
    check = sqlite3.connect(database)
    try:
        assert check.execute(
            "SELECT 1 FROM harness_threads WHERE thread_id = 'interleaved'"
        ).fetchone() is None
    finally:
        check.close()


async def test_v6_legacy_artifact_backfills_verifiable_metadata(tmp_path: Path) -> None:
    """缺少 v7 两列的旧 Artifact 迁移后补齐真实摘要和字节数。"""
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
    assert artifact.content_sha256 == hashlib.sha256("旧原文".encode()).hexdigest()
    assert artifact.byte_length == len("旧原文".encode())
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
    original_connection = sqlite3.connect(database)
    try:
        original_thread_row = original_connection.execute(
            "SELECT * FROM harness_threads WHERE thread_id = 'legacy-failure'"
        ).fetchone()
    finally:
        original_connection.close()

    async def fail_bootstrap(_self: ThreadPersistence, _source_version: int) -> None:
        raise ValueError("invalid legacy payload")

    monkeypatch.setattr(ThreadPersistence, "_bootstrap_legacy_transcripts", fail_bootstrap)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "bootstrap_failure")
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
        restored_thread_row = connection.execute(
            "SELECT * FROM harness_threads WHERE thread_id = 'legacy-failure'"
        ).fetchone()
    finally:
        connection.close()
    assert version == 6
    assert transcript_table is None
    assert metadata_table is None
    assert restored_thread_row == original_thread_row
    backup = database.with_name(f"{database.name}.pre-v6-migration.bak")
    assert backup.exists()
    backup_connection = sqlite3.connect(backup)
    try:
        assert backup_connection.execute(
            "SELECT * FROM harness_threads WHERE thread_id = 'legacy-failure'"
        ).fetchone() == original_thread_row
    finally:
        backup_connection.close()

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
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "after_final_validation")
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        _MIGRATION_CHILD_TEST_DEADLINE,
    )
    task = asyncio.create_task(ThreadPersistence.open(project=project, home=home))
    state_path = database.with_name(database.name + ".migration-state.json")
    await _await_migration_state(
        state_path,
        lambda _state: True,
        _MIGRATION_CHILD_TEST_DEADLINE + 5.0,
        "did not create recovery state",
    )
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
    await store._connection.execute("BEGIN IMMEDIATE")
    fingerprint = await store._database_fingerprint_async()
    await store._create_migration_backup(thread_persistence_module._SCHEMA_VERSION, fingerprint)
    await store._create_migration_backup(thread_persistence_module._SCHEMA_VERSION, fingerprint)
    await store._connection.rollback()
    assert len(temporary_paths) == 2
    assert len(set(temporary_paths)) == 2
    assert all(path.name.endswith(".tmp") for path in temporary_paths)
    assert all(not path.exists() for path in temporary_paths)
    await store.close()


async def test_migration_restore_failure_keeps_backup_and_retries_on_next_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restore 失败保持 verified backup/state；下一次启动可确定性恢复并继续迁移。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    await initial.close()
    _downgrade_to_v6(database)

    async def fail_bootstrap(_self: ThreadPersistence, _source_version: int) -> None:
        raise ValueError("injected migration failure")

    async def fail_restore(
        _self: ThreadPersistence,
        _backup_path: Path,
        _expected: Any,
    ) -> None:
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_RESTORE_VALIDATION_FAILED")

    monkeypatch.setattr(ThreadPersistence, "_bootstrap_legacy_transcripts", fail_bootstrap)
    monkeypatch.setattr(ThreadPersistence, "_restore_migration_backup_async", fail_restore)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "restore_failure")
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_MIGRATION_RESTORE_FAILED"):
        await ThreadPersistence.open(project=project, home=home)
    monkeypatch.undo()

    backup = database.with_name(f"{database.name}.pre-v6-migration.bak")
    state_path = database.with_name(database.name + ".migration-state.json")
    assert backup.exists()
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "restore_failed"

    # 只有真正偏离 source 的半迁移库才进入 restore；完整 source 会走安全
    # retry。这里人为留下一个 version/schema 不匹配的主库，覆盖窄评审的
    # “不能仅凭 restore_failed 无条件恢复”边界。
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version=7")
        connection.commit()
    finally:
        connection.close()

    def fail_atomic_restore(
        _path: Path,
        _backup_path: Path,
        _expected: Any,
    ) -> None:
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_RESTORE_VALIDATION_FAILED")

    monkeypatch.setattr(
        ThreadPersistence,
        "_restore_backup_path_sync",
        staticmethod(fail_atomic_restore),
    )
    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_RESTORE_VALIDATION_FAILED",
    ):
        await ThreadPersistence.open(project=project, home=home)
    monkeypatch.undo()
    assert backup.exists()
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "restore_failed"

    recovered = await ThreadPersistence.open(project=project, home=home)
    try:
        check = sqlite3.connect(database)
        try:
            assert check.execute("PRAGMA user_version").fetchone()[0] == thread_persistence_module._SCHEMA_VERSION
        finally:
            check.close()
        assert not state_path.exists()
    finally:
        await recovered.close()


async def test_migration_commit_marker_prevents_rollback_after_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB commit 后模拟崩溃，下一次 open 必须保留最终库而非恢复 v6。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv(
        "HARNESS_TEST_MIGRATION_CHILD_PHASE", "after_commit_before_state"
    )
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        _MIGRATION_CHILD_TEST_DEADLINE,
    )
    recovered = await ThreadPersistence.open(project=project, home=home)
    await recovered.close()
    state_path = database.with_name(database.name + ".migration-state.json")
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == thread_persistence_module._SCHEMA_VERSION
        assert connection.execute(
            "SELECT latest_message FROM harness_threads WHERE thread_id='migration-fixture'"
        ).fetchone()[0] == "迁移 fixture"
    finally:
        connection.close()
    assert not state_path.exists()


async def test_migration_commit_worker_error_after_sqlite_commit_keeps_final_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """底层 commit 已完成但 worker await 抛错时，独立事实优先于异常。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv(
        "HARNESS_TEST_MIGRATION_CHILD_PHASE", "after_commit_before_reply"
    )
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        _MIGRATION_CHILD_TEST_DEADLINE,
    )
    migrated = await ThreadPersistence.open(project=project, home=home)
    await migrated.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == thread_persistence_module._SCHEMA_VERSION
        assert connection.execute(
            "SELECT first_message FROM harness_threads WHERE thread_id='migration-fixture'"
        ).fetchone()[0] == "迁移 fixture"
    finally:
        connection.close()
    assert not database.with_name(database.name + ".migration-state.json").exists()


async def test_migration_commit_cancel_settles_worker_before_final_rethrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外层取消终止 child 后保留 source，下一次 owner 再按事实重试。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "before_commit")
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        _MIGRATION_CHILD_TEST_DEADLINE,
    )
    opening = asyncio.create_task(ThreadPersistence.open(project=project, home=home))
    state_path = database.with_name(database.name + ".migration-state.json")
    await _await_migration_state(
        state_path,
        lambda _state: True,
        _MIGRATION_CHILD_TEST_DEADLINE + 5.0,
        "did not create recovery state",
    )
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    monkeypatch.undo()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='harness_thread_transcript'"
        ).fetchone() is None
    finally:
        connection.close()
    assert database.with_name(database.name + ".migration-state.json").exists()


@pytest.mark.parametrize("phase", ("before_commit", "after_final_validation"))
async def test_migration_child_timeout_poison_blocks_reuse_until_new_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """child 超时必须被回收；同进程 handoff fail closed，fresh owner 再按事实恢复。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", phase)
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        _MIGRATION_CHILD_TEST_DEADLINE,
    )
    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_WORKER_TIMEOUT",
    ):
        await asyncio.wait_for(
            ThreadPersistence.open(project=project, home=home),
            # 外层预算必须覆盖 deadline 本身加上杀 child 与恢复的开销。
            timeout=_MIGRATION_CHILD_TEST_DEADLINE + 5.0,
        )

    state_path = database.with_name(database.name + ".migration-state.json")
    backup_path = database.with_name(f"{database.name}.pre-v6-migration.bak")
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] in {
        "migrating",
        "committing",
    }
    assert backup_path.exists()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='harness_thread_transcript'"
        ).fetchone() is None
    finally:
        connection.close()

    # child 已被父进程 terminate/kill + wait；但同一进程的 owner handoff
    # 仍需 fail closed，不能把超时路径当普通新 open。
    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_RECOVERY_REQUIRED",
    ):
        await ThreadPersistence.open(project=project, home=home)
    monkeypatch.undo()
    with thread_persistence_module._MIGRATION_POISON_LOCK:
        thread_persistence_module._MIGRATION_POISONED_PATHS.discard(database)

    recovered = await ThreadPersistence.open(project=project, home=home)
    try:
        assert not state_path.exists()
        assert (await recovered.open_thread("migration-fixture")).summary.first_message == "迁移 fixture"
    finally:
        await recovered.close()


async def test_migration_poison_is_rechecked_after_lock_wait_for_all_waiters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """先过 precheck、再等锁的多个 opener 必须在锁后共同拒绝 poison。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "before_commit")
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        _MIGRATION_CHILD_TEST_DEADLINE,
    )
    original_assert = thread_persistence_module._assert_migration_path_available
    waiter_prechecked = asyncio.Event()
    waiter_count = 0

    def observe_assert(path: Path) -> None:
        nonlocal waiter_count
        task = asyncio.current_task()
        if task is not None and task.get_name().startswith("migration-waiter-"):
            waiter_count += 1
            if waiter_count >= 3:
                waiter_prechecked.set()
        original_assert(path)

    monkeypatch.setattr(
        thread_persistence_module,
        "_assert_migration_path_available",
        observe_assert,
    )
    opening = asyncio.create_task(ThreadPersistence.open(project=project, home=home))
    state_path = database.with_name(database.name + ".migration-state.json")
    await _await_migration_state(
        state_path,
        lambda state: state.get("status") == "committing",
        _MIGRATION_CHILD_TEST_DEADLINE + 5.0,
        "did not reach committing state",
        opening,
    )

    waiters = [
        asyncio.create_task(
            ThreadPersistence.open(project=project, home=home),
            name=f"migration-waiter-{index}",
        )
        for index in range(3)
    ]
    await asyncio.wait_for(waiter_prechecked.wait(), timeout=0.5)
    with pytest.raises(ThreadPersistenceError, match="WORKER_TIMEOUT"):
        await opening
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(
        isinstance(result, ThreadPersistenceError)
        and "CHECKPOINT_MIGRATION_RECOVERY_REQUIRED" in str(result)
        for result in results
    )
    monkeypatch.undo()
    with thread_persistence_module._MIGRATION_POISON_LOCK:
        thread_persistence_module._MIGRATION_POISONED_PATHS.discard(database)


async def test_migration_child_deadline_can_be_injected_in_milliseconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试 deadline 可压到毫秒级；生产默认值仍由独立常量控制。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "before_commit")
    monkeypatch.setattr(
        thread_persistence_module,
        "_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS",
        0.001,
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(ThreadPersistenceError, match="WORKER_TIMEOUT"):
        await ThreadPersistence.open(project=project, home=home)
    assert asyncio.get_running_loop().time() - started < 1.0
    monkeypatch.undo()
    with thread_persistence_module._MIGRATION_POISON_LOCK:
        thread_persistence_module._MIGRATION_POISONED_PATHS.discard(database)


async def test_migration_commit_slow_within_deadline_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常慢 commit 在 deadline 内完成，不误入 poisoned recovery。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    original_commit = aiosqlite.Connection.commit

    async def slow_commit(self: aiosqlite.Connection) -> None:
        await asyncio.sleep(0.02)
        await original_commit(self)

    monkeypatch.setattr(
        thread_persistence_module,
        "_MIGRATION_COMMIT_DEADLINE_SECONDS",
        0.2,
    )
    monkeypatch.setattr(aiosqlite.Connection, "commit", slow_commit)
    migrated = await ThreadPersistence.open(project=project, home=home)
    await migrated.close()
    assert database.with_name(database.name + ".migration-state.json").exists() is False


async def test_migration_commit_failure_before_sqlite_commit_preserves_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """commit 在落盘前失败时仍回到完整 v6 source，不能留下 successor 表。"""
    home, project, database = await _new_v6_fixture(tmp_path)

    async def fail_before_commit(_self: aiosqlite.Connection) -> None:
        raise RuntimeError("commit did not start")

    monkeypatch.setattr(aiosqlite.Connection, "commit", fail_before_commit)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "commit_failure_before")
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_MIGRATION_FAILED"):
        await ThreadPersistence.open(project=project, home=home)
    monkeypatch.undo()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='harness_thread_transcript'"
        ).fetchone() is None
    finally:
        connection.close()
    recovered = await ThreadPersistence.open(project=project, home=home)
    await recovered.close()


async def test_migration_committed_state_write_failure_never_restores_old_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写 committed 状态失败只留下可恢复 state，不得回滚已提交的新库。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv(
        "HARNESS_TEST_MIGRATION_CHILD_PHASE", "state_committed_failure"
    )
    migrated = await ThreadPersistence.open(project=project, home=home)
    await migrated.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == thread_persistence_module._SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='harness_thread_transcript'"
        ).fetchone() is not None
    finally:
        connection.close()
    assert not database.with_name(database.name + ".migration-state.json").exists()


async def test_migration_state_clear_failure_repairs_without_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理 committed state 失败时保留新库，下一次 open 只修复状态边界。"""
    home, project, database = await _new_v6_fixture(tmp_path)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "state_clear_failure")
    migrated = await ThreadPersistence.open(project=project, home=home)
    await migrated.close()

    state_path = database.with_name(database.name + ".migration-state.json")
    assert database.with_name(f"{database.name}.pre-v6-migration.bak").exists()
    assert not state_path.exists()


async def test_migration_recovery_retries_when_main_database_is_exact_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state 残留但主库是完整 v6 时安全重试，不调用恢复旧 backup。"""
    home, project, database = await _new_v6_fixture(tmp_path)

    async def fail_bootstrap(_self: ThreadPersistence, _source_version: int) -> None:
        raise ValueError("injected migration failure")

    async def fail_restore(
        _self: ThreadPersistence,
        _backup_path: Path,
        _expected: Any,
    ) -> None:
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_RESTORE_VALIDATION_FAILED")

    monkeypatch.setattr(ThreadPersistence, "_bootstrap_legacy_transcripts", fail_bootstrap)
    monkeypatch.setattr(ThreadPersistence, "_restore_migration_backup_async", fail_restore)
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "bootstrap_failure")
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_MIGRATION_FAILED"):
        await ThreadPersistence.open(project=project, home=home)
    monkeypatch.undo()

    def fail_restore_path(
        _path: Path,
        _backup_path: Path,
        _expected: Any,
    ) -> None:
        raise AssertionError("exact source must retry migration, not restore")

    monkeypatch.setattr(
        ThreadPersistence,
        "_restore_backup_path_sync",
        staticmethod(fail_restore_path),
    )
    retried = await ThreadPersistence.open(project=project, home=home)
    try:
        assert retried.database_path == database
    finally:
        await retried.close()
    monkeypatch.undo()


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_index_order",
        "missing_primary_unique",
        "wrong_type",
        "wrong_not_null",
        "wrong_primary_key",
        "dangerous_extra_column",
        "wrong_table_sql",
    ),
)
async def test_v6_schema_contract_rejects_same_name_malformed_objects(
    tmp_path: Path,
    mutation: str,
) -> None:
    """v6 同名但定义错误的列、PK、索引和 SQL 必须在 backup 前拒绝。"""
    _home, project, database = await _new_v6_fixture(tmp_path)
    connection = sqlite3.connect(database)
    try:
        if mutation == "wrong_index_order":
            connection.execute("DROP INDEX harness_threads_project_updated")
            connection.execute(
                """
                CREATE INDEX harness_threads_project_updated
                ON harness_threads(updated_at_ms DESC, project_fingerprint)
                """
            )
        else:
            connection.execute("DROP INDEX harness_threads_project_updated")
            connection.execute("DROP TABLE harness_threads")
            message_count = (
                "message_count TEXT NOT NULL DEFAULT 0"
                if mutation == "wrong_type"
                else "message_count INTEGER DEFAULT 0"
                if mutation == "wrong_not_null"
                else "message_count INTEGER NOT NULL DEFAULT 0"
            )
            primary_key = (
                "PRIMARY KEY (project_fingerprint)"
                if mutation == "wrong_primary_key"
                else ""
                if mutation == "missing_primary_unique"
                else "PRIMARY KEY (project_fingerprint, thread_id)"
            )
            check = "CHECK(message_count >= 0)" if mutation == "wrong_table_sql" else ""
            constraints = ",\n                    ".join(
                constraint for constraint in (check, primary_key) if constraint
            )
            constraint_clause = f",\n                    {constraints}" if constraints else ""
            connection.execute(
                f"""
                CREATE TABLE harness_threads (
                    project_fingerprint TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    first_message TEXT NOT NULL,
                    latest_message TEXT NOT NULL,
                    {message_count}{constraint_clause}
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX harness_threads_project_updated
                ON harness_threads(project_fingerprint, updated_at_ms DESC)
                """
            )
            if mutation == "dangerous_extra_column":
                connection.execute(
                    "ALTER TABLE harness_threads ADD COLUMN dangerous TEXT"
                )
        connection.execute("PRAGMA user_version=6")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:harness_threads",
    ):
        await ThreadPersistence.open(project=project, home=_home)
    assert not database.with_name(f"{database.name}.pre-v6-migration.bak").exists()


@pytest.mark.parametrize(
    "extra_table_sql",
    (
        "CREATE TABLE harness_thread_transcript (wrong TEXT CHECK(length(wrong) > 0))",
        "CREATE TABLE harness_unknown_source_table (id INTEGER)",
    ),
)
async def test_v6_source_rejects_forward_or_unknown_tables_before_backup(
    tmp_path: Path,
    extra_table_sql: str,
) -> None:
    """v6 只接受精确 source 表集，不按名称或尾列放行 successor 对象。"""
    _home, project, database = await _new_v6_fixture(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute(extra_table_sql)
        if extra_table_sql.startswith("CREATE TABLE harness_thread_transcript"):
            connection.execute(
                "CREATE INDEX harness_thread_transcript_wrong ON harness_thread_transcript(wrong)"
            )
        connection.execute("PRAGMA user_version=6")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:<database>:unexpected_table",
    ):
        await ThreadPersistence.open(project=project, home=_home)
    assert not database.with_name(f"{database.name}.pre-v6-migration.bak").exists()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (
                "harness_thread_transcript"
                if extra_table_sql.startswith("CREATE TABLE harness_thread_transcript")
                else "harness_unknown_source_table",
            ),
        ).fetchone()
    finally:
        connection.close()


@pytest.mark.parametrize("source_version", (7, 8, 9, 10, thread_persistence_module._SCHEMA_VERSION))
async def test_prompt_epoch_in_unsupported_schema_is_rejected_without_mutation(
    tmp_path: Path,
    source_version: int,
) -> None:
    """v7 及更高 schema 带 PromptEpoch 且无 migration state 时必须原样拒绝。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    initial = await ThreadPersistence.open(project=project, home=home)
    database = initial.database_path
    await initial.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE harness_prompt_epochs (
                project_fingerprint TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                prompt_version INTEGER NOT NULL,
                system_prompt TEXT NOT NULL,
                environment_snapshot TEXT NOT NULL,
                readonly_memory TEXT NOT NULL,
                skill_index TEXT NOT NULL,
                tool_schema_fingerprint TEXT NOT NULL,
                system_fingerprint TEXT NOT NULL,
                history_rewrite_version TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                prefix_change_reason TEXT NOT NULL DEFAULT 'new_thread',
                PRIMARY KEY (project_fingerprint, thread_id)
            )
            """
        )
        project_fingerprint = hashlib.sha256(
            str(project.resolve()).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO harness_prompt_epochs (
                project_fingerprint, thread_id, prompt_version, system_prompt,
                environment_snapshot, readonly_memory, skill_index,
                tool_schema_fingerprint, system_fingerprint,
                history_rewrite_version, created_at_ms
            ) VALUES (?, 'legacy-prompt', 1, '历史系统上下文', '{}', '{}', '{}', 'tool', 'fp', 'v1', 123)
            """,
            (project_fingerprint,),
        )
        connection.execute(f"PRAGMA user_version={source_version}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ThreadPersistenceError,
        match="CHECKPOINT_MIGRATION_LEGACY_TABLE_UNEXPECTED",
    ):
        await ThreadPersistence.open(project=project, home=home)

    state_path = database.with_name(database.name + ".migration-state.json")
    backup_path = database.with_name(
        f"{database.name}.pre-v{source_version}-migration.bak"
    )
    assert not state_path.exists()
    assert not backup_path.exists()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == source_version
        assert connection.execute(
            "SELECT system_prompt FROM harness_prompt_epochs WHERE thread_id='legacy-prompt'"
        ).fetchone()[0] == "历史系统上下文"
        assert connection.execute(
            "SELECT COUNT(*) FROM harness_run_context_snapshots"
        ).fetchone()[0] == 0
    finally:
        connection.close()


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
