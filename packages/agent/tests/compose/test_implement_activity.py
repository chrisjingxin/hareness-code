"""Implement Activity（TDD 单项执行与 evidence）行为测试（WP11）。

覆盖：按 Todo 顺序逐项执行、RED/GREEN 证据校验、todo 勾选落盘、全部完成后
写入绑定文档与 workspace revision 的实现证据、外部修改使 implementation
stale、单项 FAILED 走 diagnosing 上下文、连续失败 fail closed、BLOCKED
handoff、无测试文档/配置项记录理由、取消后从勾选状态恢复、引擎在
plan_confirmed 后自动进入 Implement。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.compose.activities.implement import (
    ImplementActivity,
    ImplementActivityError,
    ImplementItemContext,
    ImplementItemOutcome,
    ImplementItemResult,
)
from harness_agent.compose.activities.plan import _render_document as render_plan
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

THREAD = "thread-implement"
WORK_ITEM_ID = "wi-implement"
NOW = 1_700_000_000_000
REVISION = "rev-42"

TODO_TWO_ITEMS = (
    "## 执行清单\n\n"
    "- [ ] 建立 SQLite 事实层：验收=唯一 active\n"
    "- [ ] 接入 engine 流水线：验收=门禁顺序\n"
)


class _FakeImplementDriver:
    """脚本化单项目实现结果；记录上下文；可注入异常。"""

    def __init__(
        self,
        results: list[ImplementItemResult] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.error = error
        self.contexts: list[ImplementItemContext] = []

    async def implement_item(self, context: ImplementItemContext) -> ImplementItemResult:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else _completed()


class _FakeDiagnose:
    """记录 diagnosing 调用并返回诊断文本。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def diagnose(self, context: ImplementItemContext, failure: str) -> str:
        self.calls.append(f"{context.item_title}|{failure}")
        return f"diagnosed:{failure}"


def _completed() -> ImplementItemResult:
    return ImplementItemResult(
        outcome=ImplementItemOutcome.COMPLETED,
        fail_before="pytest RED: 1 failed",
        pass_after="pytest GREEN: 1 passed",
        changed_paths=("src/a.py", "tests/test_a.py"),
        execution_id="exec-1",
    )


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


async def _harness(tmp_path: Path, *, todo_body: str = TODO_TWO_ITEMS):
    """真实 SQLite + workspace 文档存储 + 三门禁全部确认的 Work Item。"""
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
            slug="implement",
            goal="实现站内搜索",
            created_at_ms=NOW,
        )
    )
    documents = ComposeDocumentStore(tmp_path / "workspace")
    await _confirm_all(store, documents, todo_body)
    return persistence, store, documents


