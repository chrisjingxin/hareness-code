"""ComposeWorkItemEngine 生命周期与 Turn 路由测试（WP6 tracer bullet）。

覆盖：新目标创建、完成后新建、继续 attach、active 冲突 clarification、
side answer 隔离、unclear 兜底、Esc/cancel 不终结、abandon CAS 与文件保留、
分类器误判保护、Run binding 冲突和 Mode 锁定。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness_agent.compose.activities.implement import ImplementActivity
from harness_agent.compose.activities.review import ReviewActivity
from harness_agent.compose.activities.verify import VerifyActivity
from harness_agent.compose.document_store import ComposeDocumentStore
from harness_agent.compose.guard import CompletionGuard, CompletionGuardError
from harness_agent.compose.models import (
    ComposeDocumentKind,
    ComposeWorkItemStatus,
    ThreadMode,
)
from harness_agent.compose.turn_intent import (
    TurnIntent,
    TurnIntentContext,
    TurnIntentKind,
    TurnIntentSource,
)
from harness_agent.compose.work_item_engine import (
    ComposeTurnOutcome,
    ComposeTurnPorts,
    ComposeTurnRequest,
    ComposeWorkItemEngine,
    ComposeWorkItemEngineError,
    TypedDecisionRequest,
    TypedDecisionResult,
)
from harness_agent.threads.compose_work_item_store import (
    ComposeWorkItemStoreError,
    TerminalizeComposeWorkItem,
)
from harness_agent.threads.thread_persistence import AcceptRun, ThreadPersistence
from tests.support.thread_fixtures import async_return, test_binding as make_test_binding

THREAD = "thread-1"


class _FakeClassifier:
    """按脚本返回分类输出；记录上下文；可注入异常。"""

    def __init__(self, outputs: list[object] | None = None, *, error: Exception | None = None) -> None:
        self.outputs = list(outputs or [])
        self.error = error
        self.contexts: list[TurnIntentContext] = []

    async def classify(self, context: TurnIntentContext) -> object:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0)


class _FakeInteraction:
    """按脚本返回 typed decision；记录请求。"""

    def __init__(self, choices: list[str] | None = None, *, expired: bool = False) -> None:
        self.choices = list(choices or [])
        self.expired = expired
        self.requests: list[TypedDecisionRequest] = []

    async def request_decision(self, request: TypedDecisionRequest) -> TypedDecisionResult:
        self.requests.append(request)
        if self.expired:
            return TypedDecisionResult({"answers": {}}, expired=True)
        choice = self.choices.pop(0) if self.choices else ""
        return TypedDecisionResult({"answers": {request.question_id: [choice]}})


class _FakeSideAnswer:
    """只读 side answer；记录收到的临时问题。"""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def answer(self, *, thread_id: str, question: str) -> str:
        self.questions.append(question)
        return f"side:{question}"


class _Harness:
    """真实 SQLite + Workspace 文档存储 + 全部 fake 端口的测试台。"""

    def __init__(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        home = tmp_path / "home"
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.persistence = None

    async def open(self, *, mode: ThreadMode = ThreadMode.COMPOSE) -> "_Harness":
        self.persistence = await ThreadPersistence.open(
            project=self._project_dir(),
            home=self._home_dir(),
        )
        await self.persistence.accept_run(
            AcceptRun(
                message="受理",
                binding=make_test_binding(THREAD, "run-0"),
                mode=mode,
            )
        )
        return self

    def _project_dir(self) -> Path:
        return self.workspace.parent / "project"

    def _home_dir(self) -> Path:
        return self.workspace.parent / "home"

    def engine(
        self,
        *,
        classifier: _FakeClassifier | None = None,
        interaction: _FakeInteraction | None = None,
        side: _FakeSideAnswer | None = None,
        revision: str | None = None,
    ) -> ComposeWorkItemEngine:
        ports = ComposeTurnPorts(
            store=self.persistence.compose_work_item_store(),
            documents=ComposeDocumentStore(self.workspace),
            classifier=classifier or _FakeClassifier([{"intent": "resume_current"}]),
            interaction=interaction or _FakeInteraction(),
            side_answer=side,
            workspace_revision=async_return(revision) if revision else None,
            now_ms=lambda: 1_700_000_000_000,
        )
        return ComposeWorkItemEngine(ports)


def _turn(
    message: str,
    run_id: str = "run-1",
    *,
    explicit: TurnIntent | None = None,
    cancelled: bool = False,
    amends: str | None = None,
) -> ComposeTurnRequest:
    return ComposeTurnRequest(
        thread_id=THREAD,
        run_id=run_id,
        message=message,
        explicit_intent=explicit,
        cancelled=cancelled,
        amends_work_item_id=amends,
    )


def _intent(kind: TurnIntentKind, detail: str = "") -> TurnIntent:
    return TurnIntent(kind=kind, detail=detail, source=TurnIntentSource.EXPLICIT)


async def _create_first(engine: ComposeWorkItemEngine) -> str:
    classifier = engine._ports.classifier  # noqa: SLF001 - 测试直接替换脚本
    classifier.outputs = [{"intent": "start_new_work"}] + list(classifier.outputs)
    result = await engine.execute_turn(_turn("实现搜索功能"))
    assert result.work_item is not None
    return result.work_item.work_item_id


async def test_new_coding_goal_creates_work_item_and_binds_run(tmp_path: Path) -> None:
    """无未终结项时新 coding 目标原子创建 Work Item 并固定 Run 绑定。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "start_new_work"}])
    )
    result = await engine.execute_turn(_turn("实现搜索功能"))

    assert result.status is ComposeTurnOutcome.WAITING_USER
    assert result.work_item is not None
    assert result.work_item.slug.startswith("work-")
    assert result.work_item.current_activity == "task"
    assert result.work_item.readiness.task_confirmed is False
    assert [doc.kind for doc in result.work_item.documents] == [
        "task",
        "spec",
        "plan",
        "todo",
        "report",
    ]
    assert all(not doc.present for doc in result.work_item.documents)
    store = harness.persistence.compose_work_item_store()
    active = await store.load_active(THREAD)
    assert active is not None and active.work_item_id == result.work_item.work_item_id
    assert await store.load_run_binding(THREAD, "run-1") == active.work_item_id
    await harness.persistence.close()


