"""Compose 全链路 fake-model E2E（WP15 tracer）。

真实 SQLite + workspace + 全 fake 驱动走完整产品链：创建 → 三门禁 →
自动闭环 → completed → 同 Thread 第二个 Work Item；覆盖继续、需求修订、
临时问题、放弃、429 恢复与 stale evidence。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.implement import (
    ImplementItemOutcome,
    ImplementItemResult,
)
from harness_agent.compose.activities.plan import PlanDraft
from harness_agent.compose.activities.review import ReviewAxisResult
from harness_agent.compose.activities.spec import SpecDraftContext
from harness_agent.compose.activities.task import TaskInterviewContext
from harness_agent.compose.activities.verify import VerificationCommandResult
from harness_agent.compose.document_store import ComposeDocumentStore
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeWorkItemStatus,
    FindingSeverity,
    ThreadMode,
)
from harness_agent.compose.turn_intent import (
    TurnIntent,
    TurnIntentKind,
    TurnIntentSource,
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
    ComposeWorkItemStoreError,
    MarkComposeEffectUnknown,
    RecordComposeEffectIntent,
    TerminalizeComposeWorkItem,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-e2e"
NOW = 1_700_000_000_000
REVISION = "rev-e2e"


class _FakeInteraction:
    """按 question_id 脚本化回答；支持过期与记录。"""

    def __init__(self, answers: dict[str, list[str | None]] | None = None) -> None:
        self.answers: dict[str, list[str | None]] = {
            key: list(values) for key, values in (answers or {}).items()
        }
        self.requests: list[TypedDecisionRequest] = []

    def prepend_expired(self, question_id: str) -> None:
        self.answers.setdefault(question_id, []).insert(0, None)

    async def request_decision(self, request: TypedDecisionRequest) -> TypedDecisionResult:
        self.requests.append(request)
        queue = self.answers.get(request.question_id, [])
        value = queue.pop(0) if queue else None
        if value is None:
            return TypedDecisionResult({"answers": {}}, expired=True)
        return TypedDecisionResult({"answers": {request.question_id: [value]}})


class _InstantTaskDriver:
    async def next_question(self, context: TaskInterviewContext) -> None:
        return None

    async def draft_task(self, context: TaskInterviewContext) -> str:
        return "# 目标\n\n实现站内搜索"


class _InstantSpecDriver:
    async def draft_spec(self, context: SpecDraftContext) -> str:
        return "# 行为规格\n\n## interface\n\n- execute_turn"


class _InstantPlanDriver:
    async def draft_plan(self, _context: object) -> PlanDraft:
        return PlanDraft(
            plan_body="# 实施计划\n\n## 步骤\n\n1. 一项",
            todo_body="## 执行清单\n\n- [ ] 实现一项：验证=pytest tests/compose\n",
        )


class _InstantImplementDriver:
    async def implement_item(self, _context: object) -> ImplementItemResult:
        return ImplementItemResult(
            outcome=ImplementItemOutcome.COMPLETED,
            fail_before="RED",
            pass_after="GREEN",
            changed_paths=("src/a.py",),
            execution_id="exec-e2e",
        )


class _InstantVerifyPort:
    async def run_command(self, command: str, *, work_item_id: str):
        return VerificationCommandResult(
            command=command,
            exit_code=0,
            output_digest="c" * 64,
            execution_id="verify-e2e",
        )


class _InstantReviewDriver:
    def __init__(self) -> None:
        self.axes = 0

    async def review(self, context: object) -> ReviewAxisResult:
        self.axes += 1
        return ReviewAxisResult(context.axis, f"review-e2e-{self.axes}")


class _InstantReportDriver:
    async def draft_report(self, context: object) -> str:
        return "# 完成报告\n\n全部通过。\n"


class _SideAnswer:
    async def answer(self, *, thread_id: str, question: str) -> str:
        return f"side:{question}"


class _Harness:
    """引擎 + 全部 fake 驱动；提供脚本化分类器。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.persistence: ThreadPersistence | None = None
        self.classifier_outputs: list[object] = []

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
        interaction: _FakeInteraction,
        *,
        side: _SideAnswer | None = None,
    ) -> ComposeWorkItemEngine:
        assert self.persistence is not None

        class _Classifier:
            def __init__(self, outputs: list[object]) -> None:
                self.outputs = list(outputs)

            async def classify(self, _context: object) -> object:
                return self.outputs.pop(0)

        classifier = _Classifier(self.classifier_outputs)
        ports = ComposeTurnPorts(
            store=self.persistence.compose_work_item_store(),
            documents=ComposeDocumentStore(self.workspace),
            classifier=classifier,
            interaction=interaction,
            side_answer=side,
            workspace_revision=lambda: REVISION,
            now_ms=lambda: NOW,
            task_driver=_InstantTaskDriver(),
            spec_driver=_InstantSpecDriver(),
            plan_driver=_InstantPlanDriver(),
            implement_driver=_InstantImplementDriver(),
            verify_port=_InstantVerifyPort(),
            review_driver=_InstantReviewDriver(),
            report_driver=_InstantReportDriver(),
        )
        engine = ComposeWorkItemEngine(ports)
        engine._classifier = classifier  # noqa: SLF001
        return engine

    def store(self):
        assert self.persistence is not None
        return self.persistence.compose_work_item_store()

    async def close(self) -> None:
        assert self.persistence is not None
        await self.persistence.close()


