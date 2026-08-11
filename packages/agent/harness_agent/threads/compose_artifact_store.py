"""Compose artifact 的 SQLite 存储：借用 ThreadPersistence 的连接与事务锁。

只保存结构化 ComposeRun projection 与 ComposeArtifact；不写 Transcript 或
ContextArtifact 表，也不恢复历史 running 状态为 active Run。终态只允许
保存一次，重复保存以 COMPOSE_TERMINAL_DUPLICATE 拒绝。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Mapping

from harness_agent.compose.models import (
    ArtifactKind,
    ChangeKind,
    ComposeArtifact,
    ComposeRunState,
    ComposeRunStatus,
    ComposeStage,
    ComposeStoreError,
    ComposeTask,
    EvidenceItem,
    EvidenceStatus,
    StageState,
    TaskStatus,
)


def _canonical_json(value: object) -> str:
    """稳定 JSON 编码，保证同一事实只产生同一字节串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stage_from(value: str) -> ComposeStage:
    return ComposeStage(value)


def _task_from(raw: Mapping[str, Any]) -> ComposeTask:
    return ComposeTask(
        id=str(raw["id"]),
        title=str(raw["title"]),
        kind=ChangeKind(str(raw["kind"])),
        acceptance=str(raw["acceptance"]),
        depends_on=tuple(str(item) for item in raw.get("depends_on", ())),
        verification_commands=tuple(str(item) for item in raw.get("verification_commands", ())),
        status=TaskStatus(str(raw.get("status", "pending"))),
    )


