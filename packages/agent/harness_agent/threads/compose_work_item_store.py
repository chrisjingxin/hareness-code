"""ComposeWorkItem 的 SQLite 事实存储。

该模块只保存跨 Run 的执行身份、revision 和审计事实；需求、规格、计划等
正文由后续 ComposeDocumentStore 保存到工作空间 Markdown。所有写操作借用
ThreadPersistence 的同一连接和 ``BEGIN IMMEDIATE`` 锁，确保模式冻结、唯一
未终结 Work Item、Run binding 与 terminal CAS 不会在并发下分叉。
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, AsyncIterator

from harness_agent.compose.models import (
    MAX_ARTIFACT_PAYLOAD_BYTES,
    ComposeActivity,
    ComposeActivityStatus,
    ComposeDocumentKind,
    ComposeEffect,
    ComposeEffectStatus,
    ComposeEvidence,
    ComposeWorkItem,
    ComposeWorkItemStatus,
    ThreadMode,
)

_CONTENT_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CONFIRMATION_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_CONFIRMATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CONFIRMATION_DECISIONS = frozenset({"confirmed", "approved"})
_ACTIVITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ACTIVITY_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_EFFECT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WORK_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EVIDENCE_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_RESTARTABLE_ACTIVITY_STATUSES = frozenset(
    {
        ComposeActivityStatus.INTERRUPTED,
        ComposeActivityStatus.RETRYABLE_FAILED,
        ComposeActivityStatus.FAILED,
        ComposeActivityStatus.CANCELLED,
        ComposeActivityStatus.WAITING_USER,
        ComposeActivityStatus.COMPLETED,
    }
)
_FINISHABLE_ACTIVITY_STATUSES = frozenset(
    {
        ComposeActivityStatus.RUNNING,
        ComposeActivityStatus.WAITING_USER,
    }
)
_FINISH_TARGET_STATUSES = frozenset(
    {
        ComposeActivityStatus.COMPLETED,
        ComposeActivityStatus.FAILED,
        ComposeActivityStatus.CANCELLED,
        ComposeActivityStatus.WAITING_USER,
        ComposeActivityStatus.BLOCKED,
        ComposeActivityStatus.INTERRUPTED,
        ComposeActivityStatus.RETRYABLE_FAILED,
    }
)
_NONTERMINAL_WORK_ITEM_STATUSES = frozenset(
    {
        ComposeWorkItemStatus.ACTIVE,
        ComposeWorkItemStatus.WAITING_USER,
        ComposeWorkItemStatus.BLOCKED,
    }
)

class ComposeWorkItemStoreError(RuntimeError):
    """Work Item 存储层的稳定错误码；调用方不得根据 SQLite 原文分支。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class CreateComposeWorkItem:
    """创建一个新的 active Work Item；同 Thread 不能已有未终结项。"""

    thread_id: str
    work_item_id: str
    slug: str
    goal: str
    created_at_ms: int
    amends_work_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalizeComposeWorkItem:
    """以 expected revision 原子终结 Work Item。"""

    work_item_id: str
    expected_revision: int
    status: ComposeWorkItemStatus
    terminal_at_ms: int


@dataclass(frozen=True, slots=True)
class BindRunToWorkItem:
    """把一个已受理 Run 固定绑定到实际 Work Item。"""

    thread_id: str
    run_id: str
    work_item_id: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ComposeDocumentReference:
    """SQLite 保存的 Markdown 路径与摘要事实，不携带 Markdown 正文。"""

    work_item_id: str
    kind: ComposeDocumentKind
    relative_path: str
    current_digest: str
    confirmed_digest: str | None
    revision: int
    updated_at_ms: int
    lineage: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpsertComposeDocumentReference:
    """将已安全写入 Workspace 的文档 identity 同步到 SQLite。"""

    work_item_id: str
    kind: ComposeDocumentKind
    relative_path: str
    content_digest: str
    revision: int
    updated_at_ms: int
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordComposeConfirmation:
    """记录一个 gate 对当前一组 Markdown digest 的人工确认事实。"""

    work_item_id: str
    confirmation_id: str
    confirmation_kind: str
    document_digests: tuple[str, ...]
    confirmed_at_ms: int
    decision: str = "confirmed"


@dataclass(frozen=True, slots=True)
class StartComposeActivity:
    """开始一次有界 Activity 执行；同一 activity_id 只能 start 一次。"""

    activity_id: str
    work_item_id: str
    run_id: str | None
    kind: str
    started_at_ms: int


@dataclass(frozen=True, slots=True)
class RestartComposeActivity:
    """从可恢复状态重启 Activity；attempt 递增并重新绑定 run。"""

    activity_id: str
    run_id: str | None
    started_at_ms: int


@dataclass(frozen=True, slots=True)
class FinishComposeActivity:
    """以 CAS 收敛 Activity；当前必须是 running 或 waiting_user。"""

    activity_id: str
    status: ComposeActivityStatus
    finished_at_ms: int


@dataclass(frozen=True, slots=True)
class RecordComposeEffectIntent:
    """在外部副作用执行前原子写入 intent；相同 key 幂等。"""

    effect_key: str
    work_item_id: str
    activity_id: str | None
    intent: dict[str, object]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class RecordComposeEffectReceipt:
    """以真实对账结果确认 effect；不同 receipt 冲突拒绝。"""

    effect_key: str
    receipt: dict[str, object]
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class MarkComposeEffectUnknown:
    """把结果无法证明的 intent 标记为 unknown，等待用户决策。"""

    effect_key: str
    reason: str
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class RecordComposeEvidence:
    """写入一条 verification/review 证据事实；identity 幂等。"""

    evidence_id: str
    work_item_id: str
    evidence_kind: str
    content_digest: str
    payload: dict[str, object]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class SetComposeWorkItemStatus:
    """在未终结状态间以 revision CAS 迁移 Work Item 状态。"""

    work_item_id: str
    expected_revision: int
    status: ComposeWorkItemStatus
    updated_at_ms: int


