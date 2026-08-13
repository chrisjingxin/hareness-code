"""Review Activity（双轴只读 Reviewer）行为测试（WP12）。

覆盖：Requirement/Code 双轴不同 execution、通过后写 review 总结证据、
Required finding 追加修复 Todo 并 FINDINGS、execution 复用 fail closed、
轴不匹配 fail closed、driver 异常 retryable、总结证据绑定文档与 workspace
revision。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.plan import _render_document as render_plan
from harness_agent.compose.activities.review import (
    ReviewActivity,
    ReviewActivityError,
    ReviewAxisResult,
    ReviewContext,
    ReviewFinding,
    ReviewOutcome,
)
from harness_agent.compose.activities.spec import _render_document as render_spec
from harness_agent.compose.activities.task import _render_document as render_task
from harness_agent.compose.document_store import ComposeDocumentStore, DocumentCommit
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeDocumentKind,
    FindingSeverity,
    ThreadMode,
)
from harness_agent.threads.compose_work_item_store import (
    CreateComposeWorkItem,
    RecordComposeConfirmation,
    RecordComposeEvidence,
    UpsertComposeDocumentReference,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-review"
WORK_ITEM_ID = "wi-review"
NOW = 1_700_000_000_000
REVISION = "rev-review"


class _FakeReviewDriver:
    """按轴返回脚本化审查结果；记录上下文。"""

    def __init__(self, by_axis: dict[str, ReviewAxisResult] | None = None) -> None:
        self.by_axis = by_axis or {
            "requirement": ReviewAxisResult("requirement", "exec-req"),
            "code": ReviewAxisResult("code", "exec-code"),
        }
        self.contexts: list[ReviewContext] = []

    async def review(self, context: ReviewContext) -> ReviewAxisResult:
        self.contexts.append(context)
        return self.by_axis[context.axis]


async def _harness(tmp_path: Path):
    """真实 SQLite + 四文档确认 + verification 总结证据的 Work Item。"""
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path / "home")
    await persistence.accept_run(
        AcceptRun(
            message="实现搜索",
            binding=make_test_binding(THREAD, "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    store = persistence.compose_work_item_store()
    await store.create(
        CreateComposeWorkItem(
            thread_id=THREAD,
            work_item_id=WORK_ITEM_ID,
            slug="review",
            goal="实现站内搜索",
            created_at_ms=NOW,
        )
    )
    documents = ComposeDocumentStore(tmp_path / "workspace")
    await _seed(store, documents)
    return persistence, store, documents


async def _seed(store, documents) -> None:
    kinds = (
        (ComposeDocumentKind.TASK, render_task(
            work_item_id=WORK_ITEM_ID, revision=1, status="proposed",
            updated_at_ms=NOW, body="# 目标\n\n实现站内搜索",
        )),
        (ComposeDocumentKind.SPEC, render_spec(
            work_item_id=WORK_ITEM_ID, kind=ComposeDocumentKind.SPEC,
            revision=1, status="proposed", updated_at_ms=NOW,
            body="# 行为规格\n\n## interface\n\n- execute_turn",
        )),
        (ComposeDocumentKind.PLAN, render_plan(
            work_item_id=WORK_ITEM_ID, kind=ComposeDocumentKind.PLAN,
            revision=1, status="proposed", updated_at_ms=NOW,
            body="# 实施计划\n\n## 步骤\n\n1. 两项",
        )),
        (ComposeDocumentKind.TODO, render_plan(
            work_item_id=WORK_ITEM_ID, kind=ComposeDocumentKind.TODO,
            revision=1, status="proposed", updated_at_ms=NOW,
            body="## 执行清单\n\n- [x] 已完成：验证=pytest tests/compose\n",
        )),
    )
    digests: dict[ComposeDocumentKind, str] = {}
    for kind, content in kinds:
        snapshot = await documents.commit(
            DocumentCommit(
                work_item_id=WORK_ITEM_ID,
                slug="review",
                kind=kind,
                content=content,
                expected=None,
            )
        )
        digests[kind] = snapshot.digest
        await store.upsert_document_reference(
            UpsertComposeDocumentReference(
                work_item_id=WORK_ITEM_ID,
                kind=kind,
                relative_path=snapshot.relative_path,
                content_digest=snapshot.digest,
                revision=1,
                updated_at_ms=NOW,
            )
        )
    for confirmation_kind, digest_kinds in (
        ("task", (ComposeDocumentKind.TASK,)),
        ("spec", (ComposeDocumentKind.TASK, ComposeDocumentKind.SPEC)),
        ("plan", (ComposeDocumentKind.PLAN, ComposeDocumentKind.TODO)),
    ):
        await store.record_confirmation(
            RecordComposeConfirmation(
                work_item_id=WORK_ITEM_ID,
                confirmation_id=f"{confirmation_kind}-gate-fixture",
                confirmation_kind=confirmation_kind,
                document_digests=tuple(digests[kind] for kind in digest_kinds),
                confirmed_at_ms=NOW,
            )
        )
    await store.record_evidence(
        RecordComposeEvidence(
            evidence_id=f"verification:{WORK_ITEM_ID}:{NOW}",
            work_item_id=WORK_ITEM_ID,
            evidence_kind="verification",
            content_digest="f" * 64,
            payload={
                "workspace_revision": REVISION,
                "document_digests": [digests[kind] for kind in digests],
                "passed": True,
                "execution_id": "verify-exec",
            },
            created_at_ms=NOW,
        )
    )


def _activity(store, documents, driver):
    return ReviewActivity(
        store=store,
        documents=documents,
        driver=driver,
        workspace_revision=lambda: REVISION,
        now_ms=lambda: NOW,
    )


async def _item(store):
    item = await store.load(WORK_ITEM_ID)
    assert item is not None
    return item


async def test_two_axes_pass_records_review_evidence(tmp_path: Path) -> None:
    """双轴不同 execution 通过：写绑定文档与 workspace revision 的总结证据。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeReviewDriver()
        result = await _activity(store, documents, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is ReviewOutcome.COMPLETED
        assert [context.axis for context in driver.contexts] == [
            "requirement",
            "code",
        ]
        axis_evidence = await store.load_evidence(WORK_ITEM_ID, "review_axis")
        assert len(axis_evidence) == 2
        summary = await store.load_evidence(WORK_ITEM_ID, "review")
        assert len(summary) == 1
        assert summary[0].payload["requirement"]["execution_id"] == "exec-req"
        assert summary[0].payload["code"]["execution_id"] == "exec-code"
        assert summary[0].payload["workspace_revision"] == REVISION
        activity = await store.load_activity(f"review:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
    finally:
        await persistence.close()


async def test_required_finding_appends_fix_todo_and_returns_findings(tmp_path: Path) -> None:
    """Required finding 追加来源明确的修复 Todo，Review 不通过。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeReviewDriver(
            {
                "requirement": ReviewAxisResult("requirement", "exec-req"),
                "code": ReviewAxisResult(
                    "code",
                    "exec-code",
                    findings=(
                        ReviewFinding(
                            FindingSeverity.REQUIRED,
                            "缺少错误语义测试",
                            location="/tests",
                        ),
                    ),
                ),
            }
        )
        result = await _activity(store, documents, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is ReviewOutcome.FINDINGS
        assert result.pending == "review-findings"
        todo = await documents.inspect(WORK_ITEM_ID, "review", ComposeDocumentKind.TODO)
        assert todo is not None
        assert "- [ ] 修复 Review required" in todo.content
        assert await store.load_evidence(WORK_ITEM_ID, "review") == ()
        activity = await store.load_activity(f"review:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.FAILED
    finally:
        await persistence.close()


async def test_reused_execution_identity_fails_closed(tmp_path: Path) -> None:
    """两个 Reviewer 不能复用作者 execution identity。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeReviewDriver(
            {
                "requirement": ReviewAxisResult("requirement", "same-exec"),
                "code": ReviewAxisResult("code", "same-exec"),
            }
        )
        with pytest.raises(ReviewActivityError) as excinfo:
            await _activity(store, documents, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_REVIEW_EXECUTION_REUSED"
        activity = await store.load_activity(f"review:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
    finally:
        await persistence.close()


async def test_axis_mismatch_fails_closed(tmp_path: Path) -> None:
    """driver 返回错误轴的结果不能写入证据。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeReviewDriver(
            {
                "requirement": ReviewAxisResult("code", "exec-req"),
                "code": ReviewAxisResult("code", "exec-code"),
            }
        )
        with pytest.raises(ReviewActivityError) as excinfo:
            await _activity(store, documents, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_REVIEW_AXIS_MISMATCH"
    finally:
        await persistence.close()


async def test_driver_failure_marks_retryable_failed(tmp_path: Path) -> None:
    """driver 429 等执行失败：Activity retryable_failed，不终结 Work Item。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        class _BrokenDriver:
            async def review(self, context: ReviewContext) -> ReviewAxisResult:
                raise RuntimeError("429 rate limited")

        with pytest.raises(ReviewActivityError) as excinfo:
            await _activity(store, documents, _BrokenDriver()).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_REVIEW_EXECUTION_FAILED"
        activity = await store.load_activity(f"review:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
        assert not (await _item(store)).terminal
    finally:
        await persistence.close()
