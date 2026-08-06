"""Harness thread 持久化：以用户级 SQLite 保存 LangGraph checkpoint 和当前 project 的线程索引。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Literal, Mapping

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from harness_agent.runtime.execution_binding import (
    ExecutionBindingError,
    LegacyModelBindings,
    PersistedBindingState,
    RunExecutionBinding,
)
from harness_agent.threads.context_lifecycle import RunContextSnapshot, snapshot_from_legacy_prompt_epoch
from harness_agent.threads.prompting import HISTORY_REWRITE_VERSION, canonical_json
from harness_agent.runtime.agent_engine_profile import AGENT_ENGINE_PROFILE_VERSION, AgentEngineProfile
from harness_agent.threads.context_projection import (
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
from harness_agent.threads.runtime_state import RuntimeStateError, RuntimeStateSnapshot


_SCHEMA_VERSION = 11
_MAX_PREVIEW_CHARS = 160
_MAX_INLINE_TOOL_BYTES = 64 * 1024
_TRANSCRIPT_KINDS = ("user", "assistant", "tool", "context")
_MIGRATION_STATE_VERSION = 1
_MIGRATION_LOCK_SUFFIX = ".migration.lock"
_MIGRATION_STATE_SUFFIX = ".migration-state.json"
_MIGRATION_COMMIT_DEADLINE_SECONDS = 15.0
_LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS = 30.0
_LEGACY_MIGRATION_CHILD_TERMINATE_GRACE_SECONDS = 0.25
_MIGRATION_CHILD_TEST_PHASES = frozenset(
    {
        "backup_failure",
        "bootstrap_failure",
        "restore_failure",
        "before_commit",
        "after_final_validation",
        "after_commit_before_state",
        "after_commit_before_reply",
        "commit_failure_before",
        "state_committed_failure",
        "state_clear_failure",
    }
)


# This registry contains no SQLite connection or asyncio task.  It is only a
# process-local handoff barrier published before the file lock is released
# after a child migration timeout.  A fresh process intentionally starts with
# an empty registry and must make the normal fact-based recovery decision from
# the durable state/backup instead.
_MIGRATION_POISONED_PATHS: set[Path] = set()
_MIGRATION_POISON_LOCK = threading.Lock()

# The child owns this marker only.  A permanently blocked aiosqlite worker is
# never awaited or cleaned up by the parent; the parent kills and reaps the
# whole child instead.
_MIGRATION_CHILD_POISONED_PATHS: set[Path] = set()
_MIGRATION_CHILD_TEST_PHASE: str | None = None
_MIGRATION_CHILD_PROCESS_MODE = False


def _is_supported_legacy_prompt_epoch_source(source_version: int) -> bool:
    """仅接纳具有严格历史 schema 契约的 PromptEpoch 来源。"""
    return 2 <= source_version <= 6


def _is_pre_transcript_prompt_epoch_source_sync(
    connection: sqlite3.Connection,
    source_version: int,
) -> bool:
    """识别合并前曾使用 ``user_version=7`` 的旧 PromptEpoch 数据库。

    该版本号在一条历史分支中先于 Transcript 表落地，不能直接按当前
    v7 契约解释。只有完整的 v6 表集合（加上已知 Team 状态表）、没有
    Transcript/Context Snapshot 等后继表时才降级为 v6 进入现有迁移器；
    当前 v7+ schema 携带 PromptEpoch 仍会按异常残留拒绝。
    """
    if source_version != 7:
        return False
    tables = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }
    if "harness_prompt_epochs" not in tables:
        return False
    if tables.intersection(
        {
            "harness_thread_transcript",
            "harness_thread_history_metadata",
            "harness_run_context_snapshots",
            "harness_compression_checkpoints",
        }
    ):
        return False
    try:
        _migration_validate_legacy_source_schema_sync(connection, 6)
    except ThreadPersistenceError:
        return False
    return True


def _validate_source_schema_for_fingerprint_sync(
    connection: sqlite3.Connection,
    source_version: int,
) -> None:
    """按 fingerprint 的真实版本校验旧库，兼容已识别的旧分支 v7。"""
    if 1 <= source_version <= 6:
        _migration_validate_legacy_source_schema_sync(connection, source_version)
    elif _is_pre_transcript_prompt_epoch_source_sync(connection, source_version):
        _migration_validate_legacy_source_schema_sync(connection, 6)


def _assert_migration_path_available(path: Path) -> None:
    """在 open 前检查本进程的迁移 owner poison 状态。"""
    with _MIGRATION_POISON_LOCK:
        poisoned = path in _MIGRATION_POISONED_PATHS
    if poisoned:
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_RECOVERY_REQUIRED")


def _publish_migration_poison(path: Path) -> None:
    """在释放同一路径文件锁前发布 fail-closed handoff。"""
    with _MIGRATION_POISON_LOCK:
        _MIGRATION_POISONED_PATHS.add(path)


def _clear_migration_poison(path: Path) -> None:
    """只在独立事实已收敛到可服务状态后清除当前进程 poison。"""
    with _MIGRATION_POISON_LOCK:
        _MIGRATION_POISONED_PATHS.discard(path)


async def _migration_child_pause_if_requested(phase: str) -> None:
    """测试专用可控 child failpoint；生产启动不会传入 phase。"""
    if _MIGRATION_CHILD_TEST_PHASE == phase:
        await asyncio.Future[None]()


def _migration_child_test_failure(phase: str, error: BaseException) -> None:
    """测试 failpoint 只在隔离 worker 内生效，不改变父进程生产路径。"""
    if _MIGRATION_CHILD_TEST_PHASE == phase:
        raise error


def _requested_migration_test_phase() -> str | None:
    """只允许 pytest 注入 failpoint，生产环境变量不会改变迁移行为。"""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    phase = os.environ.get("HARNESS_TEST_MIGRATION_CHILD_PHASE")
    return phase if phase in _MIGRATION_CHILD_TEST_PHASES else None


@dataclass(frozen=True, slots=True)
class _MigrationColumnContract:
    """legacy v1-v6 源表列的 SQLite 声明契约；不是只按列名存在判断。"""

    name: str
    declared_type: str
    not_null: int
    default: str | None
    primary_key: int


@dataclass(frozen=True, slots=True)
class _MigrationIndexContract:
    """legacy v1-v6 源索引的唯一性、列序、排序和 canonical SQL 契约。"""

    name: str
    unique: int
    origin: str
    partial: int
    columns: tuple[tuple[str, int], ...]
    sql: str


def _migration_column_contracts(
    table_name: str,
    source_version: int,
) -> tuple[_MigrationColumnContract, ...]:
    """返回 legacy v1-v6 源表的精确列契约；PromptEpoch 按历史版本分支。"""
    contracts: dict[str, tuple[_MigrationColumnContract, ...]] = {
        "harness_threads": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("created_at_ms", "INTEGER", 1, None, 0),
            _MigrationColumnContract("updated_at_ms", "INTEGER", 1, None, 0),
            _MigrationColumnContract("first_message", "TEXT", 1, None, 0),
            _MigrationColumnContract("latest_message", "TEXT", 1, None, 0),
            _MigrationColumnContract("message_count", "INTEGER", 1, "0", 0),
        ),
        "checkpoints": (
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 1),
            _MigrationColumnContract("checkpoint_ns", "TEXT", 1, "''", 2),
            _MigrationColumnContract("checkpoint_id", "TEXT", 1, None, 3),
            _MigrationColumnContract("parent_checkpoint_id", "TEXT", 0, None, 0),
            _MigrationColumnContract("type", "TEXT", 0, None, 0),
            _MigrationColumnContract("checkpoint", "BLOB", 0, None, 0),
            _MigrationColumnContract("metadata", "BLOB", 0, None, 0),
        ),
        "writes": (
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 1),
            _MigrationColumnContract("checkpoint_ns", "TEXT", 1, "''", 2),
            _MigrationColumnContract("checkpoint_id", "TEXT", 1, None, 3),
            _MigrationColumnContract("task_id", "TEXT", 1, None, 4),
            _MigrationColumnContract("idx", "INTEGER", 1, None, 5),
            _MigrationColumnContract("channel", "TEXT", 1, None, 0),
            _MigrationColumnContract("type", "TEXT", 0, None, 0),
            _MigrationColumnContract("value", "BLOB", 0, None, 0),
        ),
        "harness_context_artifacts": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("artifact_id", "TEXT", 1, None, 3),
            _MigrationColumnContract("kind", "TEXT", 1, None, 0),
            _MigrationColumnContract("content", "TEXT", 1, None, 0),
            _MigrationColumnContract("source_start", "INTEGER", 1, None, 0),
            _MigrationColumnContract("source_end", "INTEGER", 1, None, 0),
            _MigrationColumnContract("created_at_ms", "INTEGER", 1, None, 0),
        ),
        "harness_context_summaries": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 0),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 0),
            _MigrationColumnContract("summary_id", "INTEGER", 0, None, 1),
            _MigrationColumnContract("rewrite_version", "TEXT", 1, None, 0),
            _MigrationColumnContract("content", "TEXT", 1, None, 0),
            _MigrationColumnContract("source_start", "INTEGER", 1, None, 0),
            _MigrationColumnContract("source_end", "INTEGER", 1, None, 0),
            _MigrationColumnContract("artifact_ids", "TEXT", 1, None, 0),
            _MigrationColumnContract("created_at_ms", "INTEGER", 1, None, 0),
        ),
        "harness_context_state": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("failures", "INTEGER", 1, "0", 0),
            _MigrationColumnContract("circuit_open", "INTEGER", 1, "0", 0),
            _MigrationColumnContract("last_action", "TEXT", 1, "'none'", 0),
            _MigrationColumnContract("updated_at_ms", "INTEGER", 1, None, 0),
        ),
        "harness_runtime_profiles": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("profile_key", "TEXT", 1, None, 2),
            _MigrationColumnContract("profile_version", "INTEGER", 1, None, 0),
            _MigrationColumnContract("topology_id", "TEXT", 1, None, 0),
            _MigrationColumnContract("topology_version", "INTEGER", 1, None, 0),
            _MigrationColumnContract("profile_record", "TEXT", 1, None, 0),
            _MigrationColumnContract("created_at_ms", "INTEGER", 1, None, 0),
        ),
        "harness_thread_runtime_profiles": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("profile_key", "TEXT", 1, None, 0),
            _MigrationColumnContract("profile_version", "INTEGER", 1, None, 0),
            _MigrationColumnContract("bound_at_ms", "INTEGER", 1, None, 0),
        ),
        "harness_thread_model_bindings": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("binding_record", "TEXT", 1, None, 0),
            _MigrationColumnContract("bound_at_ms", "INTEGER", 1, None, 0),
        ),
        "harness_run_execution_bindings": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("run_id", "TEXT", 1, None, 3),
            _MigrationColumnContract("requested_selection", "TEXT", 1, None, 0),
            _MigrationColumnContract("actual_primary_binding", "TEXT", 1, None, 0),
            _MigrationColumnContract("runtime_profile_id", "TEXT", 1, None, 0),
            _MigrationColumnContract("message_digest", "TEXT", 1, None, 0),
            _MigrationColumnContract("created_at_ms", "INTEGER", 1, None, 0),
        ),
        "harness_team_runs": (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("run_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("team_id", "TEXT", 1, None, 0),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 0),
            _MigrationColumnContract("parent_run_id", "TEXT", 1, None, 0),
            _MigrationColumnContract("parent_execution_id", "TEXT", 1, None, 0),
            _MigrationColumnContract("parent_parent_execution_id", "TEXT", 0, None, 0),
            _MigrationColumnContract("status", "TEXT", 1, None, 0),
            _MigrationColumnContract("tasks_json", "TEXT", 1, None, 0),
            _MigrationColumnContract("terminal_count", "INTEGER", 1, None, 0),
        ),
    }
    if table_name == "harness_prompt_epochs":
        prompt_columns = (
            _MigrationColumnContract("project_fingerprint", "TEXT", 1, None, 1),
            _MigrationColumnContract("thread_id", "TEXT", 1, None, 2),
            _MigrationColumnContract("prompt_version", "INTEGER", 1, None, 0),
            _MigrationColumnContract("system_prompt", "TEXT", 1, None, 0),
            _MigrationColumnContract("environment_snapshot", "TEXT", 1, None, 0),
            _MigrationColumnContract("readonly_memory", "TEXT", 1, None, 0),
            _MigrationColumnContract("skill_index", "TEXT", 1, None, 0),
            _MigrationColumnContract("tool_schema_fingerprint", "TEXT", 1, None, 0),
            _MigrationColumnContract("system_fingerprint", "TEXT", 1, None, 0),
            _MigrationColumnContract("history_rewrite_version", "TEXT", 1, None, 0),
            _MigrationColumnContract("created_at_ms", "INTEGER", 1, None, 0),
        )
        if source_version >= 3:
            prompt_columns += (
                _MigrationColumnContract(
                    "prefix_change_reason", "TEXT", 1, "'new_thread'", 0
                ),
            )
        return prompt_columns
    try:
        return contracts[table_name]
    except KeyError as exc:
        raise ThreadPersistenceError(
            f"CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:{table_name}"
        ) from exc


def _migration_named_index_contracts(
    table_name: str,
) -> tuple[_MigrationIndexContract, ...]:
    """返回 v6 关键 named index 契约；PK/UNIQUE 由 table_xinfo/index_list 同时校验。"""
    common: dict[str, tuple[_MigrationIndexContract, ...]] = {
        "harness_threads": (
            _MigrationIndexContract(
                "harness_threads_project_updated",
                0,
                "c",
                0,
                (("project_fingerprint", 0), ("updated_at_ms", 1)),
                "CREATE INDEX harness_threads_project_updated ON harness_threads(project_fingerprint, updated_at_ms DESC)",
            ),
        ),
        "harness_context_artifacts": (
            _MigrationIndexContract(
                "harness_context_artifacts_thread_created",
                0,
                "c",
                0,
                (
                    ("project_fingerprint", 0),
                    ("thread_id", 0),
                    ("created_at_ms", 0),
                ),
                "CREATE INDEX harness_context_artifacts_thread_created ON harness_context_artifacts(project_fingerprint, thread_id, created_at_ms)",
            ),
        ),
        "harness_thread_runtime_profiles": (
            _MigrationIndexContract(
                "harness_thread_runtime_profiles_project_profile",
                0,
                "c",
                0,
                (("project_fingerprint", 0), ("profile_key", 0)),
                "CREATE INDEX harness_thread_runtime_profiles_project_profile ON harness_thread_runtime_profiles(project_fingerprint, profile_key)",
            ),
        ),
        "harness_run_execution_bindings": (
            _MigrationIndexContract(
                "harness_run_execution_bindings_thread_created",
                0,
                "c",
                0,
                (
                    ("project_fingerprint", 0),
                    ("thread_id", 0),
                    ("created_at_ms", 1),
                ),
                "CREATE INDEX harness_run_execution_bindings_thread_created ON harness_run_execution_bindings(project_fingerprint, thread_id, created_at_ms DESC)",
            ),
        ),
        "harness_team_runs": (
            _MigrationIndexContract(
                "harness_team_runs_parent",
                0,
                "c",
                0,
                (
                    ("project_fingerprint", 0),
                    ("thread_id", 0),
                    ("parent_run_id", 0),
                ),
                "CREATE INDEX harness_team_runs_parent ON harness_team_runs(project_fingerprint, thread_id, parent_run_id)",
            ),
        ),
    }
    return common.get(table_name, ())


def _migration_normalize_sql(sql: str | None) -> str | None:
    """规范化 sqlite_master SQL，仅消除布局差异，不放宽对象定义。"""
    if sql is None:
        return None
    return " ".join(sql.split()).lower()


def _migration_legacy_required_tables(source_version: int) -> tuple[str, ...]:
    """返回某个 legacy 版本必须存在的核心表，保持版本边界可审计。"""
    required: list[str] = []
    if source_version >= 1:
        required.extend(("harness_threads", "checkpoints", "writes"))
    if source_version >= 2:
        required.extend(
            (
                "harness_context_artifacts",
                "harness_context_summaries",
                "harness_context_state",
            )
        )
    if source_version >= 4:
        required.extend(
            ("harness_runtime_profiles", "harness_thread_runtime_profiles")
        )
    if source_version >= 5:
        required.append("harness_thread_model_bindings")
    if source_version >= 6:
        required.append("harness_run_execution_bindings")
    return tuple(required)


def _migration_table_sql_contract(
    table_name: str,
    source_version: int,
) -> str | None:
    """返回无安全尾列时的 canonical CREATE TABLE 定义。"""
    contracts = {
        "harness_threads": """
            CREATE TABLE harness_threads (
                project_fingerprint TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                first_message TEXT NOT NULL,
                latest_message TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (project_fingerprint, thread_id)
            )
        """,
        "checkpoints": """
            CREATE TABLE checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
        """,
        "writes": """
            CREATE TABLE writes (
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
        """,
        "harness_context_artifacts": """
            CREATE TABLE harness_context_artifacts (
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
        """,
        "harness_context_summaries": """
            CREATE TABLE harness_context_summaries (
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
        """,
        "harness_context_state": """
            CREATE TABLE harness_context_state (
                project_fingerprint TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                circuit_open INTEGER NOT NULL DEFAULT 0,
                last_action TEXT NOT NULL DEFAULT 'none',
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (project_fingerprint, thread_id)
            )
        """,
        "harness_runtime_profiles": """
            CREATE TABLE harness_runtime_profiles (
                project_fingerprint TEXT NOT NULL,
                profile_key TEXT NOT NULL,
                profile_version INTEGER NOT NULL,
                topology_id TEXT NOT NULL,
                topology_version INTEGER NOT NULL,
                profile_record TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                PRIMARY KEY (project_fingerprint, profile_key)
            )
        """,
        "harness_thread_runtime_profiles": """
            CREATE TABLE harness_thread_runtime_profiles (
                project_fingerprint TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                profile_key TEXT NOT NULL,
                profile_version INTEGER NOT NULL,
                bound_at_ms INTEGER NOT NULL,
                PRIMARY KEY (project_fingerprint, thread_id)
            )
        """,
        "harness_thread_model_bindings": """
            CREATE TABLE harness_thread_model_bindings (
                project_fingerprint TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                binding_record TEXT NOT NULL,
                bound_at_ms INTEGER NOT NULL,
                PRIMARY KEY (project_fingerprint, thread_id)
            )
        """,
        "harness_run_execution_bindings": """
            CREATE TABLE harness_run_execution_bindings (
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
        """,
        "harness_team_runs": """
            CREATE TABLE harness_team_runs (
                project_fingerprint TEXT NOT NULL,
                run_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                parent_run_id TEXT NOT NULL,
                parent_execution_id TEXT NOT NULL,
                parent_parent_execution_id TEXT,
                status TEXT NOT NULL,
                tasks_json TEXT NOT NULL,
                terminal_count INTEGER NOT NULL,
                PRIMARY KEY (project_fingerprint, run_id)
            )
        """,
    }
    if table_name == "harness_prompt_epochs":
        prefix = (
            ", prefix_change_reason TEXT NOT NULL DEFAULT 'new_thread'"
            if source_version >= 3
            else ""
        )
        return f"""
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
                created_at_ms INTEGER NOT NULL{prefix},
                PRIMARY KEY (project_fingerprint, thread_id)
            )
        """
    return contracts.get(table_name)


def _migration_index_contract_rows(
    column_contracts: tuple[_MigrationColumnContract, ...],
    columns: tuple[tuple[str, int], ...],
) -> tuple[tuple[object, ...], ...]:
    """把 named index 列定义展开为 PRAGMA index_xinfo 的可比形状。"""
    column_ids = {column.name: index for index, column in enumerate(column_contracts)}
    return tuple(
        (column_ids[name], name, descending, "BINARY", 1)
        for name, descending in columns
    ) + ((-1, None, 0, "BINARY", 0),)


def _migration_expected_index_contracts(
    table_name: str,
    column_contracts: tuple[_MigrationColumnContract, ...],
) -> tuple[_MigrationIndexContract, ...]:
    """展开 named index 和复合 PK autoindex 的完整索引集合。"""
    named = _migration_named_index_contracts(table_name)
    primary_columns = tuple(
        sorted(
            (
                column.primary_key,
                column.name,
            )
            for column in column_contracts
            if column.primary_key
        )
    )
    if len(primary_columns) <= 1:
        return named
    primary = _MigrationIndexContract(
        name=f"sqlite_autoindex_{table_name}_1",
        unique=1,
        origin="pk",
        partial=0,
        columns=tuple((name, 0) for _, name in primary_columns),
        sql="",
    )
    return named + (primary,)


def _migration_schema_contract_error(table_name: str, detail: str) -> ThreadPersistenceError:
    """生成统一的 source schema typed error；不把 SQLite 原文泄露到状态文件。"""
    return ThreadPersistenceError(
        f"CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:{table_name}:{detail}"
    )


def _migration_compare_table_contract(
    *,
    table_name: str,
    source_version: int,
    table_sql: str | None,
    column_rows: Iterable[tuple[object, ...]],
    index_rows: Iterable[tuple[object, ...]],
    index_xinfo: Mapping[str, Iterable[tuple[object, ...]]],
    index_sql: Mapping[str, str | None],
    foreign_keys: Iterable[tuple[object, ...]],
) -> None:
    """比较一张 v6 表的完整结构，拒绝同名畸形对象。"""
    expected_columns = _migration_column_contracts(table_name, source_version)
    actual_columns = tuple(tuple(row) for row in column_rows)
    expected_names = tuple(column.name for column in expected_columns)
    actual_names = tuple(str(row[1]) for row in actual_columns)
    if actual_names != expected_names:
        raise _migration_schema_contract_error(table_name, "column_set")
    expected_column_shape = tuple(
        (column.name, column.declared_type, column.not_null, column.default, column.primary_key, 0)
        for column in expected_columns
    )
    actual_column_shape = tuple(
        (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]), int(row[6]))
        for row in actual_columns
    )
    if actual_column_shape != expected_column_shape:
        raise _migration_schema_contract_error(table_name, "column_definition")
    if list(foreign_keys):
        raise _migration_schema_contract_error(table_name, "foreign_key")

    expected_sql = _migration_table_sql_contract(table_name, source_version)
    if _migration_normalize_sql(table_sql) != _migration_normalize_sql(expected_sql):
        raise _migration_schema_contract_error(table_name, "table_sql")

    expected_indexes = _migration_expected_index_contracts(table_name, expected_columns)
    actual_index_rows = tuple(tuple(row) for row in index_rows)
    actual_by_name = {str(row[1]): row for row in actual_index_rows}
    expected_by_name = {index.name: index for index in expected_indexes}
    if set(actual_by_name) != set(expected_by_name):
        raise _migration_schema_contract_error(table_name, "index_set")
    for index in expected_indexes:
        row = actual_by_name[index.name]
        if (int(row[2]), str(row[3]), int(row[4])) != (
            index.unique,
            index.origin,
            index.partial,
        ):
            raise _migration_schema_contract_error(table_name, f"index_definition:{index.name}")
        actual_xinfo = tuple(
            (int(item[1]), item[2], int(item[3]), str(item[4]), int(item[5]))
            for item in index_xinfo.get(index.name, ())
        )
        expected_xinfo = _migration_index_contract_rows(expected_columns, index.columns)
        if actual_xinfo != expected_xinfo:
            raise _migration_schema_contract_error(table_name, f"index_columns:{index.name}")
        expected_index_sql = None if index.name.startswith("sqlite_autoindex_") else index.sql
        if _migration_normalize_sql(index_sql.get(index.name)) != _migration_normalize_sql(
            expected_index_sql
        ):
            raise _migration_schema_contract_error(table_name, f"index_sql:{index.name}")


def _migration_validate_legacy_source_schema_sync(
    connection: sqlite3.Connection,
    source_version: int,
) -> None:
    """同步校验 backup/恢复使用的 legacy v1-v6 源 schema。"""
    required = _migration_legacy_required_tables(source_version)
    rows = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    actual_tables = {str(row[0]) for row in rows}
    allowed_tables = set(required)
    # Team 状态表由 TeamStateStore 在较新的扩展分支中提前创建；它不改变
    # Thread schema 版本，但迁移时必须作为已知用户数据原样保留。
    allowed_tables.add("harness_team_runs")
    if source_version >= 2:
        allowed_tables.add("harness_prompt_epochs")
    unknown = actual_tables - allowed_tables
    if unknown:
        raise _migration_schema_contract_error("<database>", "unexpected_table")
    for table_name in required:
        if table_name not in actual_tables:
            raise _migration_schema_contract_error(table_name, "missing_table")
    if "harness_prompt_epochs" in actual_tables:
        prompt_version = max(source_version, 2)
        _migration_validate_table_contract_sync(connection, "harness_prompt_epochs", prompt_version)
    if "harness_team_runs" in actual_tables:
        _migration_validate_table_contract_sync(connection, "harness_team_runs", source_version)
    for table_name in required:
        _migration_validate_table_contract_sync(connection, table_name, source_version)


def _migration_validate_table_contract_sync(
    connection: sqlite3.Connection,
    table_name: str,
    source_version: int,
) -> None:
    """读取 sqlite_master/PRAGMA 并比较单表 canonical contract。"""
    quoted_table = _migration_identifier(table_name)
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if table_row is None:
        raise _migration_schema_contract_error(table_name, "missing_table")
    index_rows = connection.execute(f"PRAGMA index_list({quoted_table})").fetchall()
    index_xinfo = {
        str(row[1]): connection.execute(
            f"PRAGMA index_xinfo({_migration_identifier(str(row[1]))})"
        ).fetchall()
        for row in index_rows
    }
    index_sql = {
        str(row[1]): connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (str(row[1]),),
        ).fetchone()[0]
        for row in index_rows
    }
    _migration_compare_table_contract(
        table_name=table_name,
        source_version=source_version,
        table_sql=table_row[0],
        column_rows=connection.execute(f"PRAGMA table_xinfo({quoted_table})").fetchall(),
        index_rows=index_rows,
        index_xinfo=index_xinfo,
        index_sql=index_sql,
        foreign_keys=connection.execute(f"PRAGMA foreign_key_list({quoted_table})").fetchall(),
    )


@dataclass(frozen=True, slots=True)
class _MigrationTableDigest:
    """迁移边界内一张表的列形状、行数和逐行摘要。"""

    name: str
    columns: tuple[str, ...]
    row_count: int
    digest: str

    def record(self) -> dict[str, object]:
        """返回可写入 migration state 的不含原文摘要。"""
        return {
            "name": self.name,
            "columns": list(self.columns),
            "row_count": self.row_count,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class _MigrationDatabaseFingerprint:
    """证明一次 SQLite 快照的 schema、版本和关键数据没有漂移。"""

    user_version: int
    integrity_check: str
    schema_digest: str
    data_digest: str
    tables: tuple[_MigrationTableDigest, ...]

    def record(self) -> dict[str, object]:
        """返回 migration state 使用的严格 JSON 结构。"""
        return {
            "user_version": self.user_version,
            "integrity_check": self.integrity_check,
            "schema_digest": self.schema_digest,
            "data_digest": self.data_digest,
            "tables": [table.record() for table in self.tables],
        }


def _migration_identifier(name: str) -> str:
    """引用来自 sqlite_master 的标识符，不允许把它当作 SQL 片段。"""
    return '"' + name.replace('"', '""') + '"'


def _migration_value_bytes(value: object) -> bytes:
    """以带类型边界的编码摘要 SQLite 原子值，不用 ``str`` 伪造数据。"""
    if value is None:
        return b"N;"
    if isinstance(value, bool):
        return b"I:1;" if value else b"I:0;"
    if isinstance(value, int):
        return f"I:{value};".encode("ascii")
    if isinstance(value, float):
        return f"F:{value!r};".encode("ascii")
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return b"T:" + str(len(encoded)).encode("ascii") + b":" + encoded + b";"
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = bytes(value)
        return b"B:" + str(len(encoded)).encode("ascii") + b":" + encoded + b";"
    raise TypeError(f"unsupported SQLite value type: {type(value).__name__}")


def _migration_row_digest(row: Iterable[object]) -> str:
    """计算一行的 typed digest，保持 BLOB、文本和数字不可混淆。"""
    digest = hashlib.sha256()
    for value in row:
        encoded = _migration_value_bytes(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _migration_rows_digest(rows: Iterable[Iterable[object]]) -> tuple[int, str]:
    """按无序行集合生成稳定摘要，同时保留重复行的计数。"""
    row_digests = sorted(_migration_row_digest(row) for row in rows)
    digest = hashlib.sha256()
    for row_digest in row_digests:
        encoded = row_digest.encode("ascii")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return len(row_digests), digest.hexdigest()


def _migration_schema_digest(payload: object) -> str:
    """对 sqlite_master、列和索引信息生成确定性 schema 摘要。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _migration_data_digest(tables: Iterable[_MigrationTableDigest]) -> str:
    """汇总每张用户表的行数、列形状和行摘要。"""
    payload = [table.record() for table in tables]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _migration_fingerprint_from_record(value: object) -> _MigrationDatabaseFingerprint:
    """严格解析 state 中的 fingerprint；损坏状态必须 fail closed。"""
    if not isinstance(value, Mapping):
        raise ValueError("fingerprint must be an object")
    user_version = value.get("user_version")
    integrity_check = value.get("integrity_check")
    schema_digest = value.get("schema_digest")
    data_digest = value.get("data_digest")
    raw_tables = value.get("tables")
    if (
        not isinstance(user_version, int)
        or isinstance(user_version, bool)
        or not isinstance(integrity_check, str)
        or not isinstance(schema_digest, str)
        or not isinstance(data_digest, str)
        or not isinstance(raw_tables, list)
    ):
        raise ValueError("fingerprint fields are invalid")
    tables: list[_MigrationTableDigest] = []
    for raw_table in raw_tables:
        if not isinstance(raw_table, Mapping):
            raise ValueError("fingerprint table is invalid")
        name = raw_table.get("name")
        columns = raw_table.get("columns")
        row_count = raw_table.get("row_count")
        digest = raw_table.get("digest")
        if (
            not isinstance(name, str)
            or not isinstance(columns, list)
            or not all(isinstance(column, str) for column in columns)
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
            or not isinstance(digest, str)
        ):
            raise ValueError("fingerprint table fields are invalid")
        tables.append(
            _MigrationTableDigest(
                name=name,
                columns=tuple(columns),
                row_count=row_count,
                digest=digest,
            )
        )
    return _MigrationDatabaseFingerprint(
        user_version=user_version,
        integrity_check=integrity_check,
        schema_digest=schema_digest,
        data_digest=data_digest,
        tables=tuple(tables),
    )


def _migration_fingerprint_matches(
    expected: _MigrationDatabaseFingerprint,
    actual: _MigrationDatabaseFingerprint,
) -> bool:
    """比较 backup/恢复快照的证明字段，不接受仅版本相同的弱校验。"""
    return (
        expected.user_version == actual.user_version
        and expected.integrity_check == actual.integrity_check == "ok"
        and expected.schema_digest == actual.schema_digest
        and expected.data_digest == actual.data_digest
        and expected.tables == actual.tables
    )


def _migration_sqlite_schema_payload(connection: sqlite3.Connection) -> object:
    """同步收集 sqlite_master、表列和索引列形状。"""
    master_rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
          AND type IN ('table', 'index', 'trigger', 'view')
        ORDER BY type, name
        """
    ).fetchall()
    tables: list[dict[str, object]] = []
    for row in master_rows:
        if row[0] != "table":
            continue
        table_name = str(row[1])
        columns = connection.execute(
            f"PRAGMA table_info({_migration_identifier(table_name)})"
        ).fetchall()
        indexes = connection.execute(
            f"PRAGMA index_list({_migration_identifier(table_name)})"
        ).fetchall()
        tables.append(
            {
                "name": table_name,
                "columns": [tuple(column) for column in columns],
                "indexes": [
                    {
                        "row": tuple(index),
                        "columns": [
                            tuple(index_column)
                            for index_column in connection.execute(
                                f"PRAGMA index_info({_migration_identifier(str(index[1]))})"
                            ).fetchall()
                        ],
                    }
                    for index in indexes
                ],
            }
        )
    return {
        "master": [tuple(row) for row in master_rows],
        "tables": tables,
    }


def _migration_table_digest_sync(
    connection: sqlite3.Connection,
    table_name: str,
    columns: tuple[str, ...] | None = None,
) -> _MigrationTableDigest:
    """同步计算一张表的行数和 typed digest。"""
    if columns is None:
        raw_columns = connection.execute(
            f"PRAGMA table_info({_migration_identifier(table_name)})"
        ).fetchall()
        columns = tuple(str(row[1]) for row in raw_columns)
    select_columns = ", ".join(_migration_identifier(column) for column in columns)
    rows = connection.execute(
        f"SELECT {select_columns} FROM {_migration_identifier(table_name)}"
    ).fetchall()
    row_count, digest = _migration_rows_digest(rows)
    return _MigrationTableDigest(table_name, columns, row_count, digest)


def _migration_table_rows_sync(
    backup_path: Path,
    table_name: str,
    columns: tuple[str, ...],
) -> list[tuple[object, ...]]:
    """从已验证 backup 读取迁移前行，供有意转换表做逐行比对。"""
    select_columns = ", ".join(_migration_identifier(column) for column in columns)
    connection = sqlite3.connect(backup_path)
    try:
        return [
            tuple(row)
            for row in connection.execute(
                f"SELECT {select_columns} FROM {_migration_identifier(table_name)}"
            ).fetchall()
        ]
    finally:
        connection.close()


def _migration_database_fingerprint_sync(
    connection: sqlite3.Connection,
) -> _MigrationDatabaseFingerprint:
    """同步计算 backup/恢复目标的完整证明摘要。"""
    integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
    integrity = str(integrity_row[0]) if integrity_row else ""
    version_row = connection.execute("PRAGMA user_version").fetchone()
    version = int(version_row[0]) if version_row else 0
    master_rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    tables = tuple(
        _migration_table_digest_sync(connection, str(row[0])) for row in master_rows
    )
    return _MigrationDatabaseFingerprint(
        user_version=version,
        integrity_check=integrity,
        schema_digest=_migration_schema_digest(_migration_sqlite_schema_payload(connection)),
        data_digest=_migration_data_digest(tables),
        tables=tables,
    )


class ThreadPersistenceError(RuntimeError):
    """线程存储不可用、损坏或版本不兼容时返回的可诊断错误。"""


def _restrict_owner_mode(descriptor: int) -> None:
    """把文件收窄为所有者读写；Windows 没有 fchmod，权限由创建时的 ACL 决定。"""
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)


def _fsync_directory_best_effort(directory: Path) -> None:
    """目录 fsync 仅 POSIX 可用；Windows 依赖 NTFS 日志保证崩溃一致性。"""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file_path(path: Path) -> None:
    """把文件内容刷到磁盘；Windows 的 FlushFileBuffers 要求写权限句柄。"""
    if os.name == "nt":
        # POSIX 允许对只读句柄 fsync；Windows 上只读句柄会报 EBADF，
        # 因此以可写方式重新打开同一文件再刷盘。
        descriptor = os.open(path, os.O_RDWR)
    else:
        descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _MigrationFileLock:
    """跨进程串行化数据库 open/migration/recovery 的短生命周期锁。"""

    def __init__(self, path: Path) -> None:
        """保存锁文件路径；真正加锁在异步 open 中完成。"""
        self.path = path
        self._descriptor: int | None = None
        self._fcntl: Any | None = None
        self._msvcrt: Any | None = None

    async def acquire(self) -> None:
        """以非阻塞轮询等待锁，避免同一事件循环中的 opener 互相死锁。"""
        if os.name == "nt":
            # Windows 没有 flock；改用 msvcrt 对锁文件首字节的非阻塞区间锁。
            import msvcrt

            self._msvcrt = msvcrt
        else:
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - POSIX 平台均提供 fcntl。
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LOCK_UNAVAILABLE") from exc
            self._fcntl = fcntl
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            _restrict_owner_mode(descriptor)
        except OSError as exc:
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LOCK_UNAVAILABLE") from exc
        self._descriptor = descriptor
        try:
            while True:
                try:
                    if self._msvcrt is not None:
                        # msvcrt.locking 按当前文件位置加锁，先回到首字节。
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        self._msvcrt.locking(descriptor, self._msvcrt.LK_NBLCK, 1)
                    else:
                        self._fcntl.flock(descriptor, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                    return
                # POSIX 竞争抛 BlockingIOError；Windows 竞争抛 EACCES（PermissionError）。
                except (BlockingIOError, PermissionError):
                    await asyncio.sleep(0.01)
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        """释放并关闭锁文件，不删除锁文件避免 inode 竞态。"""
        descriptor, fcntl, msvcrt = self._descriptor, self._fcntl, self._msvcrt
        self._descriptor = None
        self._fcntl = None
        self._msvcrt = None
        if descriptor is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif msvcrt is not None:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    # 解锁失败不阻碍关闭；进程退出时系统会回收区间锁。
                    pass
        finally:
            os.close(descriptor)


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
    """自动压缩熔断和最近一次真实运行态。"""

    failures: int = 0
    circuit_open: bool = False
    last_action: str = "none"
    runtime_state: RuntimeStateSnapshot | None = None


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
        path: Path | None = None
        connection: aiosqlite.Connection | None = None
        migration_lock: _MigrationFileLock | None = None
        try:
            data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(data_dir, 0o700)
            except PermissionError:
                # 受限沙箱可能禁止对已存在目录重复 chmod；目录本身仍必须保持
                # 用户私有，否则继续失败而不掩盖真正的安全问题。
                if stat.S_IMODE(data_dir.stat().st_mode) & 0o077:
                    raise
            path = data_dir / "threads.sqlite3"
            _assert_migration_path_available(path)
            migration_lock = _MigrationFileLock(
                path.with_name(path.name + _MIGRATION_LOCK_SUFFIX)
            )
            await migration_lock.acquire()
            # The first check is only a fast rejection.  A waiter may have
            # passed it while another opener still owned the file lock; the
            # second check is the actual handoff boundary and must happen
            # before any recovery, SQLite connection, backup, or write.
            _assert_migration_path_available(path)
            # Recovery 必须在 migration lock 仍由当前 opener 持有时完成；这里
            # 不把同步的 SQLite backup 丢到可被取消的后台线程，避免提前释放锁。
            cls._recover_interrupted_migration_sync(path)
            _assert_migration_path_available(path)
            project_fingerprint = _project_fingerprint(project)
            source_version, has_legacy_prompt_epoch = _inspect_migration_source_sync(path)
            if has_legacy_prompt_epoch and not _is_supported_legacy_prompt_epoch_source(
                source_version
            ):
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_LEGACY_TABLE_UNEXPECTED"
                )
            # A brand-new empty user database is not a legacy migration and
            # stays on the normal parent bootstrap path.  Only an existing
            # historical schema (including the public v6 source) enters the
            # killable child boundary.
            if path.is_file() and (
                (0 < source_version < _SCHEMA_VERSION) or has_legacy_prompt_epoch
            ):
                await _run_legacy_migration_child(
                    path,
                    project_fingerprint,
                )
                # A child can finish or be killed immediately before its
                # response.  Recheck both the process-local handoff and the
                # durable database fact before opening the service connection.
                _assert_migration_path_available(path)
                source_version, has_legacy_prompt_epoch = _inspect_migration_source_sync(path)
                if source_version != _SCHEMA_VERSION or has_legacy_prompt_epoch:
                    raise ThreadPersistenceError(
                        "CHECKPOINT_MIGRATION_FINAL_VALIDATION_FAILED"
                    )
            connection = await aiosqlite.connect(path)
            try:
                os.chmod(path, 0o600)
            except PermissionError:
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise
            connection.row_factory = aiosqlite.Row
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
        finally:
            if migration_lock is not None:
                migration_lock.release()

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

    def team_state_store(self) -> "SqliteTeamStateStore":
        """返回借用同一连接和事务锁的项目级 Team 状态存储。"""
        self._ensure_open()
        from harness_agent.runtime.team_coordinator import SqliteTeamStateStore

        return SqliteTeamStateStore(
            self._connection,
            project_fingerprint=self._project_fingerprint,
            lock=self._lock,
        )

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

    async def load_latest_context_snapshot(
        self, thread_id: str
    ) -> RunContextSnapshot | None:
        """返回当前 Thread 最近一次已受理 Run 的上下文快照。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT snapshot_id
                    FROM harness_run_context_snapshots
                    WHERE project_fingerprint = ? AND thread_id = ?
                    ORDER BY created_at_ms DESC, snapshot_id DESC
                    LIMIT 1
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                return None
            return await self.load_context_snapshot(
                str(row["snapshot_id"]), thread_id=thread_id
            )
        except ThreadPersistenceError:
            raise
        except (aiosqlite.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError(
                f"RUN_CONTEXT_SNAPSHOT_READ_FAILED: {exc}"
            ) from exc

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
              AND record_id != ? AND run_id IS NULL
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

    async def load_langgraph_state(self, thread_id: str) -> Mapping[str, object]:
        """读取当前根图的结构化 channel，供运行态恢复器消费。"""
        self._ensure_open()
        try:
            checkpoint = await self._checkpointer.aget_tuple(
                self.graph_config(thread_id)
            )
            if checkpoint is None or not isinstance(checkpoint.checkpoint, Mapping):
                return {}
            channels = checkpoint.checkpoint.get("channel_values")
            if not isinstance(channels, Mapping):
                return {}
            return dict(channels)
        except (aiosqlite.Error, TypeError, ValueError) as exc:
            raise ThreadPersistenceError(f"LANGGRAPH_STATE_READ_FAILED: {exc}") from exc

    async def load_context_state(self, thread_id: str) -> ContextState:
        """读取压缩策略状态，不触及 LangGraph messages 缓存。"""
        return await self._load_context_state(thread_id)

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
                            last_action, runtime_state, updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project_fingerprint, thread_id) DO UPDATE SET
                            failures = excluded.failures,
                            circuit_open = excluded.circuit_open,
                            last_action = excluded.last_action,
                            runtime_state = excluded.runtime_state,
                            updated_at_ms = excluded.updated_at_ms
                        """,
                        (
                            self._project_fingerprint,
                            command.thread_id,
                            command.state.failures,
                            int(command.state.circuit_open),
                            command.state.last_action,
                            _strict_json(
                                command.state.runtime_state.record()
                                if command.state.runtime_state is not None
                                else {}
                            ),
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
        """返回压缩熔断和结构化运行态；缺失记录按空状态初始化。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT failures, circuit_open, last_action, runtime_state
                    FROM harness_context_state
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                return ContextState()
            encoded_state = row["runtime_state"]
            decoded_state = strict_json_loads(str(encoded_state)) if encoded_state else {}
            runtime_state = (
                None
                if decoded_state in ({}, None)
                else RuntimeStateSnapshot.from_record(decoded_state)
            )
            return ContextState(
                failures=int(row["failures"]),
                circuit_open=bool(row["circuit_open"]),
                last_action=str(row["last_action"]),
                runtime_state=runtime_state,
            )
        except (aiosqlite.Error, RuntimeStateError, TypeError, ValueError, json.JSONDecodeError) as exc:
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

    async def load_thread_activity_ms(self, thread_id: str) -> int | None:
        """读取当前 project/thread 的最后活动时间，缺失时返回 ``None``。"""
        self._ensure_open()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT updated_at_ms
                    FROM harness_threads
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                return None
            value = row["updated_at_ms"]
            return value if isinstance(value, int) and not isinstance(value, bool) else None
        except (aiosqlite.Error, TypeError, ValueError) as exc:
            raise ThreadPersistenceError(f"THREAD_ACTIVITY_READ_FAILED: {exc}") from exc

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
        _assert_migration_path_available(self._path)
        self._closed = True
        try:
            async with self._lock:
                await self._connection.commit()
                await self._connection.close()
        except aiosqlite.Error as exc:
            raise ThreadPersistenceError(f"CHECKPOINT_CLOSE_FAILED: {exc}") from exc

    async def _prepare(self) -> None:
        """在单一排他状态机内校验、备份、迁移并验证 Harness schema。"""
        migration_state_written = False
        migration_committed = False
        migration_commit_poisoned = False
        migration_backup: Path | None = None
        source_fingerprint: _MigrationDatabaseFingerprint | None = None
        final_fingerprint: _MigrationDatabaseFingerprint | None = None
        try:
            # busy_timeout 是连接级设置；它不改变数据库文件，真正的
            # schema/data/journal 边界从 BEGIN IMMEDIATE 开始。
            await self._connection.execute("PRAGMA busy_timeout=5000")
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
            except aiosqlite.Error as exc:
                if "not a database" in str(exc).lower():
                    raise ThreadPersistenceError("CHECKPOINT_DATABASE_CORRUPT") from exc
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LOCK_FAILED") from exc

            # 合并前旧分支曾把尚未建立 Transcript 表的 v6 数据库标成 v7。
            # 只在内存中把迁移步骤解释为 v6；原始 fingerprint 仍保留真实的
            # user_version=7，保证 backup/recovery 能逐字节证明源库没有被改写。
            pre_transcript_legacy = await self._is_pre_transcript_prompt_epoch_source()
            source_fingerprint = await self._database_fingerprint_async()
            source_version = 6 if pre_transcript_legacy else source_fingerprint.user_version
            if source_version > _SCHEMA_VERSION:
                raise ThreadPersistenceError(
                    f"CHECKPOINT_SCHEMA_TOO_NEW: found {source_version}, supports {_SCHEMA_VERSION}"
                )
            await self._validate_required_schema_async(source_version)
            has_legacy_prompt_epoch = await self._table_exists("harness_prompt_epochs")
            if has_legacy_prompt_epoch and not _is_supported_legacy_prompt_epoch_source(
                source_version
            ):
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_LEGACY_TABLE_UNEXPECTED"
                )
            if (
                (0 < source_version < _SCHEMA_VERSION or has_legacy_prompt_epoch)
                and not _MIGRATION_CHILD_PROCESS_MODE
            ):
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_WORKER_REQUIRED")
            if source_version == _SCHEMA_VERSION and not has_legacy_prompt_epoch:
                await self._validate_final_database_async(source_fingerprint)
                await self._connection.commit()
                self._checkpointer.is_setup = True
                return

            # 从此处到最终 commit，所有 writer 都被同一 SQLite 写事务阻止；
            # backup 看到的是含已提交 WAL 内容的同一个稳定快照。
            migration_backup = await self._create_migration_backup(
                source_fingerprint.user_version,
                source_fingerprint,
            )
            await self._write_migration_state(
                status="migrating",
                source_fingerprint=source_fingerprint,
                backup_path=migration_backup,
            )
            migration_state_written = True

            # legacy migration 现在整体运行在隔离 child 中。测试故障注入必须
            # 位于统一事务边界，不能依赖 monkeypatch 某个只在特定旧版本调用的
            # bootstrap 方法，否则 v8 等版本会绕过真实失败路径。
            _migration_child_test_failure(
                "bootstrap_failure",
                RuntimeError("injected migration bootstrap failure"),
            )

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
                        runtime_state TEXT NOT NULL DEFAULT '{}',
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
                if has_legacy_prompt_epoch:
                    await self._migrate_legacy_prompt_epochs_to_snapshots()
                version = 8
            if has_legacy_prompt_epoch:
                await self._finalize_legacy_prompt_epoch_adapter()
            elif source_version < 2 and await self._table_exists("harness_prompt_epochs"):
                # 旧空 schema 在本次事务内创建的表不是 legacy input；只清理
                # 迁移自产生的空表，绝不把它当作 PromptEpoch adapter 输入。
                await self._connection.execute("DROP TABLE harness_prompt_epochs")
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
            if version < 11:
                await self._add_context_runtime_state_column()
                version = 11
            await self._connection.execute(f"PRAGMA user_version={version}")
            final_fingerprint = await self._database_fingerprint_async()
            await self._validate_final_database_async(final_fingerprint)
            if source_fingerprint is not None:
                if migration_backup is None:
                    raise ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_MISSING")
                await self._validate_preserved_source_data_async(
                    source_fingerprint,
                    migration_backup,
                )
            await _migration_child_pause_if_requested("after_final_validation")
            if migration_backup is None or source_fingerprint is None:
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_MISSING")
            # final fingerprint 必须在 DB commit 之前落盘。这样即使进程在
            # commit 返回后、写 committed/清理 state 前退出，启动也能用
            # “当前主库 == 这个 final”证明新库已经提交，绝不把成功迁移回滚。
            await self._write_migration_state(
                status="committing",
                source_fingerprint=source_fingerprint,
                backup_path=migration_backup,
                final_fingerprint=final_fingerprint,
            )
            await _migration_child_pause_if_requested("before_commit")
            commit_outcome, commit_error = await self._commit_and_classify_outcome_async(
                source_fingerprint,
                final_fingerprint,
            )
            if commit_outcome == "unknown":
                migration_commit_poisoned = True
                _MIGRATION_CHILD_POISONED_PATHS.add(self._path)
                await self._write_migration_state(
                    status="commit_unknown",
                    source_fingerprint=source_fingerprint,
                    backup_path=migration_backup,
                    final_fingerprint=final_fingerprint,
                )
                raise commit_error or ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_COMMIT_OUTCOME_UNKNOWN"
                )
            if commit_outcome == "source":
                if commit_error is None:
                    raise ThreadPersistenceError(
                        "CHECKPOINT_MIGRATION_COMMIT_NOT_APPLIED"
                    )
                raise commit_error
            if commit_outcome != "final":
                if commit_error is not None:
                    raise commit_error
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_COMMIT_OUTCOME_UNKNOWN"
                )
            migration_committed = True
            await _migration_child_pause_if_requested("after_commit_before_state")
            await self._write_migration_state(
                status="committed",
                source_fingerprint=source_fingerprint,
                backup_path=migration_backup,
                final_fingerprint=final_fingerprint,
            )
            await self._clear_migration_state()
            if commit_error is not None:
                if isinstance(commit_error, (asyncio.CancelledError, ThreadPersistenceError)):
                    raise commit_error
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_FINALIZE_FAILED"
                ) from commit_error
            self._checkpointer.is_setup = True
        except BaseException as exc:
            if migration_commit_poisoned:
                # commit worker 仍可能在另一个线程中晚到；当前 owner 不得
                # rollback/restore/close 或复用同一路径，只释放外层 open 锁。
                if isinstance(exc, (asyncio.CancelledError, ThreadPersistenceError)):
                    raise
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_COMMIT_OUTCOME_UNKNOWN"
                ) from exc
            if migration_committed:
                # DB 已经提交；状态发布或清理失败只能 fail closed，绝不能
                # rollback/restore 旧 backup。保留 committing/committed state
                # 让下一次 open 按 final fingerprint 修复边界。
                if isinstance(exc, (asyncio.CancelledError, ThreadPersistenceError)):
                    raise
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_FINALIZE_FAILED"
                ) from exc
            try:
                await self._connection.rollback()
            except aiosqlite.Error:
                pass
            if migration_state_written and migration_backup is not None and source_fingerprint is not None:
                try:
                    await self._restore_migration_backup_async(
                        migration_backup,
                        source_fingerprint,
                    )
                    await self._clear_migration_state()
                except BaseException as restore_exc:
                    try:
                        self._mark_migration_restore_failed(restore_exc)
                    except BaseException:
                        pass
                    raise ThreadPersistenceError(
                        "CHECKPOINT_MIGRATION_RESTORE_FAILED"
                    ) from restore_exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, ThreadPersistenceError):
                raise
            raise ThreadPersistenceError(
                f"CHECKPOINT_MIGRATION_FAILED: {type(exc).__name__}"
            ) from exc

    async def _commit_and_classify_outcome_async(
        self,
        source: _MigrationDatabaseFingerprint,
        final: _MigrationDatabaseFingerprint,
    ) -> tuple[Literal["final", "source", "mismatch", "unknown"], BaseException | None]:
        """在有界 deadline 内 settle commit，之后才用独立连接确认落盘事实。"""
        _migration_child_test_failure(
            "commit_failure_before",
            RuntimeError("injected commit failure before sqlite commit"),
        )
        commit_task = asyncio.create_task(self._connection.commit())
        commit_error: BaseException | None = None
        deadline = asyncio.get_running_loop().time() + _MIGRATION_COMMIT_DEADLINE_SECONDS
        while not commit_task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return (
                    "unknown",
                    ThreadPersistenceError(
                        "CHECKPOINT_MIGRATION_COMMIT_OUTCOME_UNKNOWN"
                    ),
                )
            try:
                # wait_for 只取消 shield wrapper，不取消 commit_task；超时
                # 后 task 仍由 poisoned owner 跟踪，禁止任何 restore 竞态。
                await asyncio.wait_for(
                    asyncio.shield(commit_task),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return (
                    "unknown",
                    ThreadPersistenceError(
                        "CHECKPOINT_MIGRATION_COMMIT_OUTCOME_UNKNOWN"
                    ),
                )
            except BaseException as exc:
                # 外层取消和 worker await 异常都不能把结果解释成未提交；
                # 若 task 尚未结束，继续在同一个绝对 deadline 内观察。
                if commit_error is None:
                    commit_error = exc
        try:
            commit_task.result()
        except BaseException as exc:
            if commit_error is None:
                commit_error = exc
        outcome = self._classify_migration_database_sync(
            self._path,
            source,
            final,
        )
        return outcome, commit_error

    @staticmethod
    def _classify_migration_database_sync(
        path: Path,
        source: _MigrationDatabaseFingerprint,
        final: _MigrationDatabaseFingerprint,
    ) -> Literal["final", "source", "mismatch"]:
        """独立连接只按完整 fingerprint/schema 事实分类 commit outcome。"""
        if not path.is_file():
            return "mismatch"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path)
            actual = _migration_database_fingerprint_sync(connection)
            if _migration_fingerprint_matches(final, actual):
                if final.user_version != _SCHEMA_VERSION or final.integrity_check != "ok":
                    return "mismatch"
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='harness_prompt_epochs'"
                ).fetchone() is not None:
                    return "mismatch"
                return "final"
            if _migration_fingerprint_matches(source, actual):
                _validate_source_schema_for_fingerprint_sync(connection, source.user_version)
                return "source"
        except (OSError, sqlite3.Error, ThreadPersistenceError):
            return "mismatch"
        finally:
            if connection is not None:
                connection.close()
        return "mismatch"

    async def _database_fingerprint_async(self) -> _MigrationDatabaseFingerprint:
        """在当前 SQLite 连接和当前事务快照上计算完整证明摘要。"""
        integrity_cursor = await self._connection.execute("PRAGMA integrity_check")
        integrity_row = await integrity_cursor.fetchone()
        await integrity_cursor.close()
        version_cursor = await self._connection.execute("PRAGMA user_version")
        version_row = await version_cursor.fetchone()
        await version_cursor.close()
        master_cursor = await self._connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'index', 'trigger', 'view')
            ORDER BY type, name
            """
        )
        master_rows = [tuple(row) for row in await master_cursor.fetchall()]
        await master_cursor.close()
        schema_tables: list[dict[str, object]] = []
        table_names = [str(row[1]) for row in master_rows if row[0] == "table"]
        tables: list[_MigrationTableDigest] = []
        for table_name in table_names:
            column_cursor = await self._connection.execute(
                f"PRAGMA table_info({_migration_identifier(table_name)})"
            )
            column_rows = [tuple(row) for row in await column_cursor.fetchall()]
            await column_cursor.close()
            columns = tuple(str(row[1]) for row in column_rows)
            index_cursor = await self._connection.execute(
                f"PRAGMA index_list({_migration_identifier(table_name)})"
            )
            index_rows = [tuple(row) for row in await index_cursor.fetchall()]
            await index_cursor.close()
            schema_tables.append(
                {
                    "name": table_name,
                    "columns": column_rows,
                    "indexes": [
                        {
                            "row": index,
                            "columns": await self._index_columns_async(str(index[1])),
                        }
                        for index in index_rows
                    ],
                }
            )
            tables.append(await self._table_digest_async(table_name, columns))
        schema_payload = {"master": master_rows, "tables": schema_tables}
        table_tuple = tuple(tables)
        return _MigrationDatabaseFingerprint(
            user_version=int(version_row[0]) if version_row else 0,
            integrity_check=str(integrity_row[0]) if integrity_row else "",
            schema_digest=_migration_schema_digest(schema_payload),
            data_digest=_migration_data_digest(table_tuple),
            tables=table_tuple,
        )

    async def _index_columns_async(self, index_name: str) -> list[tuple[object, ...]]:
        """读取一个索引的列序，不把索引名拼接成可执行输入。"""
        cursor = await self._connection.execute(
            f"PRAGMA index_info({_migration_identifier(index_name)})"
        )
        rows = [tuple(row) for row in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _table_columns_async(self, table_name: str) -> tuple[str, ...]:
        """读取表列名，供 schema contract 和数据保留校验复用。"""
        cursor = await self._connection.execute(
            f"PRAGMA table_info({_migration_identifier(table_name)})"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple(str(row[1]) for row in rows)

    async def _table_digest_async(
        self,
        table_name: str,
        columns: tuple[str, ...] | None = None,
    ) -> _MigrationTableDigest:
        """在当前事务快照中计算指定列的行摘要。"""
        selected = columns or await self._table_columns_async(table_name)
        if not selected:
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_SCHEMA_INVALID")
        select_columns = ", ".join(_migration_identifier(column) for column in selected)
        cursor = await self._connection.execute(
            f"SELECT {select_columns} FROM {_migration_identifier(table_name)}"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        row_count, digest = _migration_rows_digest(rows)
        return _MigrationTableDigest(table_name, selected, row_count, digest)

    async def _validate_legacy_source_schema_async(self, source_version: int) -> None:
        """异步读取 legacy v1-v6 schema 的完整 contract，拒绝未知对象。"""
        cursor = await self._connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        actual_tables = {str(row[0]) for row in await cursor.fetchall()}
        await cursor.close()
        required = _migration_legacy_required_tables(source_version)
        allowed_tables = set(required)
        # 见同步校验：Team 状态表是已知扩展对象，不参与 Thread schema 版本。
        allowed_tables.add("harness_team_runs")
        if source_version >= 2:
            allowed_tables.add("harness_prompt_epochs")
        if actual_tables - allowed_tables:
            raise _migration_schema_contract_error("<database>", "unexpected_table")
        for table_name in required:
            if table_name not in actual_tables:
                raise _migration_schema_contract_error(table_name, "missing_table")
        if "harness_prompt_epochs" in actual_tables:
            await self._validate_table_contract_async(
                "harness_prompt_epochs",
                max(source_version, 2),
            )
        if "harness_team_runs" in actual_tables:
            await self._validate_table_contract_async(
                "harness_team_runs",
                source_version,
            )
        for table_name in required:
            await self._validate_table_contract_async(table_name, source_version)

    async def _validate_table_contract_async(
        self,
        table_name: str,
        source_version: int,
    ) -> None:
        """读取单表 sqlite_master/PRAGMA 并复用同步 contract 比较器。"""
        cursor = await self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        table_row = await cursor.fetchone()
        await cursor.close()
        if table_row is None:
            raise _migration_schema_contract_error(table_name, "missing_table")
        quoted_table = _migration_identifier(table_name)
        cursor = await self._connection.execute(f"PRAGMA index_list({quoted_table})")
        index_rows = [tuple(row) for row in await cursor.fetchall()]
        await cursor.close()
        index_xinfo: dict[str, list[tuple[object, ...]]] = {}
        index_sql: dict[str, str | None] = {}
        for index_row in index_rows:
            index_name = str(index_row[1])
            cursor = await self._connection.execute(
                f"PRAGMA index_xinfo({_migration_identifier(index_name)})"
            )
            index_xinfo[index_name] = [tuple(row) for row in await cursor.fetchall()]
            await cursor.close()
            cursor = await self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            )
            index_master_row = await cursor.fetchone()
            await cursor.close()
            index_sql[index_name] = (
                str(index_master_row[0]) if index_master_row and index_master_row[0] is not None else None
            )
        cursor = await self._connection.execute(f"PRAGMA table_xinfo({quoted_table})")
        column_rows = [tuple(row) for row in await cursor.fetchall()]
        await cursor.close()
        cursor = await self._connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
        foreign_keys = [tuple(row) for row in await cursor.fetchall()]
        await cursor.close()
        _migration_compare_table_contract(
            table_name=table_name,
            source_version=source_version,
            table_sql=str(table_row[0]) if table_row[0] is not None else None,
            column_rows=column_rows,
            index_rows=index_rows,
            index_xinfo=index_xinfo,
            index_sql=index_sql,
            foreign_keys=foreign_keys,
        )

    async def _validate_required_schema_async(self, version: int) -> None:
        """校验源版本的关键表、列和索引，拒绝半套 schema 进入 backup。"""
        if 1 <= version <= 6:
            await self._validate_legacy_source_schema_async(version)
            return
        required: list[tuple[str, tuple[str, ...]]] = []
        if version >= 1:
            required.extend(
                [
                    (
                        "harness_threads",
                        (
                            "project_fingerprint",
                            "thread_id",
                            "created_at_ms",
                            "updated_at_ms",
                            "first_message",
                            "latest_message",
                            "message_count",
                        ),
                    ),
                    (
                        "checkpoints",
                        ("thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint"),
                    ),
                    (
                        "writes",
                        ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
                    ),
                ]
            )
        if version >= 2:
            required.extend(
                [
                    (
                        "harness_context_artifacts",
                        (
                            "project_fingerprint",
                            "thread_id",
                            "artifact_id",
                            "kind",
                            "content",
                            "source_start",
                            "source_end",
                        ),
                    ),
                    (
                        "harness_context_summaries",
                        (
                            "project_fingerprint",
                            "thread_id",
                            "summary_id",
                            "content",
                            "source_start",
                            "source_end",
                            "artifact_ids",
                        ),
                    ),
                    (
                        "harness_context_state",
                        (
                            "project_fingerprint",
                            "thread_id",
                            "failures",
                            "circuit_open",
                            "last_action",
                            "updated_at_ms",
                        ),
                    ),
                ]
            )
        if version >= 4:
            required.extend(
                [
                    (
                        "harness_runtime_profiles",
                        ("project_fingerprint", "profile_key", "profile_record"),
                    ),
                    (
                        "harness_thread_runtime_profiles",
                        ("project_fingerprint", "thread_id", "profile_key"),
                    ),
                ]
            )
        if version >= 5:
            required.append(
                (
                    "harness_thread_model_bindings",
                    ("project_fingerprint", "thread_id", "binding_record"),
                )
            )
        if version >= 6:
            required.append(
                (
                    "harness_run_execution_bindings",
                    (
                        "project_fingerprint",
                        "thread_id",
                        "run_id",
                        "requested_selection",
                        "actual_primary_binding",
                        "runtime_profile_id",
                        "message_digest",
                    ),
                )
            )
        if version >= 7:
            required.extend(
                [
                    (
                        "harness_thread_transcript",
                        (
                            "project_fingerprint",
                            "thread_id",
                            "record_id",
                            "sequence",
                            "kind",
                            "payload",
                            "content_sha256",
                            "byte_length",
                        ),
                    ),
                    (
                        "harness_thread_history_metadata",
                        (
                            "project_fingerprint",
                            "thread_id",
                            "legacy_incomplete_history",
                            "source_schema_version",
                        ),
                    ),
                ]
            )
        if version >= 8:
            required.extend(
                [
                    (
                        "harness_run_context_snapshots",
                        (
                            "project_fingerprint",
                            "snapshot_id",
                            "thread_id",
                            "snapshot_record",
                            "system_fingerprint",
                            "legacy",
                        ),
                    ),
                    (
                        "harness_run_execution_bindings",
                        ("context_snapshot_id",),
                    ),
                ]
            )
        if version >= 9:
            required.append(
                (
                    "harness_compression_checkpoints",
                    (
                        "project_fingerprint",
                        "thread_id",
                        "checkpoint_id",
                        "source_record_sequence",
                        "source_digest",
                        "mode",
                        "projected_messages",
                    ),
                )
            )
        if version >= 10:
            required.append(("harness_compression_checkpoints", ("commit_payload",)))
        if version >= 11:
            required.append(("harness_context_state", ("runtime_state",)))

        for table_name, expected_columns in required:
            actual_columns = await self._table_columns_async(table_name)
            if not actual_columns or any(column not in actual_columns for column in expected_columns):
                raise ThreadPersistenceError(
                    f"CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:{table_name}"
                )
        if await self._table_exists("harness_prompt_epochs"):
            prompt_columns = await self._table_columns_async("harness_prompt_epochs")
            if any(
                column not in prompt_columns
                for column in ("project_fingerprint", "thread_id", "system_prompt", "created_at_ms")
            ):
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:harness_prompt_epochs"
                )
        required_indexes = []
        if version >= 1:
            required_indexes.append("harness_threads_project_updated")
        if version >= 2:
            required_indexes.append("harness_context_artifacts_thread_created")
        if version >= 4:
            required_indexes.append("harness_thread_runtime_profiles_project_profile")
        if version >= 6:
            required_indexes.append("harness_run_execution_bindings_thread_created")
        if version >= 7:
            required_indexes.append("harness_thread_transcript_thread_sequence")
        if version >= 8:
            required_indexes.append("harness_run_context_snapshots_thread_created")
        if version >= 9:
            required_indexes.append("harness_compression_checkpoints_latest")
        for index_name in required_indexes:
            cursor = await self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            )
            exists = await cursor.fetchone()
            await cursor.close()
            if exists is None:
                raise ThreadPersistenceError(
                    f"CHECKPOINT_MIGRATION_SOURCE_SCHEMA_INVALID:{index_name}"
                )

    async def _validate_final_database_async(
        self,
        fingerprint: _MigrationDatabaseFingerprint,
    ) -> None:
        """验证最终 schema、版本、完整性和一次性 legacy 表退出条件。"""
        if (
            fingerprint.user_version != _SCHEMA_VERSION
            or fingerprint.integrity_check != "ok"
        ):
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_FINAL_VALIDATION_FAILED")
        await self._validate_required_schema_async(_SCHEMA_VERSION)
        if await self._table_exists("harness_prompt_epochs"):
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LEGACY_TABLE_REMAINS")

    async def _validate_preserved_source_data_async(
        self,
        source: _MigrationDatabaseFingerprint,
        backup_path: Path,
    ) -> None:
        """确认旧数据逐行保留；仅放行有明确 canonical 转换的表。"""
        current_tables = {
            table.name: table for table in await self._database_tables_async()
        }
        for source_table in source.tables:
            if source_table.name == "harness_prompt_epochs":
                await self._validate_prompt_epoch_rows_async(backup_path, source_table)
                continue
            if source_table.name == "harness_threads":
                await self._validate_thread_rows_async(backup_path, source_table)
                continue
            if source_table.name in {
                "harness_compression_checkpoints",
                "harness_run_context_snapshots",
                "harness_thread_transcript",
                "harness_thread_history_metadata",
                wdl1
                "harness_context_artifacts",
                # transcript bootstrap 会把超过内联上限的旧工具输出提取为新
                # artifact；原 artifact 行逐字段保留，允许 canonical 追加。
                "harness_context_artifacts",
            }:
                await self._validate_append_only_table_async(backup_path, source_table)
                continue
            if source_table.name == "harness_run_execution_bindings":
                await self._validate_binding_rows_async(backup_path, source_table)
                continue
            if source_table.name not in current_tables:
                raise ThreadPersistenceError(
                    f"CHECKPOINT_MIGRATION_DATA_TABLE_MISSING:{source_table.name}"
                )
            current_columns = current_tables[source_table.name].columns
            if any(column not in current_columns for column in source_table.columns):
                raise ThreadPersistenceError(
                    f"CHECKPOINT_MIGRATION_DATA_COLUMNS_MISSING:{source_table.name}"
                )
            current = await self._table_digest_async(
                source_table.name,
                source_table.columns,
            )
            if (
                current.row_count != source_table.row_count
                or current.digest != source_table.digest
            ):
                raise ThreadPersistenceError(
                    f"CHECKPOINT_MIGRATION_DATA_CHANGED:{source_table.name}"
                )

    async def _validate_prompt_epoch_rows_async(
        self,
        backup_path: Path,
        source_table: _MigrationTableDigest,
    ) -> None:
        """逐行确认每个旧 PromptEpoch 只变成对应的 legacy snapshot。"""
        source_rows = _migration_table_rows_sync(
            backup_path,
            source_table.name,
            ("project_fingerprint", "thread_id", "system_prompt", "created_at_ms"),
        )
        for project, thread_id, system_prompt, created_at_ms in source_rows:
            expected = snapshot_from_legacy_prompt_epoch(
                project_fingerprint=str(project),
                thread_id=str(thread_id),
                system_prompt=str(system_prompt),
                created_at_ms=int(created_at_ms),
            )
            cursor = await self._connection.execute(
                """
                SELECT snapshot_id, snapshot_record, system_fingerprint, created_at_ms
                FROM harness_run_context_snapshots
                WHERE project_fingerprint = ? AND thread_id = ? AND legacy = 1
                """,
                (str(project), str(thread_id)),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LEGACY_DATA_LOSS")
            try:
                record = json.loads(str(row["snapshot_record"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_LEGACY_DATA_INVALID"
                ) from exc
            if (
                str(row["snapshot_id"]) != expected.snapshot_id
                or str(row["system_fingerprint"]) != expected.system_fingerprint
                or int(row["created_at_ms"]) != expected.created_at_ms
                or record != expected.record()
            ):
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LEGACY_DATA_CHANGED")

    async def _validate_thread_rows_async(
        self,
        backup_path: Path,
        source_table: _MigrationTableDigest,
    ) -> None:
        """校验 Thread 事实行；只允许索引摘要字段被 transcript 刷新。"""
        source_rows = _migration_table_rows_sync(
            backup_path,
            source_table.name,
            source_table.columns,
        )
        immutable_columns = (
            "project_fingerprint",
            "thread_id",
            "created_at_ms",
            "first_message",
        )
        indexes = {column: source_table.columns.index(column) for column in immutable_columns}
        for source_row in source_rows:
            cursor = await self._connection.execute(
                """
                SELECT project_fingerprint, thread_id, created_at_ms, first_message
                FROM harness_threads
                WHERE project_fingerprint = ? AND thread_id = ?
                """,
                (source_row[indexes["project_fingerprint"]], source_row[indexes["thread_id"]]),
            )
            current = await cursor.fetchone()
            await cursor.close()
            if current is None or tuple(current) != tuple(
                source_row[indexes[column]] for column in immutable_columns
            ):
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_DATA_CHANGED:harness_threads")

    async def _validate_append_only_table_async(
        self,
        backup_path: Path,
        source_table: _MigrationTableDigest,
    ) -> None:
        """校验只会由 canonical bootstrap 追加记录的旧表，禁止改写/删除原行。"""
        primary_keys = {
            "harness_context_artifacts": (
                "project_fingerprint",
                "thread_id",
                "artifact_id",
            ),
            "harness_compression_checkpoints": (
                "project_fingerprint",
                "thread_id",
                "checkpoint_id",
            ),
            "harness_run_context_snapshots": (
                "project_fingerprint",
                "snapshot_id",
            ),
            "harness_thread_transcript": (
                "project_fingerprint",
                "thread_id",
                "record_id",
            ),
            "harness_thread_history_metadata": (
                "project_fingerprint",
                "thread_id",
            ),
            "harness_context_artifacts": (
                "project_fingerprint",
                "thread_id",
                "artifact_id",
            ),
        }
        key_columns = primary_keys[source_table.name]
        key_indexes = {column: source_table.columns.index(column) for column in key_columns}
        select_columns = ", ".join(
            _migration_identifier(column) for column in source_table.columns
        )
        source_rows = _migration_table_rows_sync(
            backup_path,
            source_table.name,
            source_table.columns,
        )
        for source_row in source_rows:
            cursor = await self._connection.execute(
                f"SELECT {select_columns} FROM {_migration_identifier(source_table.name)} "
                f"WHERE "
                + " AND ".join(
                    f"{_migration_identifier(column)} = ?" for column in key_columns
                ),
                tuple(source_row[key_indexes[column]] for column in key_columns),
            )
            current = await cursor.fetchone()
            await cursor.close()
            if current is None or tuple(current) != source_row:
                raise ThreadPersistenceError(
                    f"CHECKPOINT_MIGRATION_DATA_CHANGED:{source_table.name}"
                )

    async def _validate_binding_rows_async(
        self,
        backup_path: Path,
        source_table: _MigrationTableDigest,
    ) -> None:
        """允许 adapter 只回填 binding 的 context_snapshot_id，其余字段逐行不变。"""
        key_columns = ("project_fingerprint", "thread_id", "run_id")
        ignored_columns = {"context_snapshot_id"}
        key_indexes = {column: source_table.columns.index(column) for column in key_columns}
        select_columns = ", ".join(
            _migration_identifier(column) for column in source_table.columns
        )
        source_rows = _migration_table_rows_sync(
            backup_path,
            source_table.name,
            source_table.columns,
        )
        for source_row in source_rows:
            cursor = await self._connection.execute(
                f"SELECT {select_columns} FROM {_migration_identifier(source_table.name)} "
                "WHERE "
                + " AND ".join(
                    f"{_migration_identifier(column)} = ?" for column in key_columns
                ),
                tuple(source_row[key_indexes[column]] for column in key_columns),
            )
            current = await cursor.fetchone()
            await cursor.close()
            if current is None:
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_DATA_CHANGED:harness_run_execution_bindings"
                )
            for index, column in enumerate(source_table.columns):
                if column not in ignored_columns and current[index] != source_row[index]:
                    raise ThreadPersistenceError(
                        "CHECKPOINT_MIGRATION_DATA_CHANGED:harness_run_execution_bindings"
                    )

    async def _database_tables_async(self) -> tuple[_MigrationTableDigest, ...]:
        """读取当前数据库所有用户表的摘要，避免重复依赖完整 fingerprint。"""
        cursor = await self._connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        names = [str(row[0]) for row in await cursor.fetchall()]
        await cursor.close()
        digests: list[_MigrationTableDigest] = []
        for name in names:
            digests.append(await self._table_digest_async(name))
        return tuple(digests)

    def _migration_state_path(self) -> Path:
        """返回当前数据库唯一 migration state 文件。"""
        return self._path.with_name(self._path.name + _MIGRATION_STATE_SUFFIX)

    def _migration_backup_path(self, source_version: int) -> Path:
        """返回固定备份槽，避免每次启动无限累积 backup。"""
        return self._path.with_name(
            f"{self._path.name}.pre-v{source_version}-migration.bak"
        )

    async def _create_migration_backup(
        self,
        source_version: int,
        source_fingerprint: _MigrationDatabaseFingerprint | None = None,
    ) -> Path:
        """在已持有 BEGIN IMMEDIATE 时生成并严格验证独立 SQLite backup。"""
        _migration_child_test_failure(
            "backup_failure",
            ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_FAILED"),
        )
        if not self._connection.in_transaction:
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BOUNDARY_REQUIRED")
        source = source_fingerprint or await self._database_fingerprint_async()
        backup_path = self._migration_backup_path(source_version)
        temporary = backup_path.with_name(
            f"{backup_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        source_connection: sqlite3.Connection | None = None
        target: sqlite3.Connection | None = None
        try:
            # Python sqlite3 cannot run backup from the same connection that
            # currently owns BEGIN IMMEDIATE: SQLite waits on its own write
            # transaction.  A second read connection observes the same
            # committed snapshot while that transaction prevents all writers;
            # both raw connections stay on this event-loop thread.
            source_connection = sqlite3.connect(self._path, timeout=5.0)
            target = sqlite3.connect(temporary)
            source_connection.backup(target)
            target.commit()
            target.close()
            target = None
            if any(
                temporary.with_name(temporary.name + suffix).exists()
                for suffix in ("-wal", "-shm", "-journal")
            ):
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_BACKUP_NOT_STANDALONE"
                )
            backup_connection = sqlite3.connect(temporary)
            try:
                backup = _migration_database_fingerprint_sync(backup_connection)
            finally:
                backup_connection.close()
            if source.user_version != source_version or not _migration_fingerprint_matches(
                source, backup
            ):
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_BACKUP_VALIDATION_FAILED"
                )
            os.chmod(temporary, 0o600)
            _fsync_file_path(temporary)
            os.replace(temporary, backup_path)
            _fsync_directory_best_effort(backup_path.parent)
            return backup_path
        except ThreadPersistenceError:
            raise

        except (OSError, sqlite3.Error, aiosqlite.Error) as exc:
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_FAILED") from exc
        finally:
            if source_connection is not None:
                source_connection.close()
            if target is not None:
                target.close()
            temporary.unlink(missing_ok=True)

    async def _is_pre_transcript_prompt_epoch_source(self) -> bool:
        """异步识别可按 v6 迁移的旧分支 v7 数据库，不修改原始版本号。"""
        cursor = await self._connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        source_version = int(row[0]) if row else 0
        if source_version != 7:
            return False
        if not await self._table_exists("harness_prompt_epochs"):
            return False
        for table_name in (
            "harness_thread_transcript",
            "harness_thread_history_metadata",
            "harness_run_context_snapshots",
            "harness_compression_checkpoints",
        ):
            if await self._table_exists(table_name):
                return False
        try:
            await self._validate_legacy_source_schema_async(6)
        except ThreadPersistenceError:
            # 保持当前 v7+ 异常残留的原有 fail-closed 行为；上层会返回
            # CHECKPOINT_MIGRATION_LEGACY_TABLE_UNEXPECTED。
            return False
        return True

    async def _write_migration_state(
        self,
        *,
        status: str,
        source_fingerprint: _MigrationDatabaseFingerprint,
        backup_path: Path,
        final_fingerprint: _MigrationDatabaseFingerprint | None = None,
    ) -> None:
        """原子写入迁移状态；状态写失败时禁止继续启动或迁移。"""
        if status == "committed" and _MIGRATION_CHILD_TEST_PHASE == "state_committed_failure":
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_WRITE_FAILED")
        payload: dict[str, object] = {
            "version": _MIGRATION_STATE_VERSION,
            "status": status,
            "database": self._path.name,
            "backup": backup_path.name,
            "source": source_fingerprint.record(),
        }
        if final_fingerprint is not None:
            payload["final"] = final_fingerprint.record()
        try:
            self._write_migration_state_sync(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_WRITE_FAILED") from exc

    def _write_migration_state_sync(self, payload: Mapping[str, object]) -> None:
        """以 fsync + 同目录 replace 持久化状态，避免半个 JSON。"""
        state_path = self._migration_state_path()
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{state_path.name}.",
                suffix=".tmp",
                dir=state_path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                _restrict_owner_mode(handle.fileno())
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state_path)
            temporary = None
            _fsync_directory_best_effort(state_path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _clear_migration_state(self) -> None:
        """删除已封口 state；删除失败保持 fail closed，下一次可重试。"""
        _migration_child_test_failure(
            "state_clear_failure",
            ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_CLEAR_FAILED"),
        )
        try:
            self._unlink_migration_state_sync()
        except OSError as exc:
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_CLEAR_FAILED") from exc

    def _unlink_migration_state_sync(self) -> None:
        """删除迁移状态并同步目录，避免已完成状态跨重启复活。"""
        state_path = self._migration_state_path()
        state_path.unlink(missing_ok=True)
        _fsync_directory_best_effort(state_path.parent)

    def _mark_migration_restore_failed(self, error: BaseException) -> None:
        """保留 backup 并记录稳定错误类型，供下一次启动确定性重试。"""
        state_path = self._migration_state_path()
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            raw["status"] = "restore_failed"
            raw["error"] = "CHECKPOINT_MIGRATION_RESTORE_FAILED"
            raw["error_type"] = type(error).__name__
            self._write_migration_state_sync(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return

    async def _restore_migration_backup_async(
        self,
        backup_path: Path,
        expected: _MigrationDatabaseFingerprint,
    ) -> None:
        """关闭旧连接后原子替换完整快照，不让旧 WAL/SHM 继续挂到目标上。"""
        self._validate_backup_file_sync(backup_path, expected)
        await self._connection.rollback()
        await self._connection.close()
        self._closed = True
        self._restore_backup_path_sync(self._path, backup_path, expected)

    def _validate_backup_file_sync(
        self,
        backup_path: Path,
        expected: _MigrationDatabaseFingerprint,
    ) -> None:
        """验证固定 backup 槽仍是可独立恢复的原始快照。"""
        if not backup_path.is_file():
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_UNAVAILABLE")
        if any(
            backup_path.with_name(backup_path.name + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_NOT_STANDALONE")
        connection = sqlite3.connect(backup_path)
        try:
            actual = _migration_database_fingerprint_sync(connection)
            _validate_source_schema_for_fingerprint_sync(connection, expected.user_version)
        finally:
            connection.close()
        if not _migration_fingerprint_matches(expected, actual):
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_VALIDATION_FAILED")

    @staticmethod
    def _validate_final_database_path_sync(
        path: Path,
        expected: _MigrationDatabaseFingerprint,
    ) -> None:
        """验证提交标记对应的最终库，不以 user_version 单独认定成功。"""
        if expected.user_version != _SCHEMA_VERSION or expected.integrity_check != "ok":
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_FINAL_VALIDATION_FAILED")
        connection = sqlite3.connect(path)
        try:
            actual = _migration_database_fingerprint_sync(connection)
            if not _migration_fingerprint_matches(expected, actual):
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_FINAL_VALIDATION_FAILED")
            _migration_validate_final_schema_sync(connection)
            legacy = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='harness_prompt_epochs'"
            ).fetchone()
            if legacy is not None:
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LEGACY_TABLE_REMAINS")
        finally:
            connection.close()

    @staticmethod
    def _validate_source_database_path_sync(
        path: Path,
        expected: _MigrationDatabaseFingerprint,
    ) -> None:
        """验证可重试的当前 v6 主库仍满足 source contract。"""
        connection = sqlite3.connect(path)
        try:
            actual = _migration_database_fingerprint_sync(connection)
            if not _migration_fingerprint_matches(expected, actual):
                raise ThreadPersistenceError("CHECKPOINT_MIGRATION_SOURCE_CHANGED")
            _validate_source_schema_for_fingerprint_sync(connection, expected.user_version)
        finally:
            connection.close()

    @staticmethod
    def _recover_interrupted_migration_sync(
        path: Path,
        *,
        preserve_recovery_state: bool = False,
    ) -> None:
        """启动前处理上次崩溃留下的 migration state；失败则不创建正常连接。"""
        _assert_migration_path_available(path)
        state_path = path.with_name(path.name + _MIGRATION_STATE_SUFFIX)
        if not state_path.exists():
            return
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != _MIGRATION_STATE_VERSION:
                raise ValueError("migration state version is invalid")
            if raw.get("database") != path.name:
                raise ValueError("migration state database is invalid")
            status = raw.get("status")
            if status not in {
                "migrating",
                "committing",
                "commit_unknown",
                "committed",
                "restore_failed",
            }:
                raise ValueError("migration state status is invalid")
            source = _migration_fingerprint_from_record(raw.get("source"))
            expected_backup = path.with_name(
                f"{path.name}.pre-v{source.user_version}-migration.bak"
            )
            if raw.get("backup") != expected_backup.name:
                raise ValueError("migration state backup is invalid")
            backup_path = expected_backup
            final_value = raw.get("final")
            final = (
                _migration_fingerprint_from_record(final_value)
                if final_value is not None
                else None
            )
            if status in {"committing", "commit_unknown", "committed"} and final is None:
                raise ValueError("committed migration state has no final fingerprint")

            current: _MigrationDatabaseFingerprint | None = None
            if path.is_file():
                try:
                    current_connection = sqlite3.connect(path)
                    try:
                        current = _migration_database_fingerprint_sync(current_connection)
                    finally:
                        current_connection.close()
                except (OSError, sqlite3.Error):
                    current = None

            # 第一优先级是证明主库已经提交。此分支不要求 backup 仍存在：
            # backup 清理可以在 state 清理前后失败，但绝不能把完整新库回滚。
            if (
                final is not None
                and current is not None
                and _migration_fingerprint_matches(final, current)
            ):
                ThreadPersistence._validate_final_database_path_sync(path, final)
                ThreadPersistence._unlink_migration_state_path_sync(state_path)
                return

            # 主库仍是严格 source，说明 DB commit 尚未落地或事务已安全回滚；
            # 清除过期 state 后让 canonical open 重新生成一次 verified backup。
            if current is not None and _migration_fingerprint_matches(source, current):
                ThreadPersistence._validate_backup_path_sync(backup_path, source)
                ThreadPersistence._validate_source_database_path_sync(path, source)
                # restore_failed is itself diagnostic durable state.  Keep it
                # until the next canonical child attempt overwrites it; a
                # verified exact source is safe to retry but must not erase
                # evidence that restore failed in the prior owner.
                if not preserve_recovery_state and status != "restore_failed":
                    ThreadPersistence._unlink_migration_state_path_sync(state_path)
                return

            # 只有主库既不是已证明最终库、也不是完整源库时，才验证并恢复
            # verified backup。半迁移/损坏库不能被 sidecar status 单独决定。
            ThreadPersistence._validate_backup_path_sync(backup_path, source)
            ThreadPersistence._restore_backup_path_sync(path, backup_path, source)
            if not preserve_recovery_state:
                ThreadPersistence._unlink_migration_state_path_sync(state_path)
        except ThreadPersistenceError as exc:
            ThreadPersistence._mark_restore_state_sync(state_path, exc)
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            ThreadPersistence._mark_restore_state_sync(
                state_path,
                ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_INVALID"),
            )
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_STATE_INVALID") from exc

    @staticmethod
    def _validate_backup_path_sync(
        backup_path: Path,
        expected: _MigrationDatabaseFingerprint,
    ) -> None:
        """无实例状态地验证启动恢复使用的 backup。"""
        if not backup_path.is_file():
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_UNAVAILABLE")
        if any(
            backup_path.with_name(backup_path.name + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_NOT_STANDALONE")
        connection = sqlite3.connect(backup_path)
        try:
            actual = _migration_database_fingerprint_sync(connection)
            _validate_source_schema_for_fingerprint_sync(connection, expected.user_version)
        finally:
            connection.close()
        if not _migration_fingerprint_matches(expected, actual):
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_BACKUP_VALIDATION_FAILED")

    @staticmethod
    def _restore_backup_path_sync(
        path: Path,
        backup_path: Path,
        expected: _MigrationDatabaseFingerprint,
    ) -> None:
        """把 backup 恢复到新文件并原子替换，清除旧目标的所有 journal sidecar。"""
        _migration_child_test_failure(
            "restore_failure",
            ThreadPersistenceError("CHECKPOINT_MIGRATION_RESTORE_VALIDATION_FAILED"),
        )
        ThreadPersistence._validate_backup_path_sync(backup_path, expected)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore.tmp")
        source = sqlite3.connect(backup_path)
        target: sqlite3.Connection | None = None
        try:
            target = sqlite3.connect(temporary)
            source.backup(target)
            target.commit()
            target.close()
            target = None
            for suffix in ("-wal", "-shm", "-journal"):
                if temporary.with_name(temporary.name + suffix).exists():
                    raise ThreadPersistenceError(
                        "CHECKPOINT_MIGRATION_RESTORE_NOT_STANDALONE"
                    )
            restored = sqlite3.connect(temporary)
            try:
                actual = _migration_database_fingerprint_sync(restored)
            finally:
                restored.close()
            if not _migration_fingerprint_matches(expected, actual):
                raise ThreadPersistenceError(
                    "CHECKPOINT_MIGRATION_RESTORE_VALIDATION_FAILED"
                )
            os.chmod(temporary, 0o600)
            _fsync_file_path(temporary)
            # The old target connection has been closed, so these exact sidecars
            # cannot receive any more frames.  Never leave them beside the new
            # inode, where a subsequent opener could mistake them for its WAL.
            for suffix in ("-wal", "-shm", "-journal"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            os.replace(temporary, path)
            _fsync_directory_best_effort(path.parent)
        except ThreadPersistenceError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ThreadPersistenceError("CHECKPOINT_MIGRATION_RESTORE_FAILED") from exc
        finally:
            source.close()
            if target is not None:
                target.close()
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _mark_restore_state_sync(state_path: Path, error: BaseException) -> None:
        """恢复失败只更新稳定错误码，绝不删除已验证 backup。"""
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            raw["status"] = "restore_failed"
            raw["error"] = (
                str(error)
                if isinstance(error, ThreadPersistenceError)
                and str(error).startswith("CHECKPOINT_MIGRATION_")
                else "CHECKPOINT_MIGRATION_RESTORE_FAILED"
            )
            raw["error_type"] = type(error).__name__
            directory = state_path.parent
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{state_path.name}.", suffix=".tmp", dir=directory
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    _restrict_owner_mode(handle.fileno())
                    json.dump(raw, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, state_path)
                _fsync_directory_best_effort(directory)
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    @staticmethod
    def _unlink_migration_state_path_sync(state_path: Path) -> None:
        """恢复流程使用的无实例 state 删除操作，并同步目录元数据。"""
        state_path.unlink(missing_ok=True)
        _fsync_directory_best_effort(state_path.parent)

    async def _migrate_legacy_prompt_epochs_to_snapshots(self) -> None:
        """把旧 PromptEpoch 单向转换为 legacy snapshot，并回填旧 Run 引用。"""
        # 合法 v2-v6 数据库可以没有可选 PromptEpoch；缺少旧上下文时保持
        # legacy incomplete，不能为满足迁移形状而伪造 snapshot。
        if not await self._table_exists("harness_prompt_epochs"):
            return
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

    async def _finalize_legacy_prompt_epoch_adapter(self) -> None:
        """删除已转换的 PromptEpoch 表；legacy 兼容只存在于迁移事务内。"""
        if not await self._table_exists("harness_prompt_epochs"):
            return
        await self._connection.execute("DROP TABLE harness_prompt_epochs")

    async def _table_exists(self, table_name: str) -> bool:
        """读取 Harness schema 是否仍包含一次性迁移表。"""
        cursor = await self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

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

    async def _add_context_runtime_state_column(self) -> None:
        """v11 为 ContextState 增加严格 JSON 运行态快照列。"""
        cursor = await self._connection.execute(
            "PRAGMA table_info(harness_context_state)"
        )
        columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "runtime_state" not in columns:
            await self._connection.execute(
                """
                ALTER TABLE harness_context_state
                ADD COLUMN runtime_state TEXT NOT NULL DEFAULT '{}'
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
        if _MIGRATION_CHILD_TEST_PHASE in {"bootstrap_failure", "restore_failure"}:
            raise ValueError("injected legacy bootstrap failure")
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
                    "runtime_state": (
                        state.runtime_state.record()
                        if state.runtime_state is not None
                        else None
                    ),
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
    # 不支持的 legacy 对象不是可证明的文本事实；不要通过 str() 把它
    # 伪造成合法 Transcript，周围历史仍由 legacy_incomplete 标记说明边界。
    return ""


