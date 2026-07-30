"""Harness thread 持久化：以用户级 SQLite 保存 LangGraph checkpoint 和当前 project 的线程索引。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Mapping

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from harness_agent.execution_binding import (
    ExecutionBindingError,
    LegacyModelBindings,
    PersistedBindingState,
    RunExecutionBinding,
)
from harness_agent.prompting import PromptEpoch, canonical_json
from harness_agent.agent_engine_profile import AGENT_ENGINE_PROFILE_VERSION, AgentEngineProfile


_SCHEMA_VERSION = 6
_MAX_PREVIEW_CHARS = 160


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
    """由 checkpoint 归一化出的稳定消息历史，供 CLI 表现层回放。"""

    kind: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class OpenThread:
    """已校验归属 project 的线程快照和可回放消息。"""

    summary: ThreadSummary
    messages: tuple[ThreadMessage, ...]


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


@dataclass(frozen=True, slots=True)
class ContextCommit:
    """Context 状态转换提交后的 typed 结果。"""

    artifacts: tuple[ContextArtifact, ...] = ()
    summary: ContextSummary | None = None
    state: ContextState | None = None


class ProjectScopedAsyncSqliteSaver(AsyncSqliteSaver):
    """将 LangGraph 自动归一的 checkpoint namespace 固定映射到当前 project。"""

    def __init__(self, connection: aiosqlite.Connection, project_fingerprint: str) -> None:
        """复用同一 SQLite 连接，并保留 project 指纹作为根 namespace。"""
        super().__init__(connection)
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
    ) -> None:
        """保存已验证的连接和固定 project namespace。"""
        self._connection = connection
        self._checkpointer = checkpointer
        self._path = path
        self._project_fingerprint = project_fingerprint
        self._closed = False
        self._lock = asyncio.Lock()

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
            checkpointer = ProjectScopedAsyncSqliteSaver(connection, project_fingerprint)
            persistence = cls(
                connection=connection,
                checkpointer=checkpointer,
                path=path,
                project_fingerprint=project_fingerprint,
            )
            await persistence._prepare()
            return persistence
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
        """原子受理一个 Run，并把 Thread 首条消息索引与绑定一起提交。"""
        return RunAcceptance(
            created=await self._record_run_start(command.message, command.binding),
            binding=command.binding,
        )

    async def _record_run_start(
        self,
        message: str,
        binding: RunExecutionBinding,
    ) -> bool:
        """原子登记 Thread 索引与 Run 绑定；同一 Run ID 重试不得重复执行。"""
        self._ensure_open()
        thread_id = binding.thread_id
        run_id = binding.run_id
        now = binding.created_at_ms
        preview = _preview(message)
        encoded_selection = canonical_json(binding.requested_selection_record())
        encoded_primary = canonical_json(binding.actual_primary_record())
        message_digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT requested_selection, actual_primary_binding, runtime_profile_id, message_digest
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
                    ):
                        return False
                    raise ThreadPersistenceError("RUN_EXECUTION_BINDING_CONFLICT")
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
                await self._connection.execute(
                    """
                    INSERT INTO harness_run_execution_bindings (
                        project_fingerprint, thread_id, run_id, requested_selection,
                        actual_primary_binding, runtime_profile_id, message_digest, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
                await self._connection.commit()
                return True
        except ThreadPersistenceError:
            raise
        except aiosqlite.Error as exc:
            try:
                await self._connection.rollback()
            except aiosqlite.Error:
                pass
            raise ThreadPersistenceError(f"RUN_EXECUTION_BINDING_WRITE_FAILED: {exc}") from exc

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
                           runtime_profile_id, created_at_ms
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
            selection = json.loads(str(row["requested_selection"]))
            primary = json.loads(str(row["actual_primary_binding"]))
            if not isinstance(selection, dict) or not isinstance(primary, dict):
                raise ThreadPersistenceError("RUN_EXECUTION_BINDING_INVALID")
            return RunExecutionBinding.from_records(
                thread_id=str(row["thread_id"]),
                run_id=str(row["run_id"]),
                requested_selection=selection,
                actual_primary_binding=primary,
                runtime_profile_id=str(row["runtime_profile_id"]),
                created_at_ms=int(row["created_at_ms"]),
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

    async def load_run_state(self, thread_id: str) -> PersistedBindingState:
        """读取 Run 恢复所需的 typed 状态，不向调用方暴露表结构。"""
        return PersistedBindingState(
            latest_run=await self._get_latest_run_execution_binding(thread_id),
            legacy_models=await self._get_legacy_model_bindings(thread_id),
            has_legacy_runtime=await self._has_legacy_runtime_binding(thread_id),
        )

    async def complete_run(self, thread_id: str) -> None:
        """在 Run 终态用 checkpoint 消息数更新可恢复 Thread 摘要。"""
        self._ensure_open()
        try:
            messages = await self._messages_for_thread(thread_id)
            count = (
                sum(_normalize_message(message) is not None for message in messages)
                if messages is not None
                else 0
            )
            async with self._lock:
                await self._connection.execute(
                    """
                    UPDATE harness_threads
                    SET updated_at_ms = ?, message_count = ?
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (_now_ms(), count, self._project_fingerprint, thread_id),
                )
                await self._connection.commit()
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_INDEX_REFRESH_FAILED: {exc}") from exc

    async def load_context(self, thread_id: str) -> ContextSnapshot:
        """读取当前 Thread 的消息和 Context 状态，供 Context module 完成一次重写。"""
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

    async def load_prompt_epoch(self, thread_id: str) -> PromptEpoch | None:
        """返回既有 thread 的不可变提示词 epoch，恢复时绝不重新扫描环境或 Skill。"""
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
        """首次创建 thread 时保存完整前缀；同一 thread 的不同 epoch 一律拒绝覆盖。"""
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
            record = json.loads(str(row["binding_record"]))
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

    async def commit_context(self, command: CommitContextRewrite) -> ContextCommit:
        """原子提交 Context 的归档、摘要和状态，隐藏 SQLite 多表事务。"""
        self._ensure_open()
        artifacts: list[ContextArtifact] = []
        for draft in command.artifacts:
            if not draft.content:
                raise ThreadPersistenceError("CONTEXT_ARTIFACT_EMPTY")
            if not draft.kind or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in draft.kind
            ):
                raise ThreadPersistenceError("CONTEXT_ARTIFACT_KIND_INVALID")
            source_start = max(0, draft.source_start)
            artifacts.append(
                ContextArtifact(
                    artifact_id=f"{draft.kind}-{uuid.uuid4().hex}",
                    kind=draft.kind,
                    content=draft.content,
                    source_start=source_start,
                    source_end=max(source_start, draft.source_end),
                    created_at_ms=_now_ms(),
                )
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

        try:
            async with self._lock:
                for artifact in artifacts:
                    await self._connection.execute(
                        """
                        INSERT INTO harness_context_artifacts (
                            project_fingerprint, thread_id, artifact_id, kind, content,
                            source_start, source_end, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                await self._connection.commit()
        except ThreadPersistenceError:
            try:
                await self._connection.rollback()
            except aiosqlite.Error:
                pass
            raise
        except aiosqlite.Error as exc:
            try:
                await self._connection.rollback()
            except aiosqlite.Error:
                pass
            raise ThreadPersistenceError(f"CONTEXT_REWRITE_WRITE_FAILED: {exc}") from exc
        return ContextCommit(tuple(artifacts), summary, command.state)

    async def load_context_artifact(self, thread_id: str, artifact_id: str) -> ContextArtifact | None:
        """读取仅归属当前 project/thread 的归档，调用方不得从数据库路径推断真实位置。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT artifact_id, kind, content, source_start, source_end, created_at_ms
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
        """读取一个归属当前 project 的可恢复 thread；索引和 checkpoint 均缺失即拒绝。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT thread_id, created_at_ms, updated_at_ms, first_message,
                           latest_message, message_count
                    FROM harness_threads
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                raise ThreadPersistenceError("THREAD_NOT_FOUND")
            messages = await self._messages_for_thread(thread_id)
            if messages is None:
                raise ThreadPersistenceError("THREAD_NOT_RECOVERABLE")
            normalized = tuple(_normalize_message(message) for message in messages)
            return OpenThread(
                summary=_summary(row),
                messages=tuple(message for message in normalized if message is not None),
            )
        except ThreadPersistenceError:
            raise
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_READ_FAILED: {exc}") from exc

    async def close(self) -> None:
        """提交并关闭连接，确保 CLI 退出后用户可安全删除数据库及 WAL 文件。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._connection.commit()
            await self._connection.close()
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_CLOSE_FAILED: {exc}") from exc

    async def _prepare(self) -> None:
        """验证 SQLite 可读性、初始化 LangGraph 表并升级 Harness 线程索引。"""
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
            cursor = await self._connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            version = int(row[0]) if row else 0
            if version > _SCHEMA_VERSION:
                raise ThreadPersistenceError(
                    f"CHECKPOINT_SCHEMA_TOO_NEW: found {version}, supports {_SCHEMA_VERSION}"
                )
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA busy_timeout=5000")
            await self._checkpointer.setup()
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
            await self._connection.execute(f"PRAGMA user_version={version}")
            await self._connection.commit()
        except ThreadPersistenceError:
            raise
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_MIGRATION_FAILED: {exc}") from exc

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
        return ThreadMessage(kind="tool", content=content, tool_name=str(getattr(value, "name", "tool")))
    return None


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