async def test_completed_work_item_releases_slot_for_next_goal(tmp_path: Path) -> None:
    """完成后同 Thread 可直接创建第二个 Work Item，不新建 Thread。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine()
    first_id = await _create_first(engine)

    store = harness.persistence.compose_work_item_store()
    await store.terminalize(
        TerminalizeComposeWorkItem(
            work_item_id=first_id,
            expected_revision=0,
            status=ComposeWorkItemStatus.COMPLETED,
            terminal_at_ms=1_700_000_000_100,
        )
    )
    engine._ports.classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
    result = await engine.execute_turn(_turn("实现第二个功能", run_id="run-2"))

    assert result.work_item is not None
    assert result.work_item.work_item_id != first_id
    active = await store.load_active(THREAD)
    assert active is not None and active.work_item_id == result.work_item.work_item_id
    await harness.persistence.close()


async def test_new_work_item_slug_is_unique_across_threads(tmp_path: Path) -> None:
    """同一 workspace 的不同 Thread 使用相同目标时分配不同文档目录。"""
    harness = await _Harness(tmp_path).open()
    await harness.persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding("thread-2", "run-0"),
            mode=ThreadMode.COMPOSE,
        )
    )
    engine = harness.engine(
        classifier=_FakeClassifier(
            [{"intent": "start_new_work"}, {"intent": "start_new_work"}]
        )
    )

    first = await engine.execute_turn(_turn("实现搜索功能"))
    second = await engine.execute_turn(
        ComposeTurnRequest(
            thread_id="thread-2",
            run_id="run-1",
            message="实现搜索功能",
        )
    )

    assert first.work_item is not None
    assert second.work_item is not None
    assert second.work_item.slug == f"{first.work_item.slug}-2"
    await harness.persistence.close()


async def test_slug_allocation_stops_after_repeated_concurrent_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持续并发冲突必须有界失败，不能让一次 Turn 永久自旋。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "start_new_work"}])
    )
    store = engine._ports.store  # noqa: SLF001 - 注入稳定并发冲突
    attempts = 0

    async def always_conflicts(command: object) -> object:
        nonlocal attempts
        attempts += 1
        raise ComposeWorkItemStoreError("COMPOSE_WORK_ITEM_SLUG_CONFLICT")

    monkeypatch.setattr(store, "create", always_conflicts)

    with pytest.raises(
        ComposeWorkItemEngineError,
        match="COMPOSE_WORK_ITEM_SLUG_ALLOCATION_FAILED",
    ):
        await asyncio.wait_for(engine.execute_turn(_turn("实现搜索功能")), 0.2)
    assert attempts > 1
    await harness.persistence.close()


@pytest.mark.parametrize(
    "stage",
    ("implement", "verify", "review", "report", "complete"),
)
async def test_internal_activity_errors_are_retryable_turn_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """内部 Activity 异常不能伪装成正常等待用户的 completed Turn。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "start_new_work"}])
    )
    created = await engine.execute_turn(_turn("实现搜索功能"))
    assert created.work_item is not None
    item = await engine._ports.store.load(created.work_item.work_item_id)  # noqa: SLF001
    assert item is not None

    async def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    if stage == "implement":
        engine._ports.implement_driver = object()  # type: ignore[assignment]  # noqa: SLF001
        engine._ports.workspace_revision = async_return("revision")  # noqa: SLF001
        monkeypatch.setattr(ImplementActivity, "run", explode)
        result = await engine._run_implement(_turn("继续"), item, 1_700_000_000_000)  # noqa: SLF001
    elif stage == "verify":
        engine._ports.verify_port = object()  # type: ignore[assignment]  # noqa: SLF001
        monkeypatch.setattr(VerifyActivity, "run", explode)
        result = await engine._run_verify(_turn("继续"), item, 1_700_000_000_000)  # noqa: SLF001
    elif stage == "review":
        engine._ports.review_driver = object()  # type: ignore[assignment]  # noqa: SLF001
        monkeypatch.setattr(ReviewActivity, "run", explode)
        result = await engine._run_review(_turn("继续"), item, 1_700_000_000_000)  # noqa: SLF001
    else:
        async def document_digests(*args: object) -> tuple[str, ...]:
            return ("task", "spec", "plan", "todo")

        async def evidence_digest(*args: object) -> str:
            return "verification"

        async def review_payload(*args: object) -> tuple[str, str]:
            return ("requirements", "code")

        engine._ports.report_driver = object()  # type: ignore[assignment]  # noqa: SLF001
        monkeypatch.setattr(ComposeWorkItemEngine, "_four_document_digests", document_digests)
        monkeypatch.setattr(ComposeWorkItemEngine, "_latest_evidence_digest", evidence_digest)
        monkeypatch.setattr(ComposeWorkItemEngine, "_latest_review_payload", review_payload)

        async def guard_error(*args: object, **kwargs: object) -> object:
            raise CompletionGuardError("COMPOSE_TEST_FAILURE")

        if stage == "report":
            monkeypatch.setattr(CompletionGuard, "write_report", guard_error)
            result = await engine._run_report(_turn("继续"), item, 1_700_000_000_000)  # noqa: SLF001
        else:
            monkeypatch.setattr(CompletionGuard, "complete", guard_error)
            result = await engine._complete(item, 1_700_000_000_000)  # noqa: SLF001

    assert result is not None
    assert result.status is ComposeTurnOutcome.RETRYABLE_FAILED
    await harness.persistence.close()


