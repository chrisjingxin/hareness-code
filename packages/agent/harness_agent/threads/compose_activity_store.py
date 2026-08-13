"""Compose activity 有界审计存储：与根 Transcript 隔离。

只持久化 Spec 允许的审计字段：阶段/Task/attempt、Runtime 摘要、
Tool 名与终态、唯一 truncation 标记。不落库 Reasoning、原始 Tool
参数/结果、Interaction 正文或 artifact JSON。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping

# 与 Spec 共用的 canonical 上限；store / restore / 测试必须一致。
COMPOSE_ACTIVITY_MAX_RECORDS = 500
COMPOSE_ACTIVITY_MAX_TOTAL_BYTES = 1 * 1024 * 1024
COMPOSE_ACTIVITY_MAX_TEXT_BYTES = 4 * 1024

ActivityKind = Literal["summary", "tool_terminal", "truncation"]


class ComposeActivityStoreError(RuntimeError):
    """Compose activity 存储失败；上层应 fail closed，不得假装已保存。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class ComposeActivityRecord:
    """单条有界 Compose activity 审计记录。"""

    run_id: str
    event_sequence: int
    activity_id: str
    stage: str
    attempt: int
    kind: ActivityKind
    label: str
    status: str
    created_at_ms: int
    task_id: str | None = None
    task_title: str | None = None
    execution_id: str | None = None
    agent_id: str | None = None
    bounded_text: str | None = None


def bound_activity_text(value: str | None) -> str | None:
    """按 4 KiB UTF-8 上限截断单条文本。"""
    if value is None:
        return None
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= COMPOSE_ACTIVITY_MAX_TEXT_BYTES:
        return text
    return encoded[:COMPOSE_ACTIVITY_MAX_TEXT_BYTES].decode("utf-8", errors="ignore")


