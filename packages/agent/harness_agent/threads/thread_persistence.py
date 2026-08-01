"""Harness thread 持久化：以用户级 SQLite 保存 LangGraph checkpoint 和当前 project 的线程索引。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Mapping

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from harness_agent.runtime.execution_binding import (
    ExecutionBindingError,
    LegacyModelBindings,
    PersistedBindingState,
    RunExecutionBinding,
)
from harness_agent.context_lifecycle import RunContextSnapshot, snapshot_from_legacy_prompt_epoch
from harness_agent.prompting import HISTORY_REWRITE_VERSION, PromptEpoch, canonical_json
from harness_agent.agent_engine_profile import AGENT_ENGINE_PROFILE_VERSION, AgentEngineProfile
from harness_agent.context_projection import (
    CompressionCheckpoint,
    CompressionCheckpointDraft,
    ContextProjectionError,
    SUPPORTED_REWRITE_VERSIONS,
    artifact_references,
    decode_projected_messages,
    encode_projected_messages,
    source_digest,
    strict_json_loads,
)


_SCHEMA_VERSION = 10
_MAX_PREVIEW_CHARS = 160
_MAX_INLINE_TOOL_BYTES = 64 * 1024
_TRANSCRIPT_KINDS = ("user", "assistant", "tool", "context")


class ThreadPersistenceError(RuntimeError):
    """线程存储不可用、损坏或版本不兼容时返回的可诊断错误。"""


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """恢复选择器所需的当前 project 线程摘要；内部 ID 不应直接展示给用户。"""

    thread_id: str
    created_at_ms: int
    updated_at_ms: int
    first_message: str
    latest_message: str
    message_count: int


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """由规范记录归一化出的稳定消息历史，供 CLI 表现层回放。"""

    kind: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class OpenThread:
    """已校验归属 project 的线程快照和可回放消息。"""

    summary: ThreadSummary
    messages: tuple[ThreadMessage, ...]
    legacy_incomplete_history: bool = False


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """当前 Thread 的 checkpoint 消息与 Context 熔断状态。"""

    messages: tuple[Any, ...]
    state: ContextState
    recoverable: bool


@dataclass(frozen=True, slots=True)
class ContextArtifact:
    """仅当前 project/thread 可见的不可变会话归档。"""

    artifact_id: str
    kind: str
    content: str
    source_start: int
    source_end: int
    created_at_ms: int
    content_sha256: str = ""
    byte_length: int = 0


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """一次上下文重写的结构化摘要和来源范围。"""

    rewrite_version: str
    content: str
    source_start: int
    source_end: int
    artifact_ids: tuple[str, ...]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ContextState:
    """自动压缩的失败熔断和最近一次策略状态。"""

    failures: int = 0
    circuit_open: bool = False
    last_action: str = "none"


@dataclass(frozen=True, slots=True)
class AcceptRun:
    """受理 Run 所需的完整领域输入；SQLite 不再接收散落字段。"""

    message: str
    binding: RunExecutionBinding
    context_snapshot: RunContextSnapshot | None = None


@dataclass(frozen=True, slots=True)
class TranscriptAppend:
    """追加一条完整语义记录所需的 typed 输入。"""

    thread_id: str
    record_id: str
    kind: Literal["user", "assistant", "tool", "context"]
    content: str
    run_id: str | None = None
    execution_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_call_id_status: str | None = None
    legacy_invalid_fields: tuple[str, ...] = ()
    tool_calls: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    """一条追加且可审计的 Thread 规范记录。"""

    record_id: str
    thread_id: str
    run_id: str | None
    execution_id: str | None
    sequence: int
    kind: Literal["user", "assistant", "tool", "context"]
    payload: Mapping[str, object]
    content_sha256: str
    byte_length: int
    artifact_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class RunAcceptance:
    """Run 受理结果；``created=False`` 表示同一请求的幂等重试。"""

    created: bool
    binding: RunExecutionBinding


@dataclass(frozen=True, slots=True)
class ContextArtifactDraft:
    """待写入 Context 归档的领域值，不包含存储生成的 artifact ID。"""

    kind: str
    content: str
    source_start: int = 0
    source_end: int = 0
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextSummaryDraft:
    """待写入摘要；``artifact_indexes`` 引用同一事务中的归档草稿。"""

    rewrite_version: str
    content: str
    source_start: int
    source_end: int
    artifact_indexes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitContextRewrite:
    """一次 Context 状态转换，归档、摘要和熔断状态同成同败。"""

    thread_id: str
    artifacts: tuple[ContextArtifactDraft, ...] = ()
    summary: ContextSummaryDraft | None = None
    state: ContextState | None = None
    checkpoint: CompressionCheckpointDraft | None = None


@dataclass(frozen=True, slots=True)
class ContextCommit:
    """Context 状态转换提交后的 typed 结果。"""

    artifacts: tuple[ContextArtifact, ...] = ()
    summary: ContextSummary | None = None
    state: ContextState | None = None
    checkpoint: CompressionCheckpoint | None = None


class ProjectScopedAsyncSqliteSaver(AsyncSqliteSaver):
    """将 LangGraph 自动归一的 checkpoint namespace 固定映射到当前 project。"""

    def __init__(
        self,
        connection: aiosqlite.Connection,
        project_fingerprint: str,
        *,
        operation_lock: asyncio.Lock | None = None,
    ) -> None:
        """复用同一 SQLite 连接，并保留 project 指纹作为根 namespace。"""
        super().__init__(connection)
        if operation_lock is not None:
            self.lock = operation_lock
        self._project_fingerprint = project_fingerprint

    async def aget_tuple(self, config: dict[str, Any]) -> Any:
        """读取时即使根图丢弃 namespace，仍只查询当前 project 的 checkpoint。"""
        return await super().aget_tuple(self._scoped_config(config))

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        """列举时要求 thread 范围，禁止通过底层 saver 跨 project 扫描。"""
        if config is None:
            raise ThreadPersistenceError("CHECKPOINT_LIST_REQUIRES_THREAD")
        scoped_before = self._scoped_config(before) if before is not None else None
        async for checkpoint in super().alist(
            self._scoped_config(config),
            filter=filter,
            before=scoped_before,
            limit=limit,
        ):
            yield checkpoint

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """写入时给根图补回 project namespace，并为子图保留独立后缀。"""
        return await super().aput(
            self._scoped_config(config),
            checkpoint,
            metadata,
            new_versions,
        )

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        """将中间 writes 与对应 checkpoint 放入同一 project namespace。"""
        await super().aput_writes(
            self._scoped_config(config),
            writes,
            task_id,
            task_path,
        )

    async def adelete_thread(self, thread_id: str) -> None:
        """删除操作只能清理当前 project 的根和子图 namespace，不能跨 project。"""
        prefix = f"{self._project_fingerprint}:%"
        async with self.lock, self.conn.cursor() as cursor:
            for table in ("checkpoints", "writes"):
                await cursor.execute(
                    f"DELETE FROM {table} WHERE thread_id = ? AND (checkpoint_ns = ? OR checkpoint_ns LIKE ?)",
                    (str(thread_id), self._project_fingerprint, prefix),
                )
            await self.conn.commit()

    def _scoped_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """合成 namespace：根图为指纹，子图为指纹加 LangGraph 原始后缀。"""
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise ThreadPersistenceError("CHECKPOINT_CONFIG_INVALID")
        raw_namespace = configurable.get("checkpoint_ns")
        namespace = str(raw_namespace) if raw_namespace is not None else ""
        if namespace in {"", self._project_fingerprint}:
            scoped_namespace = self._project_fingerprint
        elif namespace.startswith(f"{self._project_fingerprint}:"):
            scoped_namespace = namespace
        else:
            scoped_namespace = f"{self._project_fingerprint}:{namespace}"
        return {
            **config,
            "configurable": {**configurable, "checkpoint_ns": scoped_namespace},
        }


class ThreadPersistence:
    """按 Thread、Run 和 Context 生命周期封装 SQLite 与 checkpoint 细节。"""

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        checkpointer: ProjectScopedAsyncSqliteSaver,
        path: Path,
        project_fingerprint: str,
        operation_lock: asyncio.Lock | None = None,
    ) -> None:
        """保存已验证的连接和固定 project namespace。"""
        self._connection = connection
        self._checkpointer = checkpointer
        self._path = path
        self._project_fingerprint = project_fingerprint
        self._closed = False
        self._lock = operation_lock or checkpointer.lock
        if checkpointer.lock is not self._lock:
            checkpointer.lock = self._lock

    @classmethod
    async def open(
        cls,
        *,
        project: Path,
        home: Path | None = None,
    ) -> "ThreadPersistence":
        """打开用户级数据库、检查完整性并应用 Harness 自有索引迁移。"""
        base_home = (home or Path.home()).expanduser().resolve()
        data_dir = base_home / ".harness"
        connection: aiosqlite.Connection | None = None
        try:
            data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(data_dir, 0o700)
            path = data_dir / "threads.sqlite3"
            connection = await aiosqlite.connect(path)
            os.chmod(path, 0o600)
            connection.row_factory = aiosqlite.Row
            project_fingerprint = _project_fingerprint(project)
            operation_lock = asyncio.Lock()
            checkpointer = ProjectScopedAsyncSqliteSaver(
                connection,
                project_fingerprint,
                operation_lock=operation_lock,
            )
            persistence = cls(
                connection=connection,
                checkpointer=checkpointer,
                path=path,
                project_fingerprint=project_fingerprint,
                operation_lock=operation_lock,
            )
            await persistence._prepare()
            return persistence
        except asyncio.CancelledError:
            if connection is not None:
                try:
                    await connection.rollback()
                except Exception:
                    pass
                try:
                    await connection.close()
                except Exception:
                    pass
            raise
        except ThreadPersistenceError:
            if connection is not None:
                try:
                    await connection.close()
                except aiosqlite.Error:
                    pass
            raise
        except (OSError, aiosqlite.Error) as exc:
            if connection is not None:
                try:
                    await connection.close()
                except aiosqlite.Error:
                    pass
            raise ThreadPersistenceError(f"CHECKPOINT_OPEN_FAILED: {exc}") from exc

    @property
    def checkpointer(self) -> ProjectScopedAsyncSqliteSaver:
        """返回注入 DeepAgents 图的异步 LangGraph checkpointer。"""
        return self._checkpointer

    @property
    def database_path(self) -> Path:
        """返回当前用户可手动清理的数据库路径。"""
        return self._path

    @property
    def project_fingerprint(self) -> str:
        """返回仅用于 namespace 和索引过滤的不可逆 project 标识。"""
        return self._project_fingerprint

    def graph_config(self, thread_id: str) -> dict[str, dict[str, str]]:
        """构造 LangGraph 所需的 thread_id 和 project 隔离 checkpoint namespace。"""
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": self._project_fingerprint,
            }
        }

    async def accept_run(self, command: AcceptRun) -> RunAcceptance:
        """原子受理一个 Run，并提交或复用 Context snapshot、索引和绑定。"""
        return RunAcceptance(
            created=await self._record_run_start(
                command.message, command.binding, command.context_snapshot
            ),
            binding=command.binding,
        )

    async def _record_run_start(
        self,
        message: str,
        binding: RunExecutionBinding,
        context_snapshot: RunContextSnapshot | None = None,
    ) -> bool:
        """原子登记 snapshot、binding、Thread 索引和用户记录。"""
        self._ensure_open()
        thread_id = binding.thread_id
        run_id = binding.run_id
        now = binding.created_at_ms
        if context_snapshot is not None:
            self._validate_context_snapshot(context_snapshot, binding)
        elif binding.context_snapshot_id is not None:
            raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_MISSING")
        preview = _preview(message)
        encoded_selection = canonical_json(binding.requested_selection_record())
        encoded_primary = canonical_json(binding.actual_primary_record())
        message_digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(
                    """
                    SELECT requested_selection, actual_primary_binding, runtime_profile_id,
                           message_digest, context_snapshot_id
                    FROM harness_run_execution_bindings
                    WHERE project_fingerprint = ? AND thread_id = ? AND run_id = ?
                    """,
                    (self._project_fingerprint, thread_id, run_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    if (
                        str(existing["requested_selection"]) == encoded_selection
                        and str(existing["actual_primary_binding"]) == encoded_primary
                        and str(existing["runtime_profile_id"]) == binding.runtime_profile_id
                        and str(existing["message_digest"]) == message_digest
                        and (
                            str(existing["context_snapshot_id"])
                            if existing["context_snapshot_id"] is not None
                            else None
                        )
                        == binding.context_snapshot_id
                    ):
                        if context_snapshot is not None:
                            await self._insert_context_snapshot_in_transaction(context_snapshot)
                        user_command = TranscriptAppend(
                            thread_id=thread_id,
                            record_id=_user_record_id(run_id),
                            kind="user",
                            content=message,
                            run_id=run_id,
                            execution_id=_root_execution_id(run_id),
                        )
                        if not await self._has_legacy_user_record_in_transaction(
                            thread_id, run_id, message
                        ):
                            await self._append_transcript_in_transaction(user_command)
                        await self._refresh_thread_index_in_transaction(thread_id, now)
                        await self._connection.commit()
                        return False
                    raise ThreadPersistenceError("RUN_EXECUTION_BINDING_CONFLICT")
                if context_snapshot is not None:
                    await self._insert_context_snapshot_in_transaction(context_snapshot)
                await self._connection.execute(
                    """
                    INSERT INTO harness_threads (
                        project_fingerprint, thread_id, created_at_ms, updated_at_ms,
                        first_message, latest_message, message_count
                    ) VALUES (?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(project_fingerprint, thread_id) DO UPDATE SET
                        updated_at_ms = excluded.updated_at_ms,
                        latest_message = excluded.latest_message
                    """,
                    (
                        self._project_fingerprint,
                        thread_id,
                        now,
                        now,
                        preview,
                        preview,
                    ),
                )
                await self._append_transcript_in_transaction(
                    TranscriptAppend(
                        thread_id=thread_id,
                        record_id=_user_record_id(run_id),
                        kind="user",
                        content=message,
                        run_id=run_id,
                        execution_id=_root_execution_id(run_id),
                    )
                )
                await self._connection.execute(
                    """
                    INSERT INTO harness_run_execution_bindings (
                        project_fingerprint, thread_id, run_id, requested_selection,
                        actual_primary_binding, runtime_profile_id, message_digest,
                        created_at_ms, context_snapshot_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._project_fingerprint,
                        thread_id,
                        run_id,
                        encoded_selection,
                        encoded_primary,
                        binding.runtime_profile_id,
                        message_digest,
                        now,
                        binding.context_snapshot_id,
                    ),
                )
                await self._refresh_thread_index_in_transaction(thread_id, now)
                await self._connection.commit()
                return True
            except BaseException as exc:
                try:
                    await self._connection.rollback()
                except aiosqlite.Error:
                    pass
                if isinstance(exc, ThreadPersistenceError) or isinstance(
                    exc, asyncio.CancelledError
                ):
                    raise
                if isinstance(exc, aiosqlite.Error):
                    raise ThreadPersistenceError(
                        f"RUN_EXECUTION_BINDING_WRITE_FAILED: {exc}"
                    ) from exc
                raise

    def _validate_context_snapshot(
        self,
        snapshot: RunContextSnapshot,
        binding: RunExecutionBinding,
    ) -> None:
        """验证 snapshot 与当前 project、Thread 和 binding 是同一准备结果。"""
        if snapshot.project_fingerprint != self._project_fingerprint:
            raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_PROJECT_MISMATCH")
        if snapshot.thread_id != binding.thread_id:
            raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_THREAD_MISMATCH")
        if binding.context_snapshot_id != snapshot.snapshot_id:
            raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_BINDING_MISMATCH")

    async def load_context_snapshot(
        self, snapshot_id: str, *, thread_id: str | None = None
    ) -> RunContextSnapshot:
        """按当前 project 和可选 Thread 读取可审计的 Context snapshot。"""
        self._ensure_open()
        try:
            async with self._lock:
                query = """
                    SELECT snapshot_record
                    FROM harness_run_context_snapshots
                    WHERE project_fingerprint = ? AND snapshot_id = ?
                """
                parameters: tuple[object, ...] = (
                    self._project_fingerprint,
                    snapshot_id,
                )
                if thread_id is not None:
                    query += " AND thread_id = ?"
                    parameters += (thread_id,)
                cursor = await self._connection.execute(query, parameters)
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_NOT_FOUND")
            snapshot = RunContextSnapshot.from_record(strict_json_loads(str(row["snapshot_record"])))
            if snapshot.project_fingerprint != self._project_fingerprint:
                raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_PROJECT_MISMATCH")
            if thread_id is not None and snapshot.thread_id != thread_id:
                raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_THREAD_MISMATCH")
            return snapshot
        except ThreadPersistenceError:
            raise
        except (aiosqlite.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError(f"RUN_CONTEXT_SNAPSHOT_READ_FAILED: {exc}") from exc

    async def _insert_context_snapshot_in_transaction(
        self, snapshot: RunContextSnapshot
    ) -> None:
        """在 accept_run 的 IMMEDIATE 事务中幂等保存 snapshot。"""
        encoded = canonical_json(snapshot.record())
        cursor = await self._connection.execute(
            """
            SELECT thread_id, snapshot_record, system_fingerprint, legacy
            FROM harness_run_context_snapshots
            WHERE project_fingerprint = ? AND snapshot_id = ?
            """,
            (self._project_fingerprint, snapshot.snapshot_id),
        )
        existing = await cursor.fetchone()
        await cursor.close()
        if existing is not None:
            try:
                existing_record = strict_json_loads(str(existing["snapshot_record"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_CONFLICT") from exc
            incoming_record = snapshot.record()
            # ``created_at_ms`` describes the first durable materialization, not
            # the identity of equivalent content prepared by a later Run.
            existing_record["created_at_ms"] = incoming_record["created_at_ms"]
            if (
                str(existing["thread_id"]) != snapshot.thread_id
                or canonical_json(existing_record) != canonical_json(incoming_record)
                or str(existing["system_fingerprint"]) != snapshot.system_fingerprint
                or bool(existing["legacy"]) != snapshot.legacy
            ):
                raise ThreadPersistenceError("RUN_CONTEXT_SNAPSHOT_CONFLICT")
            return
        await self._connection.execute(
            """
            INSERT INTO harness_run_context_snapshots (
                project_fingerprint, snapshot_id, thread_id, snapshot_record,
                system_fingerprint, created_at_ms, legacy
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._project_fingerprint,
                snapshot.snapshot_id,
                snapshot.thread_id,
                encoded,
                snapshot.system_fingerprint,
                snapshot.created_at_ms,
                1 if snapshot.legacy else 0,
            ),
        )

    async def _has_legacy_user_record_in_transaction(
        self, thread_id: str, run_id: str, message: str
    ) -> bool:
        """识别 v6 已迁移的同一用户消息，避免幂等重试制造重复可见记录。"""
        cursor = await self._connection.execute(
            """
            SELECT payload
            FROM harness_thread_transcript
            WHERE project_fingerprint = ? AND thread_id = ? AND kind = 'user'
              AND record_id != ?
            ORDER BY sequence ASC
            """,
            (self._project_fingerprint, thread_id, _user_record_id(run_id)),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return any(_payload_content(row["payload"]) == message for row in rows)

    async def append_transcript(self, command: TranscriptAppend) -> TranscriptRecord:
        """原子追加一条完整语义记录及其可选 Artifact。"""
        records = await self.append_transcript_batch((command,))
        return records[0]

    async def append_transcript_batch(
        self, commands: tuple[TranscriptAppend, ...]
    ) -> tuple[TranscriptRecord, ...]:
        """在一个事务中追加同一 Thread 的记录，失败时全部回滚。"""
        self._ensure_open()
        if not commands:
            return ()
        thread_id = commands[0].thread_id
        if any(command.thread_id != thread_id for command in commands):
            raise ThreadPersistenceError("TRANSCRIPT_BATCH_THREAD_MISMATCH")
        if any(command.kind not in _TRANSCRIPT_KINDS for command in commands):
            raise ThreadPersistenceError("TRANSCRIPT_KIND_INVALID")
        if any(command.tool_calls and command.kind != "assistant" for command in commands):
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_KIND_INVALID")
        if any(command.legacy_invalid_fields for command in commands):
            raise ThreadPersistenceError("TRANSCRIPT_LEGACY_MARKER_FORBIDDEN")
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                records_list: list[TranscriptRecord] = []
                for command in commands:
                    records_list.append(await self._append_transcript_in_transaction(command))
                await self._refresh_thread_index_in_transaction(thread_id, _now_ms())
                await self._connection.commit()
                return tuple(records_list)
            except BaseException as exc:
                try:
                    await self._connection.rollback()
                except aiosqlite.Error:
                    pass
                if isinstance(exc, ThreadPersistenceError) or isinstance(
                    exc, asyncio.CancelledError
                ):
                    raise
                if isinstance(exc, aiosqlite.Error):
                    raise ThreadPersistenceError(f"TRANSCRIPT_WRITE_FAILED: {exc}") from exc
                raise

    async def load_transcript(self, thread_id: str) -> tuple[TranscriptRecord, ...]:
        """按稳定 sequence 读取当前 project/thread 的规范记录。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT record_id, thread_id, run_id, execution_id, sequence,
                           kind, payload, content_sha256, byte_length, artifact_id,
                           created_at_ms
                    FROM harness_thread_transcript
                    WHERE project_fingerprint = ? AND thread_id = ?
                    ORDER BY sequence ASC
                    """,
                    (self._project_fingerprint, thread_id),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(_transcript_record(row) for row in rows)
        except (aiosqlite.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError(f"TRANSCRIPT_READ_FAILED: {exc}") from exc

    async def _get_latest_run_execution_binding(
        self, thread_id: str
    ) -> RunExecutionBinding | None:
        """读取最后一个已受理 Run 的模型选择与实际模型，不推测或修复损坏记录。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT thread_id, run_id, requested_selection, actual_primary_binding,
                           runtime_profile_id, created_at_ms, context_snapshot_id
                    FROM harness_run_execution_bindings
                    WHERE project_fingerprint = ? AND thread_id = ?
                    ORDER BY created_at_ms DESC, rowid DESC
                    LIMIT 1
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                return None
            selection = strict_json_loads(str(row["requested_selection"]))
            primary = strict_json_loads(str(row["actual_primary_binding"]))
            if not isinstance(selection, dict) or not isinstance(primary, dict):
                raise ThreadPersistenceError("RUN_EXECUTION_BINDING_INVALID")
            return RunExecutionBinding.from_records(
                thread_id=str(row["thread_id"]),
                run_id=str(row["run_id"]),
                requested_selection=selection,
                actual_primary_binding=primary,
                runtime_profile_id=str(row["runtime_profile_id"]),
                created_at_ms=int(row["created_at_ms"]),
                context_snapshot_id=(
                    str(row["context_snapshot_id"])
                    if row["context_snapshot_id"] is not None
                    else None
                ),
            )
        except ThreadPersistenceError:
            raise
        except (
            aiosqlite.Error,
            ExecutionBindingError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ThreadPersistenceError(f"RUN_EXECUTION_BINDING_READ_FAILED: {exc}") from exc

    async def _append_transcript_in_transaction(
        self,
        command: TranscriptAppend,
        *,
        project_fingerprint: str | None = None,
        allow_legacy_invalid: bool = False,
    ) -> TranscriptRecord:
        """在调用方已持有 IMMEDIATE 事务时追加或校验一条记录。"""
        project = project_fingerprint or self._project_fingerprint
        if not command.thread_id or not command.record_id:
            raise ThreadPersistenceError("TRANSCRIPT_RECORD_ID_INVALID")
        if command.kind not in _TRANSCRIPT_KINDS:
            raise ThreadPersistenceError("TRANSCRIPT_KIND_INVALID")
        cursor = await self._connection.execute(
            """
            SELECT record_id, thread_id, run_id, execution_id, sequence, kind,
                   payload, content_sha256, byte_length, artifact_id, created_at_ms
            FROM harness_thread_transcript
            WHERE project_fingerprint = ? AND thread_id = ? AND record_id = ?
            """,
            (project, command.thread_id, command.record_id),
        )
        existing = await cursor.fetchone()
        await cursor.close()
        if existing is not None:
            record = _transcript_record(existing)
            if _transcript_matches(
                record,
                command,
                project_fingerprint=project,
                allow_legacy_invalid=allow_legacy_invalid,
            ):
                artifact_id = record.artifact_id
                if artifact_id is not None:
                    await self._ensure_transcript_artifact_exists(
                        command.thread_id, artifact_id, project_fingerprint=project
                    )
                return record
            raise ThreadPersistenceError("TRANSCRIPT_RECORD_CONFLICT")

        cursor = await self._connection.execute(
            """
            SELECT 1 FROM harness_threads
            WHERE project_fingerprint = ? AND thread_id = ?
            """,
            (project, command.thread_id),
        )
        thread_exists = await cursor.fetchone()
        await cursor.close()
        if thread_exists is None:
            raise ThreadPersistenceError("THREAD_NOT_FOUND")

        content_bytes = command.content.encode("utf-8")
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        artifact_id: str | None = None
        if command.kind == "tool" and len(content_bytes) > _MAX_INLINE_TOOL_BYTES:
            artifact_id = _transcript_artifact_id(
                project,
                command.thread_id,
                command.record_id,
            )
            await self._insert_transcript_artifact_in_transaction(
                thread_id=command.thread_id,
                artifact_id=artifact_id,
                kind="transcript-tool",
                content=command.content,
                source_start=0,
                source_end=len(content_bytes),
                content_sha256=content_sha256,
                byte_length=len(content_bytes),
                project_fingerprint=project,
            )

        payload: dict[str, object] = {
            "content": _preview(command.content) if artifact_id else command.content,
            "content_sha256": content_sha256,
            "original_bytes": len(content_bytes),
        }
        if command.kind == "assistant":
            normalized_tool_calls = _normalize_transcript_tool_calls(
                command.tool_calls, allow_legacy_invalid=allow_legacy_invalid
            )
            if normalized_tool_calls:
                payload["tool_calls"] = normalized_tool_calls
        if command.kind == "tool":
            payload["tool_call_id"] = command.tool_call_id or command.record_id
            if "name" not in command.legacy_invalid_fields:
                payload["name"] = command.tool_name or "tool"
            if "status" not in command.legacy_invalid_fields:
                payload["status"] = command.tool_status or "success"
            if command.tool_call_id_status is not None:
                payload["tool_call_id_status"] = command.tool_call_id_status
            if command.legacy_invalid_fields:
                payload["legacy_invalid_fields"] = list(command.legacy_invalid_fields)
        if artifact_id is not None:
            payload["artifact_id"] = artifact_id

        encoded_payload = canonical_json(payload)
        cursor = await self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM harness_thread_transcript
            WHERE project_fingerprint = ? AND thread_id = ?
            """,
            (project, command.thread_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        sequence = int(row["next_sequence"] if row is not None else 1)
        created_at_ms = _now_ms()
        try:
            await self._connection.execute(
                """
                INSERT INTO harness_thread_transcript (
                    project_fingerprint, thread_id, record_id, run_id, execution_id,
                    sequence, kind, payload, content_sha256, byte_length, artifact_id,
                    created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project,
                    command.thread_id,
                    command.record_id,
                    command.run_id,
                    command.execution_id,
                    sequence,
                    command.kind,
                    encoded_payload,
                    content_sha256,
                    len(content_bytes),
                    artifact_id,
                    created_at_ms,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ThreadPersistenceError("TRANSCRIPT_SEQUENCE_CONFLICT") from exc
        return TranscriptRecord(
            record_id=command.record_id,
            thread_id=command.thread_id,
            run_id=command.run_id,
            execution_id=command.execution_id,
            sequence=sequence,
            kind=command.kind,
            payload=payload,
            content_sha256=content_sha256,
            byte_length=len(content_bytes),
            artifact_id=artifact_id,
            created_at_ms=created_at_ms,
        )

    async def _insert_transcript_artifact_in_transaction(
        self,
        *,
        thread_id: str,
        artifact_id: str,
        kind: str,
        content: str,
        source_start: int,
        source_end: int,
        content_sha256: str,
        byte_length: int,
        project_fingerprint: str | None = None,
    ) -> None:
        """在 Transcript 事务内幂等保存大型工具原文。"""
        project = project_fingerprint or self._project_fingerprint
        cursor = await self._connection.execute(
            """
            SELECT content, content_sha256, byte_length
            FROM harness_context_artifacts
            WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
            """,
            (project, thread_id, artifact_id),
        )
        existing = await cursor.fetchone()
        await cursor.close()
        if existing is not None:
            if (
                str(existing["content"]) != content
                or str(existing["content_sha256"]) not in {"", content_sha256}
                or int(existing["byte_length"]) not in {0, byte_length}
            ):
                raise ThreadPersistenceError("TRANSCRIPT_ARTIFACT_CONFLICT")
            if str(existing["content_sha256"]) == "" or int(existing["byte_length"]) == 0:
                await self._connection.execute(
                    """
                    UPDATE harness_context_artifacts
                    SET content_sha256 = ?, byte_length = ?
                    WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
                    """,
                    (
                        content_sha256,
                        byte_length,
                        project,
                        thread_id,
                        artifact_id,
                    ),
                )
            return
        await self._connection.execute(
            """
            INSERT INTO harness_context_artifacts (
                project_fingerprint, thread_id, artifact_id, kind, content,
                source_start, source_end, created_at_ms, content_sha256, byte_length
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project,
                thread_id,
                artifact_id,
                kind,
                content,
                source_start,
                source_end,
                _now_ms(),
                content_sha256,
                byte_length,
            ),
        )

    async def _ensure_transcript_artifact_exists(
        self,
        thread_id: str,
        artifact_id: str,
        *,
        project_fingerprint: str | None = None,
    ) -> None:
        """校验幂等重试引用的 Artifact 仍属于当前 project/thread。"""
        project = project_fingerprint or self._project_fingerprint
        cursor = await self._connection.execute(
            """
            SELECT 1 FROM harness_context_artifacts
            WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
            """,
            (project, thread_id, artifact_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise ThreadPersistenceError("TRANSCRIPT_ARTIFACT_MISSING")

    async def _refresh_thread_index_in_transaction(
        self,
        thread_id: str,
        updated_at_ms: int,
        *,
        project_fingerprint: str | None = None,
    ) -> None:
        """用 Transcript 重新计算索引摘要，不读取 LangGraph checkpoint。"""
        project = project_fingerprint or self._project_fingerprint
        cursor = await self._connection.execute(
            """
            SELECT COUNT(*) AS message_count
            FROM harness_thread_transcript
            WHERE project_fingerprint = ? AND thread_id = ? AND kind != 'context'
            """,
            (project, thread_id),
        )
        count_row = await cursor.fetchone()
        await cursor.close()
        cursor = await self._connection.execute(
            """
            SELECT payload FROM harness_thread_transcript
            WHERE project_fingerprint = ? AND thread_id = ? AND kind != 'context'
            ORDER BY sequence DESC LIMIT 1
            """,
            (project, thread_id),
        )
        latest_row = await cursor.fetchone()
        await cursor.close()
        latest = _payload_content(latest_row["payload"]) if latest_row is not None else None
        await self._connection.execute(
            """
            UPDATE harness_threads
            SET updated_at_ms = ?, message_count = ?,
                latest_message = COALESCE(?, latest_message)
            WHERE project_fingerprint = ? AND thread_id = ?
            """,
            (
                updated_at_ms,
                int(count_row["message_count"] if count_row is not None else 0),
                _preview(latest) if latest else None,
                project,
                thread_id,
            ),
        )

    async def load_run_state(self, thread_id: str) -> PersistedBindingState:
        """读取 Run 恢复所需的 typed 状态，不向调用方暴露表结构。"""
        return PersistedBindingState(
            latest_run=await self._get_latest_run_execution_binding(thread_id),
            legacy_models=await self._get_legacy_model_bindings(thread_id),
            has_legacy_runtime=await self._has_legacy_runtime_binding(thread_id),
        )

    async def complete_run(self, thread_id: str) -> None:
        """在 Run 终态刷新活动时间；消息数量由 Transcript 追加事务维护。"""
        self._ensure_open()
        try:
            async with self._lock:
                await self._connection.execute(
                    """
                    UPDATE harness_threads
                    SET updated_at_ms = ?
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (_now_ms(), self._project_fingerprint, thread_id),
                )
                await self._connection.commit()
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_INDEX_REFRESH_FAILED: {exc}") from exc

    async def load_context(self, thread_id: str) -> ContextSnapshot:
        """读取 LangGraph 缓存和 Context 状态，仅供诊断与兼容测试。"""
        self._ensure_open()
        try:
            messages = await self._messages_for_thread(thread_id)
            return ContextSnapshot(
                messages=tuple(messages or ()),
                state=await self._load_context_state(thread_id),
                recoverable=messages is not None,
            )
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CONTEXT_MESSAGES_READ_FAILED: {exc}") from exc

    async def load_context_state(self, thread_id: str) -> ContextState:
        """读取压缩策略状态，不触及 LangGraph messages 缓存。"""
        return await self._load_context_state(thread_id)

    async def load_prompt_epoch(self, thread_id: str) -> PromptEpoch | None:
        """读取旧库 PromptEpoch；生产 Run 不再以它作为上下文来源。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT thread_id, prompt_version, system_prompt, environment_snapshot,
                           readonly_memory, skill_index, tool_schema_fingerprint,
                           system_fingerprint, history_rewrite_version, prefix_change_reason,
                           created_at_ms
                    FROM harness_prompt_epochs
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            return PromptEpoch.from_record(dict(row)) if row is not None else None
        except (aiosqlite.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError(f"PROMPT_EPOCH_READ_FAILED: {exc}") from exc

    async def persist_prompt_epoch(self, epoch: PromptEpoch) -> None:
        """保留旧嵌入式调用的兼容写入口；AgentHost 生产路径不会调用它。"""
        self._ensure_open()
        record = epoch.record()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT system_fingerprint FROM harness_prompt_epochs
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, epoch.thread_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    if str(existing[0]) != epoch.system_fingerprint:
                        raise ThreadPersistenceError("PROMPT_EPOCH_IMMUTABLE")
                    return
                await self._connection.execute(
                    """
                    INSERT INTO harness_prompt_epochs (
                        project_fingerprint, thread_id, prompt_version, system_prompt,
                        environment_snapshot, readonly_memory, skill_index,
                        tool_schema_fingerprint, system_fingerprint,
                        history_rewrite_version, prefix_change_reason, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._project_fingerprint,
                        record["thread_id"],
                        record["prompt_version"],
                        record["system_prompt"],
                        record["environment_snapshot"],
                        record["readonly_memory"],
                        record["skill_index"],
                        record["tool_schema_fingerprint"],
                        record["system_fingerprint"],
                        record["history_rewrite_version"],
                        record["prefix_change_reason"],
                        record["created_at_ms"],
                    ),
                )
                await self._connection.commit()
        except ThreadPersistenceError:
            raise
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"PROMPT_EPOCH_WRITE_FAILED: {exc}") from exc

    async def persist_agent_engine_profile(self, profile: AgentEngineProfile) -> None:
        """保存按 project/profile key 去重的 AgentEngine Profile，不绑定 Thread。"""
        self._ensure_open()
        if profile.is_legacy:
            raise ThreadPersistenceError("RUNTIME_PROFILE_LEGACY_READ_ONLY")
        if profile.project_fingerprint != self._project_fingerprint:
            raise ThreadPersistenceError("RUNTIME_PROFILE_PROJECT_MISMATCH")
        record = profile.record()
        encoded_record = canonical_json(record)
        try:
            async with self._lock:
                await self._connection.execute(
                    """
                    INSERT INTO harness_runtime_profiles (
                        project_fingerprint, profile_key, profile_version,
                        topology_id, topology_version, profile_record, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_fingerprint, profile_key) DO NOTHING
                    """,
                    (
                        self._project_fingerprint,
                        profile.profile_key,
                        AGENT_ENGINE_PROFILE_VERSION,
                        profile.topology_id,
                        profile.topology_version,
                        encoded_record,
                        _now_ms(),
                    ),
                )
                await self._connection.commit()
        except ThreadPersistenceError:
            raise
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"RUNTIME_PROFILE_WRITE_FAILED: {exc}") from exc

    async def _get_legacy_model_bindings(
        self, thread_id: str
    ) -> LegacyModelBindings | None:
        """读取并校验 v5 legacy Thread 模型快照。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT binding_record FROM harness_thread_model_bindings
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                return None
            record = strict_json_loads(str(row["binding_record"]))
            if not isinstance(record, Mapping):
                raise ThreadPersistenceError("THREAD_MODEL_BINDING_INVALID")
            return LegacyModelBindings.from_record(record)
        except ThreadPersistenceError:
            raise
        except (
            aiosqlite.Error,
            ExecutionBindingError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ThreadPersistenceError(f"THREAD_MODEL_BINDING_READ_FAILED: {exc}") from exc

    async def _has_legacy_runtime_binding(self, thread_id: str) -> bool:
        """判断 v4/v5 Thread 是否只有不可逆 AgentEngine 指纹。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT 1 FROM harness_thread_runtime_profiles
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            return row is not None
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"RUNTIME_PROFILE_READ_FAILED: {exc}") from exc

    async def load_latest_valid_compression_checkpoint(
        self,
        thread_id: str,
        *,
        max_source_sequence: int | None = None,
        include_legacy_incomplete: bool = False,
    ) -> CompressionCheckpoint | None:
        """按来源边界和创建顺序选择 latest-valid 检查点。

        损坏 JSON、未知版本、错误 digest、非原子工具组或越界/缺失
        Artifact 只会使当前候选失效；较早的有效版本仍可恢复。
        """
        self._ensure_open()
        records = await self.load_transcript(thread_id)
        upper_bound = (
            max_source_sequence
            if max_source_sequence is not None
            else (records[-1].sequence if records else 0)
        )
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT checkpoint_id, thread_id, source_record_sequence,
                           source_digest, mode, rewrite_version, projected_messages,
                           artifact_ids, trigger, pressure_before, pressure_after,
                           created_at_ms, legacy_incomplete
                    FROM harness_compression_checkpoints
                    WHERE project_fingerprint = ? AND thread_id = ?
                      AND source_record_sequence <= ?
                    ORDER BY source_record_sequence DESC, created_at_ms DESC,
                             checkpoint_id DESC
                    """,
                    (self._project_fingerprint, thread_id, upper_bound),
                )
                rows = await cursor.fetchall()
                await cursor.close()
                for row in rows:
                    try:
                        checkpoint = await self._checkpoint_from_row_in_transaction(
                            row, records
                        )
                    except (
                        ContextProjectionError,
                        ThreadPersistenceError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        continue
                    if checkpoint.legacy_incomplete and not include_legacy_incomplete:
                        continue
                    return checkpoint
            return None
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(
                f"COMPRESSION_CHECKPOINT_READ_FAILED: {exc}"
            ) from exc

    async def _checkpoint_from_row_in_transaction(
        self,
        row: Mapping[str, Any],
        records: tuple[TranscriptRecord, ...],
    ) -> CompressionCheckpoint:
        """在同一 project 连接上校验一个候选检查点。"""
        sequence = int(row["source_record_sequence"])
        if str(row["source_digest"]) != source_digest(records, sequence):
            raise ContextProjectionError("PROJECTION_SOURCE_DIGEST_MISMATCH")
        rewrite_version = str(row["rewrite_version"])
        legacy_incomplete = bool(row["legacy_incomplete"])
        if rewrite_version != HISTORY_REWRITE_VERSION and not (
            legacy_incomplete and rewrite_version in SUPPORTED_REWRITE_VERSIONS
        ):
            raise ContextProjectionError("PROJECTION_REWRITE_VERSION_UNSUPPORTED")
        mode = str(row["mode"])
        if mode not in {"micro", "full"}:
            raise ContextProjectionError("PROJECTION_MODE_INVALID")
        messages = decode_projected_messages(str(row["projected_messages"]))
        artifact_ids_value = strict_json_loads(str(row["artifact_ids"]))
        before = strict_json_loads(str(row["pressure_before"]))
        after = strict_json_loads(str(row["pressure_after"]))
        if (
            not isinstance(artifact_ids_value, list)
            or not all(isinstance(value, str) and value for value in artifact_ids_value)
            or len(set(artifact_ids_value)) != len(artifact_ids_value)
            or not isinstance(before, Mapping)
            or not isinstance(after, Mapping)
        ):
            raise ContextProjectionError("PROJECTION_CHECKPOINT_JSON_INVALID")
        try:
            _strict_json(artifact_ids_value)
            _strict_json(dict(before))
            _strict_json(dict(after))
        except (TypeError, ValueError) as exc:
            raise ContextProjectionError(
                "PROJECTION_CHECKPOINT_JSON_INVALID"
            ) from exc
        artifact_ids = tuple(artifact_ids_value)
        if not set(artifact_references(messages)).issubset(set(artifact_ids)):
            raise ContextProjectionError("PROJECTION_CHECKPOINT_ARTIFACT_UNDECLARED")
        for artifact_id in artifact_ids:
            cursor = await self._connection.execute(
                """
                SELECT content, content_sha256, byte_length
                FROM harness_context_artifacts
                WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
                """,
                (self._project_fingerprint, str(row["thread_id"]), artifact_id),
            )
            artifact = await cursor.fetchone()
            await cursor.close()
            if artifact is None:
                raise ContextProjectionError("PROJECTION_CHECKPOINT_ARTIFACT_MISSING")
            content = str(artifact["content"])
            if (
                str(artifact["content_sha256"]) != _content_sha256(content)
                or int(artifact["byte_length"]) != len(content.encode("utf-8"))
            ):
                raise ContextProjectionError("PROJECTION_CHECKPOINT_ARTIFACT_INVALID")
        return CompressionCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            thread_id=str(row["thread_id"]),
            source_record_sequence=sequence,
            source_digest=str(row["source_digest"]),
            mode=mode,  # type: ignore[arg-type]
            rewrite_version=rewrite_version,
            projected_messages=messages,
            artifact_ids=artifact_ids,
            trigger=str(row["trigger"]),
            pressure_before=dict(before),
            pressure_after=dict(after),
            created_at_ms=int(row["created_at_ms"]),
            legacy_incomplete=legacy_incomplete,
        )

    async def commit_context(self, command: CommitContextRewrite) -> ContextCommit:
        """原子提交 Artifact、摘要、状态和模型投影检查点。"""
        self._ensure_open()
        checkpoint_draft = command.checkpoint
        artifacts: list[ContextArtifact] = []
        for index, draft in enumerate(command.artifacts):
            if not draft.content:
                raise ThreadPersistenceError("CONTEXT_ARTIFACT_EMPTY")
            if not draft.kind or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in draft.kind
            ):
                raise ThreadPersistenceError("CONTEXT_ARTIFACT_KIND_INVALID")
            source_start = max(0, draft.source_start)
            artifact_id = draft.artifact_id or (
                _rewrite_artifact_id(
                    self._project_fingerprint,
                    command.thread_id,
                    checkpoint_draft.checkpoint_id,
                    index,
                    draft,
                )
                if checkpoint_draft is not None
                else f"{draft.kind}-{uuid.uuid4().hex}"
            )
            if not artifact_id or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in artifact_id
            ):
                raise ThreadPersistenceError("CONTEXT_ARTIFACT_ID_INVALID")
            artifacts.append(
                ContextArtifact(
                    artifact_id=artifact_id,
                    kind=draft.kind,
                    content=draft.content,
                    source_start=source_start,
                    source_end=max(source_start, draft.source_end),
                    created_at_ms=_now_ms(),
                    content_sha256=_content_sha256(draft.content),
                    byte_length=len(draft.content.encode("utf-8")),
                )
            )

        checkpoint_messages = (
            encode_projected_messages(checkpoint_draft.projected_messages)
            if checkpoint_draft is not None
            else None
        )
        if checkpoint_draft is not None:
            if (
                not checkpoint_draft.checkpoint_id
                or len(checkpoint_draft.checkpoint_id) > 160
                or any(
                    char
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                    for char in checkpoint_draft.checkpoint_id
                )
                or checkpoint_draft.mode not in {"micro", "full"}
                or (
                    checkpoint_draft.rewrite_version != HISTORY_REWRITE_VERSION
                    and not (
                        checkpoint_draft.legacy_incomplete
                        and checkpoint_draft.rewrite_version
                        in SUPPORTED_REWRITE_VERSIONS
                    )
                )
            ):
                raise ThreadPersistenceError("COMPRESSION_CHECKPOINT_INVALID")
            # 诊断只允许严格 JSON，禁止 NaN/Infinity 和隐式字符串化。
            _strict_json(dict(checkpoint_draft.pressure_before))
            _strict_json(dict(checkpoint_draft.pressure_after))
            if not set(
                artifact_references(checkpoint_draft.projected_messages)
            ).issubset(set(checkpoint_draft.artifact_ids)):
                raise ThreadPersistenceError(
                    "COMPRESSION_CHECKPOINT_ARTIFACT_UNDECLARED"
                )

        summary: ContextSummary | None = None
        if command.summary is not None:
            draft = command.summary
            if any(index < 0 or index >= len(artifacts) for index in draft.artifact_indexes):
                raise ThreadPersistenceError("CONTEXT_SUMMARY_ARTIFACT_INDEX_INVALID")
            artifact_ids = tuple(artifacts[index].artifact_id for index in draft.artifact_indexes)
            source_start = max(0, draft.source_start)
            summary = ContextSummary(
                rewrite_version=draft.rewrite_version,
                content=draft.content,
                source_start=source_start,
                source_end=max(source_start, draft.source_end),
                artifact_ids=artifact_ids,
                created_at_ms=_now_ms(),
            )

        checkpoint: CompressionCheckpoint | None = None
        try:
            async with self._lock:
                await self._connection.execute("BEGIN IMMEDIATE")
                existing_checkpoint: aiosqlite.Row | None = None
                if checkpoint_draft is not None:
                    cursor = await self._connection.execute(
                        """
                        SELECT 1 FROM harness_threads
                        WHERE project_fingerprint = ? AND thread_id = ?
                        """,
                        (self._project_fingerprint, command.thread_id),
                    )
                    thread_exists = await cursor.fetchone()
                    await cursor.close()
                    if thread_exists is None:
                        raise ThreadPersistenceError("THREAD_NOT_FOUND")
                    cursor = await self._connection.execute(
                        """
                        SELECT checkpoint_id, thread_id, source_record_sequence,
                               source_digest, mode, rewrite_version, projected_messages,
                               artifact_ids, trigger, pressure_before, pressure_after,
                               created_at_ms, legacy_incomplete, commit_payload
                        FROM harness_compression_checkpoints
                        WHERE project_fingerprint = ? AND thread_id = ? AND checkpoint_id = ?
                        """,
                        (
                            self._project_fingerprint,
                            command.thread_id,
                            checkpoint_draft.checkpoint_id,
                        ),
                    )
                    existing_checkpoint = await cursor.fetchone()
                    await cursor.close()
                if existing_checkpoint is not None:
                    records = await self._load_transcript_in_transaction(command.thread_id)
                    latest_sequence = records[-1].sequence if records else 0
                    requested_sequence = (
                        latest_sequence
                        if checkpoint_draft.source_record_sequence is None
                        else checkpoint_draft.source_record_sequence
                    )
                    if requested_sequence < 0 or requested_sequence > latest_sequence:
                        raise ThreadPersistenceError(
                            "COMPRESSION_CHECKPOINT_SOURCE_INVALID"
                        )
                    requested_digest = source_digest(records, requested_sequence)
                    requested_commit_payload = _context_commit_payload(
                        command.thread_id,
                        artifacts,
                        summary,
                        command.state,
                        checkpoint_draft,
                        checkpoint_messages or "",
                        requested_sequence,
                        requested_digest,
                    )
                    existing = await self._checkpoint_from_row_in_transaction(
                        existing_checkpoint, records
                    )
                    if (
                        existing_checkpoint["commit_payload"] is None
                        or str(existing_checkpoint["commit_payload"])
                        != requested_commit_payload
                        or existing.source_record_sequence != requested_sequence
                        or existing.source_digest != requested_digest
                        or existing.mode != checkpoint_draft.mode
                        or existing.rewrite_version != checkpoint_draft.rewrite_version
                        or encode_projected_messages(existing.projected_messages)
                        != checkpoint_messages
                        or existing.artifact_ids != checkpoint_draft.artifact_ids
                        or existing.trigger != checkpoint_draft.trigger
                        or dict(existing.pressure_before)
                        != dict(checkpoint_draft.pressure_before)
                        or dict(existing.pressure_after)
                        != dict(checkpoint_draft.pressure_after)
                        or existing.legacy_incomplete
                        != checkpoint_draft.legacy_incomplete
                    ):
                        raise ThreadPersistenceError(
                            "COMPRESSION_CHECKPOINT_CONFLICT"
                        )
                    original = await self._context_commit_result_in_transaction(
                        command.thread_id, artifacts, summary, command.state, existing
                    )
                    await self._connection.rollback()
                    return original
                for artifact in artifacts:
                    cursor = await self._connection.execute(
                        """
                        SELECT kind, content, source_start, source_end,
                               content_sha256, byte_length
                        FROM harness_context_artifacts
                        WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
                        """,
                        (
                            self._project_fingerprint,
                            command.thread_id,
                            artifact.artifact_id,
                        ),
                    )
                    existing_artifact = await cursor.fetchone()
                    await cursor.close()
                    if existing_artifact is not None:
                        if (
                            str(existing_artifact["kind"]) != artifact.kind
                            or str(existing_artifact["content"]) != artifact.content
                            or int(existing_artifact["source_start"])
                            != artifact.source_start
                            or int(existing_artifact["source_end"]) != artifact.source_end
                            or str(existing_artifact["content_sha256"])
                            != artifact.content_sha256
                            or int(existing_artifact["byte_length"])
                            != artifact.byte_length
                        ):
                            raise ThreadPersistenceError("CONTEXT_ARTIFACT_CONFLICT")
                        continue
                    await self._connection.execute(
                        """
                        INSERT INTO harness_context_artifacts (
                            project_fingerprint, thread_id, artifact_id, kind, content,
                            source_start, source_end, created_at_ms, content_sha256, byte_length
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._project_fingerprint,
                            command.thread_id,
                            artifact.artifact_id,
                            artifact.kind,
                            artifact.content,
                            artifact.source_start,
                            artifact.source_end,
                            artifact.created_at_ms,
                            _content_sha256(artifact.content),
                            len(artifact.content.encode("utf-8")),
                        ),
                    )
                if summary is not None:
                    await self._connection.execute(
                        """
                        INSERT INTO harness_context_summaries (
                            project_fingerprint, thread_id, rewrite_version, content,
                            source_start, source_end, artifact_ids, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._project_fingerprint,
                            command.thread_id,
                            summary.rewrite_version,
                            summary.content,
                            summary.source_start,
                            summary.source_end,
                            canonical_json(summary.artifact_ids),
                            summary.created_at_ms,
                        ),
                    )
                if command.state is not None:
                    await self._connection.execute(
                        """
                        INSERT INTO harness_context_state (
                            project_fingerprint, thread_id, failures, circuit_open,
                            last_action, updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project_fingerprint, thread_id) DO UPDATE SET
                            failures = excluded.failures,
                            circuit_open = excluded.circuit_open,
                            last_action = excluded.last_action,
                            updated_at_ms = excluded.updated_at_ms
                        """,
                        (
                            self._project_fingerprint,
                            command.thread_id,
                            command.state.failures,
                            int(command.state.circuit_open),
                            command.state.last_action,
                            _now_ms(),
                        ),
                    )
                if checkpoint_draft is not None:
                    records = await self._load_transcript_in_transaction(command.thread_id)
                    latest_sequence = records[-1].sequence if records else 0
                    sequence = (
                        latest_sequence
                        if checkpoint_draft.source_record_sequence is None
                        else checkpoint_draft.source_record_sequence
                    )
                    if sequence < 0 or sequence > latest_sequence:
                        raise ThreadPersistenceError(
                            "COMPRESSION_CHECKPOINT_SOURCE_INVALID"
                        )
                    linked_artifacts = set(checkpoint_draft.artifact_ids)
                    existing_artifact_ids = await self._artifact_ids_in_transaction(
                        command.thread_id
                    )
                    if not linked_artifacts.issubset(existing_artifact_ids):
                        raise ThreadPersistenceError(
                            "COMPRESSION_CHECKPOINT_ARTIFACT_MISSING"
                        )
                    created_at_ms = _now_ms()
                    digest = source_digest(records, sequence)
                    commit_payload = _context_commit_payload(
                        command.thread_id,
                        artifacts,
                        summary,
                        command.state,
                        checkpoint_draft,
                        checkpoint_messages or "",
                        sequence,
                        digest,
                    )
                    await self._connection.execute(
                        """
                        INSERT INTO harness_compression_checkpoints (
                            project_fingerprint, thread_id, checkpoint_id,
                            source_record_sequence, source_digest, mode,
                            rewrite_version, projected_messages, artifact_ids,
                            trigger, pressure_before, pressure_after, created_at_ms,
                            legacy_incomplete, commit_payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._project_fingerprint,
                            command.thread_id,
                            checkpoint_draft.checkpoint_id,
                            sequence,
                            digest,
                            checkpoint_draft.mode,
                            checkpoint_draft.rewrite_version,
                            checkpoint_messages,
                            _strict_json(checkpoint_draft.artifact_ids),
                            checkpoint_draft.trigger,
                            _strict_json(dict(checkpoint_draft.pressure_before)),
                            _strict_json(dict(checkpoint_draft.pressure_after)),
                            created_at_ms,
                            int(checkpoint_draft.legacy_incomplete),
                            commit_payload,
                        ),
                    )
                    checkpoint = CompressionCheckpoint(
                        checkpoint_id=checkpoint_draft.checkpoint_id,
                        thread_id=command.thread_id,
                        source_record_sequence=sequence,
                        source_digest=digest,
                        mode=checkpoint_draft.mode,
                        rewrite_version=checkpoint_draft.rewrite_version,
                        projected_messages=checkpoint_draft.projected_messages,
                        artifact_ids=checkpoint_draft.artifact_ids,
                        trigger=checkpoint_draft.trigger,
                        pressure_before=dict(checkpoint_draft.pressure_before),
                        pressure_after=dict(checkpoint_draft.pressure_after),
                        created_at_ms=created_at_ms,
                        legacy_incomplete=checkpoint_draft.legacy_incomplete,
                    )
                await self._connection.commit()
        except BaseException as exc:
            try:
                await self._connection.rollback()
            except aiosqlite.Error:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, ThreadPersistenceError):
                raise
            if isinstance(exc, ContextProjectionError):
                raise ThreadPersistenceError(
                    f"COMPRESSION_CHECKPOINT_INVALID: {exc}"
                ) from exc
            if isinstance(exc, aiosqlite.Error):
                raise ThreadPersistenceError(
                    f"CONTEXT_REWRITE_WRITE_FAILED: {exc}"
                ) from exc
            raise
        return ContextCommit(tuple(artifacts), summary, command.state, checkpoint)

    async def _context_commit_result_in_transaction(
        self,
        thread_id: str,
        artifacts: list[ContextArtifact],
        summary: ContextSummary | None,
        state: ContextState | None,
        checkpoint: CompressionCheckpoint,
    ) -> ContextCommit:
        """按已验证的稳定命令恢复首次提交结果，而不是返回空的快速成功。"""
        original_artifacts: list[ContextArtifact] = []
        for artifact in artifacts:
            cursor = await self._connection.execute(
                """
                SELECT artifact_id, kind, content, source_start, source_end,
                       created_at_ms, content_sha256, byte_length
                FROM harness_context_artifacts
                WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
                """,
                (self._project_fingerprint, thread_id, artifact.artifact_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ThreadPersistenceError("COMPRESSION_CHECKPOINT_CONFLICT")
            original_artifacts.append(_context_artifact_from_row(row))

        original_summary: ContextSummary | None = None
        if summary is not None:
            cursor = await self._connection.execute(
                """
                SELECT rewrite_version, content, source_start, source_end,
                       artifact_ids, created_at_ms
                FROM harness_context_summaries
                WHERE project_fingerprint = ? AND thread_id = ?
                  AND rewrite_version = ? AND content = ?
                  AND source_start = ? AND source_end = ? AND artifact_ids = ?
                ORDER BY summary_id ASC
                LIMIT 1
                """,
                (
                    self._project_fingerprint,
                    thread_id,
                    summary.rewrite_version,
                    summary.content,
                    summary.source_start,
                    summary.source_end,
                    _strict_json(summary.artifact_ids),
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ThreadPersistenceError("COMPRESSION_CHECKPOINT_CONFLICT")
            artifact_ids = strict_json_loads(str(row["artifact_ids"]))
            if not isinstance(artifact_ids, list) or not all(
                isinstance(value, str) for value in artifact_ids
            ):
                raise ThreadPersistenceError("COMPRESSION_CHECKPOINT_CONFLICT")
            original_summary = ContextSummary(
                rewrite_version=str(row["rewrite_version"]),
                content=str(row["content"]),
                source_start=int(row["source_start"]),
                source_end=int(row["source_end"]),
                artifact_ids=tuple(artifact_ids),
                created_at_ms=int(row["created_at_ms"]),
            )
        return ContextCommit(
            tuple(original_artifacts), original_summary, state, checkpoint
        )

    async def _load_transcript_in_transaction(
        self, thread_id: str
    ) -> tuple[TranscriptRecord, ...]:
        """在调用方已持有锁和事务时读取 Transcript 前缀。"""
        cursor = await self._connection.execute(
            """
            SELECT record_id, thread_id, run_id, execution_id, sequence,
                   kind, payload, content_sha256, byte_length, artifact_id,
                   created_at_ms
            FROM harness_thread_transcript
            WHERE project_fingerprint = ? AND thread_id = ?
            ORDER BY sequence ASC
            """,
            (self._project_fingerprint, thread_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple(_transcript_record(row) for row in rows)

    async def _artifact_ids_in_transaction(self, thread_id: str) -> set[str]:
        """返回当前 project/thread 已存在的 Artifact ID。"""
        cursor = await self._connection.execute(
            """
            SELECT artifact_id FROM harness_context_artifacts
            WHERE project_fingerprint = ? AND thread_id = ?
            """,
            (self._project_fingerprint, thread_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["artifact_id"]) for row in rows}

    async def load_context_artifact(self, thread_id: str, artifact_id: str) -> ContextArtifact | None:
        """读取仅归属当前 project/thread 的归档，调用方不得从数据库路径推断真实位置。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT artifact_id, kind, content, source_start, source_end, created_at_ms
                           , content_sha256, byte_length
                    FROM harness_context_artifacts
                    WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
                    """,
                    (self._project_fingerprint, thread_id, artifact_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                return None
            return ContextArtifact(
                artifact_id=str(row["artifact_id"]),
                kind=str(row["kind"]),
                content=str(row["content"]),
                source_start=int(row["source_start"]),
                source_end=int(row["source_end"]),
                created_at_ms=int(row["created_at_ms"]),
                content_sha256=str(row["content_sha256"] or ""),
                byte_length=int(row["byte_length"] or 0),
            )
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CONTEXT_ARTIFACT_READ_FAILED: {exc}") from exc

    async def _load_context_state(self, thread_id: str) -> ContextState:
        """返回压缩失败熔断状态；缺失记录按未失败初始化。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT failures, circuit_open, last_action FROM harness_context_state
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                return ContextState()
            return ContextState(int(row["failures"]), bool(row["circuit_open"]), str(row["last_action"]))
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CONTEXT_STATE_READ_FAILED: {exc}") from exc

    async def list_threads(self, limit: int = 80) -> tuple[ThreadSummary, ...]:
        """按最后活动时间返回当前 project 的有限线程摘要。"""
        self._ensure_open()
        if limit < 1 or limit > 200:
            raise ThreadPersistenceError("CHECKPOINT_LIST_INVALID_LIMIT")
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT thread_id, created_at_ms, updated_at_ms, first_message,
                           latest_message, message_count
                    FROM harness_threads
                    WHERE project_fingerprint = ?
                    ORDER BY updated_at_ms DESC, thread_id ASC
                    LIMIT ?
                    """,
                    (self._project_fingerprint, limit),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(_summary(row) for row in rows)
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_LIST_FAILED: {exc}") from exc

    async def open_thread(self, thread_id: str) -> OpenThread:
        """从 Transcript 读取完整 UI 历史；不把 checkpoint 作为 UI fallback。"""
        self._ensure_open()
        try:
            async with self._lock:
                await self._connection.execute("BEGIN")
                try:
                    cursor = await self._connection.execute(
                        """
                        SELECT thread_id, created_at_ms, updated_at_ms, first_message,
                               latest_message, message_count
                        FROM harness_threads
                        WHERE project_fingerprint = ? AND thread_id = ?
                        """,
                        (self._project_fingerprint, thread_id),
                    )
                    summary_row = await cursor.fetchone()
                    await cursor.close()
                    if summary_row is None:
                        await self._connection.commit()
                        raise ThreadPersistenceError("THREAD_NOT_FOUND")

                    cursor = await self._connection.execute(
                        """
                        SELECT record_id, thread_id, run_id, execution_id, sequence,
                               kind, payload, content_sha256, byte_length, artifact_id,
                               created_at_ms
                        FROM harness_thread_transcript
                        WHERE project_fingerprint = ? AND thread_id = ?
                        ORDER BY sequence ASC
                        """,
                        (self._project_fingerprint, thread_id),
                    )
                    transcript_rows = await cursor.fetchall()
                    await cursor.close()

                    cursor = await self._connection.execute(
                        """
                        SELECT legacy_incomplete_history
                        FROM harness_thread_history_metadata
                        WHERE project_fingerprint = ? AND thread_id = ?
                        """,
                        (self._project_fingerprint, thread_id),
                    )
                    metadata_row = await cursor.fetchone()
                    await cursor.close()
                    await self._connection.commit()
                except BaseException:
                    try:
                        await self._connection.rollback()
                    except aiosqlite.Error:
                        pass
                    raise

            records = tuple(_transcript_record(row) for row in transcript_rows)
            history_incomplete = bool(
                metadata_row and metadata_row["legacy_incomplete_history"]
            )
            return OpenThread(
                summary=_summary(summary_row),
                messages=tuple(
                    message
                    for record in records
                    if record.kind != "context"
                    for message in (_thread_message_from_transcript(record),)
                    if message is not None
                ),
                legacy_incomplete_history=history_incomplete,
            )
        except ThreadPersistenceError:
            raise
        except (aiosqlite.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_READ_FAILED: {exc}") from exc

    async def close(self) -> None:
        """提交并关闭连接，确保 CLI 退出后用户可安全删除数据库及 WAL 文件。"""
        if self._closed:
            return
        self._closed = True
        try:
            async with self._lock:
                await self._connection.commit()
                await self._connection.close()
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_CLOSE_FAILED: {exc}") from exc

    async def _prepare(self) -> None:
        """验证 SQLite、备份旧库并原子升级 Harness 自有 schema。"""
        try:
            try:
                cursor = await self._connection.execute("PRAGMA integrity_check")
                row = await cursor.fetchone()
                await cursor.close()
            except aiosqlite.Error as exc:
                raise ThreadPersistenceError(f"CHECKPOINT_DATABASE_CORRUPT: {exc}") from exc
            if not row or row[0] != "ok":
                detail = row[0] if row else "no result"
                raise ThreadPersistenceError(f"CHECKPOINT_DATABASE_CORRUPT: {detail}")
            await self._connection.execute("PRAGMA busy_timeout=5000")
            try:
                await self._connection.execute("PRAGMA journal_mode=WAL")
            except aiosqlite.OperationalError as exc:
                # Two project Hosts may enter bootstrap together.  SQLite's
                # journal-mode switch can reject the stale opener immediately
                # even with busy_timeout; the authoritative BEGIN IMMEDIATE
                # version check below still serializes the migration.  Do not
                # turn this harmless race into a failed open.
                if "locked" not in str(exc).lower():
                    raise
            candidate_version = await self._read_user_version_under_write_lock()
            if candidate_version > _SCHEMA_VERSION:
                raise ThreadPersistenceError(
                    f"CHECKPOINT_SCHEMA_TOO_NEW: found {candidate_version}, supports {_SCHEMA_VERSION}"
                )
            if candidate_version >= _SCHEMA_VERSION:
                # The checkpoint schema was created by the winning opener.  Do
                # not run setup here: AsyncSqliteSaver.setup() commits and would
                # break the migration transaction boundary.
                self._checkpointer.is_setup = True
                return

            # A backup is only a recovery artifact.  It is made outside the
            # migration write transaction because SQLite backup on a connection
            # holding BEGIN IMMEDIATE can block.  The second lock/version read
            # below is authoritative and prevents a stale migrator from doing
            # any work after another opener has committed the successor schema.
            await self._create_migration_backup(candidate_version)
            await self._connection.execute("BEGIN IMMEDIATE")
            cursor = await self._connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            source_version = int(row[0]) if row else 0
            if source_version > _SCHEMA_VERSION:
                raise ThreadPersistenceError(
                    f"CHECKPOINT_SCHEMA_TOO_NEW: found {source_version}, supports {_SCHEMA_VERSION}"
                )
            if source_version >= _SCHEMA_VERSION:
                await self._connection.rollback()
                self._checkpointer.is_setup = True
                return

            await self._create_checkpointer_tables_in_transaction()
            version = source_version
            if version < 1:
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_threads (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        first_message TEXT NOT NULL,
                        latest_message TEXT NOT NULL,
                        message_count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (project_fingerprint, thread_id)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_threads_project_updated
                    ON harness_threads(project_fingerprint, updated_at_ms DESC)
                    """
                )
                version = 1
            if version < 2:
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_prompt_epochs (
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
                        PRIMARY KEY (project_fingerprint, thread_id)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_context_artifacts (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        artifact_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        content TEXT NOT NULL,
                        source_start INTEGER NOT NULL,
                        source_end INTEGER NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, thread_id, artifact_id)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_context_artifacts_thread_created
                    ON harness_context_artifacts(project_fingerprint, thread_id, created_at_ms)
                    """
                )
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_context_summaries (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        rewrite_version TEXT NOT NULL,
                        content TEXT NOT NULL,
                        source_start INTEGER NOT NULL,
                        source_end INTEGER NOT NULL,
                        artifact_ids TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_context_state (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        failures INTEGER NOT NULL DEFAULT 0,
                        circuit_open INTEGER NOT NULL DEFAULT 0,
                        last_action TEXT NOT NULL DEFAULT 'none',
                        updated_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, thread_id)
                    )
                    """
                )
                version = 2
            if version < 3:
                await self._connection.execute(
                    """
                    ALTER TABLE harness_prompt_epochs
                    ADD COLUMN prefix_change_reason TEXT NOT NULL DEFAULT 'new_thread'
                    """
                )
                version = 3
            if version < 4:
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_runtime_profiles (
                        project_fingerprint TEXT NOT NULL,
                        profile_key TEXT NOT NULL,
                        profile_version INTEGER NOT NULL,
                        topology_id TEXT NOT NULL,
                        topology_version INTEGER NOT NULL,
                        profile_record TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, profile_key)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_thread_runtime_profiles (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        profile_key TEXT NOT NULL,
                        profile_version INTEGER NOT NULL,
                        bound_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, thread_id)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_thread_runtime_profiles_project_profile
                    ON harness_thread_runtime_profiles(project_fingerprint, profile_key)
                    """
                )
                version = 4
            if version < 5:
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_thread_model_bindings (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        binding_record TEXT NOT NULL,
                        bound_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, thread_id)
                    )
                    """
                )
                version = 5
            if version < 6:
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_run_execution_bindings (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        requested_selection TEXT NOT NULL,
                        actual_primary_binding TEXT NOT NULL,
                        runtime_profile_id TEXT NOT NULL,
                        message_digest TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, thread_id, run_id)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_run_execution_bindings_thread_created
                    ON harness_run_execution_bindings(project_fingerprint, thread_id, created_at_ms DESC)
                    """
                )
                version = 6
            if version < 7:
                await self._add_artifact_metadata_columns()
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_thread_transcript (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        record_id TEXT NOT NULL,
                        run_id TEXT,
                        execution_id TEXT,
                        sequence INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('user', 'assistant', 'tool', 'context')),
                        payload TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        byte_length INTEGER NOT NULL,
                        artifact_id TEXT,
                        created_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, thread_id, record_id),
                        UNIQUE (project_fingerprint, thread_id, sequence)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_thread_transcript_thread_sequence
                    ON harness_thread_transcript(project_fingerprint, thread_id, sequence)
                    """
                )
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_thread_history_metadata (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        legacy_incomplete_history INTEGER NOT NULL,
                        source_schema_version INTEGER NOT NULL,
                        migrated_at_ms INTEGER NOT NULL,
                        PRIMARY KEY (project_fingerprint, thread_id)
                    )
                    """
                )
                await self._bootstrap_legacy_transcripts(source_version)
                version = 7
            if version < 8:
                await self._add_context_snapshot_column()
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_run_context_snapshots (
                        project_fingerprint TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        snapshot_record TEXT NOT NULL,
                        system_fingerprint TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        legacy INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (project_fingerprint, snapshot_id)
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_run_context_snapshots_thread_created
                    ON harness_run_context_snapshots(project_fingerprint, thread_id, created_at_ms)
                    """
                )
                await self._migrate_legacy_prompt_epochs_to_snapshots()
                version = 8
            if version < 9:
                await self._backfill_artifact_metadata()
                await self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS harness_compression_checkpoints (
                        project_fingerprint TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        source_record_sequence INTEGER NOT NULL,
                        source_digest TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK(mode IN ('micro', 'full')),
                        rewrite_version TEXT NOT NULL,
                        projected_messages TEXT NOT NULL,
                        artifact_ids TEXT NOT NULL,
                        trigger TEXT NOT NULL,
                        pressure_before TEXT NOT NULL,
                        pressure_after TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        legacy_incomplete INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (
                            project_fingerprint, thread_id, checkpoint_id
                        )
                    )
                    """
                )
                await self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS harness_compression_checkpoints_latest
                    ON harness_compression_checkpoints(
                        project_fingerprint, thread_id,
                        source_record_sequence DESC, created_at_ms DESC
                    )
                    """
                )
                await self._bootstrap_legacy_compression_checkpoints()
                version = 9
            if version < 10:
                await self._add_compression_commit_payload_column()
                version = 10
            await self._connection.execute(f"PRAGMA user_version={version}")
            await self._connection.commit()
            self._checkpointer.is_setup = True
        except BaseException as exc:
            try:
                await self._connection.rollback()
            except aiosqlite.Error:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, ThreadPersistenceError):
                raise
            raise ThreadPersistenceError(f"CHECKPOINT_MIGRATION_FAILED: {exc}") from exc

    async def _read_user_version_under_write_lock(self) -> int:
        """短暂取得 SQLite 写锁并读取版本，协调并发 migration candidate。"""
        await self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            await self._connection.rollback()
            return int(row[0]) if row else 0
        except BaseException:
            try:
                await self._connection.rollback()
            except aiosqlite.Error:
                pass
            raise

    async def _migrate_legacy_prompt_epochs_to_snapshots(self) -> None:
        """把旧 PromptEpoch 单向转换为 legacy snapshot，并回填旧 Run 引用。"""
        cursor = await self._connection.execute(
            """
            SELECT project_fingerprint, thread_id, system_prompt, created_at_ms
            FROM harness_prompt_epochs
            ORDER BY project_fingerprint, thread_id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            project = str(row["project_fingerprint"])
            thread_id = str(row["thread_id"])
            snapshot = snapshot_from_legacy_prompt_epoch(
                project_fingerprint=project,
                thread_id=thread_id,
                system_prompt=str(row["system_prompt"]),
                created_at_ms=int(row["created_at_ms"]),
            )
            encoded = canonical_json(snapshot.record())
            await self._connection.execute(
                """
                INSERT INTO harness_run_context_snapshots (
                    project_fingerprint, snapshot_id, thread_id, snapshot_record,
                    system_fingerprint, created_at_ms, legacy
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(project_fingerprint, snapshot_id) DO NOTHING
                """,
                (
                    project,
                    snapshot.snapshot_id,
                    thread_id,
                    encoded,
                    snapshot.system_fingerprint,
                    snapshot.created_at_ms,
                ),
            )
            await self._connection.execute(
                """
                UPDATE harness_run_execution_bindings
                SET context_snapshot_id = ?
                WHERE project_fingerprint = ? AND thread_id = ?
                  AND context_snapshot_id IS NULL
                """,
                (snapshot.snapshot_id, project, thread_id),
            )

    async def _create_checkpointer_tables_in_transaction(self) -> None:
        """在 Harness migration 事务内建立 LangGraph 的基础表，避免 setup 自行 commit。"""
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
            """
        )

    async def _create_migration_backup(self, source_version: int) -> Path | None:
        """在候选版本仍可能升级时创建唯一临时 SQLite 备份。"""
        backup_path = self._path.with_name(
            f"{self._path.name}.pre-v{source_version}-migration.bak"
        )
        temporary = backup_path.with_name(
            f"{backup_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            target = sqlite3.connect(temporary)
            try:
                await self._connection.backup(target)
                target.commit()
                row = target.execute("PRAGMA user_version").fetchone()
                backup_version = int(row[0]) if row else 0
            finally:
                target.close()
            if backup_version != source_version:
                return None
            os.replace(temporary, backup_path)
            os.chmod(backup_path, 0o600)
            return backup_path
        finally:
            temporary.unlink(missing_ok=True)

    async def _add_artifact_metadata_columns(self) -> None:
        """为已有 Context Artifact 补齐可验证的摘要和字节长度列。"""
        cursor = await self._connection.execute("PRAGMA table_info(harness_context_artifacts)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "content_sha256" not in columns:
            await self._connection.execute(
                """
                ALTER TABLE harness_context_artifacts
                ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT ''
                """
            )
        if "byte_length" not in columns:
            await self._connection.execute(
                """
                ALTER TABLE harness_context_artifacts
                ADD COLUMN byte_length INTEGER NOT NULL DEFAULT 0
                """
            )

    async def _backfill_artifact_metadata(self) -> None:
        """为 v7 以前归档补齐可验证元数据，不改动原文。"""
        cursor = await self._connection.execute(
            """
            SELECT project_fingerprint, thread_id, artifact_id, content
            FROM harness_context_artifacts
            WHERE content_sha256 = '' OR byte_length = 0
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            content = str(row["content"])
            await self._connection.execute(
                """
                UPDATE harness_context_artifacts
                SET content_sha256 = ?, byte_length = ?
                WHERE project_fingerprint = ? AND thread_id = ? AND artifact_id = ?
                """,
                (
                    _content_sha256(content),
                    len(content.encode("utf-8")),
                    str(row["project_fingerprint"]),
                    str(row["thread_id"]),
                    str(row["artifact_id"]),
                ),
            )

    async def _bootstrap_legacy_compression_checkpoints(self) -> None:
        """将旧 LangGraph 工作视图诚实记为 legacy/incomplete 起点。"""
        cursor = await self._connection.execute(
            """
            SELECT project_fingerprint, thread_id
            FROM harness_threads
            ORDER BY project_fingerprint, thread_id
            """
        )
        threads = await cursor.fetchall()
        await cursor.close()
        for row in threads:
            project = str(row["project_fingerprint"])
            thread_id = str(row["thread_id"])
            messages = await self._legacy_messages_for_project(project, thread_id)
            if not messages:
                continue
            try:
                encoded_messages = encode_projected_messages(messages)
            except ContextProjectionError:
                # 旧视图若已有孤儿/残缺工具组，禁止把它升格为有效投影。
                continue
            referenced_artifacts = artifact_references(messages)
            if referenced_artifacts:
                placeholders = ",".join("?" for _ in referenced_artifacts)
                cursor = await self._connection.execute(
                    f"""
                    SELECT artifact_id FROM harness_context_artifacts
                    WHERE project_fingerprint = ? AND thread_id = ?
                      AND artifact_id IN ({placeholders})
                    """,
                    (project, thread_id, *referenced_artifacts),
                )
                found_artifacts = {
                    str(item["artifact_id"]) for item in await cursor.fetchall()
                }
                await cursor.close()
                if found_artifacts != set(referenced_artifacts):
                    continue
            cursor = await self._connection.execute(
                """
                SELECT record_id, thread_id, run_id, execution_id, sequence,
                       kind, payload, content_sha256, byte_length, artifact_id,
                       created_at_ms
                FROM harness_thread_transcript
                WHERE project_fingerprint = ? AND thread_id = ?
                ORDER BY sequence ASC
                """,
                (project, thread_id),
            )
            records = tuple(_transcript_record(item) for item in await cursor.fetchall())
            await cursor.close()
            sequence = records[-1].sequence if records else 0
            checkpoint_id = "legacy-" + hashlib.sha256(
                f"{project}:{thread_id}:{sequence}".encode("utf-8")
            ).hexdigest()[:32]
            await self._connection.execute(
                """
                INSERT INTO harness_compression_checkpoints (
                    project_fingerprint, thread_id, checkpoint_id,
                    source_record_sequence, source_digest, mode,
                    rewrite_version, projected_messages, artifact_ids,
                    trigger, pressure_before, pressure_after, created_at_ms,
                    legacy_incomplete
                ) VALUES (?, ?, ?, ?, ?, 'full', 'legacy-incomplete-v1', ?,
                          ?, 'legacy-migration', '{}', '{}', ?, 1)
                ON CONFLICT(project_fingerprint, thread_id, checkpoint_id) DO NOTHING
                """,
                (
                    project,
                    thread_id,
                    checkpoint_id,
                    sequence,
                    source_digest(records, sequence),
                    encoded_messages,
                    _strict_json(referenced_artifacts),
                    _now_ms(),
                ),
            )
    async def _add_compression_commit_payload_column(self) -> None:
        """v10 为整条 rewrite 幂等语义增加列，并兼容降版本测试库。"""
        cursor = await self._connection.execute(
            "PRAGMA table_info(harness_compression_checkpoints)"
        )
        columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "commit_payload" not in columns:
            await self._connection.execute(
                """
                ALTER TABLE harness_compression_checkpoints
                ADD COLUMN commit_payload TEXT
                """
            )

    async def _add_context_snapshot_column(self) -> None:
        """为旧 Run binding 增加可为空的 snapshot 引用，兼容降级库。"""
        cursor = await self._connection.execute(
            "PRAGMA table_info(harness_run_execution_bindings)"
        )
        columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "context_snapshot_id" not in columns:
            await self._connection.execute(
                """
                ALTER TABLE harness_run_execution_bindings
                ADD COLUMN context_snapshot_id TEXT
                """
            )
    async def _bootstrap_legacy_transcripts(self, source_version: int) -> None:
        """从所有 project 的现存 checkpoint 建立明确不完整的 legacy 起点。"""
        cursor = await self._connection.execute(
            """
            SELECT project_fingerprint, thread_id
            FROM harness_threads
            ORDER BY project_fingerprint, thread_id
            """
        )
        threads = [(str(row["project_fingerprint"]), str(row["thread_id"])) for row in await cursor.fetchall()]
        await cursor.close()
        for project_fingerprint, thread_id in threads:
            cursor = await self._connection.execute(
                """
                SELECT 1 FROM harness_thread_history_metadata
                WHERE project_fingerprint = ? AND thread_id = ?
                """,
                (project_fingerprint, thread_id),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None:
                continue
            messages = await self._legacy_messages_for_project(project_fingerprint, thread_id)
            sequence = 0
            pending_tool_call_ids: list[str] = []
            for message in messages or ():
                normalized = _normalize_message(message)
                if normalized is None:
                    continue
                sequence += 1
                kind = normalized.kind
                record_id = _legacy_record_id(project_fingerprint, thread_id, sequence)
                legacy_tool_calls = (
                    _legacy_tool_calls(
                        message,
                        project_fingerprint=project_fingerprint,
                        thread_id=thread_id,
                        sequence=sequence,
                    )
                    if kind == "assistant"
                    else ()
                )
                if legacy_tool_calls:
                    pending_tool_call_ids.extend(
                        call["id"]
                        for call in legacy_tool_calls
                        if not call.get("legacy_invalid_fields")
                        and call.get("arguments_status") == "valid"
                        and isinstance(call.get("id"), str)
                        and call["id"]
                    )
                legacy_tool_call_id: str | None = None
                legacy_tool_call_id_status: str | None = None
                legacy_invalid_fields: list[str] = []
                legacy_tool_name: str | None = None
                legacy_tool_status: str | None = None
                if kind == "tool":
                    raw_tool_call_id = getattr(message, "tool_call_id", None)
                    raw_tool_name = getattr(message, "name", None)
                    raw_tool_status = getattr(message, "status", None)
                    if isinstance(raw_tool_name, str) and raw_tool_name:
                        legacy_tool_name = raw_tool_name
                    else:
                        legacy_invalid_fields.append("name")
                    if isinstance(raw_tool_status, str) and raw_tool_status in {
                        "success",
                        "error",
                    }:
                        legacy_tool_status = raw_tool_status
                    else:
                        legacy_invalid_fields.append("status")
                    if isinstance(raw_tool_call_id, str) and raw_tool_call_id:
                        legacy_tool_call_id = raw_tool_call_id
                        if (
                            not legacy_invalid_fields
                            and raw_tool_call_id in pending_tool_call_ids
                        ):
                            pending_tool_call_ids.remove(raw_tool_call_id)
                        else:
                            legacy_tool_call_id_status = "unmatched"
                    elif (raw_tool_call_id is None or raw_tool_call_id == "") and (
                        not legacy_invalid_fields
                        and len(pending_tool_call_ids) == 1
                    ):
                        # A single unresolved assistant declaration is the
                        # only safe legacy no-ID result binding.  This also
                        # preserves the synthetic ID generated for an
                        # assistant call that had no provider ID.
                        legacy_tool_call_id = pending_tool_call_ids.pop()
                    else:
                        if raw_tool_call_id is not None and raw_tool_call_id != "":
                            legacy_invalid_fields.append("tool_call_id")
                        # Keep the result, but make the missing association
                        # explicit instead of assigning a record ID and
                        # pretending it matched an assistant call.
                        legacy_tool_call_id_status = "unmatched"
                command = TranscriptAppend(
                    thread_id=thread_id,
                    record_id=record_id,
                    kind=kind,
                    content=normalized.content,
                    tool_call_id=legacy_tool_call_id,
                    tool_name=legacy_tool_name,
                    tool_status=legacy_tool_status,
                    tool_call_id_status=legacy_tool_call_id_status,
                    legacy_invalid_fields=tuple(legacy_invalid_fields),
                    tool_calls=legacy_tool_calls,
                )
                await self._append_transcript_in_transaction(
                    command,
                    project_fingerprint=project_fingerprint,
                    allow_legacy_invalid=True,
                )
            await self._connection.execute(
                """
                INSERT INTO harness_thread_history_metadata (
                    project_fingerprint, thread_id, legacy_incomplete_history,
                    source_schema_version, migrated_at_ms
                ) VALUES (?, ?, 1, ?, ?)
                """,
                (
                    project_fingerprint,
                    thread_id,
                    source_version,
                    _now_ms(),
                ),
            )
            await self._refresh_thread_index_in_transaction(
                thread_id, _now_ms(), project_fingerprint=project_fingerprint
            )

    async def _legacy_messages_for_project(
        self, project_fingerprint: str, thread_id: str
    ) -> list[Any] | None:
        """使用 project-scoped saver 读取迁移前 checkpoint，不伪造 Run 身份。"""
        saver = ProjectScopedAsyncSqliteSaver(
            self._connection,
            project_fingerprint,
            operation_lock=self._lock,
        )
        saver.is_setup = True
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": project_fingerprint,
            }
        }
        checkpoint = await saver.aget_tuple(config)
        if checkpoint is None:
            return None
        direct = _checkpoint_messages(checkpoint.checkpoint)
        if direct is not None:
            return direct
        history = await saver.aget_delta_channel_history(
            config=config,
            channels=["messages"],
        )
        return _replay_delta_messages(history)

    async def _legacy_history_incomplete(self, thread_id: str) -> bool:
        """读取 v6 bootstrap 的诚实边界，仅供内部恢复诊断使用。"""
        async with self._lock:
            cursor = await self._connection.execute(
                """
                SELECT legacy_incomplete_history
                FROM harness_thread_history_metadata
                WHERE project_fingerprint = ? AND thread_id = ?
                """,
                (self._project_fingerprint, thread_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return bool(row and row["legacy_incomplete_history"])

    def _ensure_open(self) -> None:
        """阻止关闭后的 handler 继续使用失效连接。"""
        if self._closed:
            raise ThreadPersistenceError("CHECKPOINT_STORE_CLOSED")

    async def _messages_for_thread(self, thread_id: str) -> list[Any] | None:
        """读取普通或 DeltaChannel checkpoint 的完整消息，兼容 DeepAgents 的增量存储。"""
        checkpoint = await self._checkpointer.aget_tuple(self.graph_config(thread_id))
        if checkpoint is None:
            return None
        direct = _checkpoint_messages(checkpoint.checkpoint)
        if direct is not None:
            return direct
        history = await self._checkpointer.aget_delta_channel_history(
            config=self.graph_config(thread_id),
            channels=["messages"],
        )
        return _replay_delta_messages(history)


def _project_fingerprint(project: Path) -> str:
    """从规范化 project 路径生成不可逆 namespace，禁止原始路径进入数据库。"""
    return hashlib.sha256(str(project.expanduser().resolve()).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    """延迟导入时间模块，保持路径和数据转换函数的纯粹性。"""
    import time

    return int(time.time() * 1000)


def _preview(value: str) -> str:
    """将用户消息压缩为单行有限摘要，避免选择器被超长或换行文本破坏。"""
    compact = " ".join(value.split())
    return compact[:_MAX_PREVIEW_CHARS] or "(空消息)"


def _summary(row: Mapping[str, Any]) -> ThreadSummary:
    """将 SQLite 行转换为不携带 project 路径的线程摘要。"""
    return ThreadSummary(
        thread_id=str(row["thread_id"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        first_message=str(row["first_message"]),
        latest_message=str(row["latest_message"]),
        message_count=int(row["message_count"]),
    )


def _content_sha256(content: str) -> str:
    """计算 UTF-8 内容摘要，避免把原文复制进诊断或索引。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _strict_json(value: object) -> str:
    """以严格确定 JSON 保存投影元数据，拒绝 NaN 和隐式转字符串。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _rewrite_artifact_id(
    project_fingerprint: str,
    thread_id: str,
    checkpoint_id: str,
    index: int,
    draft: ContextArtifactDraft,
) -> str:
    """为带 checkpoint 的无 ID Artifact 生成可跨进程重试的稳定 ID。"""
    material = _strict_json(
        {
            "project_fingerprint": project_fingerprint,
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
            "index": index,
            "kind": draft.kind,
            "content": draft.content,
            "source_start": max(0, draft.source_start),
            "source_end": max(max(0, draft.source_start), draft.source_end),
        }
    )
    return f"{draft.kind}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _context_commit_payload(
    thread_id: str,
    artifacts: list[ContextArtifact],
    summary: ContextSummary | None,
    state: ContextState | None,
    checkpoint: CompressionCheckpointDraft,
    projected_messages: str,
    source_record_sequence: int,
    source_digest_value: str,
) -> str:
    """编码整个 CommitContextRewrite 的稳定语义，排除生成时间。"""
    return _strict_json(
        {
            "version": 1,
            "thread_id": thread_id,
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "kind": artifact.kind,
                    "content": artifact.content,
                    "source_start": artifact.source_start,
                    "source_end": artifact.source_end,
                    "content_sha256": artifact.content_sha256,
                    "byte_length": artifact.byte_length,
                }
                for artifact in artifacts
            ],
            "summary": (
                None
                if summary is None
                else {
                    "rewrite_version": summary.rewrite_version,
                    "content": summary.content,
                    "source_start": summary.source_start,
                    "source_end": summary.source_end,
                    "artifact_ids": summary.artifact_ids,
                }
            ),
            "state": (
                None
                if state is None
                else {
                    "failures": state.failures,
                    "circuit_open": state.circuit_open,
                    "last_action": state.last_action,
                }
            ),
            "checkpoint": {
                "checkpoint_id": checkpoint.checkpoint_id,
                "source_record_sequence": source_record_sequence,
                "source_digest": source_digest_value,
                "mode": checkpoint.mode,
                "rewrite_version": checkpoint.rewrite_version,
                "projected_messages": projected_messages,
                "artifact_ids": checkpoint.artifact_ids,
                "trigger": checkpoint.trigger,
                "pressure_before": dict(checkpoint.pressure_before),
                "pressure_after": dict(checkpoint.pressure_after),
                "legacy_incomplete": checkpoint.legacy_incomplete,
            },
        }
    )


def _context_artifact_from_row(row: Mapping[str, Any]) -> ContextArtifact:
    return ContextArtifact(
        artifact_id=str(row["artifact_id"]),
        kind=str(row["kind"]),
        content=str(row["content"]),
        source_start=int(row["source_start"]),
        source_end=int(row["source_end"]),
        created_at_ms=int(row["created_at_ms"]),
        content_sha256=str(row["content_sha256"] or ""),
        byte_length=int(row["byte_length"] or 0),
    )


def _user_record_id(run_id: str) -> str:
    """为新 Run 生成稳定用户记录身份。"""
    return f"run:{run_id}:user"


def _root_execution_id(run_id: str) -> str:
    """复用 RunCoordinator 的根 execution 命名，不为 legacy 数据补身份。"""
    return f"root-{run_id}"


def _transcript_artifact_id(
    project_fingerprint: str, thread_id: str, record_id: str
) -> str:
    """生成不含路径和原文的确定性 Transcript Artifact ID。"""
    seed = f"{project_fingerprint}:{thread_id}:{record_id}".encode("utf-8")
    return f"transcript-{hashlib.sha256(seed).hexdigest()[:32]}"


def _legacy_record_id(project_fingerprint: str, thread_id: str, sequence: int) -> str:
    """为 v6 可证明的 checkpoint 消息生成无 Run 身份的记录 ID。"""
    seed = f"legacy:{project_fingerprint}:{thread_id}:{sequence}".encode("utf-8")
    return f"legacy-{hashlib.sha256(seed).hexdigest()[:32]}"


def _payload_content(payload: str | Mapping[str, object]) -> str:
    """从规范 payload 读取选择器所需的可见内容。"""
    value: object
    if isinstance(payload, str):
        try:
            decoded = strict_json_loads(payload)
        except (ValueError, json.JSONDecodeError):
            return payload
        value = decoded
    else:
        value = payload
    if isinstance(value, Mapping):
        content = value.get("content")
        return content if isinstance(content, str) else str(content or "")
    return ""


def _normalize_transcript_tool_calls(
    tool_calls: tuple[Mapping[str, object], ...],
    *,
    allow_legacy_invalid: bool = False,
) -> list[dict[str, object]]:
    """规范化 assistant tool calls，使幂等比较不依赖调用方字典顺序。"""
    normalized: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for call in tool_calls:
        if not isinstance(call, Mapping):
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
        legacy_invalid_fields = call.get("legacy_invalid_fields")
        if legacy_invalid_fields is not None:
            if not allow_legacy_invalid:
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
            if (
                not isinstance(legacy_invalid_fields, (list, tuple))
                or not legacy_invalid_fields
                or not all(
                    isinstance(field, str) and field
                    for field in legacy_invalid_fields
                )
            ):
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
            try:
                canonical_invalid = strict_json_loads(
                    json.dumps(
                        dict(call),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID") from exc
            if not isinstance(canonical_invalid, dict):
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
            canonical_invalid["arguments_status"] = "invalid"
            normalized.append(canonical_invalid)
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ID_INVALID")
        if call_id in seen_ids:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ID_DUPLICATE")
        seen_ids.add(call_id)
        name = call.get("name", "tool")
        if not isinstance(name, str) or not name:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_NAME_INVALID")
        try:
            canonical = strict_json_loads(
                json.dumps(
                    dict(call),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID") from exc
        if not isinstance(canonical, dict):
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_INVALID")
        canonical["id"] = call_id
        canonical["name"] = name
        if "arguments_status" not in canonical:
            canonical["arguments_status"] = (
                "valid"
                if "arguments" in canonical or "args" in canonical
                else "unavailable"
            )
        if canonical["arguments_status"] == "valid" and not (
            "arguments" in canonical or "args" in canonical
        ):
            raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ARGUMENTS_MISSING")
        if canonical["arguments_status"] == "valid":
            arguments = (
                canonical["args"] if "args" in canonical else canonical["arguments"]
            )
            if not isinstance(arguments, Mapping):
                raise ThreadPersistenceError("TRANSCRIPT_TOOL_CALL_ARGUMENTS_INVALID")
        normalized.append(canonical)
    return normalized


def _transcript_record(row: Mapping[str, Any]) -> TranscriptRecord:
    """将 SQLite 行解析为不暴露表名的 typed Transcript。"""
    payload = strict_json_loads(str(row["payload"]))
    if not isinstance(payload, Mapping):
        raise ThreadPersistenceError("TRANSCRIPT_PAYLOAD_INVALID")
    kind = str(row["kind"])
    if kind not in _TRANSCRIPT_KINDS:
        raise ThreadPersistenceError("TRANSCRIPT_KIND_INVALID")
    return TranscriptRecord(
        record_id=str(row["record_id"]),
        thread_id=str(row["thread_id"]),
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        execution_id=(
            str(row["execution_id"]) if row["execution_id"] is not None else None
        ),
        sequence=int(row["sequence"]),
        kind=kind,  # type: ignore[arg-type]
        payload=dict(payload),
        content_sha256=str(row["content_sha256"]),
        byte_length=int(row["byte_length"]),
        artifact_id=str(row["artifact_id"]) if row["artifact_id"] is not None else None,
        created_at_ms=int(row["created_at_ms"]),
    )


def _transcript_matches(
    record: TranscriptRecord,
    command: TranscriptAppend,
    *,
    project_fingerprint: str,
    allow_legacy_invalid: bool = False,
) -> bool:
    """判断重复追加是否是同一语义，而不是吞掉 Run ID 冲突。"""
    content_bytes = command.content.encode("utf-8")
    expected_artifact_id = (
        _transcript_artifact_id(
            project_fingerprint, command.thread_id, command.record_id
        )
        if command.kind == "tool" and len(content_bytes) > _MAX_INLINE_TOOL_BYTES
        else None
    )
    expected_content = (
        _preview(command.content)
        if command.kind == "tool" and len(content_bytes) > _MAX_INLINE_TOOL_BYTES
        else command.content
    )
    payload = record.payload
    if (
        record.thread_id != command.thread_id
        or record.run_id != command.run_id
        or record.execution_id != command.execution_id
        or record.kind != command.kind
        or record.content_sha256 != _content_sha256(command.content)
        or record.byte_length != len(content_bytes)
        or payload.get("content") != expected_content
        or payload.get("content_sha256") != record.content_sha256
        or payload.get("original_bytes") != record.byte_length
    ):
        return False
    if command.kind != "tool":
        if record.artifact_id is not None:
            return False
        try:
            expected_tool_calls = _normalize_transcript_tool_calls(
                command.tool_calls, allow_legacy_invalid=allow_legacy_invalid
            )
        except ThreadPersistenceError:
            return False
        return payload.get("tool_calls", []) == expected_tool_calls
    return (
        payload.get("tool_call_id") == (command.tool_call_id or command.record_id)
        and (
            ("name" not in payload and "name" in command.legacy_invalid_fields)
            or payload.get("name") == (command.tool_name or "tool")
        )
        and (
            ("status" not in payload and "status" in command.legacy_invalid_fields)
            or payload.get("status") == (command.tool_status or "success")
        )
        and payload.get("tool_call_id_status") == command.tool_call_id_status
        and payload.get("legacy_invalid_fields", [])
        == list(command.legacy_invalid_fields)
        and record.artifact_id == expected_artifact_id
    )


def _thread_message_from_transcript(record: TranscriptRecord) -> ThreadMessage | None:
    """将 Transcript 的可见 payload 映射为不变的 v3 ThreadMessage。"""
    if record.kind not in {"user", "assistant", "tool"}:
        return None
    content = record.payload.get("content")
    if not isinstance(content, str):
        content = ""
    raw_tool_name = record.payload.get("name")
    tool_name = (
        raw_tool_name
        if record.kind == "tool"
        and isinstance(raw_tool_name, str)
        and raw_tool_name
        else None
    )
    return ThreadMessage(kind=record.kind, content=content, tool_name=tool_name)  # type: ignore[arg-type]


def _checkpoint_messages(checkpoint: Mapping[str, Any] | Any) -> list[Any] | None:
    """从非增量 LangGraph checkpoint 读取消息 channel；DeltaChannel 返回 None 交给回放。"""
    if not isinstance(checkpoint, Mapping):
        return None
    channels = checkpoint.get("channel_values")
    if not isinstance(channels, Mapping):
        return None
    messages = channels.get("messages")
    return list(messages) if isinstance(messages, list) else None


def _replay_delta_messages(history: Mapping[str, Any]) -> list[Any]:
    """使用 DeepAgents 的确定性 reducer 回放 DeltaChannel seed 和历史 writes。"""
    entry = history.get("messages")
    if not isinstance(entry, Mapping):
        return []
    seed = entry.get("seed")
    seed_messages = getattr(seed, "value", seed)
    base = list(seed_messages) if isinstance(seed_messages, list) else []
    writes = entry.get("writes")
    values = [write[2] for write in writes if isinstance(write, tuple) and len(write) >= 3] if isinstance(writes, list) else []
    if not values:
        return base
    from deepagents._messages_reducer import _messages_delta_reducer

    return list(_messages_delta_reducer(base, values))


def _normalize_message(value: Any) -> ThreadMessage | None:
    """把 LangChain 消息收敛为 TUI 可安全回放的 project/thread/message 领域值。"""
    name = type(value).__name__
    content = _message_content(getattr(value, "content", ""))
    if name == "HumanMessage":
        return ThreadMessage(kind="user", content=content)
    if name == "AIMessage":
        return ThreadMessage(kind="assistant", content=content)
    if name == "ToolMessage":
        raw_tool_name = getattr(value, "name", None)
        return ThreadMessage(
            kind="tool",
            content=content,
            tool_name=(
                raw_tool_name
                if isinstance(raw_tool_name, str) and raw_tool_name
                else None
            ),
        )
    return None


def _legacy_tool_calls(
    value: Any,
    *,
    project_fingerprint: str,
    thread_id: str,
    sequence: int,
) -> tuple[Mapping[str, object], ...]:
    """保留 v6 checkpoint 中 AI tool call 的可证明字段，不读取 Tool 结果猜参数。"""
    if type(value).__name__ != "AIMessage":
        return ()
    raw_calls = [
        call
        for call in (getattr(value, "tool_calls", None) or ())
        if isinstance(call, Mapping)
    ]
    # LangChain places calls whose JSON arguments could not be decoded in
    # ``invalid_tool_calls``.  They are still checkpoint facts and must remain
    # visible as raw/invalid typed payload rather than disappearing.
    raw_calls.extend(
        call
        for call in (getattr(value, "invalid_tool_calls", None) or ())
        if isinstance(call, Mapping)
    )
    normalized: list[Mapping[str, object]] = []
    for index, call in enumerate(raw_calls):
        invalid_fields: list[str] = []
        raw_call_id = call.get("id")
        if isinstance(raw_call_id, str) and raw_call_id:
            call_id: str | None = raw_call_id
        elif raw_call_id is None or raw_call_id == "":
            call_id = _legacy_tool_call_id(
                project_fingerprint, thread_id, sequence, index
            )
        else:
            call_id = None
            invalid_fields.append("id")
        raw_name = call.get("name")
        normalized_call: dict[str, object] = {}
        if call_id is not None:
            normalized_call["id"] = call_id
        if isinstance(raw_name, str) and raw_name:
            normalized_call["name"] = raw_name
        else:
            invalid_fields.append("name")
        raw_type = call.get("type")
        if raw_type is not None:
            if isinstance(raw_type, str):
                normalized_call["type"] = raw_type
            else:
                invalid_fields.append("type")
        arguments = call.get("args", call.get("arguments"))
        _set_legacy_tool_call_arguments(normalized_call, arguments)
        raw_arguments_status = call.get("arguments_status")
        if raw_arguments_status is not None and raw_arguments_status != "valid":
            if isinstance(raw_arguments_status, str) and raw_arguments_status in {
                "invalid",
                "unavailable",
            }:
                normalized_call["arguments_status"] = raw_arguments_status
            else:
                normalized_call["arguments_status"] = "invalid"
                invalid_fields.append("arguments_status")
        if "error" in call:
            raw_error = call["error"]
            if isinstance(raw_error, str):
                # 保持旧语义：空字符串等同没有错误，非空字符串是可证明的错误事实。
                if raw_error:
                    normalized_call["arguments_error"] = raw_error
                    normalized_call["arguments_status"] = "invalid"
            elif raw_error is not None:
                normalized_call["arguments_error_type"] = type(raw_error).__name__
                invalid_fields.append("error")
                normalized_call["arguments_status"] = "invalid"
        if invalid_fields:
            normalized_call["legacy_invalid_fields"] = invalid_fields
            normalized_call["arguments_status"] = "invalid"
        normalized.append(normalized_call)
    return tuple(normalized)


def _set_legacy_tool_call_arguments(
    call: dict[str, object], value: object
) -> None:
    """将 legacy 参数转成严格 JSON，无法解析时保留 raw/invalid。"""
    if value is None:
        call["arguments_status"] = "unavailable"
        return
    if isinstance(value, str):
        call["arguments_raw"] = value
        if not value:
            call["arguments_status"] = "unavailable"
            return
        try:
            parsed = strict_json_loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            call["arguments_status"] = "invalid"
            call["arguments_error"] = type(exc).__name__
            return
    else:
        parsed = value
    try:
        encoded = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = strict_json_loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        call["arguments_raw"] = repr(value)
        call["arguments_status"] = "invalid"
        call["arguments_error"] = type(exc).__name__
        return
    if not isinstance(normalized, Mapping):
        call["arguments_status"] = "invalid"
        call["arguments_error"] = "ToolArgumentsObjectRequired"
        return
    call["arguments"] = dict(normalized)
    call["arguments_json"] = encoded
    call["arguments_status"] = "valid"


def _legacy_tool_call_id(
    project_fingerprint: str, thread_id: str, sequence: int, index: int
) -> str:
    """为 checkpoint 明确没有 call ID 的事实生成可重复的内部标识。"""
    seed = f"legacy-call:{project_fingerprint}:{thread_id}:{sequence}:{index}"
    return f"legacy-call-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _message_content(value: Any) -> str:
    """从 LangChain string 或内容块列表中提取稳定文本，避免原始对象越过模块边界。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if value is None else str(value)
