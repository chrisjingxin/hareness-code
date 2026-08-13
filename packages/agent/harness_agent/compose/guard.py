"""Completion Guard：report.md 生成与 Work Item complete CAS。

Guard 是不可绕过的终态模块：只有全部 readiness predicate 满足且没有
pending/unknown effect 时，才先让 ReportDriver 生成 report.md（引用当前
文档、verification 与 review digest），再用 revision CAS 提交 completed。
模型输出不能直接终结 Work Item；任何谓词不满足或 effect 未对账都 fail
closed。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

from harness_agent.compose.document_store import (
    ComposeDocumentSnapshot,
    ComposeDocumentStore,
    ComposeDocumentStoreError,
    DocumentCommit,
)
from harness_agent.compose.models import (
    ComposeDocumentKind,
    ComposeWorkItem,
    ComposeWorkItemStatus,
)
from harness_agent.threads.compose_work_item_store import (
    ComposeWorkItemStore,
    ComposeWorkItemStoreError,
    RecordComposeEvidence,
    TerminalizeComposeWorkItem,
    UpsertComposeDocumentReference,
)

REPORT_DOCUMENT_KIND = ComposeDocumentKind.REPORT


class CompletionGuardError(RuntimeError):
    """Completion Guard 的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class ReportContext:
    """report driver 一次成稿所需的受限上下文。"""

    goal: str
    task_digest: str
    spec_digest: str
    plan_digest: str
    todo_digest: str
    verification_digest: str
    requirement_review_digest: str
    code_review_digest: str
    workspace_revision: str


class ReportDriver(Protocol):
    """report 模型回合 seam；只消费摘要引用，不携带全文。"""

    async def draft_report(self, context: ReportContext) -> str: ...


@dataclass(frozen=True, slots=True)
class ReportWriteResult:
    """report.md 写入与证据记录结果。"""

    snapshot: ComposeDocumentSnapshot
    source_digests: frozenset[str]