def _encode_row(record: ComposeActivityRecord) -> bytes:
    """估算记录编码体积（用于 1 MiB 上限）。"""
    payload = {
        "run_id": record.run_id,
        "event_sequence": record.event_sequence,
        "activity_id": record.activity_id,
        "stage": record.stage,
        "attempt": record.attempt,
        "kind": record.kind,
        "label": record.label,
        "status": record.status,
        "created_at_ms": record.created_at_ms,
        "task_id": record.task_id,
        "task_title": record.task_title,
        "execution_id": record.execution_id,
        "agent_id": record.agent_id,
        "bounded_text": record.bounded_text,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ComposeActivityStore:
    """借用调用方连接的 Compose activity store；不拥有连接。"""

    def __init__(
        self,
        connection: Any,
        *,
        project_fingerprint: str,
        lock: Any | None = None,
    ) -> None:
        if not project_fingerprint:
            raise ComposeActivityStoreError("COMPOSE_PROJECT_FINGERPRINT_INVALID")
        self._connection = connection
        self._project_fingerprint = project_fingerprint
        self._lock = lock

    async def append(self, record: ComposeActivityRecord) -> None:
        """追加一条审计记录；达到上限后只原子写入一次 truncation。"""
        if not record.run_id or not record.activity_id:
            raise ComposeActivityStoreError("COMPOSE_ACTIVITY_RECORD_INVALID")
        if record.kind not in {"summary", "tool_terminal", "truncation"}:
            raise ComposeActivityStoreError("COMPOSE_ACTIVITY_KIND_INVALID")
        bounded = ComposeActivityRecord(
            run_id=record.run_id,
            event_sequence=max(0, int(record.event_sequence)),
            activity_id=str(record.activity_id)[:200],
            stage=str(record.stage),
            attempt=max(1, int(record.attempt)),
            kind=record.kind,
            label=str(record.label)[:200],
            status=str(record.status)[:64],
            created_at_ms=int(record.created_at_ms or int(time.time() * 1000)),
            task_id=(str(record.task_id)[:200] if record.task_id else None),
            task_title=(str(record.task_title)[:200] if record.task_title else None),
            execution_id=(str(record.execution_id)[:200] if record.execution_id else None),
            agent_id=(str(record.agent_id)[:200] if record.agent_id else None),
            bounded_text=bound_activity_text(record.bounded_text),
        )
        async with self._transaction():
            stats = await self._run_stats(bounded.run_id)
            if stats["truncated"]:
                return
            row_bytes = len(_encode_row(bounded))
            over_count = stats["count"] >= COMPOSE_ACTIVITY_MAX_RECORDS
            over_bytes = stats["total_bytes"] + row_bytes > COMPOSE_ACTIVITY_MAX_TOTAL_BYTES
            if over_count or over_bytes or bounded.kind == "truncation":
                if not stats["truncated"]:
                    await self._insert(
                        ComposeActivityRecord(
                            run_id=bounded.run_id,
                            event_sequence=bounded.event_sequence,
                            activity_id="truncation",
                            stage=bounded.stage if bounded.stage else "build",
                            attempt=1,
                            kind="truncation",
                            label="activity history truncated",
                            status="truncated",
                            created_at_ms=bounded.created_at_ms,
                            bounded_text=(
                                f"stopped after {stats['count']} records / "
                                f"{stats['total_bytes']} bytes"
                            )[:COMPOSE_ACTIVITY_MAX_TEXT_BYTES],
                        ),
                        encoded_bytes=0,
                        truncated_flag=1,
                    )
                return
            await self._insert(bounded, encoded_bytes=row_bytes, truncated_flag=0)

    async def list_for_thread(self, thread_id: str) -> tuple[ComposeActivityRecord, ...]:
        """按 thread 下所有 run 的 event_sequence/created 顺序读取 activity。"""
        if not thread_id:
            return ()
        cursor = await self._connection.execute(
            """
            SELECT a.run_id, a.event_sequence, a.activity_id, a.stage, a.attempt,
                   a.kind, a.label, a.status, a.created_at_ms, a.task_id, a.task_title,
                   a.execution_id, a.agent_id, a.bounded_text
            FROM harness_compose_activities a
            JOIN harness_compose_runs r
              ON r.project_fingerprint = a.project_fingerprint
             AND r.run_id = a.run_id
            WHERE a.project_fingerprint = ?
              AND r.thread_id = ?
            ORDER BY a.created_at_ms ASC, a.event_sequence ASC, a.rowid ASC
            """,
            (self._project_fingerprint, thread_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple(_record_from_row(row) for row in rows)

    async def list_for_run(self, run_id: str) -> tuple[ComposeActivityRecord, ...]:
        """按 run 读取 activity 序列。"""
        cursor = await self._connection.execute(
            """
            SELECT run_id, event_sequence, activity_id, stage, attempt,
                   kind, label, status, created_at_ms, task_id, task_title,
                   execution_id, agent_id, bounded_text
            FROM harness_compose_activities
            WHERE project_fingerprint = ? AND run_id = ?
            ORDER BY event_sequence ASC, rowid ASC
            """,
            (self._project_fingerprint, run_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple(_record_from_row(row) for row in rows)

    async def _run_stats(self, run_id: str) -> dict[str, Any]:
        cursor = await self._connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(encoded_bytes), 0) AS total_bytes,
                   COALESCE(MAX(truncated_flag), 0) AS truncated
            FROM harness_compose_activities
            WHERE project_fingerprint = ? AND run_id = ?
            """,
            (self._project_fingerprint, run_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return {
            "count": int(row["count"] if row else 0),
            "total_bytes": int(row["total_bytes"] if row else 0),
            "truncated": bool(row and int(row["truncated"]) > 0),
        }

    async def _insert(
        self,
        record: ComposeActivityRecord,
        *,
        encoded_bytes: int,
        truncated_flag: int,
    ) -> None:
        await self._connection.execute(
            """
            INSERT INTO harness_compose_activities (
                project_fingerprint, run_id, event_sequence, activity_id, stage,
                task_id, task_title, attempt, execution_id, agent_id, kind, label,
                status, bounded_text, created_at_ms, encoded_bytes, truncated_flag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._project_fingerprint,
                record.run_id,
                record.event_sequence,
                record.activity_id,
                record.stage,
                record.task_id,
                record.task_title,
                record.attempt,
                record.execution_id,
                record.agent_id,
                record.kind,
                record.label,
                record.status,
                record.bounded_text,
                record.created_at_ms,
                int(encoded_bytes),
                int(truncated_flag),
            ),
        )

    class _Tx:
        def __init__(self, store: ComposeActivityStore) -> None:
            self._store = store

        async def __aenter__(self):
            if self._store._lock is not None:
                await self._store._lock.acquire()
            await self._store._connection.execute("BEGIN IMMEDIATE")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    await self._store._connection.commit()
                else:
                    try:
                        await self._store._connection.rollback()
                    except Exception:
                        pass
            finally:
                if self._store._lock is not None:
                    self._store._lock.release()
            return False

    def _transaction(self) -> _Tx:
        return ComposeActivityStore._Tx(self)


def _record_from_row(row: Mapping[str, Any]) -> ComposeActivityRecord:
    return ComposeActivityRecord(
        run_id=str(row["run_id"]),
        event_sequence=int(row["event_sequence"]),
        activity_id=str(row["activity_id"]),
        stage=str(row["stage"]),
        attempt=int(row["attempt"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        label=str(row["label"]),
        status=str(row["status"]),
        created_at_ms=int(row["created_at_ms"]),
        task_id=str(row["task_id"]) if row["task_id"] is not None else None,
        task_title=str(row["task_title"]) if row["task_title"] is not None else None,
        execution_id=str(row["execution_id"]) if row["execution_id"] is not None else None,
        agent_id=str(row["agent_id"]) if row["agent_id"] is not None else None,
        bounded_text=str(row["bounded_text"]) if row["bounded_text"] is not None else None,
    )


def activity_record_to_wire(record: ComposeActivityRecord) -> dict[str, object]:
    """映射为协议 threads.open 的 compose activity 条目。"""
    payload: dict[str, object] = {
        "run_id": record.run_id,
        "event_sequence": record.event_sequence,
        "activity_id": record.activity_id,
        "stage": record.stage,
        "attempt": record.attempt,
        "kind": record.kind,
        "label": record.label,
        "status": record.status,
        "created_at_ms": record.created_at_ms,
    }
    if record.task_id:
        payload["task_id"] = record.task_id
    if record.task_title:
        payload["task_title"] = record.task_title
    if record.execution_id:
        payload["execution_id"] = record.execution_id
    if record.agent_id:
        payload["agent_id"] = record.agent_id
    if record.bounded_text:
        payload["bounded_text"] = record.bounded_text
    return payload
