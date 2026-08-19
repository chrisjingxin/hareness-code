"""Compose 薄进度存储：当前 slug、确认 digest 与状态；不复制文档正文。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from harness_agent.compose.models import ThreadMode


class ComposeProgressStoreError(RuntimeError):
    """薄进度存储的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class ComposeSessionRecord:
    """同一 Thread 当前未完成套件的进度行。"""

    thread_id: str
    slug: str
    complexity: str
    task_confirmed_digest: str | None
    spec_confirmed_digest: str | None
    plan_confirmed_digest: str | None
    fix_rounds: int
    status: str
    revision: int


class ComposeProgressStore:
    """借用 ThreadPersistence 连接；不拥有生命周期。"""

    def __init__(
        self,
        connection: Any,
        *,
        project_fingerprint: str,
        lock: asyncio.Lock,
    ) -> None:
        if not project_fingerprint:
            raise ComposeProgressStoreError("COMPOSE_PROJECT_FINGERPRINT_INVALID")
        self._connection = connection
        self._project_fingerprint = project_fingerprint
        self._lock = lock

    async def load_thread_mode(self, thread_id: str) -> ThreadMode | None:
        """读取 Thread 已冻结的 mode。"""
        if not thread_id:
            return None
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT mode FROM harness_thread_modes
                    WHERE project_fingerprint = ? AND thread_id = ?
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
            return ThreadMode(str(row["mode"])) if row is not None else None
        except ValueError as exc:
            raise ComposeProgressStoreError("THREAD_MODE_RECORD_INVALID") from exc
        except Exception as exc:
            raise ComposeProgressStoreError("THREAD_MODE_READ_FAILED") from exc

    async def load(self, thread_id: str) -> ComposeSessionRecord | None:
        """读取当前未完成 session；没有则 None。"""
        if not thread_id:
            return None
        try:
            async with self._lock:
                cursor = await self._connection.execute(
                    """
                    SELECT thread_id, slug, complexity,
                           task_confirmed_digest, spec_confirmed_digest,
                           plan_confirmed_digest, fix_rounds, status, revision
                    FROM harness_compose_sessions
                    WHERE project_fingerprint = ? AND thread_id = ?
                      AND status NOT IN ('completed', 'abandoned')
                    """,
                    (self._project_fingerprint, thread_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
        except Exception as exc:
            raise ComposeProgressStoreError("COMPOSE_SESSION_READ_FAILED") from exc
        if row is None:
            return None
        return _row_to_record(row)

    async def upsert(self, record: ComposeSessionRecord) -> ComposeSessionRecord:
        """写入或更新当前 session。"""
        try:
            async with self._lock:
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_sessions (
                        project_fingerprint, thread_id, slug, complexity,
                        task_confirmed_digest, spec_confirmed_digest,
                        plan_confirmed_digest, fix_rounds, status, revision,
                        updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(project_fingerprint, thread_id) DO UPDATE SET
                        slug = excluded.slug,
                        complexity = excluded.complexity,
                        task_confirmed_digest = excluded.task_confirmed_digest,
                        spec_confirmed_digest = excluded.spec_confirmed_digest,
                        plan_confirmed_digest = excluded.plan_confirmed_digest,
                        fix_rounds = excluded.fix_rounds,
                        status = excluded.status,
                        revision = excluded.revision,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        self._project_fingerprint,
                        record.thread_id,
                        record.slug,
                        record.complexity,
                        record.task_confirmed_digest,
                        record.spec_confirmed_digest,
                        record.plan_confirmed_digest,
                        record.fix_rounds,
                        record.status,
                        record.revision,
                    ),
                )
                await self._connection.commit()
        except Exception as exc:
            raise ComposeProgressStoreError("COMPOSE_SESSION_WRITE_FAILED") from exc
        return record


def _row_to_record(row: Any) -> ComposeSessionRecord:
    """把 SQLite 行转成领域记录。"""
    return ComposeSessionRecord(
        thread_id=str(row["thread_id"]),
        slug=str(row["slug"]),
        complexity=str(row["complexity"]),
        task_confirmed_digest=row["task_confirmed_digest"],
        spec_confirmed_digest=row["spec_confirmed_digest"],
        plan_confirmed_digest=row["plan_confirmed_digest"],
        fix_rounds=int(row["fix_rounds"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
    )