class CompletionGuard:
    """report 生成 + complete CAS 的唯一终态路径。"""

    def __init__(
        self,
        *,
        store: ComposeWorkItemStore,
        documents: ComposeDocumentStore,
        driver: ReportDriver | None,
        workspace_revision: Callable[[], Awaitable[str | None]] | None,
        now_ms: Callable[[], int | None] | None = None,
    ) -> None:
        self._store = store
        self._documents = documents
        self._driver = driver
        self._workspace_revision = workspace_revision
        self._now_ms_port = now_ms

    async def write_report(
        self,
        item: ComposeWorkItem,
        *,
        task_digest: str,
        spec_digest: str,
        plan_digest: str,
        todo_digest: str,
        verification_digest: str,
        requirement_review_digest: str,
        code_review_digest: str,
    ) -> ReportWriteResult:
        """让 driver 生成 report.md 并记录绑定全部输入的引用证据。"""
        if self._driver is None:
            raise CompletionGuardError("COMPLETION_REPORT_DRIVER_MISSING")
        revision = (
            await self._workspace_revision()
            if self._workspace_revision is not None
            else None
        )
        if not isinstance(revision, str) or not revision:
            raise CompletionGuardError("COMPLETION_WORKSPACE_REVISION_MISSING")
        context = ReportContext(
            goal=item.goal,
            task_digest=task_digest,
            spec_digest=spec_digest,
            plan_digest=plan_digest,
            todo_digest=todo_digest,
            verification_digest=verification_digest,
            requirement_review_digest=requirement_review_digest,
            code_review_digest=code_review_digest,
            workspace_revision=revision,
        )
        try:
            body = await self._driver.draft_report(context)
        except Exception as exc:
            raise CompletionGuardError("COMPLETION_REPORT_EXECUTION_FAILED") from exc
        existing = await self._read_snapshot(item)
        content = _render_report_document(
            work_item_id=item.work_item_id,
            revision=(existing.revision if existing is not None else 0) + 1,
            status="proposed",
            updated_at_ms=self._now_ms(),
            body=body,
        )
        try:
            snapshot = await self._documents.commit(
                DocumentCommit(
                    work_item_id=item.work_item_id,
                    slug=item.slug,
                    kind=REPORT_DOCUMENT_KIND,
                    content=content,
                    expected=existing,
                )
            )
            await self._store.upsert_document_reference(
                UpsertComposeDocumentReference(
                    work_item_id=item.work_item_id,
                    kind=REPORT_DOCUMENT_KIND,
                    relative_path=snapshot.relative_path,
                    content_digest=snapshot.digest,
                    revision=snapshot.revision,
                    updated_at_ms=self._now_ms(),
                )
            )
        except ComposeDocumentStoreError as exc:
            raise CompletionGuardError("COMPLETION_REPORT_DRAFT_INVALID") from exc
        except ComposeWorkItemStoreError as exc:
            raise CompletionGuardError("COMPLETION_REPORT_WRITE_FAILED") from exc
        source_digests = frozenset(
            {
                task_digest,
                spec_digest,
                plan_digest,
                todo_digest,
                verification_digest,
                requirement_review_digest,
                code_review_digest,
            }
        )
        await self._record_report_evidence(item, snapshot, source_digests)
        return ReportWriteResult(snapshot=snapshot, source_digests=source_digests)

    async def complete(
        self,
        item: ComposeWorkItem,
        *,
        now_ms: int,
    ) -> ComposeWorkItem:
        """以 revision CAS 提交 completed；模型与普通 Turn 都不能绕过。"""
        try:
            return await self._store.terminalize(
                TerminalizeComposeWorkItem(
                    work_item_id=item.work_item_id,
                    expected_revision=item.revision,
                    status=ComposeWorkItemStatus.COMPLETED,
                    terminal_at_ms=now_ms,
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise CompletionGuardError("COMPLETION_CAS_FAILED") from exc

    async def _record_report_evidence(
        self,
        item: ComposeWorkItem,
        snapshot: ComposeDocumentSnapshot,
        source_digests: frozenset[str],
    ) -> None:
        """记录 report 正文与其全部输入摘要的引用事实。"""
        payload = {
            "document_digest": snapshot.digest,
            "source_digests": sorted(source_digests),
        }
        try:
            await self._store.record_evidence(
                RecordComposeEvidence(
                    evidence_id=f"report:{item.work_item_id}:{self._now_ms()}",
                    work_item_id=item.work_item_id,
                    evidence_kind="report",
                    content_digest=_digest_of(
                        snapshot.digest + "|" + "|".join(sorted(source_digests))
                    ),
                    payload=payload,
                    created_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise CompletionGuardError("COMPLETION_REPORT_EVIDENCE_WRITE_FAILED") from exc

    async def _read_snapshot(
        self,
        item: ComposeWorkItem,
    ) -> ComposeDocumentSnapshot | None:
        try:
            return await self._documents.inspect(
                item.work_item_id, item.slug, REPORT_DOCUMENT_KIND
            )
        except ComposeDocumentStoreError as exc:
            raise CompletionGuardError("COMPLETION_REPORT_DOCUMENT_INVALID") from exc

    def _now_ms(self) -> int:
        now = self._now_ms_port() if self._now_ms_port is not None else None
        return int(now) if now is not None else int(time.time() * 1000)


def _render_report_document(
    *,
    work_item_id: str,
    revision: int,
    status: str,
    updated_at_ms: int,
    body: str,
) -> str:
    """渲染带固定 front matter 的 report.md。"""
    body_text = body.strip()
    return (
        "---\n"
        f"work_item_id: {work_item_id}\n"
        "kind: report\n"
        f"revision: {revision}\n"
        f"status: {status}\n"
        f"updated_at: {updated_at_ms}\n"
        "---\n"
        f"{body_text}\n"
    )


def _digest_of(text: str) -> str:
    """生成稳定 SHA-256 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
