"""Spec gate（spec.md 草稿 + typed confirmation）行为测试（WP9）。

覆盖：Task 已确认后生成 spec.md、确认绑定 Task+Spec digest 组合、修改
feedback 生成新 revision 并 stale 旧确认、放弃保留文件、Task 缺失/malformed/
driver 失败 fail closed、引擎按 readiness 顺序进入 Spec gate、Task 未确认
不调用 Spec driver。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.spec import (
    SpecDraftContext,
    SpecGateActivity,
    SpecGateActivityError,
    SpecGateOutcome,
)
from harness_agent.compose.activities.task import _render_document as render_task
from harness_agent.compose.document_store import ComposeDocumentStore, DocumentCommit
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeDocumentKind,
    ThreadMode,
)
from harness_agent.compose.work_item_engine import (
    ComposeTurnPorts,
    ComposeTurnRequest,
    ComposeWorkItemEngine,
    TypedDecisionRequest,
    TypedDecisionResult,
)
from harness_agent.threads.compose_work_item_store import (
    CreateComposeWorkItem,
    RecordComposeConfirmation,
    UpsertComposeDocumentReference,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-spec-gate"
WORK_ITEM_ID = "wi-spec-gate"
NOW = 1_700_000_000_000

SPEC_BODY = "# 行为规格\n\n## 公开 interface\n\n- execute_turn / inspect / abandon\n\n## 错误语义\n\n- THREAD_MODE_LOCKED\n"


class _FakeSpecDriver:
    """脚本化 spec 草稿生成；记录上下文；可注入异常。"""

    def __init__(
        self,
        drafts: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.drafts = list(drafts or [SPEC_BODY])
        self.error = error
        self.contexts: list[SpecDraftContext] = []

    async def draft_spec(self, context: SpecDraftContext) -> str:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.drafts.pop(0) if self.drafts else SPEC_BODY


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


async def _harness(tmp_path: Path):
    """真实 SQLite + workspace 文档存储 + active Work Item + 已确认 Task。"""
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
            slug="spec-gate",
            goal="实现站内搜索",
            created_at_ms=NOW,
        )
    )
    documents = ComposeDocumentStore(tmp_path / "workspace")
    return persistence, store, documents


async def _confirm_task(store, documents) -> str:
    """写入 task.md 并记录 task 确认，返回 task digest。"""
    content = render_task(
        work_item_id=WORK_ITEM_ID,
        revision=1,
        status="proposed",
        updated_at_ms=NOW,
        body="# 目标\n\n实现站内搜索",
    )
    snapshot = await documents.commit(
        DocumentCommit(
            work_item_id=WORK_ITEM_ID,
            slug="spec-gate",
            kind=ComposeDocumentKind.TASK,
            content=content,
            expected=None,
        )
    )
    await store.upsert_document_reference(
        UpsertComposeDocumentReference(
            work_item_id=WORK_ITEM_ID,
            kind=ComposeDocumentKind.TASK,
            relative_path=snapshot.relative_path,
            content_digest=snapshot.digest,
            revision=snapshot.revision,
            updated_at_ms=NOW,
        )
    )
    await store.record_confirmation(
        RecordComposeConfirmation(
            work_item_id=WORK_ITEM_ID,
            confirmation_id="task-gate-fixture",
            confirmation_kind="task",
            document_digests=(snapshot.digest,),
            confirmed_at_ms=NOW,
        )
    )
    return snapshot.digest


def _activity(store, documents, interaction, driver):
    return SpecGateActivity(
        store=store,
        documents=documents,
        interaction=interaction,
        driver=driver,
        now_ms=lambda: NOW,
    )


async def _item(store):
    item = await store.load(WORK_ITEM_ID)
    assert item is not None
    return item


async def test_spec_draft_and_confirm_binds_task_and_spec_digests(tmp_path: Path) -> None:
    """生成 spec.md 后确认绑定 Task+Spec digest 组合并完成 Activity。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        task_digest = await _confirm_task(store, documents)
        driver = _FakeSpecDriver()
        interaction = _FakeInteraction({"spec-gate": ["confirm"]})
        result = await _activity(store, documents, interaction, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is SpecGateOutcome.CONFIRMED
        snapshot = await documents.inspect(WORK_ITEM_ID, "spec-gate", ComposeDocumentKind.SPEC)
        assert snapshot is not None
        assert snapshot.status == "proposed"
        assert driver.contexts[0].task_digest == task_digest
        assert "# 目标" in driver.contexts[0].task_body
        refs = await store.load_document_references(WORK_ITEM_ID)
        spec_ref = next(ref for ref in refs if ref.kind is ComposeDocumentKind.SPEC)
        assert spec_ref.confirmed_digest == snapshot.digest
        groups = await store.load_confirmation_groups(WORK_ITEM_ID, "spec")
        assert frozenset({task_digest, snapshot.digest}) in groups
        activity = await store.load_activity(f"spec:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
    finally:
        await persistence.close()


async def test_revise_feedback_generates_new_revision_and_stales_old_confirmation(
    tmp_path: Path,
) -> None:
    """修改 feedback 重新成稿；旧确认保留审计但不再绑定新 digest。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        await _confirm_task(store, documents)
        first = _FakeSpecDriver()
        first_interaction = _FakeInteraction({"spec-gate": ["confirm"]})
        first_result = await _activity(store, documents, first_interaction, first).run(
            await _item(store), run_id="run-1"
        )
        assert first_result.outcome is SpecGateOutcome.CONFIRMED
        old_digest = (
            await documents.inspect(WORK_ITEM_ID, "spec-gate", ComposeDocumentKind.SPEC)
        ).digest

        revise_interaction = _FakeInteraction(
            {
                "spec-gate": ["revise", "confirm"],
                "spec-feedback": ["补充安全边界"],
            }
        )
        revise_driver = _FakeSpecDriver(drafts=["# 行为规格\n\n## 安全边界\n\n- 工作空间限定"])
        second = await _activity(store, documents, revise_interaction, revise_driver).run(
            await _item(store), run_id="run-2"
        )
        assert second.outcome is SpecGateOutcome.CONFIRMED
        new_digest = (
            await documents.inspect(WORK_ITEM_ID, "spec-gate", ComposeDocumentKind.SPEC)
        ).digest
        assert new_digest != old_digest
        assert second.revision > first_result.revision
        audited = await store.load_confirmation_digests(WORK_ITEM_ID, "spec")
        assert old_digest in audited
        assert any(context.feedback == "补充安全边界" for context in revise_driver.contexts)
    finally:
        await persistence.close()


async def test_spec_gate_abandon_keeps_files(tmp_path: Path) -> None:
    """gate 放弃：文件保留，Activity 完成，Work Item 终态由 engine CAS 负责。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        await _confirm_task(store, documents)
        driver = _FakeSpecDriver()
        interaction = _FakeInteraction({"spec-gate": ["abandon"]})
        result = await _activity(store, documents, interaction, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is SpecGateOutcome.ABANDONED
        snapshot = await documents.inspect(WORK_ITEM_ID, "spec-gate", ComposeDocumentKind.SPEC)
        assert snapshot is not None
        item = await _item(store)
        assert not item.terminal
    finally:
        await persistence.close()


async def test_missing_task_fails_closed(tmp_path: Path) -> None:
    """Task 未写入时不能生成 Spec，Activity retryable_failed。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeSpecDriver()
        interaction = _FakeInteraction({})
        with pytest.raises(SpecGateActivityError) as excinfo:
            await _activity(store, documents, interaction, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_SPEC_TASK_MISSING"
        activity = await store.load_activity(f"spec:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
    finally:
        await persistence.close()


async def test_malformed_draft_fails_closed_and_marks_retryable(tmp_path: Path) -> None:
    """driver 产出空正文时 fail closed：无 spec 文档引用，Activity retryable。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        await _confirm_task(store, documents)
        driver = _FakeSpecDriver(drafts=[""])
        interaction = _FakeInteraction({})
        with pytest.raises(SpecGateActivityError) as excinfo:
            await _activity(store, documents, interaction, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_SPEC_DRAFT_INVALID"
        refs = await store.load_document_references(WORK_ITEM_ID)
        assert all(ref.kind is not ComposeDocumentKind.SPEC for ref in refs)
        activity = await store.load_activity(f"spec:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
    finally:
        await persistence.close()


async def test_driver_failure_marks_retryable_failed(tmp_path: Path) -> None:
    """driver 429 等执行失败：Activity retryable_failed，不终结 Work Item。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        await _confirm_task(store, documents)
        driver = _FakeSpecDriver(error=RuntimeError("429 rate limited"))
        interaction = _FakeInteraction({})
        with pytest.raises(SpecGateActivityError) as excinfo:
            await _activity(store, documents, interaction, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_SPEC_EXECUTION_FAILED"
        activity = await store.load_activity(f"spec:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
        assert not (await _item(store)).terminal
    finally:
        await persistence.close()


# ---------- 引擎集成 ----------


class _EngineHarness:
    """引擎 + 真实 store/documents + fake 分类/interaction/drivers。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.persistence: ThreadPersistence | None = None
        self.classifier_outputs: list[object] = [{"intent": "start_new_work"}]

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

    def engine(
        self,
        *,
        interaction: _FakeInteraction | None = None,
        task_driver: object | None = None,
        spec_driver: object | None = None,
    ) -> ComposeWorkItemEngine:
        assert self.persistence is not None

        class _Classifier:
            def __init__(self, outputs: list[object]) -> None:
                self.outputs = list(outputs)

            async def classify(self, _context: object) -> object:
                return self.outputs.pop(0)

        ports = ComposeTurnPorts(
            store=self.persistence.compose_work_item_store(),
            documents=ComposeDocumentStore(self.workspace),
            classifier=_Classifier(list(self.classifier_outputs)),
            interaction=interaction or _FakeInteraction({}),
            now_ms=lambda: NOW,
            task_driver=task_driver,
            spec_driver=spec_driver,
        )
        return ComposeWorkItemEngine(ports)

    async def close(self) -> None:
        assert self.persistence is not None
        await self.persistence.close()


def _turn(message: str, run_id: str) -> ComposeTurnRequest:
    return ComposeTurnRequest(
        thread_id=THREAD,
        run_id=run_id,
        message=message,
        explicit_intent=None,
        cancelled=False,
    )


class _InstantTaskDriver:
    """直接成稿的 grill driver：无问题、固定正文。"""

    def __init__(self) -> None:
        self.contexts = 0

    async def next_question(self, _context: object) -> None:
        return None

    async def draft_task(self, _context: object) -> str:
        self.contexts += 1
        return "# 目标\n\n实现站内搜索"


async def test_engine_runs_spec_gate_after_task_confirmed(tmp_path: Path) -> None:
    """创建 → Task gate 确认 → 下一 Turn 自动进入 Spec gate 并确认。"""
    harness = _EngineHarness(tmp_path)
    harness.classifier_outputs = [
        {"intent": "start_new_work"},
        {"intent": "resume_current"},
    ]
    try:
        await harness.open()
        task_driver = _InstantTaskDriver()
        spec_driver = _FakeSpecDriver()
        interaction = _FakeInteraction(
            {"task-gate": ["confirm"], "spec-gate": ["confirm"]}
        )
        engine = harness.engine(
            interaction=interaction, task_driver=task_driver, spec_driver=spec_driver
        )
        first = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert first.work_item is not None
        assert first.work_item.readiness.task_confirmed is True
        assert first.work_item.readiness.spec_confirmed is False

        engine = harness.engine(
            interaction=interaction, task_driver=task_driver, spec_driver=spec_driver
        )
        second = await engine.execute_turn(_turn("继续", "run-2"))
        assert second.work_item is not None
        assert second.work_item.readiness.spec_confirmed is True
        assert second.work_item.current_activity == "plan"
        assert second.pending_decision is None
        assert len(spec_driver.contexts) == 1
    finally:
        await harness.close()


async def test_engine_does_not_call_spec_driver_before_task_confirmed(tmp_path: Path) -> None:
    """Task 未确认时 Spec driver 不可达，顺序由 readiness gate 决定。"""
    harness = _EngineHarness(tmp_path)
    try:
        await harness.open()
        spec_driver = _FakeSpecDriver()
        interaction = _FakeInteraction({})
        engine = harness.engine(interaction=interaction, spec_driver=spec_driver)
        result = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert result.work_item is not None
        assert result.work_item.readiness.task_confirmed is False
        assert spec_driver.contexts == []
    finally:
        await harness.close()