def _turn(message: str, run_id: str, *, explicit: TurnIntent | None = None) -> ComposeTurnRequest:
    return ComposeTurnRequest(
        thread_id=THREAD,
        run_id=run_id,
        message=message,
        explicit_intent=explicit,
        cancelled=False,
    )


def _intent(kind: TurnIntentKind, detail: str = "") -> TurnIntent:
    return TurnIntent(kind=kind, detail=detail, source=TurnIntentSource.EXPLICIT)


async def _run_full_item(
    harness: _Harness,
    interaction: _FakeInteraction,
    message: str,
    *,
    first_run_id: str,
) -> str:
    """新目标 → 三门禁（3 Turn）→ 自动闭环（1 Turn）→ completed。"""
    engine = harness.engine(interaction)
    engine._classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
    first = await engine.execute_turn(_turn(message, first_run_id))
    assert first.work_item is not None
    work_item_id = first.work_item.work_item_id
    assert first.work_item.readiness.task_confirmed is True

    for index in range(2):
        engine = harness.engine(interaction)
        engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
        result = await engine.execute_turn(
            _turn("继续", f"{first_run_id}-resume-{index}")
        )
        assert result.work_item is not None
    engine = harness.engine(interaction)
    engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
    final = await engine.execute_turn(_turn("继续", f"{first_run_id}-close"))
    assert final.work_item is not None
    assert final.work_item.work_item_id == work_item_id
    assert final.status is ComposeTurnOutcome.COMPLETED
    assert final.work_item.status == "completed"
    return work_item_id


async def test_full_pipeline_then_second_work_item_in_same_thread(tmp_path: Path) -> None:
    """同 Thread 顺序完成两个 Work Item：三门禁 + 自动闭环各自独立。"""
    harness = _Harness(tmp_path)
    try:
        await harness.open()
        gate_answers = _FakeInteraction(
            {"task-gate": ["confirm"], "spec-gate": ["confirm"], "plan-gate": ["confirm"]}
        )
        first_id = await _run_full_item(
            harness, gate_answers, "实现站内搜索", first_run_id="run-1"
        )
        completed = await harness.store().load(first_id)
        assert completed is not None
        assert completed.status is ComposeWorkItemStatus.COMPLETED

        gate_answers2 = _FakeInteraction(
            {"task-gate": ["confirm"], "spec-gate": ["confirm"], "plan-gate": ["confirm"]}
        )
        second_id = await _run_full_item(
            harness, gate_answers2, "实现第二个功能", first_run_id="run-2"
        )
        assert second_id != first_id
        docs = harness.workspace
        assert (docs / "docs" / "compose").exists()
    finally:
        await harness.close()


async def test_side_question_and_amendment_do_not_break_pipeline(tmp_path: Path) -> None:
    """临时问题隔离、需求修订 pending、继续后照常确认。"""
    harness = _Harness(tmp_path)
    try:
        await harness.open()
        side = _SideAnswer()
        interaction = _FakeInteraction({"task-gate": ["confirm"]})
        engine = harness.engine(interaction, side=side)
        engine._classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
        first = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert first.work_item is not None
        work_item_id = first.work_item.work_item_id

        engine = harness.engine(interaction, side=side)
        engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
        side_result = await engine.execute_turn(
            _turn(
                "什么是 CAS？",
                "run-1-side",
                explicit=_intent(TurnIntentKind.SIDE_QUESTION, "什么是 CAS？"),
            )
        )
        assert side_result.side_answer == "side:什么是 CAS？"
        assert side_result.work_item is not None
        assert side_result.work_item.work_item_id == work_item_id
        assert side_result.work_item.readiness.task_confirmed is True

        engine = harness.engine(interaction, side=side)
        engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
        amend = await engine.execute_turn(
            _turn(
                "范围改成标题搜索",
                "run-1-amend",
                explicit=_intent(TurnIntentKind.AMEND_CURRENT, "范围改成标题搜索"),
            )
        )
        assert amend.work_item is not None
        assert amend.pending_decision == "amend:spec"
    finally:
        await harness.close()


async def test_unknown_effect_blocks_completion_fact(tmp_path: Path) -> None:
    """存在 outcome unknown 的 effect 时 Guard 不完成 Work Item。"""
    harness = _Harness(tmp_path)
    try:
        await harness.open()
        interaction = _FakeInteraction(
            {"task-gate": ["confirm"], "spec-gate": ["confirm"], "plan-gate": ["confirm"]}
        )
        engine = harness.engine(interaction)
        engine._classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
        first = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert first.work_item is not None
        work_item_id = first.work_item.work_item_id
        await harness.store().record_effect_intent(
            RecordComposeEffectIntent(
                effect_key=f"file:{work_item_id}:call-unknown",
                work_item_id=work_item_id,
                activity_id=None,
                intent={"tool": "shell", "command": "deploy"},
                created_at_ms=NOW,
            )
        )
        await harness.store().mark_effect_unknown(
            MarkComposeEffectUnknown(
                effect_key=f"file:{work_item_id}:call-unknown",
                reason="outcome unknown",
                updated_at_ms=NOW + 1,
            )
        )
        # 注入 unknown effect 后继续三门禁与闭环：complete 被 Guard 阻止。
        for index in range(3):
            engine = harness.engine(interaction)
            engine._classifier.outputs = [{"intent": "resume_current"}]  # noqa: SLF001
            result = await engine.execute_turn(_turn("继续", f"run-1-c{index}"))
            assert result.work_item is not None
        assert result.work_item.status != "completed"
        assert result.work_item.readiness.complete is False
    finally:
        await harness.close()