def _inspect_migration_source_sync(path: Path) -> tuple[int, bool]:
    """只读探测迁移入口；schema/data mutation 仍全部留在 child。"""
    if not path.is_file():
        return 0, False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path)
        version_row = connection.execute("PRAGMA user_version").fetchone()
        source_version = int(version_row[0]) if version_row else 0
        prompt_row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='harness_prompt_epochs'"
        ).fetchone()
        has_prompt = prompt_row is not None
        if _is_pre_transcript_prompt_epoch_source_sync(connection, source_version):
            # 旧分支把 v6 的表结构错误标成 v7；只把经过完整契约校验的
            # 数据库映射回 v6，交给既有隔离迁移流程升级到当前 schema。
            return 6, True
        if 1 <= source_version <= 6:
            _migration_validate_legacy_source_schema_sync(connection, source_version)
        return source_version, has_prompt
    except ThreadPersistenceError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise ThreadPersistenceError("CHECKPOINT_DATABASE_CORRUPT") from exc
    finally:
        if connection is not None:
            connection.close()


def _migration_validate_final_schema_sync(connection: sqlite3.Connection) -> None:
    """父进程独立确认最终 schema 契约，不把 fingerprint 自比当作证明。"""
    required: dict[str, tuple[str, ...]] = {
        "harness_threads": (
            "project_fingerprint",
            "thread_id",
            "created_at_ms",
            "updated_at_ms",
            "first_message",
            "latest_message",
            "message_count",
        ),
        "checkpoints": (
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "parent_checkpoint_id",
            "type",
            "checkpoint",
            "metadata",
        ),
        "writes": (
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
            "channel",
            "type",
            "value",
        ),
        "harness_context_artifacts": (
            "project_fingerprint",
            "thread_id",
            "artifact_id",
            "kind",
            "content",
            "source_start",
            "source_end",
            "created_at_ms",
            "content_sha256",
            "byte_length",
        ),
        "harness_context_summaries": (
            "project_fingerprint",
            "thread_id",
            "summary_id",
            "rewrite_version",
            "content",
            "source_start",
            "source_end",
            "artifact_ids",
            "created_at_ms",
        ),
        "harness_context_state": (
            "project_fingerprint",
            "thread_id",
            "failures",
            "circuit_open",
            "last_action",
            "updated_at_ms",
            "runtime_state",
        ),
        "harness_runtime_profiles": (
            "project_fingerprint",
            "profile_key",
            "profile_version",
            "topology_id",
            "topology_version",
            "profile_record",
            "created_at_ms",
        ),
        "harness_thread_runtime_profiles": (
            "project_fingerprint",
            "thread_id",
            "profile_key",
            "profile_version",
            "bound_at_ms",
        ),
        "harness_thread_model_bindings": (
            "project_fingerprint",
            "thread_id",
            "binding_record",
            "bound_at_ms",
        ),
        "harness_run_execution_bindings": (
            "project_fingerprint",
            "thread_id",
            "run_id",
            "requested_selection",
            "actual_primary_binding",
            "runtime_profile_id",
            "message_digest",
            "created_at_ms",
            "context_snapshot_id",
        ),
        "harness_thread_transcript": (
            "project_fingerprint",
            "thread_id",
            "record_id",
            "run_id",
            "execution_id",
            "sequence",
            "kind",
            "payload",
            "content_sha256",
            "byte_length",
            "artifact_id",
            "created_at_ms",
        ),
        "harness_thread_history_metadata": (
            "project_fingerprint",
            "thread_id",
            "legacy_incomplete_history",
            "source_schema_version",
            "migrated_at_ms",
        ),
        "harness_run_context_snapshots": (
            "project_fingerprint",
            "snapshot_id",
            "thread_id",
            "snapshot_record",
            "system_fingerprint",
            "created_at_ms",
            "legacy",
        ),
        "harness_compression_checkpoints": (
            "project_fingerprint",
            "thread_id",
            "checkpoint_id",
            "source_record_sequence",
            "source_digest",
            "mode",
            "rewrite_version",
            "projected_messages",
            "artifact_ids",
            "trigger",
            "pressure_before",
            "pressure_after",
            "created_at_ms",
            "legacy_incomplete",
            "commit_payload",
        ),
    }
    for table_name, expected_columns in required.items():
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchall()
        if not rows:
            raise ThreadPersistenceError(
                f"CHECKPOINT_MIGRATION_FINAL_SCHEMA_INVALID:{table_name}"
            )
        actual_columns = tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_xinfo({_migration_identifier(table_name)})"
            ).fetchall()
            if int(row[6]) == 0
        )
        valid_columns = (
            actual_columns == expected_columns
            or (
                table_name == "harness_context_state"
                and actual_columns
                == (
                    "project_fingerprint",
                    "thread_id",
                    "failures",
                    "circuit_open",
                    "last_action",
                    "runtime_state",
                    "updated_at_ms",
                )
            )
        )
        if not valid_columns:
            raise ThreadPersistenceError(
                f"CHECKPOINT_MIGRATION_FINAL_SCHEMA_INVALID:{table_name}"
            )
    required_indexes = (
        "harness_threads_project_updated",
        "harness_context_artifacts_thread_created",
        "harness_thread_runtime_profiles_project_profile",
        "harness_run_execution_bindings_thread_created",
        "harness_thread_transcript_thread_sequence",
        "harness_run_context_snapshots_thread_created",
        "harness_compression_checkpoints_latest",
    )
    for index_name in required_indexes:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone() is None:
            raise ThreadPersistenceError(
                f"CHECKPOINT_MIGRATION_FINAL_SCHEMA_INVALID:{index_name}"
            )


