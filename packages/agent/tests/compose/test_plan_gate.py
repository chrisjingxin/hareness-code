"""Plan/Todo gate（成对草稿 + 联合确认）行为测试（WP10）。

覆盖：Spec 已确认后成对生成 plan.md/todo.md、确认绑定 Plan+Todo digest
组合、revise 重跑 Plan Activity 不重新 grill Task/Spec、任一文件外部修改使
联合确认 stale、placeholder/禁词/空 todo fail closed、放弃保留文件、引擎按
readiness 顺序进入 Plan gate、Spec 未确认时 Plan driver 不可达。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.plan import (
    PlanDraft,
    PlanDraftContext,
    PlanGateActivity,
    PlanGateActivityError,
    PlanGateOutcome,
)
from harness_agent.compose.activities.spec import _render_document as render_spec
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

THREAD = "thread-plan-gate"
WORK_ITEM_ID = "wi-plan-gate"
NOW = 1_700_000_000_000

PLAN_BODY = "# 实施计划\n\n## 步骤\n\n1. 建立 store\n2. 接入 engine\n"
TODO_BODY = (
    "## 执行清单\n\n"
    "- [ ] 建立 SQLite 事实层：验收=唯一 active，验证=pytest tests/threads\n"
    "- [ ] 接入 engine 流水线：验收=门禁顺序，验证=pytest tests/compose\n"
)


class _FakePlanDriver:
    """脚本化 plan/todo 成对草稿生成；记录上下文；可注入异常。"""

    def __init__(
        self,
        drafts: list[PlanDraft] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.drafts = list(drafts or [PlanDraft(PLAN_BODY, TODO_BODY)])
        self.error = error
        self.contexts: list[PlanDraftContext] = []

    async def draft_plan(self, context: PlanDraftContext) -> PlanDraft:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.drafts.pop(0) if self.drafts else PlanDraft(PLAN_BODY, TODO_BODY)


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
    """真实 SQLite + workspace 文档存储 + active Work Item。"""
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
            slug="plan-gate",
            goal="实现站内搜索",
            created_at_ms=NOW,
        )
    )
    documents = ComposeDocumentStore(tmp_path / "workspace")
    return persistence, store, documents


async def _confirm_upstream(store, documents) -> tuple[str, str]:
    """写入并确认 Task 与 Spec，返回 (task_digest, spec_digest)。"""
    task_snapshot = await documents.commit(
        DocumentCommit(
            work_item_id=WORK_ITEM_ID,
            slug="plan-gate",
            kind=ComposeDocumentKind.TASK,
            content=render_task(
                work_item_id=WORK_ITEM_ID,
                revision=1,
                status="proposed",
                updated_at_ms=NOW,
                body="# 目标\n\n实现站内搜索",
            ),
            expected=None,
        )
    )
    await store.upsert_document_reference(
        UpsertComposeDocumentReference(
            work_item_id=WORK_ITEM_ID,
            kind=ComposeDocumentKind.TASK,
            relative_path=task_snapshot.relative_path,
            content_digest=task_snapshot.digest,
            revision=1,
            updated_at_ms=NOW,
        )
    )
    await store.record_confirmation(
        RecordComposeConfirmation(
            work_item_id=WORK_ITEM_ID,
            confirmation_id="task-gate-fixture",
            confirmation_kind="task",
            document_digests=(task_snapshot.digest,),
            confirmed_at_ms=NOW,
        )
    )
    spec_snapshot = await documents.commit(
        DocumentCommit(
            work_item_id=WORK_ITEM_ID,
            slug="plan-gate",
            kind=ComposeDocumentKind.SPEC,
            content=render_spec(
                work_item_id=WORK_ITEM_ID,
                kind=ComposeDocumentKind.SPEC,
                revision=1,
                status="proposed",
                updated_at_ms=NOW,
                body="# 行为规格\n\n## interface\n\n- execute_turn",
            ),
            expected=None,
        )
    )
    await store.upsert_document_reference(
        UpsertComposeDocumentReference(
            work_item_id=WORK_ITEM_ID,
            kind=ComposeDocumentKind.SPEC,
            relative_path=spec_snapshot.relative_path,
            content_digest=spec_snapshot.digest,
            revision=1,
            updated_at_ms=NOW,
        )
    )
    await store.record_confirmation(
        RecordComposeConfirmation(
            work_item_id=WORK_ITEM_ID,
            confirmation_id="spec-gate-fixture",
            confirmation_kind="spec",
            document_digests=(task_snapshot.digest, spec_snapshot.digest),
            confirmed_at_ms=NOW,
        )
    )
    return task_snapshot.digest, spec_snapshot.digest


def _activity(store, documents, interaction, driver):
    return PlanGateActivity(
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


async def test_plan_todo_draft_and_confirm_binds_pair_digests(tmp_path: Path) -> None:
    """成对生成 plan/todo 并确认绑定二者 digest 组合，Activity 完成。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        task_digest, spec_digest = await _confirm_upstream(store, documents)
        driver = _FakePlanDriver()
        interaction = _FakeInteraction({"plan-gate": ["confirm"]})
        result = await _activity(store, documents, interaction, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is PlanGateOutcome.CONFIRMED
        plan = await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.PLAN)
        todo = await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.TODO)
        assert plan is not None and todo is not None
        assert plan.revision == todo.revision
        assert driver.contexts[0].spec_digest == spec_digest
        assert driver.contexts[0].task_digest == task_digest
        groups = await store.load_confirmation_groups(WORK_ITEM_ID, "plan")
        assert frozenset({plan.digest, todo.digest}) in groups
        activity = await store.load_activity(f"plan:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
    finally:
        await persistence.close()


async def test_revise_feedback_regenerates_pair_without_touching_upstream(tmp_path: Path) -> None:
    """修改 feedback 只重跑 Plan Activity；Task/Spec 内容与确认不变。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        task_digest, spec_digest = await _confirm_upstream(store, documents)
        first = _FakePlanDriver()
        first_interaction = _FakeInteraction({"plan-gate": ["confirm"]})
        first_result = await _activity(store, documents, first_interaction, first).run(
            await _item(store), run_id="run-1"
        )
        assert first_result.outcome is PlanGateOutcome.CONFIRMED
        old_plan_digest = (
            await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.PLAN)
        ).digest

        revise_interaction = _FakeInteraction(
            {
                "plan-gate": ["revise", "confirm"],
                "plan-feedback": ["步骤合并为三个"],
            }
        )
        revise_driver = _FakePlanDriver(
            drafts=[
                PlanDraft(
                    "# 实施计划\n\n## 步骤\n\n1. 三合一\n",
                    TODO_BODY,
                )
            ]
        )
        second = await _activity(store, documents, revise_interaction, revise_driver).run(
            await _item(store), run_id="run-2"
        )
        assert second.outcome is PlanGateOutcome.CONFIRMED
        new_plan_digest = (
            await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.PLAN)
        ).digest
        assert new_plan_digest != old_plan_digest
        assert second.revision > first_result.revision
        task = await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.TASK)
        spec = await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.SPEC)
        assert task is not None and task.digest == task_digest
        assert spec is not None and spec.digest == spec_digest
        assert any(context.feedback == "步骤合并为三个" for context in revise_driver.contexts)
    finally:
        await persistence.close()


async def test_external_todo_edit_stales_joint_confirmation(tmp_path: Path) -> None:
    """任一文件外部修改后，旧 Plan+Todo 联合确认不再绑定新 digest。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        await _confirm_upstream(store, documents)
        driver = _FakePlanDriver()
        interaction = _FakeInteraction({"plan-gate": ["confirm"]})
        result = await _activity(store, documents, interaction, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is PlanGateOutcome.CONFIRMED

        # 外部修改 todo.md：同一 revision 内容变化，确认立即 stale。
        todo = await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.TODO)
        assert todo is not None
        from harness_agent.threads.text_backend import LocalTextMutationBackend

        backend = LocalTextMutationBackend(documents._backend._root)  # noqa: SLF001
        mutated = backend.compare_and_replace_text(
            f"/{todo.relative_path}",
            todo.identity,
            todo.content.replace("执行清单", "执行清单（外部修改）"),
        )
        refs = await store.load_document_references(WORK_ITEM_ID)
        todo_ref = next(ref for ref in refs if ref.kind is ComposeDocumentKind.TODO)
        assert todo_ref.confirmed_digest != mutated.identity.digest
    finally:
        await persistence.close()


async def test_placeholder_and_forbidden_todo_terms_fail_closed(tmp_path: Path) -> None:
    """placeholder、实施期设计项与空 todo 均拒绝成对提交。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        await _confirm_upstream(store, documents)
        cases = [
            PlanDraft("TODO: 待补充", TODO_BODY),
            PlanDraft(PLAN_BODY, "- [ ] 先决定实现方案"),
            PlanDraft(PLAN_BODY, "没有条目"),
        ]
        for draft in cases:
            driver = _FakePlanDriver(drafts=[draft])
            interaction = _FakeInteraction({})
            with pytest.raises(PlanGateActivityError):
                await _activity(store, documents, interaction, driver).run(
                    await _item(store), run_id="run-1"
                )
        refs = await store.load_document_references(WORK_ITEM_ID)
        assert all(ref.kind not in (ComposeDocumentKind.PLAN, ComposeDocumentKind.TODO) for ref in refs)
    finally:
        await persistence.close()


async def test_missing_spec_fails_closed(tmp_path: Path) -> None:
    """Spec 未写入时不能生成 Plan，Activity retryable_failed。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakePlanDriver()
        interaction = _FakeInteraction({})
        with pytest.raises(PlanGateActivityError) as excinfo:
            await _activity(store, documents, interaction, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_PLAN_SPEC_MISSING"
        activity = await store.load_activity(f"plan:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
    finally:
        await persistence.close()


async def test_gate_abandon_keeps_files(tmp_path: Path) -> None:
    """gate 放弃：文件保留，Activity 完成，Work Item 终态由 engine CAS 负责。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        await _confirm_upstream(store, documents)
        driver = _FakePlanDriver()
        interaction = _FakeInteraction({"plan-gate": ["abandon"]})
        result = await _activity(store, documents, interaction, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is PlanGateOutcome.ABANDONED
        plan = await documents.inspect(WORK_ITEM_ID, "plan-gate", ComposeDocumentKind.PLAN)
        assert plan is not None
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
        plan_driver: object | None = None,
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
            plan_driver=plan_driver,
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
    """直接成稿的 grill driver。"""

    async def next_question(self, _context: object) -> None:
        return None

    async def draft_task(self, _context: object) -> str:
        return "# 目标\n\n实现站内搜索"


class _InstantSpecDriver:
    """直接成稿的 spec driver。"""

    async def draft_spec(self, _context: object) -> str:
        return "# 行为规格\n\n## interface\n\n- execute_turn"


async def test_engine_runs_plan_gate_after_spec_confirmed(tmp_path: Path) -> None:
    """Task→Spec→Plan 三门禁在三个 Turn 内顺序推进，全部确认。"""
    harness = _EngineHarness(tmp_path)
    harness.classifier_outputs = [
        {"intent": "start_new_work"},
        {"intent": "resume_current"},
        {"intent": "resume_current"},
    ]
    try:
        await harness.open()
        interaction = _FakeInteraction(
            {"task-gate": ["confirm"], "spec-gate": ["confirm"], "plan-gate": ["confirm"]}
        )
        task_driver = _InstantTaskDriver()
        spec_driver = _InstantSpecDriver()
        plan_driver = _FakePlanDriver()
        engine = harness.engine(
            interaction=interaction,
            task_driver=task_driver,
            spec_driver=spec_driver,
            plan_driver=plan_driver,
        )
        first = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert first.work_item is not None
        assert first.work_item.readiness.task_confirmed is True
        assert first.work_item.readiness.spec_confirmed is False

        engine = harness.engine(
            interaction=interaction,
            task_driver=task_driver,
            spec_driver=spec_driver,
            plan_driver=plan_driver,
        )
        second = await engine.execute_turn(_turn("继续", "run-2"))
        assert second.work_item is not None
        assert second.work_item.readiness.spec_confirmed is True
        assert second.work_item.readiness.plan_confirmed is False

        engine = harness.engine(
            interaction=interaction,
            task_driver=task_driver,
            spec_driver=spec_driver,
            plan_driver=plan_driver,
        )
        third = await engine.execute_turn(_turn("继续", "run-3"))
        assert third.work_item is not None
        assert third.work_item.readiness.plan_confirmed is True
        assert third.work_item.current_activity == "implement"
        assert len(plan_driver.contexts) == 1
    finally:
        await harness.close()


async def test_engine_does_not_call_plan_driver_before_spec_confirmed(tmp_path: Path) -> None:
    """Spec 未确认时 Plan driver 不可达，顺序由 readiness gate 决定。"""
    harness = _EngineHarness(tmp_path)
    try:
        await harness.open()
        plan_driver = _FakePlanDriver()
        interaction = _FakeInteraction({})
        engine = harness.engine(interaction=interaction, plan_driver=plan_driver)
        result = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert result.work_item is not None
        assert result.work_item.readiness.task_confirmed is False
        assert plan_driver.contexts == []
    finally:
        await harness.close()