async def test_resume_phrase_attaches_same_work_item_without_duplicate(tmp_path: Path) -> None:
    """用户说“继续”只 attach 当前 Work Item，不重新理解已确认目标。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine()
    first_id = await _create_first(engine)

    result = await engine.execute_turn(_turn("继续", run_id="run-2"))

    assert result.work_item is not None
    assert result.work_item.work_item_id == first_id
    store = harness.persistence.compose_work_item_store()
    active = await store.load_active(THREAD)
    assert active is not None and active.work_item_id == first_id
    assert await store.load_run_binding(THREAD, "run-2") == first_id
    await harness.persistence.close()


async def test_new_goal_with_active_enters_typed_clarification(tmp_path: Path) -> None:
    """存在未终结项时新目标必须 typed clarification，不能直接创建冲突项。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["resume_current"])
    classifier = _FakeClassifier(
        [{"intent": "start_new_work"}, {"intent": "start_new_work"}]
    )
    engine = harness.engine(
        classifier=classifier,
        interaction=interaction,
    )
    first_id = await _create_first(engine)

    result = await engine.execute_turn(_turn("我想换个任务做", run_id="run-2"))

    assert result.work_item is not None
    assert result.work_item.work_item_id == first_id
    assert len(interaction.requests) == 1
    options = [option.value for option in interaction.requests[0].options]
    assert options == ["resume_current", "abandon_then_new", "cancel"]
    store = harness.persistence.compose_work_item_store()
    assert (await store.load_active(THREAD)).work_item_id == first_id
    await harness.persistence.close()