class ComposeWorkItemStore:
    """借用 ThreadPersistence 连接的 Work Item store；不拥有连接生命周期。"""

    def __init__(
        self,
        connection: Any,
        *,
        project_fingerprint: str,
        lock: asyncio.Lock,
    ) -> None:
        """保存 project 隔离身份和与 checkpoint 共用的事务锁。"""
        if not project_fingerprint:
            raise ComposeWorkItemStoreError("COMPOSE_PROJECT_FINGERPRINT_INVALID")
        self._connection = connection
        self._project_fingerprint = project_fingerprint
        self._lock = lock

    async def load_thread_mode(self, thread_id: str) -> ThreadMode | None:
        """读取 Thread 已冻结的 mode；从未受理有效 Run 时返回 ``None``。"""
        if not thread_id:
            return None
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT mode
                    FROM harness_thread_modes
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            return ThreadMode(str(row["mode"])) if row is not None else None
        except ValueError as exc:
            raise ComposeWorkItemStoreError("THREAD_MODE_RECORD_INVALID") from exc
        except Exception as exc:
            raise ComposeWorkItemStoreError("THREAD_MODE_READ_FAILED") from exc

    async def load_active(self, thread_id: str) -> ComposeWorkItem | None:
        """读取同 Thread 的唯一未终结 Work Item。"""
        if not thread_id:
            return None
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT work_item_id, thread_id, slug, goal, status, revision,
                           created_at_ms, updated_at_ms, terminal_at_ms,
                           amends_work_item_id
                    FROM harness_compose_work_items
                    WHERE project_fingerprint = ?
                      AND thread_id = ?
                      AND status IN ('active', 'waiting_user', 'blocked')
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            return _work_item_from_row(row) if row is not None else None
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_READ_FAILED") from exc

    async def load(self, work_item_id: str) -> ComposeWorkItem | None:
        """按稳定 Work Item identity 读取当前投影。"""
        if not work_item_id:
            return None
        try:
            async with self._lock:
                row = await self._load_in_transaction(work_item_id)
            return _work_item_from_row(row) if row is not None else None
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_READ_FAILED") from exc

    async def load_slugs(self, thread_id: str) -> frozenset[str]:
        """读取 Thread 已占用的 slug，供 Runtime 生成新 Work Item 时解决冲突。"""
        if not thread_id:
            return frozenset()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT slug
                    FROM harness_compose_work_items
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return frozenset(str(row["slug"]) for row in rows)
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_READ_FAILED") from exc

    async def create(self, command: CreateComposeWorkItem) -> ComposeWorkItem:
        """原子创建 active Work Item，并由数据库唯一约束兜底并发竞争。"""
        _validate_create(command)
        try:
            async with self._transaction():
                await self._require_compose_thread_in_transaction(command.thread_id)
                active = await self._load_active_in_transaction(command.thread_id)
                if active is not None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_CONFLICT")
                existing = await self._load_in_transaction(command.work_item_id)
                if existing is not None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_ID_CONFLICT")
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_work_items (
                        project_fingerprint, work_item_id, thread_id, slug, goal,
                        status, revision, amends_work_item_id, created_at_ms,
                        updated_at_ms, terminal_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'active', 0, ?, ?, ?, NULL)
                    """,
                    (
                        self._project_fingerprint,
                        command.work_item_id,
                        command.thread_id,
                        command.slug,
                        command.goal,
                        command.amends_work_item_id,
                        command.created_at_ms,
                        command.created_at_ms,
                    ),
                )
                row = await self._load_in_transaction(command.work_item_id)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_WRITE_FAILED")
                return _work_item_from_row(row)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_CONFLICT") from exc
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_WRITE_FAILED") from exc

    async def terminalize(
        self,
        command: TerminalizeComposeWorkItem,
    ) -> ComposeWorkItem:
        """以 revision CAS 终结 Work Item，拒绝 terminal 重写和陈旧请求。"""
        _validate_terminalize(command)
        try:
            async with self._transaction():
                current = await self._load_in_transaction(command.work_item_id)
                if current is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                item = _work_item_from_row(current)
                if item.terminal:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_TERMINAL")
                if item.revision != command.expected_revision:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_REVISION_CONFLICT")
                cursor = await self._connection.execute(
                    """
                    UPDATE harness_compose_work_items
                    SET status = ?, revision = revision + 1, updated_at_ms = ?,
                        terminal_at_ms = ?
                    WHERE project_fingerprint = ?
                      AND work_item_id = ?
                      AND revision = ?
                      AND status IN ('active', 'waiting_user', 'blocked')
                    """,
                    (
                        command.status.value,
                        command.terminal_at_ms,
                        command.terminal_at_ms,
                        self._project_fingerprint,
                        command.work_item_id,
                        command.expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_REVISION_CONFLICT")
                await cursor.close()
                updated = await self._load_in_transaction(command.work_item_id)
                if updated is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_WRITE_FAILED")
                return _work_item_from_row(updated)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_WRITE_FAILED") from exc

    async def bind_run(self, command: BindRunToWorkItem) -> bool:
        """固定 Run→Work Item identity；同一绑定重试返回 ``False``。"""
        _validate_run_binding(command)
        try:
            async with self._transaction():
                item_row = await self._load_in_transaction(command.work_item_id)
                if item_row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                item = _work_item_from_row(item_row)
                if item.thread_id != command.thread_id:
                    raise ComposeWorkItemStoreError("RUN_WORK_ITEM_THREAD_MISMATCH")
                if item.terminal:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_TERMINAL")
                cursor = await self._connection.execute(
                    """
                    SELECT work_item_id
                    FROM harness_compose_work_item_run_bindings
                    WHERE project_fingerprint = ? AND thread_id = ? AND run_id = ?
                    """,
                    (self._project_fingerprint, command.thread_id, command.run_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    if str(existing["work_item_id"]) == command.work_item_id:
                        return False
                    raise ComposeWorkItemStoreError("RUN_WORK_ITEM_BINDING_CONFLICT")
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_work_item_run_bindings (
                        project_fingerprint, thread_id, run_id, work_item_id,
                        created_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        self._project_fingerprint,
                        command.thread_id,
                        command.run_id,
                        command.work_item_id,
                        command.created_at_ms,
                    ),
                )
                return True
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("RUN_WORK_ITEM_BINDING_WRITE_FAILED") from exc

    async def load_run_binding(self, thread_id: str, run_id: str) -> str | None:
        """读取 Run 固定的 Work Item identity；无绑定时返回 ``None``。"""
        if not thread_id or not run_id:
            return None
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT work_item_id
                    FROM harness_compose_work_item_run_bindings
                    WHERE project_fingerprint = ? AND thread_id = ? AND run_id = ?
                    """,
                    (self._project_fingerprint, thread_id, run_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            return str(row["work_item_id"]) if row is not None else None
        except Exception as exc:
            raise ComposeWorkItemStoreError("RUN_WORK_ITEM_BINDING_READ_FAILED") from exc

    async def upsert_document_reference(
        self,
        command: UpsertComposeDocumentReference,
    ) -> ComposeDocumentReference:
        """同步 Markdown 的路径、digest、revision 与 lineage；正文永不写进 SQLite。"""
        _validate_document_reference(command)
        try:
            async with self._transaction():
                if await self._load_in_transaction(command.work_item_id) is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                lineage_json = json.dumps(
                    command.lineage,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_work_item_documents (
                        project_fingerprint, work_item_id, document_kind, relative_path,
                        current_digest, confirmed_digest, current_revision, lineage_json,
                        updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    ON CONFLICT(project_fingerprint, work_item_id, document_kind)
                    DO UPDATE SET
                        relative_path = excluded.relative_path,
                        confirmed_digest = CASE
                            WHEN harness_compose_work_item_documents.current_digest
                                = excluded.current_digest
                            THEN harness_compose_work_item_documents.confirmed_digest
                            ELSE NULL
                        END,
                        current_digest = excluded.current_digest,
                        current_revision = excluded.current_revision,
                        lineage_json = excluded.lineage_json,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        self._project_fingerprint,
                        command.work_item_id,
                        command.kind.value,
                        command.relative_path,
                        command.content_digest,
                        command.revision,
                        lineage_json,
                        command.updated_at_ms,
                    ),
                )
                reference = await self._load_document_reference_in_transaction(
                    command.work_item_id,
                    command.kind,
                )
                if reference is None:
                    raise ComposeWorkItemStoreError("COMPOSE_DOCUMENT_REFERENCE_WRITE_FAILED")
                return reference
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_DOCUMENT_REFERENCE_WRITE_FAILED") from exc

    async def load_document_references(
        self,
        work_item_id: str,
    ) -> tuple[ComposeDocumentReference, ...]:
        """读取一个 Work Item 的全部 Markdown 摘要投影，供纯 readiness 计算。"""
        if not work_item_id:
            return ()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT document_kind, relative_path, current_digest, confirmed_digest,
                           current_revision, lineage_json, updated_at_ms
                    FROM harness_compose_work_item_documents
                    WHERE project_fingerprint = ? AND work_item_id = ?
                    ORDER BY document_kind
                    """,
                    (self._project_fingerprint, work_item_id),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(
                _document_reference_from_row(work_item_id, row)
                for row in rows
            )
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_DOCUMENT_REFERENCE_READ_FAILED") from exc

    async def record_confirmation(self, command: RecordComposeConfirmation) -> frozenset[str]:
        """以当前 document refs 校验并追加 confirmation digest set，旧确认保留审计。"""
        _validate_confirmation(command)
        try:
            async with self._transaction():
                if await self._load_in_transaction(command.work_item_id) is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                cursor = await self._connection.execute(
                    """
                    SELECT confirmation_kind, decision, document_digest
                    FROM harness_compose_work_item_confirmations
                    WHERE project_fingerprint = ?
                      AND work_item_id = ?
                      AND confirmation_id = ?
                    """,
                    (
                        self._project_fingerprint,
                        command.work_item_id,
                        command.confirmation_id,
                    ),
                )
                existing_rows = await cursor.fetchall()
                await cursor.close()
                if existing_rows:
                    if (
                        {str(row["confirmation_kind"]) for row in existing_rows}
                        != {command.confirmation_kind}
                        or {str(row["decision"]) for row in existing_rows} != {command.decision}
                        or {str(row["document_digest"]) for row in existing_rows}
                        != set(command.document_digests)
                    ):
                        raise ComposeWorkItemStoreError("COMPOSE_CONFIRMATION_CONFLICT")
                    return frozenset(command.document_digests)
                placeholders = ", ".join("?" for _ in command.document_digests)
                cursor = await self._connection.execute(
                    f"""
                    SELECT current_digest
                    FROM harness_compose_work_item_documents
                    WHERE project_fingerprint = ?
                      AND work_item_id = ?
                      AND current_digest IN ({placeholders})
                    """,
                    (
                        self._project_fingerprint,
                        command.work_item_id,
                        *command.document_digests,
                    ),
                )
                rows = await cursor.fetchall()
                await cursor.close()
                if {str(row["current_digest"]) for row in rows} != set(command.document_digests):
                    raise ComposeWorkItemStoreError("COMPOSE_CONFIRMATION_DOCUMENT_STALE")
                await self._connection.executemany(
                    """
                    INSERT INTO harness_compose_work_item_confirmations (
                        project_fingerprint, work_item_id, confirmation_id,
                        confirmation_kind, document_digest, decision, confirmed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(
                        (
                            self._project_fingerprint,
                            command.work_item_id,
                            command.confirmation_id,
                            command.confirmation_kind,
                            digest,
                            command.decision,
                            command.confirmed_at_ms,
                        )
                        for digest in command.document_digests
                    ),
                )
                await self._connection.execute(
                    f"""
                    UPDATE harness_compose_work_item_documents
                    SET confirmed_digest = current_digest
                    WHERE project_fingerprint = ?
                      AND work_item_id = ?
                      AND current_digest IN ({placeholders})
                    """,
                    (
                        self._project_fingerprint,
                        command.work_item_id,
                        *command.document_digests,
                    ),
                )
                return frozenset(command.document_digests)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_CONFIRMATION_WRITE_FAILED") from exc

    async def load_confirmation_digests(
        self,
        work_item_id: str,
        confirmation_kind: str,
    ) -> frozenset[str]:
        """读取某一 gate 已审计的所有 digest；不把旧值伪装为当前值。"""
        if not work_item_id or not _CONFIRMATION_KIND.fullmatch(confirmation_kind):
            return frozenset()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT document_digest
                    FROM harness_compose_work_item_confirmations
                    WHERE project_fingerprint = ?
                      AND work_item_id = ?
                      AND confirmation_kind = ?
                      AND decision IN ('confirmed', 'approved')
                    """,
                    (self._project_fingerprint, work_item_id, confirmation_kind),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return frozenset(str(row["document_digest"]) for row in rows)
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_CONFIRMATION_READ_FAILED") from exc

    async def load_confirmation_groups(
        self,
        work_item_id: str,
        confirmation_kind: str,
    ) -> tuple[frozenset[str], ...]:
        """按 typed confirmation 原子边界返回 digest groups，禁止历史集合拼接。"""
        if not work_item_id or not _CONFIRMATION_KIND.fullmatch(confirmation_kind):
            return ()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT confirmation_id, document_digest
                    FROM harness_compose_work_item_confirmations
                    WHERE project_fingerprint = ?
                      AND work_item_id = ?
                      AND confirmation_kind = ?
                      AND decision IN ('confirmed', 'approved')
                    ORDER BY confirmation_id, document_digest
                    """,
                    (self._project_fingerprint, work_item_id, confirmation_kind),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            groups: dict[str, set[str]] = {}
            for row in rows:
                groups.setdefault(str(row["confirmation_id"]), set()).add(
                    str(row["document_digest"])
                )
            return tuple(frozenset(digests) for digests in groups.values())
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_CONFIRMATION_READ_FAILED") from exc

    # ---------- Activity 事实 ----------

    async def start_activity(self, command: StartComposeActivity) -> ComposeActivity:
        """原子开始一次 Activity；同一 activity_id 只能存在一行。"""
        _validate_activity_start(command)
        try:
            async with self._transaction():
                if await self._load_in_transaction(command.work_item_id) is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                existing = await self._load_activity_in_transaction(command.activity_id)
                if existing is not None:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_ID_CONFLICT")
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_work_item_activities (
                        project_fingerprint, activity_id, work_item_id, run_id, kind,
                        status, attempt, started_at_ms, finished_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'running', 1, ?, NULL)
                    """,
                    (
                        self._project_fingerprint,
                        command.activity_id,
                        command.work_item_id,
                        command.run_id,
                        command.kind,
                        command.started_at_ms,
                    ),
                )
                row = await self._load_activity_in_transaction(command.activity_id)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_WRITE_FAILED")
                return _activity_from_row(row)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_WRITE_FAILED") from exc

    async def restart_activity(
        self,
        command: RestartComposeActivity,
    ) -> ComposeActivity:
        """从可恢复状态重启 Activity：attempt 递增、重新绑定 run。"""
        _validate_activity_restart(command)
        try:
            async with self._transaction():
                row = await self._load_activity_in_transaction(command.activity_id)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_NOT_FOUND")
                activity = _activity_from_row(row)
                if activity.status not in _RESTARTABLE_ACTIVITY_STATUSES:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_RESTART_INVALID")
                await self._connection.execute(
                    """
                    UPDATE harness_compose_work_item_activities
                    SET status = 'running', attempt = attempt + 1,
                        run_id = ?, started_at_ms = ?, finished_at_ms = NULL
                    WHERE project_fingerprint = ? AND activity_id = ?
                    """,
                    (
                        command.run_id,
                        command.started_at_ms,
                        self._project_fingerprint,
                        command.activity_id,
                    ),
                )
                updated = await self._load_activity_in_transaction(command.activity_id)
                if updated is None:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_WRITE_FAILED")
                return _activity_from_row(updated)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_WRITE_FAILED") from exc

    async def finish_activity(self, command: FinishComposeActivity) -> ComposeActivity:
        """以 CAS 收敛 Activity；已终结或 running 之外的状态不能覆盖。"""
        _validate_activity_finish(command)
        try:
            async with self._transaction():
                row = await self._load_activity_in_transaction(command.activity_id)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_NOT_FOUND")
                activity = _activity_from_row(row)
                if activity.status not in _FINISHABLE_ACTIVITY_STATUSES:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_STATUS_CONFLICT")
                await self._connection.execute(
                    """
                    UPDATE harness_compose_work_item_activities
                    SET status = ?, finished_at_ms = ?
                    WHERE project_fingerprint = ? AND activity_id = ?
                    """,
                    (
                        command.status.value,
                        command.finished_at_ms,
                        self._project_fingerprint,
                        command.activity_id,
                    ),
                )
                updated = await self._load_activity_in_transaction(command.activity_id)
                if updated is None:
                    raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_WRITE_FAILED")
                return _activity_from_row(updated)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_WRITE_FAILED") from exc

    async def load_activity(self, activity_id: str) -> ComposeActivity | None:
        """按稳定 identity 读取 Activity 投影。"""
        if not activity_id:
            return None
        try:
            async with self._lock:
                row = await self._load_activity_in_transaction(activity_id)
            return _activity_from_row(row) if row is not None else None
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_READ_FAILED") from exc

    async def load_activities(
        self,
        work_item_id: str,
    ) -> tuple[ComposeActivity, ...]:
        """按开始时间读取一个 Work Item 的全部 Activity 事实。"""
        if not work_item_id:
            return ()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT activity_id, work_item_id, run_id, kind, status, attempt,
                           started_at_ms, finished_at_ms
                    FROM harness_compose_work_item_activities
                    WHERE project_fingerprint = ? AND work_item_id = ?
                    ORDER BY started_at_ms, activity_id
                    """,
                    (self._project_fingerprint, work_item_id),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(_activity_from_row(row) for row in rows)
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_READ_FAILED") from exc

    async def mark_running_activities_interrupted(self, now_ms: int) -> int:
        """启动恢复扫描：把遗留 running Activity 全部收敛为 interrupted。"""
        if now_ms <= 0:
            raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_SCAN_INVALID")
        try:
            async with self._transaction():
                cursor = await self._connection.execute(
                    """
                    UPDATE harness_compose_work_item_activities
                    SET status = 'interrupted', finished_at_ms = ?
                    WHERE project_fingerprint = ? AND status = 'running'
                    """,
                    (now_ms, self._project_fingerprint),
                )
                count = cursor.rowcount
                await cursor.close()
                return int(count)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_WRITE_FAILED") from exc

    # ---------- Effect 事实 ----------

    async def record_effect_intent(
        self,
        command: RecordComposeEffectIntent,
    ) -> ComposeEffect:
        """副作用执行前原子写 intent；相同 key 重试幂等返回已有行。"""
        _validate_effect_intent(command)
        try:
            async with self._transaction():
                if await self._load_in_transaction(command.work_item_id) is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                existing = await self._load_effect_in_transaction(command.effect_key)
                if existing is not None:
                    effect = _effect_from_row(existing)
                    if effect.work_item_id != command.work_item_id:
                        raise ComposeWorkItemStoreError("COMPOSE_EFFECT_INTENT_CONFLICT")
                    return effect
                intent_json = _dump_payload(command.intent, "COMPOSE_EFFECT_INTENT_INVALID")
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_work_item_effects (
                        project_fingerprint, effect_key, work_item_id, activity_id,
                        intent_json, receipt_json, status, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, NULL, 'intent', ?, ?)
                    """,
                    (
                        self._project_fingerprint,
                        command.effect_key,
                        command.work_item_id,
                        command.activity_id,
                        intent_json,
                        command.created_at_ms,
                        command.created_at_ms,
                    ),
                )
                row = await self._load_effect_in_transaction(command.effect_key)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_WRITE_FAILED")
                return _effect_from_row(row)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EFFECT_WRITE_FAILED") from exc

    async def record_effect_receipt(
        self,
        command: RecordComposeEffectReceipt,
    ) -> ComposeEffect:
        """以真实对账结果确认 effect；相同 receipt 幂等，不同 receipt 拒绝。"""
        _validate_effect_receipt(command)
        try:
            async with self._transaction():
                row = await self._load_effect_in_transaction(command.effect_key)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_NOT_FOUND")
                effect = _effect_from_row(row)
                if effect.status is ComposeEffectStatus.CONFIRMED:
                    if effect.receipt == command.receipt:
                        return effect
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_RECEIPT_CONFLICT")
                if effect.status is ComposeEffectStatus.UNKNOWN:
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_OUTCOME_UNKNOWN")
                receipt_json = _dump_payload(command.receipt, "COMPOSE_EFFECT_RECEIPT_INVALID")
                await self._connection.execute(
                    """
                    UPDATE harness_compose_work_item_effects
                    SET receipt_json = ?, status = 'confirmed', updated_at_ms = ?
                    WHERE project_fingerprint = ? AND effect_key = ?
                      AND status = 'intent'
                    """,
                    (
                        receipt_json,
                        command.updated_at_ms,
                        self._project_fingerprint,
                        command.effect_key,
                    ),
                )
                updated = await self._load_effect_in_transaction(command.effect_key)
                if updated is None:
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_WRITE_FAILED")
                return _effect_from_row(updated)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EFFECT_WRITE_FAILED") from exc

    async def mark_effect_unknown(
        self,
        command: MarkComposeEffectUnknown,
    ) -> ComposeEffect:
        """结果无法证明的 intent 迁移为 unknown；已确认效果不可逆转。"""
        _validate_effect_unknown(command)
        try:
            async with self._transaction():
                row = await self._load_effect_in_transaction(command.effect_key)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_NOT_FOUND")
                effect = _effect_from_row(row)
                if effect.status is not ComposeEffectStatus.INTENT:
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_OUTCOME_UNKNOWN")
                await self._connection.execute(
                    """
                    UPDATE harness_compose_work_item_effects
                    SET status = 'unknown', updated_at_ms = ?
                    WHERE project_fingerprint = ? AND effect_key = ?
                      AND status = 'intent'
                    """,
                    (
                        command.updated_at_ms,
                        self._project_fingerprint,
                        command.effect_key,
                    ),
                )
                updated = await self._load_effect_in_transaction(command.effect_key)
                if updated is None:
                    raise ComposeWorkItemStoreError("COMPOSE_EFFECT_WRITE_FAILED")
                return _effect_from_row(updated)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EFFECT_WRITE_FAILED") from exc

    async def load_effect(self, effect_key: str) -> ComposeEffect | None:
        """按稳定 effect key 读取 ledger 事实。"""
        if not effect_key:
            return None
        try:
            async with self._lock:
                row = await self._load_effect_in_transaction(effect_key)
            return _effect_from_row(row) if row is not None else None
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EFFECT_READ_FAILED") from exc

    async def load_effects(self, work_item_id: str) -> tuple[ComposeEffect, ...]:
        """按创建时间读取一个 Work Item 的全部 effect 事实。"""
        if not work_item_id:
            return ()
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT effect_key, work_item_id, activity_id, intent_json,
                           receipt_json, status, created_at_ms, updated_at_ms
                    FROM harness_compose_work_item_effects
                    WHERE project_fingerprint = ? AND work_item_id = ?
                    ORDER BY created_at_ms, effect_key
                    """,
                    (self._project_fingerprint, work_item_id),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(_effect_from_row(row) for row in rows)
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EFFECT_READ_FAILED") from exc

    async def load_torn_effects(self) -> tuple[ComposeEffect, ...]:
        """枚举全部 intent 无 receipt 的撕裂效果，供启动扫描对账。"""
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT effect_key, work_item_id, activity_id, intent_json,
                           receipt_json, status, created_at_ms, updated_at_ms
                    FROM harness_compose_work_item_effects
                    WHERE project_fingerprint = ? AND receipt_json IS NULL
                      AND status = 'intent'
                    ORDER BY created_at_ms, effect_key
                    """,
                    (self._project_fingerprint,),
                )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(_effect_from_row(row) for row in rows)
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EFFECT_READ_FAILED") from exc

    # ---------- Evidence 事实 ----------

    async def record_evidence(self, command: RecordComposeEvidence) -> ComposeEvidence:
        """写入证据事实；identity 幂等，冲突 payload 拒绝。"""
        _validate_evidence(command)
        try:
            async with self._transaction():
                if await self._load_in_transaction(command.work_item_id) is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                existing = await self._load_evidence_in_transaction(command.evidence_id)
                if existing is not None:
                    evidence = _evidence_from_row(existing)
                    if (
                        evidence.work_item_id != command.work_item_id
                        or evidence.evidence_kind != command.evidence_kind
                        or evidence.content_digest != command.content_digest
                        or evidence.payload != command.payload
                    ):
                        raise ComposeWorkItemStoreError("COMPOSE_EVIDENCE_CONFLICT")
                    return evidence
                payload_json = _dump_payload(command.payload, "COMPOSE_EVIDENCE_INVALID")
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_work_item_evidence (
                        project_fingerprint, evidence_id, work_item_id, evidence_kind,
                        content_digest, payload_json, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._project_fingerprint,
                        command.evidence_id,
                        command.work_item_id,
                        command.evidence_kind,
                        command.content_digest,
                        payload_json,
                        command.created_at_ms,
                    ),
                )
                row = await self._load_evidence_in_transaction(command.evidence_id)
                if row is None:
                    raise ComposeWorkItemStoreError("COMPOSE_EVIDENCE_WRITE_FAILED")
                return _evidence_from_row(row)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EVIDENCE_WRITE_FAILED") from exc

    async def load_evidence(
        self,
        work_item_id: str,
        evidence_kind: str | None = None,
    ) -> tuple[ComposeEvidence, ...]:
        """按 kind 读取证据；不指定时返回全部。"""
        if not work_item_id:
            return ()
        if evidence_kind is not None and not _EVIDENCE_KIND.fullmatch(evidence_kind):
            return ()
        try:
            async with self._lock:
                if evidence_kind is None:
                    cursor = await self._connection.execute(
                        """
                        SELECT evidence_id, work_item_id, evidence_kind, content_digest,
                               payload_json, created_at_ms
                        FROM harness_compose_work_item_evidence
                        WHERE project_fingerprint = ? AND work_item_id = ?
                        ORDER BY created_at_ms, evidence_id
                        """,
                        (self._project_fingerprint, work_item_id),
                    )
                else:
                    cursor = await self._connection.execute(
                        """
                        SELECT evidence_id, work_item_id, evidence_kind, content_digest,
                               payload_json, created_at_ms
                        FROM harness_compose_work_item_evidence
                        WHERE project_fingerprint = ? AND work_item_id = ?
                          AND evidence_kind = ?
                        ORDER BY created_at_ms, evidence_id
                        """,
                        (self._project_fingerprint, work_item_id, evidence_kind),
                    )
                rows = await cursor.fetchall()
                await cursor.close()
            return tuple(_evidence_from_row(row) for row in rows)
        except ComposeWorkItemStoreError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_EVIDENCE_READ_FAILED") from exc

    # ---------- Work Item 状态 CAS ----------

    async def set_status(self, command: SetComposeWorkItemStatus) -> ComposeWorkItem:
        """在未终结状态间以 revision CAS 迁移；terminal 不经过此方法。"""
        _validate_status(command)
        try:
            async with self._transaction():
                current = await self._load_in_transaction(command.work_item_id)
                if current is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_NOT_FOUND")
                item = _work_item_from_row(current)
                if item.revision != command.expected_revision:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_REVISION_CONFLICT")
                if item.terminal:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_TERMINAL")
                cursor = await self._connection.execute(
                    """
                    UPDATE harness_compose_work_items
                    SET status = ?, revision = revision + 1, updated_at_ms = ?
                    WHERE project_fingerprint = ?
                      AND work_item_id = ?
                      AND revision = ?
                      AND status IN ('active', 'waiting_user', 'blocked')
                    """,
                    (
                        command.status.value,
                        command.updated_at_ms,
                        self._project_fingerprint,
                        command.work_item_id,
                        command.expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_REVISION_CONFLICT")
                await cursor.close()
                updated = await self._load_in_transaction(command.work_item_id)
                if updated is None:
                    raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_WRITE_FAILED")
                return _work_item_from_row(updated)
        except ComposeWorkItemStoreError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_WRITE_FAILED") from exc

    async def _load_activity_in_transaction(self, activity_id: str) -> Any | None:
        """在调用方持锁时读取一行 Activity 事实。"""
        cursor = await self._connection.execute(
            """
            SELECT activity_id, work_item_id, run_id, kind, status, attempt,
                   started_at_ms, finished_at_ms
            FROM harness_compose_work_item_activities
            WHERE project_fingerprint = ? AND activity_id = ?
            """,
            (self._project_fingerprint, activity_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _load_effect_in_transaction(self, effect_key: str) -> Any | None:
        """在调用方持锁时读取一行 effect 事实。"""
        cursor = await self._connection.execute(
            """
            SELECT effect_key, work_item_id, activity_id, intent_json,
                   receipt_json, status, created_at_ms, updated_at_ms
            FROM harness_compose_work_item_effects
            WHERE project_fingerprint = ? AND effect_key = ?
            """,
            (self._project_fingerprint, effect_key),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _load_evidence_in_transaction(self, evidence_id: str) -> Any | None:
        """在调用方持锁时读取一行 evidence 事实。"""
        cursor = await self._connection.execute(
            """
            SELECT evidence_id, work_item_id, evidence_kind, content_digest,
                   payload_json, created_at_ms
            FROM harness_compose_work_item_evidence
            WHERE project_fingerprint = ? AND evidence_id = ?
            """,
            (self._project_fingerprint, evidence_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _require_compose_thread_in_transaction(self, thread_id: str) -> None:
        """只允许已冻结为 Compose 的 Thread 创建 Work Item。"""
        cursor = await self._connection.execute(
            """
            SELECT mode
            FROM harness_thread_modes
            WHERE project_fingerprint = ? AND thread_id = ?
            """,
            (self._project_fingerprint, thread_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or str(row["mode"]) != ThreadMode.COMPOSE.value:
            raise ComposeWorkItemStoreError("COMPOSE_THREAD_MODE_REQUIRED")

    async def _load_active_in_transaction(self, thread_id: str) -> Any | None:
        """在写事务中读取唯一 nonterminal 行，避免 pre-check 与 INSERT 竞争。"""
        cursor = await self._connection.execute(
            """
            SELECT work_item_id, thread_id, slug, goal, status, revision,
                   created_at_ms, updated_at_ms, terminal_at_ms,
                   amends_work_item_id
            FROM harness_compose_work_items
            WHERE project_fingerprint = ?
              AND thread_id = ?
              AND status IN ('active', 'waiting_user', 'blocked')
            """,
            (self._project_fingerprint, thread_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _load_in_transaction(self, work_item_id: str) -> Any | None:
        """在调用方已持锁时按 Work Item identity 读取完整投影。"""
        cursor = await self._connection.execute(
            """
            SELECT work_item_id, thread_id, slug, goal, status, revision,
                   created_at_ms, updated_at_ms, terminal_at_ms,
                   amends_work_item_id
            FROM harness_compose_work_items
            WHERE project_fingerprint = ? AND work_item_id = ?
            """,
            (self._project_fingerprint, work_item_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _load_document_reference_in_transaction(
        self,
        work_item_id: str,
        kind: ComposeDocumentKind,
    ) -> ComposeDocumentReference | None:
        """在调用方持锁的事务中读取一份文档摘要投影。"""
        cursor = await self._connection.execute(
            """
            SELECT document_kind, relative_path, current_digest, confirmed_digest,
                   current_revision, lineage_json, updated_at_ms
            FROM harness_compose_work_item_documents
            WHERE project_fingerprint = ? AND work_item_id = ? AND document_kind = ?
            """,
            (self._project_fingerprint, work_item_id, kind.value),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _document_reference_from_row(work_item_id, row) if row is not None else None

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """使用共享锁串行 BEGIN IMMEDIATE，并在取消/异常时确保 rollback。"""
        await self._lock.acquire()
        begun = False
        try:
            await self._connection.execute("BEGIN IMMEDIATE")
            begun = True
            try:
                yield
            except BaseException:
                await self._rollback_after_failure()
                raise
            try:
                await self._connection.commit()
            except BaseException:
                await self._rollback_after_failure()
                raise
        finally:
            if begun:
                # rollback 已在异常路径尽力执行；成功 commit 后再次 rollback 无害，
                # 但不需要增加一次 SQL 往返。
                begun = False
            self._lock.release()

    async def _rollback_after_failure(self) -> None:
        """尽力回滚，且不让 rollback 诊断遮蔽原始业务错误。"""
        try:
            await self._connection.rollback()
        except Exception:
            pass


def _work_item_from_row(row: Any) -> ComposeWorkItem:
    """把受信 SQLite 行转换为严格领域模型；非法 enum 作为损坏事实拒绝。"""
    try:
        item = ComposeWorkItem(
            work_item_id=str(row["work_item_id"]),
            thread_id=str(row["thread_id"]),
            slug=str(row["slug"]),
            goal=str(row["goal"]),
            status=ComposeWorkItemStatus(str(row["status"])),
            revision=int(row["revision"]),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            terminal_at_ms=(
                int(row["terminal_at_ms"])
                if row["terminal_at_ms"] is not None
                else None
            ),
            amends_work_item_id=(
                str(row["amends_work_item_id"])
                if row["amends_work_item_id"] is not None
                else None
            ),
        )
        if (
            not item.work_item_id
            or not item.thread_id
            or not item.slug.strip()
            or not item.goal.strip()
            or item.revision < 0
            or item.created_at_ms <= 0
            or item.updated_at_ms < item.created_at_ms
        ):
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_RECORD_INVALID")
        if item.terminal:
            if item.terminal_at_ms != item.updated_at_ms:
                raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_RECORD_INVALID")
        elif item.terminal_at_ms is not None:
            raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_RECORD_INVALID")
        return item
    except ComposeWorkItemStoreError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_RECORD_INVALID") from exc


def _document_reference_from_row(work_item_id: str, row: Any) -> ComposeDocumentReference:
    """将 SQLite 文档摘要行还原为严格引用，不接受正文或畸形 lineage。"""
    try:
        kind = ComposeDocumentKind(str(row["document_kind"]))
        relative_path = str(row["relative_path"])
        current_digest = str(row["current_digest"])
        confirmed_value = row["confirmed_digest"]
        confirmed_digest = str(confirmed_value) if confirmed_value is not None else None
        revision = int(row["current_revision"])
        updated_at_ms = int(row["updated_at_ms"])
        lineage_value = json.loads(str(row["lineage_json"]))
        if not isinstance(lineage_value, list) or not all(
            isinstance(item, str) for item in lineage_value
        ):
            raise ValueError("lineage")
        reference = ComposeDocumentReference(
            work_item_id=work_item_id,
            kind=kind,
            relative_path=relative_path,
            current_digest=current_digest,
            confirmed_digest=confirmed_digest,
            revision=revision,
            updated_at_ms=updated_at_ms,
            lineage=tuple(lineage_value),
        )
        _validate_document_reference(
            UpsertComposeDocumentReference(
                work_item_id=reference.work_item_id,
                kind=reference.kind,
                relative_path=reference.relative_path,
                content_digest=reference.current_digest,
                revision=reference.revision,
                updated_at_ms=reference.updated_at_ms,
                lineage=reference.lineage,
            )
        )
        if confirmed_digest is not None and _CONTENT_DIGEST.fullmatch(confirmed_digest) is None:
            raise ValueError("confirmed_digest")
        return reference
    except ComposeWorkItemStoreError as exc:
        raise ComposeWorkItemStoreError("COMPOSE_DOCUMENT_REFERENCE_RECORD_INVALID") from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ComposeWorkItemStoreError("COMPOSE_DOCUMENT_REFERENCE_RECORD_INVALID") from exc


def _validate_create(command: CreateComposeWorkItem) -> None:
    """拒绝无法成为稳定 Work Item identity 的空值或无效时间。"""
    if (
        not command.thread_id
        or not command.work_item_id
        or not command.slug.strip()
        or not command.goal.strip()
        or command.created_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_CREATE_INVALID")
    if len(command.slug) > 120 or len(command.goal) > 4_000:
        raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_CREATE_INVALID")


def _validate_terminalize(command: TerminalizeComposeWorkItem) -> None:
    """terminal 只能是 completed/abandoned，避免普通 Run 覆盖长期状态。"""
    if (
        not command.work_item_id
        or command.expected_revision < 0
            or command.terminal_at_ms <= 0
        or command.status
        not in {
            ComposeWorkItemStatus.COMPLETED,
            ComposeWorkItemStatus.ABANDONED,
        }
    ):
        raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_TERMINAL_INVALID")


def _validate_run_binding(command: BindRunToWorkItem) -> None:
    """Run binding 必须提供完整且不可歧义的四元身份。"""
    if (
        not command.thread_id
        or not command.run_id
        or not command.work_item_id
        or command.created_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("RUN_WORK_ITEM_BINDING_INVALID")


def _validate_document_reference(command: UpsertComposeDocumentReference) -> None:
    """拒绝路径穿越、非 SHA-256 digest 与把正文伪装成 lineage 的写入。"""
    try:
        path = PurePosixPath(command.relative_path)
    except TypeError as exc:
        raise ComposeWorkItemStoreError("COMPOSE_DOCUMENT_REFERENCE_INVALID") from exc
    if (
        not command.work_item_id
        or not isinstance(command.kind, ComposeDocumentKind)
        or not isinstance(command.relative_path, str)
        or path.is_absolute()
        or not path.parts
        or command.relative_path != path.as_posix()
        or any(part in {"", ".", "..", "~"} for part in path.parts)
        or path.parts[0] == ".harness"
        or path.name != f"{command.kind.value}.md"
        or len(command.relative_path) > 512
        or not isinstance(command.content_digest, str)
        or _CONTENT_DIGEST.fullmatch(command.content_digest) is None
        or not isinstance(command.revision, int)
        or isinstance(command.revision, bool)
        or command.revision < 1
        or not isinstance(command.updated_at_ms, int)
        or isinstance(command.updated_at_ms, bool)
        or command.updated_at_ms <= 0
        or not isinstance(command.lineage, tuple)
        or len(command.lineage) > 32
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in command.lineage)
    ):
        raise ComposeWorkItemStoreError("COMPOSE_DOCUMENT_REFERENCE_INVALID")


def _validate_confirmation(command: RecordComposeConfirmation) -> None:
    """确认必须绑定至少一个已记录的当前 SHA-256 digest，且不能重复。"""
    if (
        not command.work_item_id
        or not isinstance(command.confirmation_id, str)
        or _CONFIRMATION_ID.fullmatch(command.confirmation_id) is None
        or not isinstance(command.confirmation_kind, str)
        or _CONFIRMATION_KIND.fullmatch(command.confirmation_kind) is None
        or not isinstance(command.document_digests, tuple)
        or not command.document_digests
        or len(command.document_digests) > len(ComposeDocumentKind)
        or len(set(command.document_digests)) != len(command.document_digests)
        or any(
            not isinstance(digest, str) or _CONTENT_DIGEST.fullmatch(digest) is None
            for digest in command.document_digests
        )
        or not isinstance(command.confirmed_at_ms, int)
        or isinstance(command.confirmed_at_ms, bool)
        or command.confirmed_at_ms <= 0
        or not isinstance(command.decision, str)
        or command.decision not in _CONFIRMATION_DECISIONS
    ):
        raise ComposeWorkItemStoreError("COMPOSE_CONFIRMATION_INVALID")


def _activity_from_row(row: Any) -> ComposeActivity:
    """把受信 SQLite 行还原为严格 Activity 模型；非法 enum 拒绝。"""
    try:
        return ComposeActivity(
            activity_id=str(row["activity_id"]),
            work_item_id=str(row["work_item_id"]),
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
            kind=str(row["kind"]),
            status=ComposeActivityStatus(str(row["status"])),
            attempt=int(row["attempt"]),
            started_at_ms=int(row["started_at_ms"]),
            finished_at_ms=(
                int(row["finished_at_ms"])
                if row["finished_at_ms"] is not None
                else None
            ),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_RECORD_INVALID") from exc


def _effect_from_row(row: Any) -> ComposeEffect:
    """把受信 SQLite 行还原为严格 Effect 模型；JSON 损坏 fail closed。"""
    try:
        intent = _load_payload(row["intent_json"], "COMPOSE_EFFECT_RECORD_INVALID")
        receipt_json = row["receipt_json"]
        receipt = (
            _load_payload(receipt_json, "COMPOSE_EFFECT_RECORD_INVALID")
            if receipt_json is not None
            else None
        )
        return ComposeEffect(
            effect_key=str(row["effect_key"]),
            work_item_id=str(row["work_item_id"]),
            activity_id=str(row["activity_id"]) if row["activity_id"] is not None else None,
            intent=intent,
            receipt=receipt,
            status=ComposeEffectStatus(str(row["status"])),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )
    except ComposeWorkItemStoreError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise ComposeWorkItemStoreError("COMPOSE_EFFECT_RECORD_INVALID") from exc


def _evidence_from_row(row: Any) -> ComposeEvidence:
    """把受信 SQLite 行还原为严格 Evidence 模型。"""
    try:
        payload = _load_payload(row["payload_json"], "COMPOSE_EVIDENCE_RECORD_INVALID")
        return ComposeEvidence(
            evidence_id=str(row["evidence_id"]),
            work_item_id=str(row["work_item_id"]),
            evidence_kind=str(row["evidence_kind"]),
            content_digest=str(row["content_digest"]),
            payload=payload,
            created_at_ms=int(row["created_at_ms"]),
        )
    except ComposeWorkItemStoreError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise ComposeWorkItemStoreError("COMPOSE_EVIDENCE_RECORD_INVALID") from exc


def _dump_payload(payload: dict[str, object], error_code: str) -> str:
    """把有界 JSON payload 紧凑序列化；超限或不可序列化直接拒绝。"""
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ComposeWorkItemStoreError(error_code) from exc
    if len(encoded.encode("utf-8")) > MAX_ARTIFACT_PAYLOAD_BYTES:
        raise ComposeWorkItemStoreError(error_code)
    return encoded


def _load_payload(raw: object, error_code: str) -> dict[str, object]:
    """解析 ledger JSON；只接受对象形状，拒绝数组或标量伪装。"""
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise ComposeWorkItemStoreError(error_code) from exc
    if not isinstance(value, dict):
        raise ComposeWorkItemStoreError(error_code)
    return value


def _validate_activity_start(command: StartComposeActivity) -> None:
    """Activity 必须携带完整、格式稳定且可排序的身份与时间。"""
    if (
        not command.work_item_id
        or not isinstance(command.activity_id, str)
        or _ACTIVITY_ID.fullmatch(command.activity_id) is None
        or not isinstance(command.kind, str)
        or _ACTIVITY_KIND.fullmatch(command.kind) is None
        or (
            command.run_id is not None
            and (not isinstance(command.run_id, str) or not command.run_id)
        )
        or not isinstance(command.started_at_ms, int)
        or isinstance(command.started_at_ms, bool)
        or command.started_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_START_INVALID")


def _validate_activity_restart(command: RestartComposeActivity) -> None:
    """restart 必须提供完整 identity 与新的开始时间。"""
    if (
        not isinstance(command.activity_id, str)
        or _ACTIVITY_ID.fullmatch(command.activity_id) is None
        or (
            command.run_id is not None
            and (not isinstance(command.run_id, str) or not command.run_id)
        )
        or not isinstance(command.started_at_ms, int)
        or isinstance(command.started_at_ms, bool)
        or command.started_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_RESTART_INVALID")


def _validate_activity_finish(command: FinishComposeActivity) -> None:
    """finish 目标必须是受控枚举，时间必须有效。"""
    if (
        not isinstance(command.activity_id, str)
        or _ACTIVITY_ID.fullmatch(command.activity_id) is None
        or not isinstance(command.status, ComposeActivityStatus)
        or command.status not in _FINISH_TARGET_STATUSES
        or not isinstance(command.finished_at_ms, int)
        or isinstance(command.finished_at_ms, bool)
        or command.finished_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_ACTIVITY_FINISH_INVALID")


def _validate_effect_intent(command: RecordComposeEffectIntent) -> None:
    """intent 必须绑定 Work Item、有效 key 与可序列化 JSON。"""
    if (
        not command.work_item_id
        or not isinstance(command.effect_key, str)
        or _EFFECT_KEY.fullmatch(command.effect_key) is None
        or (
            command.activity_id is not None
            and (
                not isinstance(command.activity_id, str)
                or _ACTIVITY_ID.fullmatch(command.activity_id) is None
            )
        )
        or not isinstance(command.intent, dict)
        or not command.intent
        or not isinstance(command.created_at_ms, int)
        or isinstance(command.created_at_ms, bool)
        or command.created_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_EFFECT_INTENT_INVALID")


def _validate_effect_receipt(command: RecordComposeEffectReceipt) -> None:
    """receipt 必须是非空 JSON 与有效时间。"""
    if (
        not isinstance(command.effect_key, str)
        or _EFFECT_KEY.fullmatch(command.effect_key) is None
        or not isinstance(command.receipt, dict)
        or not command.receipt
        or not isinstance(command.updated_at_ms, int)
        or isinstance(command.updated_at_ms, bool)
        or command.updated_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_EFFECT_RECEIPT_INVALID")


def _validate_effect_unknown(command: MarkComposeEffectUnknown) -> None:
    """unknown 标记必须携带非空 reason 与有效时间。"""
    if (
        not isinstance(command.effect_key, str)
        or _EFFECT_KEY.fullmatch(command.effect_key) is None
        or not isinstance(command.reason, str)
        or not command.reason
        or not isinstance(command.updated_at_ms, int)
        or isinstance(command.updated_at_ms, bool)
        or command.updated_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_EFFECT_UNKNOWN_INVALID")


def _validate_evidence(command: RecordComposeEvidence) -> None:
    """evidence 必须携带完整 identity、SHA-256 digest 与 payload。"""
    if (
        not command.work_item_id
        or not isinstance(command.evidence_id, str)
        or _EVIDENCE_ID.fullmatch(command.evidence_id) is None
        or not isinstance(command.evidence_kind, str)
        or _EVIDENCE_KIND.fullmatch(command.evidence_kind) is None
        or not isinstance(command.content_digest, str)
        or _CONTENT_DIGEST.fullmatch(command.content_digest) is None
        or not isinstance(command.payload, dict)
        or not isinstance(command.created_at_ms, int)
        or isinstance(command.created_at_ms, bool)
        or command.created_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_EVIDENCE_INVALID")


def _validate_status(command: SetComposeWorkItemStatus) -> None:
    """状态 CAS 只接受未终结状态；terminal 必须走 terminalize。"""
    if (
        not isinstance(command.work_item_id, str)
        or _WORK_ITEM_ID.fullmatch(command.work_item_id) is None
        or not isinstance(command.expected_revision, int)
        or isinstance(command.expected_revision, bool)
        or command.expected_revision < 0
        or not isinstance(command.status, ComposeWorkItemStatus)
        or command.status not in _NONTERMINAL_WORK_ITEM_STATUSES
        or not isinstance(command.updated_at_ms, int)
        or isinstance(command.updated_at_ms, bool)
        or command.updated_at_ms <= 0
    ):
        raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_STATUS_INVALID")