# Windows 的 Winsock/DLL 装载与临时目录依赖这些系统变量；child 若拿不到，
# asyncio 的 overlapped IO 初始化会直接失败（WinError 10106）。
_WINDOWS_REQUIRED_ENV_VARS = (
    "SystemRoot",
    "SYSTEMROOT",
    "SystemDrive",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "APPDATA",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "CommonProgramFiles",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PROCESSOR_ARCHITECTURE",
)


def _legacy_migration_child_environment() -> dict[str, str]:
    """为 migration child 提供最小非秘密环境，避免继承 Host 运行上下文。"""
    package_root = str(Path(__file__).resolve().parents[1])
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": package_root,
    }
    if os.name == "nt":
        # 环境变量名在 Windows 上不区分大小写；按小写索引后回填真实键值。
        lowered = {name.lower(): (name, value) for name, value in os.environ.items()}
        for required in _WINDOWS_REQUIRED_ENV_VARS:
            entry = lowered.get(required.lower())
            if entry is not None:
                environment[entry[0]] = entry[1]
    test_phase = _requested_migration_test_phase()
    if test_phase is not None:
        environment["HARNESS_TEST_MIGRATION_CHILD_PHASE"] = test_phase
    return environment


async def _wait_migration_child(process: subprocess.Popen[bytes], deadline: float) -> bool:
    """轮询 child，避免在父事件循环中引入不可控等待线程。"""
    loop = asyncio.get_running_loop()
    while process.poll() is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.01, remaining))
    return True