async def test_clarification_abandon_then_new_terminates_and_creates(tmp_path: Path) -> None:
    """用户在 clarification 中选择放弃后新建：旧项 abandon CAS，新项接替。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["abandon_then_new"])
    engine = harness.engine(
        classifier=_FakeClassifier(
            [{"intent": "start_new_work"}, {"intent": "start_new_work"}]
        ),
        interaction=interaction,
    )
    first_id = await _create_first(engine)

    result = await engine.execute_turn(_turn("换个新目标", run_id="run-2"))

    store = harness.persistence.compose_work_item_store()
    abandoned = await store.load(first_id)
    assert abandoned is not None and abandoned.status is ComposeWorkItemStatus.ABANDONED
    assert abandoned.revision == 1
    assert result.work_item is not None
    assert result.work_item.work_item_id != first_id
    assert (await store.load_active(THREAD)).work_item_id == result.work_item.work_item_id
    await harness.persistence.close()


async def test_clarification_cancel_keeps_active_unchanged(tmp_path: Path) -> None:
    """澄清中选择取消：不 abandon、不创建，当前 Work Item 原样保留。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["cancel"])
    engine = harness.engine(
        classifier=_FakeClassifier(
            [{"intent": "start_new_work"}, {"intent": "start_new_work"}]
        ),
        interaction=interaction,
    )
    first_id = await _create_first(engine)

    result = await engine.execute_turn(_turn("换个新目标", run_id="run-2"))

    store = harness.persistence.compose_work_item_store()
    active = await store.load_active(THREAD)
    assert active is not None and active.work_item_id == first_id
    assert (await store.load(first_id)).revision == 0
    assert result.work_item.work_item_id == first_id
    await harness.persistence.close()


async def test_side_question_is_isolated_from_ledger_and_readiness(tmp_path: Path) -> None:
    """/btw 式临时问题只走只读 side answer，不写 ledger、不改变 readiness。"""
    harness = await _Harness(tmp_path).open()
    side = _FakeSideAnswer()
    engine = harness.engine(side=side)
    first_id = await _create_first(engine)
    store = harness.persistence.compose_work_item_store()

    result = await engine.execute_turn(
        _turn("什么是 CAS？", explicit=_intent(TurnIntentKind.SIDE_QUESTION, "什么是 CAS？"))
    )

    assert result.side_answer == "side:什么是 CAS？"
    assert side.questions == ["什么是 CAS？"]
    assert result.work_item is not None and result.work_item.work_item_id == first_id
    assert await store.load_active(THREAD) is not None
    assert await store.load_document_references(first_id) == ()
    await harness.persistence.close()


async def test_side_question_works_without_active_work_item(tmp_path: Path) -> None:
    """没有未终结项时临时问题同样可直接回答，不创建 Work Item。"""
    harness = await _Harness(tmp_path).open()
    side = _FakeSideAnswer()
    engine = harness.engine(side=side)

    result = await engine.execute_turn(
        _turn("解释一下线程锁", explicit=_intent(TurnIntentKind.SIDE_QUESTION, "解释一下线程锁"))
    )

    assert result.side_answer == "side:解释一下线程锁"
    assert result.work_item is None
    store = harness.persistence.compose_work_item_store()
    assert await store.load_active(THREAD) is None
    await harness.persistence.close()


