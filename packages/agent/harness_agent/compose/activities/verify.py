"""Verify Activity：按 todo 声明的 required command 运行真实验证。

只有 `implementation_current` 后才会进入本 Activity。全部 required command
对同一 workspace revision 产生 exit code 0 才写入 verification 总结证据；
任一失败追加来源明确的修复 Todo 并回到 Implement。命令输出 digest、exit
code 与执行 identity 写入证据，模型输出不能充当 exit-code evidence。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from harness_agent.compose.document_store import (
    ComposeDocumentSnapshot,
    ComposeDocumentStore,
    ComposeDocumentStoreError,
    DocumentCommit,
)
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeDocumentKind,
    ComposeWorkItem,
)
from harness_agent.threads.compose_work_item_store import (
    ComposeWorkItemStore,
    ComposeWorkItemStoreError,
    FinishComposeActivity,
    RecordComposeEvidence,
    RestartComposeActivity,
    StartComposeActivity,
    UpsertComposeDocumentReference,
)

VERIFY_ACTIVITY_KIND = "verify"
"""verify Activity 的稳定 ledger kind。"""

_COMMAND_IN_TODO = re.compile(r"验证=([^\n]+)")


class VerifyActivityError(RuntimeError):
    """Verify Activity 的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class VerifyOutcome(str, Enum):
    """Verify Activity 的收敛结果。"""

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class VerificationCommandResult:
    """一次真实命令执行的事实。"""

    command: str
    exit_code: int
    output_digest: str
    execution_id: str


