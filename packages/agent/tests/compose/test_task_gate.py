"""Task gate（grill 访谈 + typed confirmation）行为测试（WP8）。

覆盖：一次一问、草稿落盘、确认绑定 digest、修改 feedback 生成新 revision 并
使旧确认 stale、放弃保留文件、崩溃/重启后从最后已答问题恢复、turn budget
暂停、malformed 草稿 fail closed、429 标记 retryable_failed、引擎在创建与
继续时按 readiness 自动进入 Task gate、Task 未确认不推进 Spec。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness_agent.compose.activities.task import (
    MAX_INTERVIEW_ROUNDS_PER_RUN,
    TaskGateActivity,
    TaskGateActivityError,
    TaskGateOutcome,
    TaskInterviewContext,
)
from harness_agent.compose.document_store import ComposeDocumentStore
from harness_agent.compose.models import (
    ComposeActivityStatus,
    ComposeDocumentKind,
    ComposeWorkItemStatus,
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
    CreateComposeWorkItem,
    TerminalizeComposeWorkItem,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding

THREAD = "thread-task-gate"
WORK_ITEM_ID = "wi-task-gate"
NOW = 1_700_000_000_000

TASK_BODY = "# 目标\n\n实现站内搜索\n\n## 范围\n\n标题与正文搜索"


class _FakeDriver:
    """脚本化 grill 回合：问题队列、草稿队列与可选异常。"""

    def __init__(
        self,
        questions: list[str | None] | None = None,
        drafts: list[str] | None = None,
        *,
        error: Exception | None = None,
        error_on: str = "question",
        raise_after: int | None = None,
    ) -> None:
        self.questions = list(questions or [])
        self.drafts = list(drafts or [TASK_BODY])
        self.error = error
        self.error_on = error_on
        self.raise_after = raise_after
        self.contexts: list[TaskInterviewContext] = []

    def _maybe_raise(self) -> None:
        if (
            self.error is not None
            and self.raise_after is not None
            and len(self.contexts) >= self.raise_after
        ):
            raise self.error

    async def next_question(self, context: TaskInterviewContext) -> str | None:
        self.contexts.append(context)
        self._maybe_raise()
        if (
            self.error is not None
            and self.raise_after is None
            and self.error_on == "question"
        ):
            raise self.error
        return self.questions.pop(0) if self.questions else None

    async def draft_task(self, context: TaskInterviewContext) -> str:
        self.contexts.append(context)
        self._maybe_raise()
        if (
            self.error is not None
            and self.raise_after is None
            and self.error_on == "draft"
        ):
            raise self.error
        return self.drafts.pop(0) if self.drafts else TASK_BODY


class _FakeInteraction:
    """按 question_id 脚本化回答；支持过期注入。"""

    def __init__(self, answers: dict[str, list[str | None]] | None = None) -> None:
        self.answers: dict[str, list[str | None]] = {
            key: list(values) for key, values in (answers or {}).items()
        }
        self.requests: list[TypedDecisionRequest] = []

    def set_expired(self, question_id: str) -> None:
        self.answers.setdefault(question_id, []).insert(0, None)

    async def request_decision(self, request: TypedDecisionRequest) -> TypedDecisionResult:
        self.requests.append(request)
        queue = self.answers.get(request.question_id, [])
        value = queue.pop(0) if queue else None
        if value is None:
            return TypedDecisionResult({"answers": {}}, expired=True)
        return TypedDecisionResult({"answers": {request.question_id: [value]}})


async def _harness(tmp_path: Path) -> tuple[ThreadPersistence, ComposeWorkItemStore, ComposeDocumentStore, Any]:
    """真实 SQLite + workspace 文档存储 + active Compose Work Item。"""
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
            slug="task-gate",
            goal="实现站内搜索",
            created_at_ms=NOW,
        )
    )
    documents = ComposeDocumentStore(tmp_path / "workspace")
    return persistence, store, documents


def _activity(store, documents, interaction, driver, *, max_rounds: int = MAX_INTERVIEW_ROUNDS_PER_RUN):
    return TaskGateActivity(
        store=store,
        documents=documents,
        interaction=interaction,
        driver=driver,
        now_ms=lambda: NOW,
        max_rounds_per_run=max_rounds,
    )


async def _item(store):
    item = await store.load(WORK_ITEM_ID)
    assert item is not None
    return item


async def test_interview_asks_one_question_at_a_time_then_confirm_binds_digest(
    tmp_path: Path,
) -> None:
    """一次一问；成稿后 typed gate 确认绑定当前 digest 并完成 Activity。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeDriver(questions=["目标是什么？"])
        interaction = _FakeInteraction(
            {"task-interview": ["实现站内搜索"], "task-gate": ["confirm"]}
        )
        result = await _activity(store, documents, interaction, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is TaskGateOutcome.CONFIRMED
        snapshot = await documents.inspect(WORK_ITEM_ID, "task-gate", ComposeDocumentKind.TASK)
        assert snapshot is not None
        assert snapshot.status == "proposed"
        assert "实现站内搜索" in snapshot.content
        refs = await store.load_document_references(WORK_ITEM_ID)
        assert len(refs) == 1 and refs[0].current_digest == snapshot.digest
        assert refs[0].confirmed_digest == snapshot.digest
        activity = await store.load_activity(f"task:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
        assert driver.contexts[0].answers == ()
        assert driver.contexts[-1].answers == (("目标是什么？", "实现站内搜索"),)
        # 交互顺序：问题 → gate，各一次。
        assert [request.question_id for request in interaction.requests] == [
            "task-interview",
            "task-gate",
        ]
    finally:
        await persistence.close()


async def test_interview_state_resumes_from_draft_after_crash(tmp_path: Path) -> None:
    """崩溃后从 task.md draft 恢复已答问题，不重新提问。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeDriver(
            questions=["Q1", "Q2"],
            error=asyncio.CancelledError(),
            error_on="question",
            raise_after=2,
        )
        interaction = _FakeInteraction({"task-interview": ["A1"]})
        with pytest.raises(asyncio.CancelledError):
            await _activity(store, documents, interaction, driver).run(
                await _item(store), run_id="run-1"
            )
        # 第一次 run：Q1 得到回答后，在 Q2 前被取消。
        # 模拟进程被杀：activity 遗留 running，由启动扫描收敛。
        activity = await store.load_activity(f"task:{WORK_ITEM_ID}")
        assert activity is not None and activity.status is ComposeActivityStatus.RUNNING
        converged = await store.mark_running_activities_interrupted(now_ms=NOW + 100)
        assert converged == 1

        resumed_driver = _FakeDriver(questions=["Q2"])
        interaction2 = _FakeInteraction({"task-interview": ["A2"], "task-gate": ["confirm"]})
        result = await _activity(store, documents, interaction2, resumed_driver).run(
            await _item(store), run_id="run-2"
        )
        assert result.outcome is TaskGateOutcome.CONFIRMED
        first_context = resumed_driver.contexts[0]
        assert first_context.answers == (("Q1", "A1"),)
        activity = await store.load_activity(f"task:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
        assert activity.attempt == 2
    finally:
        await persistence.close()


async def test_revise_feedback_generates_new_revision_and_stales_old_confirmation(
    tmp_path: Path,
) -> None:
    """修改 feedback 生成新 revision；旧确认保留审计但对新 digest 不再 fresh。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        first_driver = _FakeDriver(questions=[None])
        first_interaction = _FakeInteraction({"task-gate": ["confirm"]})
        first = await _activity(store, documents, first_interaction, first_driver).run(
            await _item(store), run_id="run-1"
        )
        assert first.outcome is TaskGateOutcome.CONFIRMED
        old_digest = (await documents.inspect(WORK_ITEM_ID, "task-gate", ComposeDocumentKind.TASK)).digest

        # 外部修改文件触发重新进入 gate：revise → feedback → 新草稿 → confirm。
        revise_interaction = _FakeInteraction(
            {
                "task-gate": ["revise", "confirm"],
                "task-feedback": ["只做标题搜索"],
                "task-interview": ["范围OK"],
            }
        )
        # 第二个 gate 决策 consume 顺序：revise → feedback → interview → gate confirm。
        revise_driver = _FakeDriver(questions=["范围缩小为标题搜索可以吗？"], drafts=["# 任务\n\n只做标题搜索"])
        second = await _activity(store, documents, revise_interaction, revise_driver).run(
            await _item(store), run_id="run-2"
        )
        assert second.outcome is TaskGateOutcome.CONFIRMED
        assert second.revision > first.revision
        new_digest = (await documents.inspect(WORK_ITEM_ID, "task-gate", ComposeDocumentKind.TASK)).digest
        assert new_digest != old_digest
        refs = await store.load_document_references(WORK_ITEM_ID)
        assert refs[0].confirmed_digest == new_digest
        # 旧确认仍作为审计存在，但不再是当前 digest。
        audited = await store.load_confirmation_digests(WORK_ITEM_ID, "task")
        assert old_digest in audited
        # feedback 进入新一轮访谈上下文。
        assert any(context.feedback == "只做标题搜索" for context in revise_driver.contexts)
    finally:
        await persistence.close()


async def test_gate_abandon_keeps_files_and_does_not_terminalize(tmp_path: Path) -> None:
    """gate 放弃：文件保留，Activity 完成，Work Item 终态由 engine CAS 负责。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeDriver(questions=[None])
        interaction = _FakeInteraction({"task-gate": ["abandon"]})
        result = await _activity(store, documents, interaction, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is TaskGateOutcome.ABANDONED
        snapshot = await documents.inspect(WORK_ITEM_ID, "task-gate", ComposeDocumentKind.TASK)
        assert snapshot is not None
        item = await _item(store)
        assert not item.terminal
    finally:
        await persistence.close()


async def test_malformed_draft_fails_closed_and_marks_retryable(tmp_path: Path) -> None:
    """driver 产出无正文内容时 fail closed：无文档写入，Activity retryable_failed。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeDriver(questions=[None], drafts=[""])
        interaction = _FakeInteraction({})
        with pytest.raises(TaskGateActivityError) as excinfo:
            await _activity(store, documents, interaction, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_TASK_DRAFT_INVALID"
        assert await store.load_document_references(WORK_ITEM_ID) == ()
        activity = await store.load_activity(f"task:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
    finally:
        await persistence.close()


async def test_driver_failure_marks_retryable_failed(tmp_path: Path) -> None:
    """driver 429 等执行失败：Activity retryable_failed，可恢复不终结 Work Item。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeDriver(error=RuntimeError("429 rate limited"))
        interaction = _FakeInteraction({})
        with pytest.raises(TaskGateActivityError) as excinfo:
            await _activity(store, documents, interaction, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_TASK_EXECUTION_FAILED"
        activity = await store.load_activity(f"task:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
        item = await _item(store)
        assert not item.terminal
    finally:
        await persistence.close()


async def test_turn_budget_pauses_interview_with_pending_question(tmp_path: Path) -> None:
    """每 Run 问题轮次有界；超出后等待下个 Run 从草稿继续。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeDriver(questions=["Q1", "Q2", "Q3"])
        interaction = _FakeInteraction({"task-interview": ["A1", "A2"]})
        result = await _activity(store, documents, interaction, driver, max_rounds=2).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is TaskGateOutcome.WAITING_ANSWER
        assert result.pending == "task-interview"
        activity = await store.load_activity(f"task:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.WAITING_USER

        resumed_driver = _FakeDriver(questions=["Q3"])
        interaction2 = _FakeInteraction({"task-interview": ["A3"], "task-gate": ["confirm"]})
        second = await _activity(store, documents, interaction2, resumed_driver, max_rounds=2).run(
            await _item(store), run_id="run-2"
        )
        assert second.outcome is TaskGateOutcome.CONFIRMED
        assert resumed_driver.contexts[0].answers == (("Q1", "A1"), ("Q2", "A2"))
    finally:
        await persistence.close()


# ---------- 引擎集成 ----------


class _EngineHarness:
    """引擎 + 真实 store/documents + fake 分类/interaction/driver。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.persistence: ThreadPersistence | None = None
        self.classifier_outputs: list[object] = [{"intent": "start_new_work"}]
        self._classifier_queue: list[object] = []

    async def open(self) -> "ComposeWorkItemEngine":
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
        self._classifier_queue = list(self.classifier_outputs)
        return self.engine()

    def engine(
        self,
        *,
        interaction: _FakeInteraction | None = None,
        driver: _FakeDriver | None = None,
    ) -> ComposeWorkItemEngine:
        assert self.persistence is not None

        class _Classifier:
            """共享消费队列：多次 engine 构建共用同一意图脚本。"""

            def __init__(self, outputs: list[object]) -> None:
                self.outputs = outputs

            async def classify(self, _context: object) -> object:
                return self.outputs.pop(0)

        ports = ComposeTurnPorts(
            store=self.persistence.compose_work_item_store(),
            documents=ComposeDocumentStore(self.workspace),
            classifier=_Classifier(self._classifier_queue),
            interaction=interaction or _FakeInteraction({}),
            now_ms=lambda: NOW,
            task_driver=driver,
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


async def test_engine_runs_task_gate_after_creation_and_confirms(tmp_path: Path) -> None:
    """创建后自动进入 grill；确认后 readiness task_confirmed 变真。"""
    harness = _EngineHarness(tmp_path)
    try:
        driver = _FakeDriver(questions=[None])
        interaction = _FakeInteraction({"task-gate": ["confirm"]})
        engine = await harness.open()
        # open() 的 engine 不含 driver/interaction；重建带 driver 的实例。
        engine = harness.engine(interaction=interaction, driver=driver)
        result = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert result.work_item is not None
        assert result.work_item.readiness.task_confirmed is True
        assert result.work_item.current_activity == "spec"
        assert result.pending_decision is None
        store = harness.persistence.compose_work_item_store()
        refs = await store.load_document_references(result.work_item.work_item_id)
        assert len(refs) == 1
    finally:
        await harness.close()


async def test_engine_terminalizes_work_item_when_task_gate_is_abandoned(
    tmp_path: Path,
) -> None:
    """Task gate 选择放弃后保留文档，并由 Engine 提交 abandoned 终态。"""
    harness = _EngineHarness(tmp_path)
    try:
        driver = _FakeDriver(questions=[None])
        interaction = _FakeInteraction({"task-gate": ["abandon"]})
        await harness.open()
        engine = harness.engine(interaction=interaction, driver=driver)

        result = await engine.execute_turn(_turn("实现站内搜索", "run-1"))

        assert result.work_item is not None
        assert result.work_item.status == ComposeWorkItemStatus.ABANDONED.value
        store = harness.persistence.compose_work_item_store()
        abandoned = await store.load(result.work_item.work_item_id)
        assert abandoned is not None
        assert abandoned.status is ComposeWorkItemStatus.ABANDONED
        assert (harness.workspace / "docs" / "compose" / result.work_item.slug / "task.md").is_file()
    finally:
        await harness.close()


async def test_engine_resume_after_expired_question_keeps_work_item(tmp_path: Path) -> None:
    """访谈问题过期后 Turn 收敛 waiting；继续从草稿恢复并最终确认。"""
    harness = _EngineHarness(tmp_path)
    harness.classifier_outputs = [
        {"intent": "start_new_work"},
        {"intent": "resume_current"},
    ]
    try:
        driver = _FakeDriver(questions=["目标边界是什么？"])
        interaction = _FakeInteraction({"task-interview": ["A1"], "task-gate": ["confirm"]})
        interaction.set_expired("task-interview")
        engine = await harness.open()
        engine = harness.engine(interaction=interaction, driver=driver)
        first = await engine.execute_turn(_turn("实现站内搜索", "run-1"))
        assert first.pending_decision == "task-interview"
        assert first.work_item.readiness.task_confirmed is False

        # 继续：新 run 恢复访谈，回答后 gate 确认。
        engine = harness.engine(interaction=interaction, driver=driver)
        second = await engine.execute_turn(_turn("继续", "run-2"))
        assert second.work_item is not None
        assert second.work_item.readiness.task_confirmed is True
    finally:
        await harness.close()