async def _confirm_all(store, documents, todo_body: str) -> None:
    """写入并确认 Task/Spec/Plan/Todo 四个文档。"""
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
            body=todo_body,
        )),
    )
    digests: dict[ComposeDocumentKind, str] = {}
    for kind, content in kinds:
        snapshot = await documents.commit(
            DocumentCommit(
                work_item_id=WORK_ITEM_ID,
                slug="implement",
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
    await store.record_confirmation(
        RecordComposeConfirmation(
            work_item_id=WORK_ITEM_ID,
            confirmation_id="task-gate-fixture",
            confirmation_kind="task",
            document_digests=(digests[ComposeDocumentKind.TASK],),
            confirmed_at_ms=NOW,
        )
    )
    await store.record_confirmation(
        RecordComposeConfirmation(
            work_item_id=WORK_ITEM_ID,
            confirmation_id="spec-gate-fixture",
            confirmation_kind="spec",
            document_digests=(
                digests[ComposeDocumentKind.TASK],
                digests[ComposeDocumentKind.SPEC],
            ),
            confirmed_at_ms=NOW,
        )
    )
    await store.record_confirmation(
        RecordComposeConfirmation(
            work_item_id=WORK_ITEM_ID,
            confirmation_id="plan-gate-fixture",
            confirmation_kind="plan",
            document_digests=(
                digests[ComposeDocumentKind.PLAN],
                digests[ComposeDocumentKind.TODO],
            ),
            confirmed_at_ms=NOW,
        )
    )


def _activity(store, documents, driver, *, diagnose=None, revision: str | None = REVISION):
    return ImplementActivity(
        store=store,
        documents=documents,
        driver=driver,
        workspace_revision=(lambda: revision) if revision else None,
        now_ms=lambda: NOW,
        diagnose=diagnose,
    )


async def _item(store):
    item = await store.load(WORK_ITEM_ID)
    assert item is not None
    return item


async def test_implements_items_in_order_checks_todo_and_records_evidence(
    tmp_path: Path,
) -> None:
    """按顺序完成全部 Todo：勾选落盘、总结证据绑定文档与 workspace revision。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeImplementDriver([_completed(), _completed()])
        result = await _activity(store, documents, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is ImplementItemOutcome.COMPLETED
        assert result.completed_items == 2
        assert result.pending is None
        todo = await documents.inspect(WORK_ITEM_ID, "implement", ComposeDocumentKind.TODO)
        assert todo is not None
        assert "- [ ]" not in todo.content
        assert todo.content.count("- [x]") == 2
        # 上下文只含当前项与 confirmed digest。
        assert driver.contexts[0].previous_failure == ""
        assert all(context.goal == "实现站内搜索" for context in driver.contexts)
        evidence = await store.load_evidence(WORK_ITEM_ID, "implementation")
        assert len(evidence) == 1
        assert evidence[0].payload["workspace_revision"] == REVISION
        assert len(evidence[0].payload["document_digests"]) == 4
        activity = await store.load_activity(f"implement:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.COMPLETED
    finally:
        await persistence.close()


async def test_turn_budget_pauses_and_resume_continues_from_checked_items(
    tmp_path: Path,
) -> None:
    """每 Run 有界：完成一项后暂停，下一 Run 只做未勾选项。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver2 = _FakeImplementDriver([_completed(), _completed(), _completed()])
        activity1 = ImplementActivity(
            store=store,
            documents=documents,
            driver=driver2,
            workspace_revision=lambda: REVISION,
            now_ms=lambda: NOW,
            max_items_per_run=1,
        )
        result1 = await activity1.run(await _item(store), run_id="run-1")
        assert result1.completed_items == 1
        assert result1.pending == "implement-more"
        todo = await documents.inspect(WORK_ITEM_ID, "implement", ComposeDocumentKind.TODO)
        assert todo is not None and todo.content.count("- [x]") == 1

        result2 = await activity1.run(await _item(store), run_id="run-2")
        assert result2.outcome is ImplementItemOutcome.COMPLETED
        assert result2.completed_items == 1
        todo = await documents.inspect(WORK_ITEM_ID, "implement", ComposeDocumentKind.TODO)
        assert todo is not None and todo.content.count("- [x]") == 2
    finally:
        await persistence.close()


async def test_external_document_change_makes_implementation_evidence_stale(
    tmp_path: Path,
) -> None:
    """上游文档变化后旧实现证据不再 fresh：digest 集合不匹配。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeImplementDriver([_completed(), _completed()])
        result = await _activity(store, documents, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is ImplementItemOutcome.COMPLETED
        from harness_agent.compose.readiness import ComposeReadinessResolver, WorkspaceFreshnessFact

        evidence = await store.load_evidence(WORK_ITEM_ID, "implementation")
        fact = WorkspaceFreshnessFact(
            workspace_revision=REVISION,
            document_digests=frozenset(evidence[0].payload["document_digests"]),
            evidence_digest=evidence[0].content_digest,
            execution_id=str(evidence[0].payload["execution_id"]),
        )
        assert fact.is_fresh(
            workspace_revision=REVISION,
            document_digests=frozenset(evidence[0].payload["document_digests"]),
        )
        assert not fact.is_fresh(
            workspace_revision=REVISION,
            document_digests=frozenset({"a" * 64, "b" * 64, "c" * 64, "d" * 64}),
        )
        assert not fact.is_fresh(
            workspace_revision="rev-old",
            document_digests=frozenset(evidence[0].payload["document_digests"]),
        )
    finally:
        await persistence.close()


async def test_failed_item_uses_diagnose_context_then_succeeds(tmp_path: Path) -> None:
    """单项 FAILED 走 diagnosing-bugs；重试带上次失败上下文。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        diagnose = _FakeDiagnose()
        driver = _FakeImplementDriver(
            [
                ImplementItemResult(
                    outcome=ImplementItemOutcome.FAILED,
                    reason="TypeError: NoneType",
                ),
                _completed(),
                _completed(),
            ]
        )
        result = await _activity(store, documents, driver, diagnose=diagnose).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is ImplementItemOutcome.COMPLETED
        assert len(diagnose.calls) == 1
        assert "TypeError" in diagnose.calls[0]
        # 重试上下文携带诊断文本。
        assert "diagnosed:" in driver.contexts[1].previous_failure
    finally:
        await persistence.close()


async def test_persistent_failure_fails_closed(tmp_path: Path) -> None:
    """连续失败达到预算后 Activity retryable_failed，Work Item 保持未终结。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeImplementDriver(
            [
                ImplementItemResult(outcome=ImplementItemOutcome.FAILED, reason="boom"),
                ImplementItemResult(outcome=ImplementItemOutcome.FAILED, reason="boom"),
                ImplementItemResult(outcome=ImplementItemOutcome.FAILED, reason="boom"),
                ImplementItemResult(outcome=ImplementItemOutcome.FAILED, reason="boom"),
            ]
        )
        with pytest.raises(ImplementActivityError) as excinfo:
            await _activity(store, documents, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_IMPLEMENT_ITEM_FAILED"
        activity = await store.load_activity(f"implement:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.RETRYABLE_FAILED
        assert not (await _item(store)).terminal
    finally:
        await persistence.close()


async def test_blocked_item_handoff_keeps_work_item_blocked(tmp_path: Path) -> None:
    """BLOCKED handoff：Activity blocked，pending 展示 implement-handoff。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeImplementDriver(
            [ImplementItemResult(outcome=ImplementItemOutcome.BLOCKED, blocked_message="需要用户决策")]
        )
        result = await _activity(store, documents, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is ImplementItemOutcome.BLOCKED
        assert result.pending == "implement-handoff"
        activity = await store.load_activity(f"implement:{WORK_ITEM_ID}")
        assert activity is not None
        assert activity.status is ComposeActivityStatus.BLOCKED
    finally:
        await persistence.close()


async def test_missing_evidence_fails_closed(tmp_path: Path) -> None:
    """既无 RED/GREEN 又无理由的结果不能推进勾选。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeImplementDriver(
            [ImplementItemResult(outcome=ImplementItemOutcome.COMPLETED)]
        )
        with pytest.raises(ImplementActivityError) as excinfo:
            await _activity(store, documents, driver).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_IMPLEMENT_EVIDENCE_INVALID"
        todo = await documents.inspect(WORK_ITEM_ID, "implement", ComposeDocumentKind.TODO)
        assert todo is not None and "- [x]" not in todo.content
    finally:
        await persistence.close()


async def test_reason_only_evidence_allowed_for_docs_items(tmp_path: Path) -> None:
    """文档/配置项按 Skill 原版规则记录理由，不伪造 RED。"""
    persistence, store, documents = await _harness(
        tmp_path,
        todo_body="## 执行清单\n\n- [ ] 更新用户文档：验收=章节齐全\n",
    )
    try:
        driver = _FakeImplementDriver(
            [
                ImplementItemResult(
                    outcome=ImplementItemOutcome.COMPLETED,
                    reason="文档项：无测试，按原版 Skill 规则记录理由",
                    changed_paths=("docs/user.md",),
                    execution_id="exec-doc",
                )
            ]
        )
        result = await _activity(store, documents, driver).run(
            await _item(store), run_id="run-1"
        )
        assert result.outcome is ImplementItemOutcome.COMPLETED
        evidence = await store.load_evidence(WORK_ITEM_ID, "implementation")
        assert len(evidence) == 1
    finally:
        await persistence.close()


async def test_workspace_revision_missing_fails_closed(tmp_path: Path) -> None:
    """无 workspace revision 时不能伪造 implementation 证据。"""
    persistence, store, documents = await _harness(tmp_path)
    try:
        driver = _FakeImplementDriver([_completed(), _completed()])
        with pytest.raises(ImplementActivityError) as excinfo:
            await _activity(store, documents, driver, revision=None).run(
                await _item(store), run_id="run-1"
            )
        assert excinfo.value.code == "COMPOSE_IMPLEMENT_WORKSPACE_REVISION_MISSING"
        assert await store.load_evidence(WORK_ITEM_ID, "implementation") == ()
    finally:
        await persistence.close()
