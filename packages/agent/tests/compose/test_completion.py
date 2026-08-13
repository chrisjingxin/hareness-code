"""Completion Guard 与全链路自动闭环测试（WP12 Checkpoint C tracer）。

覆盖：report.md 生成绑定全部输入 digest、pending/unknown effect 阻止
complete、Guard complete 走 revision CAS、模型不能直接终结 Work Item、
引擎在批准实施后一个 Turn 内自动闭环 Implement → Verify → Review →
Report → Complete，完成后同 Thread 可创建下一个 Work Item。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.implement import (
    ImplementItemOutcome,
    ImplementItemResult,
)
from harness_agent.compose.activities.plan import _render_document as render_plan
from harness_agent.compose.activities.review import ReviewAxisResult
from harness_agent.compose.activities.spec import _render_document as render_spec
from harness_agent.compose.activities.task import _render_document as render_task
from harness_agent.compose.activities.verify import VerificationCommandResult
from harness_agent.compose.document_store import ComposeDocumentStore, DocumentCommit
from harness_agent.compose.guard import (
    CompletionGuard,
    CompletionGuardError,
    ReportContext,
)
from harness_agent.compose.models import (
    ComposeDocumentKind,
    ComposeEffectStatus,
    ComposeWorkItemStatus,
    ThreadMode,
)
from harness_agent.compose.work_item_engine import (
    ComposeTurnOutcome,
    ComposeTurnPorts,
    ComposeTurnRequest,
    ComposeWorkItemEngine,
    TypedDecisionRequest,
    TypedDecisionResult,
)
from harness_agent.threads.compose_work_item_store import (
    CreateComposeWorkItem,
    RecordComposeEffectIntent,
    RecordComposeConfirmation,
    UpsertComposeDocumentReference,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import async_return, test_binding as make_test_binding

THREAD = "thread-completion"
WORK_ITEM_ID = "wi-completion"
NOW = 1_700_000_000_000
REVISION = "rev-completion"

REPORT_BODY = "# 完成报告\n\n实现站内搜索已完成：验证与双轴 Review 全部通过。\n"


class _FakeReportDriver:
    """脚本化 report 成稿；记录上下文。"""

    def __init__(self, body: str = REPORT_BODY) -> None:
        self.body = body
        self.contexts: list[ReportContext] = []

    async def draft_report(self, context: ReportContext) -> str:
        self.contexts.append(context)
        return self.body


async def _guard_harness(tmp_path: Path):
    """真实 SQLite + 文档齐全的 Work Item + 已确认上游。"""
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
            slug="completion",
            goal="实现站内搜索",
            created_at_ms=NOW,
        )
    )
    documents = ComposeDocumentStore(tmp_path / "workspace")
    digests: dict[ComposeDocumentKind, str] = {}
    for kind, content in (
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
            body="# 实施计划\n\n## 步骤\n\n1. 一项",
        )),
        (ComposeDocumentKind.TODO, render_plan(
            work_item_id=WORK_ITEM_ID, kind=ComposeDocumentKind.TODO,
            revision=1, status="proposed", updated_at_ms=NOW,
            body="## 执行清单\n\n- [ ] 实现一项：验证=pytest tests/compose\n",
        )),
    ):
        snapshot = await documents.commit(
            DocumentCommit(
                work_item_id=WORK_ITEM_ID,
                slug="completion",
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
    return persistence, store, documents, digests


def _guard(store, documents, driver):
    return CompletionGuard(
        store=store,
        documents=documents,
        driver=driver,
        workspace_revision=async_return(REVISION),
        now_ms=lambda: NOW,
    )


async def _item(store):
    item = await store.load(WORK_ITEM_ID)
    assert item is not None
    return item


async def test_write_report_binds_all_input_digests(tmp_path: Path) -> None:
    """report.md 引用全部输入摘要；外部修改 report 使引用 stale。"""
    persistence, store, documents, digests = await _guard_harness(tmp_path)
    try:
        driver = _FakeReportDriver()
        result = await _guard(store, documents, driver).write_report(
            await _item(store),
            task_digest=digests[ComposeDocumentKind.TASK],
            spec_digest=digests[ComposeDocumentKind.SPEC],
            plan_digest=digests[ComposeDocumentKind.PLAN],
            todo_digest=digests[ComposeDocumentKind.TODO],
            verification_digest="f" * 64,
            requirement_review_digest="a" * 64,
            code_review_digest="b" * 64,
        )
        report = await documents.inspect(
            WORK_ITEM_ID, "completion", ComposeDocumentKind.REPORT
        )
        assert report is not None
        assert report.digest == result.snapshot.digest
        evidence = await store.load_evidence(WORK_ITEM_ID, "report")
        assert len(evidence) == 1
        assert evidence[0].payload["document_digest"] == report.digest
        assert len(evidence[0].payload["source_digests"]) == 7
        assert "f" * 64 in evidence[0].payload["source_digests"]
    finally:
        await persistence.close()


async def test_complete_cas_requires_exact_revision(tmp_path: Path) -> None:
    """complete 走 revision CAS；陈旧 revision 拒绝。"""
    persistence, store, documents, _ = await _guard_harness(tmp_path)
    try:
        guard = _guard(store, documents, _FakeReportDriver())
        item = await _item(store)
        completed = await guard.complete(item, now_ms=NOW)
        assert completed.status is ComposeWorkItemStatus.COMPLETED
        assert completed.terminal_at_ms == NOW
        with pytest.raises(CompletionGuardError) as excinfo:
            await guard.complete(item, now_ms=NOW + 1)
        assert excinfo.value.code == "COMPLETION_CAS_FAILED"
    finally:
        await persistence.close()


async def test_pending_effect_blocks_completion_fact(tmp_path: Path) -> None:
    """存在 intent 无 receipt 的 effect 时 completion fact 不 ready。"""
    persistence, store, documents, _ = await _guard_harness(tmp_path)
    try:
        await store.record_effect_intent(
            RecordComposeEffectIntent(
                effect_key=f"file:{WORK_ITEM_ID}:call-1",
                work_item_id=WORK_ITEM_ID,
                activity_id=None,
                intent={"tool": "write", "path": "/src/a.py"},
                created_at_ms=NOW,
            )
        )
        effects = await store.load_effects(WORK_ITEM_ID)
        assert any(effect.status is ComposeEffectStatus.INTENT for effect in effects)
    finally:
        await persistence.close()


# ---------- 引擎全链路自动闭环 ----------


class _FakeInteraction:
    """按 question_id 脚本化回答。"""

    def __init__(self, answers: dict[str, list[str | None]] | None = None) -> None:
        self.answers: dict[str, list[str | None]] = {
            key: list(values) for key, values in (answers or {}).items()
        }
        self.requests: list[TypedDecisionRequest] = []

    async def request_decision(self, request: TypedDecisionRequest) -> TypedDecisionResult:
        self.requests.append(request)
        queue = self.answers.get(request.question_id, [])
        value = queue.pop(0) if queue else None
        if value is None:
            return TypedDecisionResult({"answers": {}}, expired=True)
        return TypedDecisionResult({"answers": {request.question_id: [value]}})


class _InstantTaskDriver:
    async def next_question(self, _context: object) -> None:
        return None

    async def draft_task(self, _context: object) -> str:
        return "# 目标\n\n实现站内搜索"


class _InstantSpecDriver:
    async def draft_spec(self, _context: object) -> str:
        return "# 行为规格\n\n## interface\n\n- execute_turn"


class _InstantPlanDriver:
    async def draft_plan(self, _context: object):
        from harness_agent.compose.activities.plan import PlanDraft

        return PlanDraft(
            plan_body="# 实施计划\n\n## 步骤\n\n1. 一项",
            todo_body="## 执行清单\n\n- [ ] 实现一项：验证=pytest tests/compose\n",
        )


class _InstantImplementDriver:
    def __init__(self) -> None:
        self.calls = 0

    async def implement_item(self, _context: object) -> ImplementItemResult:
        self.calls += 1
        return ImplementItemResult(
            outcome=ImplementItemOutcome.COMPLETED,
            fail_before="RED: 1 failed",
            pass_after="GREEN: 1 passed",
            changed_paths=("src/a.py",),
            execution_id=f"exec-{self.calls}",
        )


class _InstantVerifyPort:
    async def run_command(self, command: str, *, work_item_id: str):
        return VerificationCommandResult(
            command=command,
            exit_code=0,
            output_digest="c" * 64,
            execution_id="verify-exec",
        )


class _InstantReviewDriver:
    def __init__(self) -> None:
        self.axes = 0

    async def review(self, context: object) -> ReviewAxisResult:
        self.axes += 1
        return ReviewAxisResult(context.axis, f"review-exec-{self.axes}")


class _PipelineHarness:
    """引擎 + 全部 fake 驱动；三门禁三 Turn 后一个 Turn 自动闭环。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.persistence: ThreadPersistence | None = None

    async def open(self) -> None:
        project = self.tmp_path / "project"
        project.mkdir()
        self.persistence = await ThreadPersistence.open(
            project=project, home=self.tmp_path / "home"
        )
        await self.persistence.accept_run(
            AcceptRun(
                message="受理",
                binding=make_test_binding(THREAD, "run-0"),
                mode=ThreadMode.COMPOSE,
            )
        )

    def engine(self, interaction: _FakeInteraction) -> ComposeWorkItemEngine:
        assert self.persistence is not None

        class _Classifier:
            def __init__(self) -> None:
                self.outputs: list[object] = []

            async def classify(self, _context: object) -> object:
                return self.outputs.pop(0)

        classifier = _Classifier()
        ports = ComposeTurnPorts(
            store=self.persistence.compose_work_item_store(),
            documents=ComposeDocumentStore(self.workspace),
            classifier=classifier,
            interaction=interaction,
            workspace_revision=async_return(REVISION),
            now_ms=lambda: NOW,
            task_driver=_InstantTaskDriver(),
            spec_driver=_InstantSpecDriver(),
            plan_driver=_InstantPlanDriver(),
            implement_driver=_InstantImplementDriver(),
            verify_port=_InstantVerifyPort(),
            review_driver=_InstantReviewDriver(),
            report_driver=_FakeReportDriver(),
        )
        engine = ComposeWorkItemEngine(ports)
        engine._classifier = classifier  # noqa: SLF001
        return engine

    async def close(self) -> None:
        assert self.persistence is not None
        await self.persistence.close()