async def test_unclear_intent_enters_four_option_clarification(tmp_path: Path) -> None:
    """歧义输入必须让用户选择，不能猜测执行任何状态变更。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["cancel"])
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "unclear"}]),
        interaction=interaction,
    )
    result = await engine.execute_turn(_turn("随便吧"))

    assert len(interaction.requests) == 1
    options = [option.value for option in interaction.requests[0].options]
    assert options == ["amend_current", "start_new_work", "side_question", "cancel"]
    assert result.work_item is None
    store = harness.persistence.compose_work_item_store()
    assert await store.load_active(THREAD) is None
    await harness.persistence.close()


async def test_unclear_clarification_amend_routes_as_amendment(tmp_path: Path) -> None:
    """澄清中选择“修改当前任务”按 amend 路由：attach 且不新建。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["amend_current"])
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "unclear"}]),
        interaction=interaction,
    )
    first_id = await _create_first(engine)

    result = await engine.execute_turn(_turn("把目标改成搜索排序", run_id="run-2"))

    assert result.work_item is not None
    assert result.work_item.work_item_id == first_id
    assert result.pending_decision == "amend:task"
    store = harness.persistence.compose_work_item_store()
    assert (await store.load_active(THREAD)).work_item_id == first_id
    await harness.persistence.close()


async def test_unclear_clarification_start_new_work_creates_item(tmp_path: Path) -> None:
    """澄清中选择“开始新任务”按 start_new_work 路由并创建新项。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["start_new_work"])
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "unclear"}]),
        interaction=interaction,
    )
    result = await engine.execute_turn(_turn("做点别的事情"))

    assert result.work_item is not None
    store = harness.persistence.compose_work_item_store()
    assert (await store.load_active(THREAD)).work_item_id == result.work_item.work_item_id
    await harness.persistence.close()


async def test_cancelled_turn_never_creates_or_terminalizes(tmp_path: Path) -> None:
    """Esc/Run cancel 只中断当前执行，不终结 Work Item 也不创建新项。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "start_new_work"}])
    )
    result = await engine.execute_turn(_turn("实现搜索功能", cancelled=True))

    assert result.work_item is None
    store = harness.persistence.compose_work_item_store()
    assert await store.load_active(THREAD) is None

    first_id = await _create_first(engine)
    result = await engine.execute_turn(_turn("继续", run_id="run-2", cancelled=True))
    assert result.work_item is not None
    assert result.work_item.work_item_id == first_id
    assert (await store.load(first_id)).status is ComposeWorkItemStatus.ACTIVE
    await harness.persistence.close()


async def test_abandon_requires_revision_cas_and_preserves_documents(tmp_path: Path) -> None:
    """abandon 使用 expected revision CAS；陈旧请求失败，已写文档不被删除。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine()
    first_id = await _create_first(engine)
    store = harness.persistence.compose_work_item_store()
    documents = ComposeDocumentStore(harness.workspace)
    snapshot = await documents.commit(
        _document_commit(first_id, "compose-slug", ComposeDocumentKind.TASK)
    )
    assert snapshot is not None

    with pytest.raises(ComposeWorkItemStoreError, match="COMPOSE_WORK_ITEM_REVISION_CONFLICT"):
        await engine.abandon(
            thread_id=THREAD,
            work_item_id=first_id,
            expected_revision=1,
            reason="范围变化",
        )

    projection = await engine.abandon(
        thread_id=THREAD,
        work_item_id=first_id,
        expected_revision=0,
        reason="范围变化",
    )
    assert projection.status == ComposeWorkItemStatus.ABANDONED.value
    assert projection.revision == 1
    abandoned = await store.load(first_id)
    assert abandoned is not None and abandoned.status is ComposeWorkItemStatus.ABANDONED
    # 文档目录与正文保留，不自动回滚。
    remaining = await documents.inspect(first_id, "compose-slug", ComposeDocumentKind.TASK)
    assert remaining is not None and remaining.digest == snapshot.digest
    await harness.persistence.close()


async def test_abandon_missing_item_fails_closed(tmp_path: Path) -> None:
    """abandon 不存在的 Work Item 返回稳定错误。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine()
    with pytest.raises(ComposeWorkItemEngineError, match="COMPOSE_WORK_ITEM_NOT_FOUND"):
        await engine.abandon(
            thread_id=THREAD,
            work_item_id="wi-missing",
            expected_revision=0,
            reason=None,
        )
    await harness.persistence.close()


