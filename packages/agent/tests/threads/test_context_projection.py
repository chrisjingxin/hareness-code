"""ContextProjector 与版本化 CompressionCheckpoint 回归测试。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.runnables import Runnable
from langgraph.checkpoint.base import empty_checkpoint

from harness_agent.threads.context_projection import (
    CompressionCheckpointDraft,
    ContextProjectionError,
    ContextProjector,
    tail_user_exclude_id,
)
from harness_agent.threads.prompting import HISTORY_REWRITE_VERSION
from harness_agent.threads.thread_persistence import (
    CommitContextRewrite,
    ContextArtifactDraft,
    ContextState,
    ContextSummaryDraft,
    ThreadPersistence,
    ThreadPersistenceError,
    TranscriptAppend,
)
from tests.support.thread_fixtures import accept_thread


def test_tail_user_exclude_id_only_when_user_record_is_last() -> None:
    """同一 Run 二次拉 Runtime 时，用户消息已不是末条，不得再排除。"""
    user = type("Rec", (), {"record_id": "run:run-1:user"})()
    tool = type("Rec", (), {"record_id": "run:run-1:tool:1"})()
    assert tail_user_exclude_id((user,), "run-1") == "run:run-1:user"
    assert tail_user_exclude_id((user, tool), "run-1") is None
    assert tail_user_exclude_id((), "run-1") is None


async def _store(tmp_path, name: str = "project") -> ThreadPersistence:
    project = tmp_path / name
    project.mkdir()
    return await ThreadPersistence.open(project=project, home=tmp_path / "home")


async def test_projector_rebuilds_transcript_and_rehydrates_large_tool_artifact(tmp_path):
    """无检查点时只从 Transcript 生成消息，大工具原文由 Artifact 恢复。"""
    store = await _store(tmp_path)
    await accept_thread(store, "thread", "请求", run_id="run-1")
    large = "x" * (70 * 1024)
    await store.append_transcript_batch(
        (
            TranscriptAppend(
                thread_id="thread",
                record_id="assistant-1",
                kind="assistant",
                content="",
                tool_calls=(
                    {
                        "id": "call-1",
                        "name": "read_file",
                        "arguments": {"path": "/tmp/example"},
                        "arguments_status": "valid",
                    },
                ),
            ),
            TranscriptAppend(
                thread_id="thread",
                record_id="tool-1",
                kind="tool",
                content=large,
                tool_call_id="call-1",
                tool_name="read_file",
            ),
        )
    )

    projection = await ContextProjector(store).project("thread")

    assert [type(message) for message in projection.messages] == [
        HumanMessage,
        AIMessage,
        ToolMessage,
    ]
    assert projection.messages[-1].content == large
    assert projection.checkpoint is None
    await store.close()


async def test_latest_checkpoint_plus_tail_and_multiple_versions_are_auditable(tmp_path):
    """连续检查点都保留，恢复只使用最新有效版本及 tail。"""
    store = await _store(tmp_path)
    await accept_thread(store, "thread", "u1", run_id="run-1")
    first = await store.commit_context(
        CommitContextRewrite(
            thread_id="thread",
            checkpoint=CompressionCheckpointDraft(
                checkpoint_id="checkpoint-1",
                mode="full",
                rewrite_version=HISTORY_REWRITE_VERSION,
                projected_messages=(HumanMessage(content="summary-1"),),
            ),
        )
    )
    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="assistant-tail",
            kind="assistant",
            content="a1",
        )
    )
    second = await store.commit_context(
        CommitContextRewrite(
            thread_id="thread",
            checkpoint=CompressionCheckpointDraft(
                checkpoint_id="checkpoint-2",
                mode="full",
                rewrite_version=HISTORY_REWRITE_VERSION,
                projected_messages=(HumanMessage(content="summary-2"),),
            ),
        )
    )
    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="user-tail",
            kind="user",
            content="u2",
        )
    )

    projection = await ContextProjector(store).project("thread")
    cursor = await store._connection.execute(
        "SELECT COUNT(*) FROM harness_compression_checkpoints WHERE thread_id = ?",
        ("thread",),
    )
    count = int((await cursor.fetchone())[0])
    await cursor.close()

    assert first.checkpoint is not None and second.checkpoint is not None
    assert count == 2
    assert projection.checkpoint is not None
    assert projection.checkpoint.checkpoint_id == "checkpoint-2"
    assert [message.content for message in projection.messages] == ["summary-2", "u2"]
    await store.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("source_digest", "'wrong'"),
        ("rewrite_version", "'unknown'"),
        ("projected_messages", "'{broken'"),
        ("artifact_ids", "'[\"missing\"]'"),
        ("projected_messages", "'{\"version\":1,\"messages\":[{\"type\":\"user\",\"content\":NaN}]}'"),
        ("pressure_before", "'{\"ratio\":Infinity}'"),
    ],
)
async def test_latest_invalid_checkpoint_falls_back_to_previous_valid(
    tmp_path, column: str, value: str
):
    """digest、版本、消息 JSON 或 Artifact 损坏时不采用最新候选。"""
    store = await _store(tmp_path)
    await accept_thread(store, "thread", "u1")
    for checkpoint_id, content in (("good", "good"), ("bad", "bad")):
        await store.commit_context(
            CommitContextRewrite(
                thread_id="thread",
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id=checkpoint_id,
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content=content),),
                ),
            )
        )
    await store._connection.execute(
        f"UPDATE harness_compression_checkpoints SET {column} = {value}, created_at_ms = created_at_ms + 1 WHERE checkpoint_id = 'bad'"
    )
    await store._connection.commit()

    projection = await ContextProjector(store).project("thread")

    assert projection.checkpoint is not None
    assert projection.checkpoint.checkpoint_id == "good"
    await store.close()


async def test_checkpoint_rejects_cross_thread_artifact_and_non_finite_json(tmp_path):
    """Artifact 必须归属当前 thread，诊断 JSON 不得包含 NaN。"""
    store = await _store(tmp_path)
    await accept_thread(store, "a", "a")
    await accept_thread(store, "b", "b")
    artifact = (
        await store.commit_context(
            CommitContextRewrite(
                thread_id="b",
                artifacts=(
                    ContextArtifactDraft(
                        kind="history", content="secret", artifact_id="artifact-b"
                    ),
                ),
            )
        )
    ).artifacts[0]

    with pytest.raises(Exception, match="COMPRESSION_CHECKPOINT_ARTIFACT_MISSING"):
        await store.commit_context(
            CommitContextRewrite(
                thread_id="a",
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id="cross-thread",
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content="summary"),),
                    artifact_ids=(artifact.artifact_id,),
                ),
            )
        )
    with pytest.raises(ValueError):
        await store.commit_context(
            CommitContextRewrite(
                thread_id="a",
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id="nan",
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content="summary"),),
                    pressure_before={"ratio": float("nan")},
                ),
            )
        )
    await store.close()


async def test_projection_and_artifacts_are_isolated_between_projects(tmp_path):
    """同一用户库中的同名 thread/checkpoint/Artifact 不得跨 project 串线。"""
    first = await _store(tmp_path, "project-a")
    second = await _store(tmp_path, "project-b")
    await accept_thread(first, "thread", "a")
    await accept_thread(second, "thread", "b")
    for store, content in ((first, "summary-a"), (second, "summary-b")):
        await store.commit_context(
            CommitContextRewrite(
                thread_id="thread",
                artifacts=(
                    ContextArtifactDraft(
                        kind="history",
                        content=content,
                        artifact_id="same-artifact",
                    ),
                ),
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id="same-checkpoint",
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content=content),),
                    artifact_ids=("same-artifact",),
                ),
            )
        )

    assert (await ContextProjector(first).project("thread")).messages[0].content == "summary-a"
    assert (await ContextProjector(second).project("thread")).messages[0].content == "summary-b"
    assert (await first.load_context_artifact("thread", "same-artifact")).content == "summary-a"  # type: ignore[union-attr]
    assert (await second.load_context_artifact("thread", "same-artifact")).content == "summary-b"  # type: ignore[union-attr]
    await first.close()
    await second.close()


async def test_atomic_tool_groups_fail_closed_for_orphan_and_incomplete_tail(tmp_path):
    """孤儿结果和未完成的并行工具 tail 都不能成为模型历史。"""
    store = await _store(tmp_path)
    await accept_thread(store, "orphan", "u")
    await store.append_transcript(
        TranscriptAppend(
            thread_id="orphan",
            record_id="tool",
            kind="tool",
            content="result",
            tool_call_id="missing",
        )
    )
    with pytest.raises(ContextProjectionError, match="TOOL_RESULT_ORPHAN"):
        await ContextProjector(store).project("orphan")

    await accept_thread(store, "incomplete", "u")
    await store.append_transcript_batch(
        (
            TranscriptAppend(
                thread_id="incomplete",
                record_id="assistant",
                kind="assistant",
                content="",
                tool_calls=(
                    {"id": "a", "name": "tool", "arguments": {}, "arguments_status": "valid"},
                    {"id": "b", "name": "tool", "arguments": {}, "arguments_status": "valid"},
                ),
            ),
            TranscriptAppend(
                thread_id="incomplete",
                record_id="tool-a",
                kind="tool",
                content="a",
                tool_call_id="a",
            ),
        )
    )
    with pytest.raises(ContextProjectionError, match="TOOL_GROUP_INCOMPLETE"):
        await ContextProjector(store).project("incomplete")
    await store.close()


async def test_checkpoint_commit_is_idempotent_and_conflict_fails(tmp_path):
    """相同 checkpoint ID 的相同提交可重试，不同载荷拒绝。"""
    store = await _store(tmp_path)
    await accept_thread(store, "thread", "u")
    command = CommitContextRewrite(
        thread_id="thread",
        checkpoint=CompressionCheckpointDraft(
            checkpoint_id="stable",
            mode="full",
            rewrite_version=HISTORY_REWRITE_VERSION,
            projected_messages=(HumanMessage(content="same"),),
        ),
    )
    first = await store.commit_context(command)
    second = await store.commit_context(command)
    assert first.checkpoint == second.checkpoint

    with pytest.raises(ThreadPersistenceError, match="COMPRESSION_CHECKPOINT_CONFLICT"):
        await store.commit_context(
            CommitContextRewrite(
                thread_id="thread",
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id="stable",
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content="different"),),
                ),
            )
        )

    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="later-user",
            kind="user",
            content="later",
        )
    )
    with pytest.raises(ThreadPersistenceError, match="COMPRESSION_CHECKPOINT_CONFLICT"):
        await store.commit_context(command)

    explicit_boundary = CommitContextRewrite(
        thread_id="thread",
        checkpoint=CompressionCheckpointDraft(
            checkpoint_id="explicit-boundary",
            mode="full",
            rewrite_version=HISTORY_REWRITE_VERSION,
            projected_messages=(HumanMessage(content="same"),),
            source_record_sequence=1,
        ),
    )
    explicit_first = await store.commit_context(explicit_boundary)
    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="after-explicit-boundary",
            kind="user",
            content="after explicit boundary",
        )
    )
    explicit_second = await store.commit_context(explicit_boundary)
    assert explicit_first.checkpoint == explicit_second.checkpoint
    await store.close()


async def test_full_context_rewrite_idempotence_covers_artifact_summary_and_state(tmp_path):
    """checkpoint 幂等键必须覆盖整个领域命令，并返回首次提交的完整结果。"""
    store = await _store(tmp_path)
    await accept_thread(store, "thread", "u")
    command = CommitContextRewrite(
        thread_id="thread",
        artifacts=(
            ContextArtifactDraft(
                kind="history",
                content="original",
                source_start=0,
                source_end=1,
                artifact_id="stable-artifact",
            ),
        ),
        summary=ContextSummaryDraft(
            rewrite_version=HISTORY_REWRITE_VERSION,
            content="summary",
            source_start=0,
            source_end=1,
            artifact_indexes=(0,),
        ),
        state=ContextState(failures=1, last_action="summary"),
        checkpoint=CompressionCheckpointDraft(
            checkpoint_id="whole-command",
            mode="full",
            rewrite_version=HISTORY_REWRITE_VERSION,
            projected_messages=(
                HumanMessage(
                    content="summary\n/.harness/history/stable-artifact.md"
                ),
            ),
            artifact_ids=("stable-artifact",),
        ),
    )
    first = await store.commit_context(command)
    retried = await store.commit_context(command)
    assert retried == first
    assert retried.artifacts and retried.summary and retried.state

    variants = (
        {"artifacts": (ContextArtifactDraft(kind="history", content="different", artifact_id="stable-artifact"),)},
        {"summary": ContextSummaryDraft(rewrite_version=HISTORY_REWRITE_VERSION, content="different", source_start=0, source_end=1, artifact_indexes=(0,))},
        {"state": ContextState(failures=2, last_action="summary")},
    )
    for changes in variants:
        with pytest.raises(
            ThreadPersistenceError, match="COMPRESSION_CHECKPOINT_CONFLICT"
        ):
            await store.commit_context(
                CommitContextRewrite(
                    thread_id=command.thread_id,
                    artifacts=changes.get("artifacts", command.artifacts),
                    summary=changes.get("summary", command.summary),
                    state=changes.get("state", command.state),
                    checkpoint=command.checkpoint,
                )
            )
    cursor = await store._connection.execute(
        "SELECT COUNT(*) FROM harness_context_artifacts WHERE artifact_id = 'stable-artifact'"
    )
    assert (await cursor.fetchone())[0] == 1
    await cursor.close()
    cursor = await store._connection.execute(
        "SELECT COUNT(*) FROM harness_context_summaries WHERE thread_id = 'thread'"
    )
    assert (await cursor.fetchone())[0] == 1
    await cursor.close()
    assert await store._load_context_state("thread") == command.state
    await store.close()


async def test_missing_tool_arguments_and_non_string_ids_fail_closed(tmp_path):
    """valid 参数必须有显式对象，持久化 JSON 中的 typed ID 不允许强制转字符串。"""
    from harness_agent.threads.context_projection import decode_projected_messages

    store = await _store(tmp_path)
    await accept_thread(store, "thread", "u")
    with pytest.raises(
        ThreadPersistenceError, match="TRANSCRIPT_TOOL_CALL_ARGUMENTS_MISSING"
    ):
        await store.append_transcript(
            TranscriptAppend(
                thread_id="thread",
                record_id="missing-args",
                kind="assistant",
                content="",
                tool_calls=(
                    {
                        "id": "call",
                        "name": "read",
                        "arguments_status": "valid",
                    },
                ),
            )
        )

    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="corrupt-args",
            kind="assistant",
            content="",
            tool_calls=(
                {
                    "id": "corrupt-call",
                    "name": "read",
                    "arguments": {},
                    "arguments_status": "valid",
                },
            ),
        )
    )
    cursor = await store._connection.execute(
        "SELECT payload FROM harness_thread_transcript WHERE record_id = 'corrupt-args'"
    )
    row = await cursor.fetchone()
    await cursor.close()
    payload = json.loads(row[0])
    del payload["tool_calls"][0]["arguments"]
    await store._connection.execute(
        "UPDATE harness_thread_transcript SET payload = ? WHERE record_id = 'corrupt-args'",
        (json.dumps(payload),),
    )
    await store._connection.commit()
    with pytest.raises(
        ContextProjectionError, match="PROJECTION_TOOL_CALL_ARGUMENTS_MISSING"
    ):
        await ContextProjector(store).project("thread")

    for encoded in (
        '{"version":1,"messages":[{"type":"assistant","content":"","tool_calls":[{"id":"call","name":"read","type":"tool_call"}]}]}',
        '{"version":1,"messages":[{"type":"user","content":"u","id":7}]}',
        '{"version":1,"messages":[{"type":"assistant","content":"","tool_calls":[{"id":7,"name":"read","args":{},"type":"tool_call"}]}]}',
    ):
        with pytest.raises(ContextProjectionError):
            decode_projected_messages(encoded)
    await store.close()


async def test_projector_is_the_only_writer_that_replaces_langgraph_cache(tmp_path):
    """首次 Run 尚无 checkpoint 时也能从 Transcript 创建空的前置缓存。"""
    from harness_agent.runtime.agent import create_harness_agent

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    store = await _store(tmp_path)
    await accept_thread(store, "thread", "current", run_id="run-current")
    model = ToolModel(responses=[AIMessage(content="unused")])
    model.profile = {"max_input_tokens": 16_384}
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path / "project"),
        checkpointer=store.checkpointer,
        thread_persistence=store,
        context_window_tokens=16_384,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
    )

    projection = await ContextProjector(store).sync_cache(
        agent,
        "thread",
        exclude_record_id="run:run-current:user",
    )
    cached = await store.load_context("thread")

    assert projection.messages == ()
    assert cached.recoverable is True
    assert cached.messages == ()
    await store.close()


async def test_v8_migration_keeps_legacy_projection_auditable_but_syncs_transcript(tmp_path):
    """legacy 投影只供审计；正常 project/sync_cache 必须使用可用 Transcript。"""
    store = await _store(tmp_path)
    await accept_thread(store, "legacy", "visible")
    await store.commit_context(
        CommitContextRewrite(
            thread_id="legacy",
            artifacts=(
                ContextArtifactDraft(
                    kind="history",
                    content="legacy original",
                    artifact_id="legacy-artifact",
                ),
            ),
        )
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            HumanMessage(
                content=(
                    "legacy projection\n"
                    "Archived original: /.harness/history/legacy-artifact.md"
                )
            )
        ]
    }
    await store.checkpointer.aput(store.graph_config("legacy"), checkpoint, {}, {})
    database = store.database_path
    await store.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE harness_compression_checkpoints")
        connection.execute("PRAGMA user_version=8")
        connection.commit()
    finally:
        connection.close()

    migrated = await ThreadPersistence.open(
        project=tmp_path / "project", home=tmp_path / "home"
    )
    projection = await ContextProjector(migrated).project("legacy")
    opened = await migrated.open_thread("legacy")
    audit = await migrated.load_latest_valid_compression_checkpoint(
        "legacy", include_legacy_incomplete=True
    )

    assert audit is not None and audit.legacy_incomplete is True
    assert audit.rewrite_version == "legacy-incomplete-v1"
    assert audit.artifact_ids == ("legacy-artifact",)
    assert projection.checkpoint is None
    assert [message.content for message in projection.messages] == ["visible"]
    assert [message.content for message in opened.messages] == ["visible"]

    from harness_agent.runtime.agent import create_harness_agent

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    model = ToolModel(responses=[AIMessage(content="unused")])
    model.profile = {"max_input_tokens": 16_384}
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path / "project"),
        checkpointer=migrated.checkpointer,
        thread_persistence=migrated,
        context_window_tokens=16_384,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
    )
    synced = await ContextProjector(migrated).sync_cache(agent, "legacy")
    cached = await migrated.load_context("legacy")
    assert [message.content for message in synced.messages] == ["visible"]
    assert [message.content for message in cached.messages] == ["visible"]
    await migrated.close()


async def test_checkpoint_failure_rolls_back_artifact_and_state(tmp_path):
    """检查点写入失败时同一事务的 Artifact 与状态不得半提交。"""
    store = await _store(tmp_path)
    await accept_thread(store, "thread", "u")
    await store._connection.execute(
        """
        CREATE TRIGGER fail_projection_insert
        BEFORE INSERT ON harness_compression_checkpoints
        BEGIN
            SELECT RAISE(ABORT, 'injected projection failure');
        END
        """
    )
    await store._connection.commit()

    with pytest.raises(ThreadPersistenceError, match="CONTEXT_REWRITE_WRITE_FAILED"):
        await store.commit_context(
            CommitContextRewrite(
                thread_id="thread",
                artifacts=(
                    ContextArtifactDraft(
                        kind="history",
                        content="must rollback",
                        artifact_id="rollback-artifact",
                    ),
                ),
                checkpoint=CompressionCheckpointDraft(
                    checkpoint_id="rollback-checkpoint",
                    mode="full",
                    rewrite_version=HISTORY_REWRITE_VERSION,
                    projected_messages=(HumanMessage(content="summary"),),
                    artifact_ids=("rollback-artifact",),
                ),
            )
        )

    assert await store.load_context_artifact("thread", "rollback-artifact") is None
    await store.close()


async def test_v8_to_v9_migration_failure_rolls_back_schema_and_keeps_backup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """v9 bootstrap 失败时 DDL/user_version 回滚，pre-v8 备份保留。"""
    store = await _store(tmp_path)
    database = store.database_path
    await store.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE harness_compression_checkpoints")
        connection.execute("PRAGMA user_version=8")
        connection.commit()
    finally:
        connection.close()

    # legacy migration 已整体移入隔离 child；父进程 monkeypatch 不会跨进程生效。
    # 使用受 pytest 限制的 child failpoint，验证真实生产迁移边界的回滚行为。
    monkeypatch.setenv("HARNESS_TEST_MIGRATION_CHILD_PHASE", "bootstrap_failure")
    with pytest.raises(ThreadPersistenceError, match="CHECKPOINT_MIGRATION_FAILED"):
        await ThreadPersistence.open(
            project=tmp_path / "project", home=tmp_path / "home"
        )

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='harness_compression_checkpoints'"
        ).fetchone() is None
    finally:
        connection.close()
    assert database.with_name(f"{database.name}.pre-v8-migration.bak").exists()