class ComposeArtifactStore:
    """借用调用方连接的项目级 Compose 存储；不拥有也不关闭连接。"""

    def __init__(
        self,
        connection,
        *,
        project_fingerprint: str,
        lock: asyncio.Lock | None = None,
    ) -> None:
        """保存项目指纹与共享事务锁；store 本身不打开文件。"""
        if not project_fingerprint:
            raise ComposeStoreError("COMPOSE_PROJECT_FINGERPRINT_INVALID")
        self._connection = connection
        self._project_fingerprint = project_fingerprint
        self._lock = lock or asyncio.Lock()
        self._ready = False

    async def setup(self) -> None:
        """幂等确保 Compose 表存在（migration 已创建时的兜底）。"""
        async with self._lock:
            if self._ready:
                return
            await self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS harness_compose_runs (
                    project_fingerprint TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stages_json TEXT NOT NULL,
                    stage_attempts_json TEXT NOT NULL,
                    schema_retry_used_json TEXT NOT NULL,
                    tasks_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    understanding_artifact_id TEXT,
                    plan_artifact_id TEXT,
                    verification_evidence_id TEXT,
                    review_report_id TEXT,
                    verify_fix_round INTEGER NOT NULL,
                    review_fix_round INTEGER NOT NULL,
                    blocked_reason TEXT,
                    terminal_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (project_fingerprint, run_id)
                )
                """
            )
            await self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS harness_compose_runs_thread_updated
                    ON harness_compose_runs(project_fingerprint, thread_id, updated_at_ms DESC)
                """
            )
            await self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS harness_compose_artifacts (
                    project_fingerprint TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_execution_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    PRIMARY KEY (project_fingerprint, run_id, artifact_id)
                )
                """
            )
            await self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS harness_compose_artifacts_run_created
                    ON harness_compose_artifacts(project_fingerprint, run_id, created_at_ms)
                """
            )
            await self._connection.commit()
            self._ready = True

    async def save_run(self, state: ComposeRunState) -> None:
        """原子 upsert 一个 ComposeRun projection；终态只允许保存一次。"""
        await self.setup()
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(
                    """
                    SELECT terminal_count
                    FROM harness_compose_runs
                    WHERE project_fingerprint = ? AND run_id = ?
                    """,
                    (self._project_fingerprint, state.run_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
                existing_terminal = (
                    int(row["terminal_count"]) > 0 if row is not None else False
                )
                if state.terminal:
                    if existing_terminal:
                        raise ComposeStoreError(
                            "COMPOSE_TERMINAL_DUPLICATE",
                            f"run {state.run_id} already reached a terminal state",
                        )
                elif existing_terminal:
                    # 终态是不可逆审计事实：不允许用非终态覆盖已终态的行。
                    raise ComposeStoreError(
                        "COMPOSE_TERMINAL_OVERWRITE",
                        f"run {state.run_id} terminal state cannot be overwritten",
                    )
                terminal_count = 1 if state.terminal else 0
                await self._connection.execute(
                    """
                    INSERT INTO harness_compose_runs (
                        project_fingerprint, thread_id, run_id, revision, stage, status,
                        stages_json, stage_attempts_json, schema_retry_used_json, tasks_json, evidence_json,
                        understanding_artifact_id, plan_artifact_id,
                        verification_evidence_id, review_report_id,
                        verify_fix_round, review_fix_round, blocked_reason,
                        terminal_count, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_fingerprint, run_id) DO UPDATE SET
                        thread_id = excluded.thread_id,
                        revision = excluded.revision,
                        stage = excluded.stage,
                        status = excluded.status,
                        stages_json = excluded.stages_json,
                        stage_attempts_json = excluded.stage_attempts_json,
                        schema_retry_used_json = excluded.schema_retry_used_json,
                        tasks_json = excluded.tasks_json,
                        evidence_json = excluded.evidence_json,
                        understanding_artifact_id = excluded.understanding_artifact_id,
                        plan_artifact_id = excluded.plan_artifact_id,
                        verification_evidence_id = excluded.verification_evidence_id,
                        review_report_id = excluded.review_report_id,
                        verify_fix_round = excluded.verify_fix_round,
                        review_fix_round = excluded.review_fix_round,
                        blocked_reason = excluded.blocked_reason,
                        terminal_count = excluded.terminal_count,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        self._project_fingerprint,
                        state.thread_id,
                        state.run_id,
                        state.revision,
                        state.stage.value,
                        state.status.value,
                        _canonical_json(
                            {stage.value: state_value.value for stage, state_value in state.stages.items()}
                        ),
                        _canonical_json(
                            {stage.value: attempts for stage, attempts in state.stage_attempts.items()}
                        ),
                        _canonical_json(
                            {stage.value: used for stage, used in state.schema_retry_used.items()}
                        ),
                        _canonical_json(
                            [
                                {
                                    "id": task.id,
                                    "title": task.title,
                                    "kind": task.kind.value if hasattr(task.kind, "value") else task.kind,
                                    "acceptance": task.acceptance,
                                    "depends_on": list(task.depends_on),
                                    "verification_commands": list(task.verification_commands),
                                    "status": task.status.value,
                                }
                                for task in state.tasks
                            ]
                        ),
                        _canonical_json(
                            [{"label": item.label, "status": item.status.value} for item in state.evidence]
                        ),
                        state.understanding_artifact_id,
                        state.plan_artifact_id,
                        state.verification_evidence_id,
                        state.review_report_id,
                        state.verify_fix_round,
                        state.review_fix_round,
                        state.blocked_reason,
                        terminal_count,
                        int(time.time() * 1000),
                    ),
                )
                await self._connection.commit()
            except BaseException:
                # 拒绝/异常路径必须回滚，避免连接残留半开事务污染后续写。
                try:
                    await self._connection.rollback()
                except Exception:
                    pass
                raise

    async def load_run(self, run_id: str) -> ComposeRunState | None:
        """按 run_id 恢复最近保存的完整状态；历史 running 不恢复为 active。"""
        await self.setup()
        async with self._lock, self._connection.execute(
            """
            SELECT thread_id, revision, stage, status, stages_json, stage_attempts_json,
                   schema_retry_used_json, tasks_json, evidence_json, understanding_artifact_id, plan_artifact_id,
                   verification_evidence_id, review_report_id, verify_fix_round,
                   review_fix_round, blocked_reason
            FROM harness_compose_runs
            WHERE project_fingerprint = ? AND run_id = ?
            """,
            (self._project_fingerprint, run_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        stages = {
            _stage_from(stage): StageState(value)
            for stage, value in json.loads(str(row["stages_json"])).items()
        }
        attempts = {
            _stage_from(stage): int(value)
            for stage, value in json.loads(str(row["stage_attempts_json"])).items()
        }
        retry_used = {
            _stage_from(stage): bool(value)
            for stage, value in json.loads(str(row["schema_retry_used_json"])).items()
        }
        tasks = tuple(_task_from(task) for task in json.loads(str(row["tasks_json"])))
        evidence = tuple(
            EvidenceItem(label=str(item["label"]), status=EvidenceStatus(str(item["status"])))
            for item in json.loads(str(row["evidence_json"]))
        )
        return ComposeRunState(
            thread_id=str(row["thread_id"]),
            run_id=run_id,
            revision=int(row["revision"]),
            stage=_stage_from(str(row["stage"])),
            status=ComposeRunStatus(str(row["status"])),
            stages=stages,
            stage_attempts=attempts,
            schema_retry_used=retry_used,
            understanding_artifact_id=(
                str(row["understanding_artifact_id"]) if row["understanding_artifact_id"] is not None else None
            ),
            plan_artifact_id=str(row["plan_artifact_id"]) if row["plan_artifact_id"] is not None else None,
            verification_evidence_id=(
                str(row["verification_evidence_id"]) if row["verification_evidence_id"] is not None else None
            ),
            review_report_id=str(row["review_report_id"]) if row["review_report_id"] is not None else None,
            tasks=tasks,
            evidence=evidence,
            verify_fix_round=int(row["verify_fix_round"]),
            review_fix_round=int(row["review_fix_round"]),
            blocked_reason=str(row["blocked_reason"]) if row["blocked_reason"] is not None else None,
        )

    async def terminal_count(self, run_id: str) -> int:
        """返回该 Run 已保存的终态投影数量（正常应为 0 或 1）。"""
        await self.setup()
        async with self._lock, self._connection.execute(
            """
            SELECT terminal_count
            FROM harness_compose_runs
            WHERE project_fingerprint = ? AND run_id = ?
            """,
            (self._project_fingerprint, run_id),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["terminal_count"]) if row is not None else 0

    async def save_artifact(self, artifact: ComposeArtifact) -> None:
        """写入一个 ComposeArtifact；同 identity 重复写以最新为准。"""
        await self.setup()
        async with self._lock:
            await self._connection.execute(
                """
                INSERT INTO harness_compose_artifacts (
                    project_fingerprint, run_id, artifact_id, kind, version,
                    source_execution_id, created_at_ms, payload_json, content_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_fingerprint, run_id, artifact_id) DO UPDATE SET
                    kind = excluded.kind,
                    version = excluded.version,
                    source_execution_id = excluded.source_execution_id,
                    created_at_ms = excluded.created_at_ms,
                    payload_json = excluded.payload_json,
                    content_digest = excluded.content_digest
                """,
                (
                    self._project_fingerprint,
                    artifact.run_id,
                    artifact.artifact_id,
                    artifact.kind.value,
                    artifact.version,
                    artifact.source_execution_id,
                    artifact.created_at_ms,
                    _canonical_json(dict(artifact.payload)),
                    artifact.content_digest,
                ),
            )
            await self._connection.commit()

    async def load_artifact(self, run_id: str, artifact_id: str) -> ComposeArtifact | None:
        """按 run/artifact identity 读取一个 artifact。"""
        await self.setup()
        async with self._lock, self._connection.execute(
            """
            SELECT artifact_id, kind, version, source_execution_id, created_at_ms,
                   payload_json, content_digest
            FROM harness_compose_artifacts
            WHERE project_fingerprint = ? AND run_id = ? AND artifact_id = ?
            """,
            (self._project_fingerprint, run_id, artifact_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return ComposeArtifact(
            artifact_id=str(row["artifact_id"]),
            kind=ArtifactKind(str(row["kind"])),
            version=int(row["version"]),
            run_id=run_id,
            source_execution_id=str(row["source_execution_id"]),
            created_at_ms=int(row["created_at_ms"]),
            payload=json.loads(str(row["payload_json"])),
            content_digest=str(row["content_digest"]),
        )

    async def list_artifacts(self, run_id: str) -> tuple[ComposeArtifact, ...]:
        """列出 Run 的全部 artifact，按创建时间升序。"""
        await self.setup()
        async with self._lock, self._connection.execute(
            """
            SELECT artifact_id, kind, version, source_execution_id, created_at_ms,
                   payload_json, content_digest
            FROM harness_compose_artifacts
            WHERE project_fingerprint = ? AND run_id = ?
            ORDER BY created_at_ms, artifact_id
            """,
            (self._project_fingerprint, run_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(
            ComposeArtifact(
                artifact_id=str(row["artifact_id"]),
                kind=ArtifactKind(str(row["kind"])),
                version=int(row["version"]),
                run_id=run_id,
                source_execution_id=str(row["source_execution_id"]),
                created_at_ms=int(row["created_at_ms"]),
                payload=json.loads(str(row["payload_json"])),
                content_digest=str(row["content_digest"]),
            )
            for row in rows
        )