async def _terminate_and_reap_migration_child(
    process: subprocess.Popen[bytes],
) -> None:
    """先 terminate，再在固定上限内 kill，并确认没有遗留 child。"""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    loop = asyncio.get_running_loop()
    if await _wait_migration_child(
        process,
        loop.time() + _LEGACY_MIGRATION_CHILD_TERMINATE_GRACE_SECONDS,
    ):
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    if not await _wait_migration_child(
        process,
        loop.time() + _LEGACY_MIGRATION_CHILD_TERMINATE_GRACE_SECONDS,
    ):
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_WORKER_REAP_FAILED")


async def _run_legacy_migration_child_once(
    path: Path,
    project_fingerprint: str,
) -> tuple[bool, bool, str | None]:
    """运行一次可杀死的 child；返回 (正常退出, 是否超时, typed error)。"""
    command = [
        sys.executable,
        "-m",
        "harness_agent.threads.migration_worker",
        "--database",
        str(path.resolve()),
        "--project-fingerprint",
        project_fingerprint,
    ]
    test_phase = _requested_migration_test_phase()
    if test_phase is not None:
        command.extend(("--test-phase", test_phase))
    try:
        process = subprocess.Popen(
            command,
            close_fds=True,
            env=_legacy_migration_child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ThreadPersistenceError(
            "CHECKPOINT_MIGRATION_WORKER_START_FAILED"
        ) from exc
    try:
        completed = await _wait_migration_child(
            process,
            asyncio.get_running_loop().time() + _LEGACY_MIGRATION_CHILD_DEADLINE_SECONDS,
        )
        if not completed:
            await _terminate_and_reap_migration_child(process)
            return False, True, None
        output = process.stdout.read(512) if process.stdout is not None else b""
        error_code = output.decode("utf-8", errors="replace").splitlines()
        return (
            process.returncode == 0,
            False,
            error_code[0] if error_code and error_code[0].startswith("CHECKPOINT_") else None,
        )
    except BaseException:
        cleanup = asyncio.create_task(_terminate_and_reap_migration_child(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                pass
            raise
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


async def _run_legacy_migration_child(
    path: Path,
    project_fingerprint: str,
) -> None:
    """在父持有 migration lock 时运行并严格收敛一次 legacy migration。"""
    _child_succeeded, timed_out, child_error_code = await _run_legacy_migration_child_once(
        path,
        project_fingerprint,
    )
    try:
        # Exit code is only a prompt to inspect facts; it never chooses
        # restore/retry by itself.  This call runs after the child is fully
        # reaped, so no late SQLite worker can race the recovery replacement.
        ThreadPersistence._recover_interrupted_migration_sync(
            path,
            preserve_recovery_state=timed_out,
        )
        source_version, has_prompt = _inspect_migration_source_sync(path)
        if source_version == _SCHEMA_VERSION and not has_prompt:
            connection = sqlite3.connect(path)
            try:
                expected = _migration_database_fingerprint_sync(connection)
            finally:
                connection.close()
            ThreadPersistence._validate_final_database_path_sync(path, expected)
            _clear_migration_poison(path)
            return
    except BaseException:
        if timed_out:
            # Publish before returning to open's finally block, which releases
            # the file lock.  Any waiter that passed the precheck must reject
            # again after acquiring that same lock.
            _publish_migration_poison(path)
        raise

    if timed_out:
        _publish_migration_poison(path)
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_WORKER_TIMEOUT")
    raise ThreadPersistenceError(
        child_error_code or "CHECKPOINT_MIGRATION_WORKER_FAILED"
    )


async def run_legacy_migration_child(
    path: Path,
    project_fingerprint: str,
    test_phase: str | None = None,
) -> None:
    """migration_worker 入口；整个 legacy 事务只存在于 child 进程。"""
    global _MIGRATION_CHILD_PROCESS_MODE, _MIGRATION_CHILD_TEST_PHASE
    _MIGRATION_CHILD_PROCESS_MODE = True
    _MIGRATION_CHILD_TEST_PHASE = test_phase
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    operation_lock = asyncio.Lock()
    checkpointer = ProjectScopedAsyncSqliteSaver(
        connection,
        project_fingerprint,
        operation_lock=operation_lock,
    )
    persistence = ThreadPersistence(
        connection=connection,
        checkpointer=checkpointer,
        path=path,
        project_fingerprint=project_fingerprint,
        operation_lock=operation_lock,
    )
    try:
        await persistence._prepare()
        await _migration_child_pause_if_requested("after_commit_before_reply")
    finally:
        if (
            path not in _MIGRATION_CHILD_POISONED_PATHS
            and not persistence._closed
        ):
            await connection.close()