def _turn(message: str, run_id: str, intent: str) -> ComposeTurnRequest:
    return ComposeTurnRequest(
        thread_id=THREAD,
        run_id=run_id,
        message=message,
        explicit_intent=None,
        cancelled=False,
    )


async def test_engine_auto_closes_full_pipeline_and_accepts_next_work_item(
    tmp_path: Path,
) -> None:
    """三门禁三个 Turn；批准后一个 Turn 自动闭环到 completed，可再建下一项。"""
    harness = _PipelineHarness(tmp_path)
    try:
        await harness.open()
        interaction = _FakeInteraction(
            {"task-gate": ["confirm"], "spec-gate": ["confirm"], "plan-gate": ["confirm"]}
        )
        engine = harness.engine(interaction)
        engine._classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
        first = await engine.execute_turn(_turn("实现站内搜索", "run-1", "start_new_work"))
        assert first.work_item is not None
        assert first.work_item.readiness.task_confirmed is True

        engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
        second = await engine.execute_turn(_turn("继续", "run-2", "resume_current"))
        assert second.work_item is not None
        assert second.work_item.readiness.spec_confirmed is True

        engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
        third = await engine.execute_turn(_turn("继续", "run-3", "resume_current"))
        assert third.work_item is not None
        assert third.work_item.readiness.plan_confirmed is True

        # 批准实施后自动闭环：Implement → Verify → Review → Report → Complete。
        engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
        fourth = await engine.execute_turn(_turn("继续", "run-4", "resume_current"))
        assert fourth.work_item is not None
        assert fourth.status is ComposeTurnOutcome.COMPLETED
        assert fourth.work_item.status == "completed"
        store = harness.persistence.compose_work_item_store()
        completed = await store.load(fourth.work_item.work_item_id)
        assert completed is not None
        assert completed.status is ComposeWorkItemStatus.COMPLETED
        # 完成后同 Thread 创建下一个 Work Item。
        engine._classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
        interaction2 = _FakeInteraction({"task-gate": ["confirm"]})
        next_engine = harness.engine(interaction2)
        next_engine._classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
        next_result = await next_engine.execute_turn(
            _turn("实现第二个功能", "run-5", "start_new_work")
        )
        assert next_result.work_item is not None
        assert next_result.work_item.work_item_id != fourth.work_item.work_item_id
    finally:
        await harness.close()