async def test_classifier_misjudgment_cannot_create_conflicting_item(tmp_path: Path) -> None:
    """分类器误判 start_new_work 不能绕过澄清直接创建冲突项。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["cancel"])
    engine = harness.engine(
        classifier=_FakeClassifier([{"intent": "start_new_work"}]),
        interaction=interaction,
    )
    first_id = await _create_first(engine)

    result = await engine.execute_turn(_turn("再做一个", run_id="run-2"))

    assert result.work_item is not None
    assert result.work_item.work_item_id == first_id
    store = harness.persistence.compose_work_item_store()
    assert (await store.load_active(THREAD)).work_item_id == first_id
    await harness.persistence.close()


async def test_run_binding_conflict_fails_closed(tmp_path: Path) -> None:
    """同一 run_id 解析到不同 Work Item 必须返回冲突，不能静默改绑。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine()
    first_id = await _create_first(engine)
    store = harness.persistence.compose_work_item_store()
    await store.terminalize(
        TerminalizeComposeWorkItem(
            work_item_id=first_id,
            expected_revision=0,
            status=ComposeWorkItemStatus.ABANDONED,
            terminal_at_ms=1_700_000_000_100,
        )
    )
    engine._ports.classifier.outputs = [{"intent": "start_new_work"}]  # noqa: SLF001
    with pytest.raises(ComposeWorkItemStoreError, match="RUN_WORK_ITEM_BINDING_CONFLICT"):
        await engine.execute_turn(_turn("新目标", run_id="run-1"))
    await harness.persistence.close()


async def test_resume_without_active_requires_clarification(tmp_path: Path) -> None:
    """没有未终结项时“继续”不能猜测目标，必须让用户选择。"""
    harness = await _Harness(tmp_path).open()
    interaction = _FakeInteraction(["start_new_work"])
    engine = harness.engine(interaction=interaction)

    result = await engine.execute_turn(_turn("继续"))

    assert len(interaction.requests) == 1
    options = [option.value for option in interaction.requests[0].options]
    assert options == ["start_new_work", "side_question", "cancel"]
    assert result.work_item is not None
    store = harness.persistence.compose_work_item_store()
    assert (await store.load_active(THREAD)).work_item_id == result.work_item.work_item_id
    await harness.persistence.close()


async def test_build_thread_rejects_compose_turn(tmp_path: Path) -> None:
    """Build Thread 不能经由 Compose engine 受理，Mode 锁定由数据库兜底。"""
    harness = await _Harness(tmp_path).open(mode=ThreadMode.BUILD)
    engine = harness.engine()
    with pytest.raises(ComposeWorkItemEngineError, match="THREAD_MODE_LOCKED"):
        await engine.execute_turn(_turn("实现搜索功能"))
    await harness.persistence.close()


async def test_inspect_returns_read_only_projection(tmp_path: Path) -> None:
    """inspect 只读投影：不写文档引用、不创建 Run 绑定。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine()
    first_id = await _create_first(engine)
    store = harness.persistence.compose_work_item_store()

    projection = await engine.inspect(thread_id=THREAD)
    assert projection is not None
    assert projection.work_item_id == first_id
    assert await store.load_run_binding(THREAD, "run-1") == first_id
    assert await store.load_document_references(first_id) == ()

    assert await engine.inspect(thread_id=THREAD, work_item_id="wi-unknown") is None
    await harness.persistence.close()


async def test_amend_explicit_intent_attaches_without_writes(tmp_path: Path) -> None:
    """amend 只 attach 与投影待决修订，不写文档、不创建新项。"""
    harness = await _Harness(tmp_path).open()
    engine = harness.engine()
    first_id = await _create_first(engine)
    store = harness.persistence.compose_work_item_store()

    result = await engine.execute_turn(
        _turn("把验收标准改一下", explicit=_intent(TurnIntentKind.AMEND_CURRENT, "把验收标准改一下"))
    )

    assert result.work_item is not None
    assert result.work_item.work_item_id == first_id
    assert result.pending_decision == "amend:task"
    assert await store.load_document_references(first_id) == ()
    await harness.persistence.close()


def _document_commit(work_item_id: str, slug: str, kind: ComposeDocumentKind):
    """构造一个 front matter 合法的最小文档提交命令。"""
    from harness_agent.compose.document_store import DocumentCommit

    body = f"# {kind.value}\n\n目标正文。\n"
    content = (
        "---\n"
        f"work_item_id: {work_item_id}\n"
        f"kind: {kind.value}\n"
        "revision: 1\n"
        "status: draft\n"
        "updated_at: 1700000000000\n"
        "---\n\n"
        f"{body}"
    )
    return DocumentCommit(
        work_item_id=work_item_id,
        slug=slug,
        kind=kind,
        content=content,
    )