class VerificationPort(Protocol):
    """canonical 命令执行 seam；生产实现绑定 Policy/Sandbox/审批链路。"""

    async def run_command(
        self,
        command: str,
        *,
        work_item_id: str,
    ) -> VerificationCommandResult: ...


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Verify Activity 一次执行的收敛结果。"""

    outcome: VerifyOutcome
    pending: str | None


class VerifyActivity:
    """required command 全量真实验证 + fix todo 回写。"""

    def __init__(
        self,
        *,
        store: ComposeWorkItemStore,
        documents: ComposeDocumentStore,
        port: VerificationPort,
        workspace_revision: Callable[[], str | None] | None,
        now_ms: Callable[[], int | None] | None = None,
    ) -> None:
        self._store = store
        self._documents = documents
        self._port = port
        self._workspace_revision = workspace_revision
        self._now_ms_port = now_ms

    async def run(
        self,
        item: ComposeWorkItem,
        *,
        run_id: str,
    ) -> VerifyResult:
        """执行或恢复 Verify Activity；所有退出路径收敛 Activity ledger。"""
        activity_id = f"verify:{item.work_item_id}"
        await self._ensure_activity_running(activity_id, item, run_id)
        try:
            return await self._run_commands(item, activity_id)
        except asyncio.CancelledError:
            raise
        except VerifyActivityError:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise
        except Exception as exc:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise VerifyActivityError("COMPOSE_VERIFY_EXECUTION_FAILED") from exc

    async def _ensure_activity_running(
        self,
        activity_id: str,
        item: ComposeWorkItem,
        run_id: str,
    ) -> None:
        try:
            existing = await self._store.load_activity(activity_id)
            if existing is None:
                await self._store.start_activity(
                    StartComposeActivity(
                        activity_id=activity_id,
                        work_item_id=item.work_item_id,
                        run_id=run_id,
                        kind=VERIFY_ACTIVITY_KIND,
                        started_at_ms=self._now_ms(),
                    )
                )
                return
            if existing.status is ComposeActivityStatus.RUNNING:
                return
            await self._store.restart_activity(
                RestartComposeActivity(
                    activity_id=activity_id,
                    run_id=run_id,
                    started_at_ms=self._now_ms(),
                )
            )
        except VerifyActivityError:
            raise
        except ComposeWorkItemStoreError as exc:
            raise VerifyActivityError("COMPOSE_VERIFY_LEDGER_FAILED") from exc

    async def _finish(
        self,
        activity_id: str,
        status: ComposeActivityStatus,
    ) -> None:
        try:
            await self._store.finish_activity(
                FinishComposeActivity(
                    activity_id=activity_id,
                    status=status,
                    finished_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            if str(exc).startswith("COMPOSE_ACTIVITY_STATUS_CONFLICT"):
                return
            raise VerifyActivityError("COMPOSE_VERIFY_LEDGER_FAILED") from exc

    async def _run_commands(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> VerifyResult:
        todo = await self._read_snapshot(item, ComposeDocumentKind.TODO)
        if todo is None:
            raise VerifyActivityError("COMPOSE_VERIFY_TODO_MISSING")
        commands = tuple(
            match.group(1).strip()
            for match in _COMMAND_IN_TODO.finditer(todo.content)
            if match.group(1).strip()
        )
        if not commands:
            raise VerifyActivityError("COMPOSE_VERIFY_COMMANDS_MISSING")
        for command in commands:
            try:
                result = await self._port.run_command(
                    command,
                    work_item_id=item.work_item_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise VerifyActivityError("COMPOSE_VERIFY_EXECUTION_FAILED") from exc
            await self._record_command_evidence(item, result)
            if result.exit_code != 0:
                await self._append_fix_todo(item, todo, command, result)
                await self._finish(activity_id, ComposeActivityStatus.FAILED)
                return VerifyResult(VerifyOutcome.FAILED, pending="verify-failed")
        await self._record_verification_evidence(item)
        await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
        return VerifyResult(VerifyOutcome.COMPLETED, pending=None)

    async def _record_command_evidence(
        self,
        item: ComposeWorkItem,
        result: VerificationCommandResult,
    ) -> None:
        try:
            await self._store.record_evidence(
                RecordComposeEvidence(
                    evidence_id=(
                        f"verification-command:{item.work_item_id}:"
                        f"{result.execution_id}"
                    ),
                    work_item_id=item.work_item_id,
                    evidence_kind="verification_command",
                    content_digest=result.output_digest,
                    payload={
                        "command": result.command,
                        "exit_code": result.exit_code,
                        "execution_id": result.execution_id,
                    },
                    created_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise VerifyActivityError("COMPOSE_VERIFY_EVIDENCE_WRITE_FAILED") from exc

    async def _record_verification_evidence(self, item: ComposeWorkItem) -> None:
        """全部命令通过后写入绑定文档与 workspace revision 的总结证据。"""
        revision = (
            self._workspace_revision()
            if self._workspace_revision is not None
            else None
        )
        if not isinstance(revision, str) or not revision:
            raise VerifyActivityError("COMPOSE_VERIFY_WORKSPACE_REVISION_MISSING")
        digests = frozenset(await self._document_digests(item))
        payload = {
            "workspace_revision": revision,
            "document_digests": sorted(digests),
            "passed": True,
            "execution_id": f"verify:{item.work_item_id}:{self._now_ms()}",
        }
        try:
            await self._store.record_evidence(
                RecordComposeEvidence(
                    evidence_id=f"verification:{item.work_item_id}:{self._now_ms()}",
                    work_item_id=item.work_item_id,
                    evidence_kind="verification",
                    content_digest=_digest_of(
                        revision + "|" + "|".join(sorted(digests))
                    ),
                    payload=payload,
                    created_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise VerifyActivityError("COMPOSE_VERIFY_EVIDENCE_WRITE_FAILED") from exc

    async def _append_fix_todo(
        self,
        item: ComposeWorkItem,
        todo: ComposeDocumentSnapshot,
        command: str,
        result: VerificationCommandResult,
    ) -> None:
        """失败命令生成来源明确的修复 Todo，使实现证据自动 stale。"""
        content = todo.content.rstrip("\n")
        fix_line = (
            f"- [ ] 修复验证失败：{command}（exit {result.exit_code}，"
            f"来源=verify）"
        )
        updated = f"{content}\n{fix_line}\n"
        try:
            snapshot = await self._documents.commit(
                DocumentCommit(
                    work_item_id=item.work_item_id,
                    slug=item.slug,
                    kind=ComposeDocumentKind.TODO,
                    content=updated,
                    expected=todo,
                )
            )
            await self._store.upsert_document_reference(
                UpsertComposeDocumentReference(
                    work_item_id=item.work_item_id,
                    kind=ComposeDocumentKind.TODO,
                    relative_path=snapshot.relative_path,
                    content_digest=snapshot.digest,
                    revision=snapshot.revision,
                    updated_at_ms=self._now_ms(),
                )
            )
        except ComposeDocumentStoreError as exc:
            raise VerifyActivityError("COMPOSE_VERIFY_FIX_TODO_FAILED") from exc
        except ComposeWorkItemStoreError as exc:
            raise VerifyActivityError("COMPOSE_VERIFY_FIX_TODO_FAILED") from exc

    async def _document_digests(self, item: ComposeWorkItem) -> tuple[str, ...]:
        digests: list[str] = []
        for kind in (
            ComposeDocumentKind.TASK,
            ComposeDocumentKind.SPEC,
            ComposeDocumentKind.PLAN,
            ComposeDocumentKind.TODO,
        ):
            snapshot = await self._read_snapshot(item, kind)
            if snapshot is None:
                raise VerifyActivityError("COMPOSE_VERIFY_DOCUMENTS_MISSING")
            digests.append(snapshot.digest)
        return tuple(digests)

    async def _read_snapshot(
        self,
        item: ComposeWorkItem,
        kind: ComposeDocumentKind,
    ) -> ComposeDocumentSnapshot | None:
        try:
            return await self._documents.inspect(item.work_item_id, item.slug, kind)
        except ComposeDocumentStoreError as exc:
            raise VerifyActivityError("COMPOSE_VERIFY_DOCUMENT_INVALID") from exc

    def _now_ms(self) -> int:
        now = self._now_ms_port() if self._now_ms_port is not None else None
        return int(now) if now is not None else int(time.time() * 1000)


def _digest_of(text: str) -> str:
    """生成稳定 SHA-256 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
