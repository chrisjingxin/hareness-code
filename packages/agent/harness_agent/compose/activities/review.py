"""Review Activity：双轴只读 Reviewer 与 Required finding fix loop。

`verification_fresh` 后才可执行本 Activity。Requirement 与 Code 是两个独立
Managed execution，使用不同 execution identity，作者 execution 不得兼任；
Required/Critical finding 追加来源明确的修复 Todo 并回到 Implement。
两个 Review 均通过且无 Required finding 时才写入 review 总结证据。
Reviewer 保持只读：本 Activity 不提供文件写入 seam，driver 契约不接收
mutation 能力，测试以 fake driver 证明只读调用面。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

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
    FindingSeverity,
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

REVIEW_ACTIVITY_KIND = "review"
"""review Activity 的稳定 ledger kind。"""

_BLOCKING_SEVERITIES = frozenset(
    {FindingSeverity.CRITICAL, FindingSeverity.REQUIRED}
)


class ReviewActivityError(RuntimeError):
    """Review Activity 的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class ReviewOutcome(str, Enum):
    """Review Activity 的收敛结果。"""

    COMPLETED = "completed"
    FINDINGS = "findings"


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """单轴 Review 的一条 finding。"""

    severity: FindingSeverity
    message: str
    location: str = ""


@dataclass(frozen=True, slots=True)
class ReviewAxisResult:
    """一次单轴 Review 的执行事实。"""

    axis: str
    execution_id: str
    findings: tuple[ReviewFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """reviewer 一次审查的受限上下文。"""

    goal: str
    axis: str
    task_digest: str
    spec_digest: str
    plan_digest: str
    todo_digest: str
    verification_digest: str


class ReviewDriver(Protocol):
    """只读 Reviewer seam；生产实现绑定原版 code-review-and-quality。"""

    async def review(self, context: ReviewContext) -> ReviewAxisResult: ...


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Review Activity 一次执行的收敛结果。"""

    outcome: ReviewOutcome
    pending: str | None


class ReviewActivity:
    """双轴只读 Review + Required finding fix todo 回写。"""

    def __init__(
        self,
        *,
        store: ComposeWorkItemStore,
        documents: ComposeDocumentStore,
        driver: ReviewDriver,
        workspace_revision: Callable[[], Awaitable[str | None]] | None,
        now_ms: Callable[[], int | None] | None = None,
    ) -> None:
        self._store = store
        self._documents = documents
        self._driver = driver
        self._workspace_revision = workspace_revision
        self._now_ms_port = now_ms

    async def run(
        self,
        item: ComposeWorkItem,
        *,
        run_id: str,
    ) -> ReviewResult:
        """执行或恢复 Review Activity；所有退出路径收敛 Activity ledger。"""
        activity_id = f"review:{item.work_item_id}"
        await self._ensure_activity_running(activity_id, item, run_id)
        try:
            return await self._run_reviews(item, activity_id)
        except asyncio.CancelledError:
            raise
        except ReviewActivityError:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise
        except Exception as exc:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise ReviewActivityError("COMPOSE_REVIEW_EXECUTION_FAILED") from exc

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
                        kind=REVIEW_ACTIVITY_KIND,
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
        except ReviewActivityError:
            raise
        except ComposeWorkItemStoreError as exc:
            raise ReviewActivityError("COMPOSE_REVIEW_LEDGER_FAILED") from exc

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
            raise ReviewActivityError("COMPOSE_REVIEW_LEDGER_FAILED") from exc

    async def _run_reviews(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> ReviewResult:
        digests = await self._document_digests(item)
        verification = await self._latest_verification_digest(item)
        if verification is None:
            raise ReviewActivityError("COMPOSE_REVIEW_VERIFICATION_MISSING")
        requirement = await self._run_axis(
            item, "requirement", digests, verification
        )
        code = await self._run_axis(item, "code", digests, verification)
        if requirement.execution_id == code.execution_id:
            raise ReviewActivityError("COMPOSE_REVIEW_EXECUTION_REUSED")
        blocking = [
            finding
            for result in (requirement, code)
            for finding in result.findings
            if finding.severity in _BLOCKING_SEVERITIES
        ]
        if blocking:
            await self._append_fix_todo(item, blocking)
            await self._finish(activity_id, ComposeActivityStatus.FAILED)
            return ReviewResult(ReviewOutcome.FINDINGS, pending="review-findings")
        await self._record_review_evidence(item, requirement, code, digests)
        await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
        return ReviewResult(ReviewOutcome.COMPLETED, pending=None)

    async def _run_axis(
        self,
        item: ComposeWorkItem,
        axis: str,
        digests: tuple[str, ...],
        verification_digest: str,
    ) -> ReviewAxisResult:
        context = ReviewContext(
            goal=item.goal,
            axis=axis,
            task_digest=digests[0],
            spec_digest=digests[1],
            plan_digest=digests[2],
            todo_digest=digests[3],
            verification_digest=verification_digest,
        )
        try:
            result = await self._driver.review(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ReviewActivityError("COMPOSE_REVIEW_EXECUTION_FAILED") from exc
        if result.axis != axis:
            raise ReviewActivityError("COMPOSE_REVIEW_AXIS_MISMATCH")
        await self._record_axis_evidence(item, result, digests, verification_digest)
        return result

    async def _record_axis_evidence(
        self,
        item: ComposeWorkItem,
        result: ReviewAxisResult,
        digests: tuple[str, ...],
        verification_digest: str,
    ) -> None:
        try:
            await self._store.record_evidence(
                RecordComposeEvidence(
                    evidence_id=(
                        f"review-axis:{item.work_item_id}:{result.axis}:"
                        f"{result.execution_id}"
                    ),
                    work_item_id=item.work_item_id,
                    evidence_kind="review_axis",
                    content_digest=_digest_of(
                        result.axis
                        + "|"
                        + "|".join(digests)
                        + "|"
                        + verification_digest
                    ),
                    payload={
                        "axis": result.axis,
                        "execution_id": result.execution_id,
                        "findings": [
                            {
                                "severity": finding.severity.value,
                                "message": finding.message,
                                "location": finding.location,
                            }
                            for finding in result.findings
                        ],
                    },
                    created_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise ReviewActivityError("COMPOSE_REVIEW_EVIDENCE_WRITE_FAILED") from exc

    async def _record_review_evidence(
        self,
        item: ComposeWorkItem,
        requirement: ReviewAxisResult,
        code: ReviewAxisResult,
        digests: tuple[str, ...],
    ) -> None:
        """双轴通过后写入绑定文档与 workspace revision 的总结证据。"""
        revision = (
            await self._workspace_revision()
            if self._workspace_revision is not None
            else None
        )
        if not isinstance(revision, str) or not revision:
            raise ReviewActivityError("COMPOSE_REVIEW_WORKSPACE_REVISION_MISSING")
        payload = {
            "workspace_revision": revision,
            "document_digests": sorted(frozenset(digests)),
            "requirement": {
                "execution_id": requirement.execution_id,
                "passed": True,
            },
            "code": {
                "execution_id": code.execution_id,
                "passed": True,
            },
            "no_required_findings": True,
        }
        try:
            await self._store.record_evidence(
                RecordComposeEvidence(
                    evidence_id=f"review:{item.work_item_id}:{self._now_ms()}",
                    work_item_id=item.work_item_id,
                    evidence_kind="review",
                    content_digest=_digest_of(
                        revision
                        + "|"
                        + requirement.execution_id
                        + "|"
                        + code.execution_id
                        + "|"
                        + "|".join(sorted(frozenset(digests)))
                    ),
                    payload=payload,
                    created_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise ReviewActivityError("COMPOSE_REVIEW_EVIDENCE_WRITE_FAILED") from exc

    async def _append_fix_todo(
        self,
        item: ComposeWorkItem,
        findings: list[ReviewFinding],
    ) -> None:
        """Required finding 生成来源明确的修复 Todo，使下游证据全部 stale。"""
        todo = await self._read_snapshot(item, ComposeDocumentKind.TODO)
        if todo is None:
            raise ReviewActivityError("COMPOSE_REVIEW_TODO_MISSING")
        lines = [todo.content.rstrip("\n")]
        for finding in findings:
            lines.append(
                f"- [ ] 修复 Review {finding.severity.value}："
                f"{finding.message}（来源=review{finding.location}）"
            )
        updated = "\n".join(lines) + "\n"
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
            raise ReviewActivityError("COMPOSE_REVIEW_FIX_TODO_FAILED") from exc
        except ComposeWorkItemStoreError as exc:
            raise ReviewActivityError("COMPOSE_REVIEW_FIX_TODO_FAILED") from exc

    async def _latest_verification_digest(self, item: ComposeWorkItem) -> str | None:
        try:
            records = await self._store.load_evidence(
                item.work_item_id, "verification"
            )
        except ComposeWorkItemStoreError:
            return None
        if not records:
            return None
        return records[-1].content_digest

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
                raise ReviewActivityError("COMPOSE_REVIEW_DOCUMENTS_MISSING")
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
            raise ReviewActivityError("COMPOSE_REVIEW_DOCUMENT_INVALID") from exc

    def _now_ms(self) -> int:
        now = self._now_ms_port() if self._now_ms_port is not None else None
        return int(now) if now is not None else int(time.time() * 1000)


def _digest_of(text: str) -> str:
    """生成稳定 SHA-256 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
