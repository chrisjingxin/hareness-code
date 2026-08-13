"""Plan/Todo gate：planning-and-task-breakdown 双文件草稿与联合确认。

Spec 已确认后才可执行本 Activity；driver 同时生成 `plan.md` 与 `todo.md`，
二者作为同一 revision 成对提交。Runtime 在门禁前校验：正文非空、无
placeholder、todo 不含“决定/评估/选择方案”类实施期设计项、至少一项可执行
条目。确认记录绑定当前 Plan+Todo digest 组合；任一文件被外部修改都会使
`plan_confirmed`/`todo_executable` 同时 stale。模型输出不能推进确认。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

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
    has_placeholder,
)
from harness_agent.threads.compose_work_item_store import (
    ComposeWorkItemStore,
    ComposeWorkItemStoreError,
    FinishComposeActivity,
    RecordComposeConfirmation,
    RestartComposeActivity,
    StartComposeActivity,
    UpsertComposeDocumentReference,
)

PLAN_ACTIVITY_KIND = "plan"
"""plan/todo Activity 的稳定 ledger kind。"""

_GATE_QUESTION_ID = "plan-gate"
_FEEDBACK_QUESTION_ID = "plan-feedback"
_FORBIDDEN_TODO_TERMS = ("决定", "评估", "选择方案")
_TODO_ITEM_LINE = ("- [ ]", "- [x]")


class PlanGateActivityError(RuntimeError):
    """Plan/Todo gate 的稳定错误码；上层映射为可恢复投影或 blocked。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class PlanGateOutcome(str, Enum):
    """一次 Plan/Todo gate 执行的收敛结果。"""

    WAITING_GATE = "waiting_gate"
    CONFIRMED = "confirmed"
    ABANDONED = "abandoned"
    REVISE = "revise"


@dataclass(frozen=True, slots=True)
class PlanGateResult:
    """gate 收敛结果与展示用 pending decision。"""

    outcome: PlanGateOutcome
    pending: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class PlanDraftContext:
    """plan driver 一次草稿生成所需的受限上下文。"""

    goal: str
    task_digest: str
    spec_digest: str
    spec_body: str
    feedback: str = ""


@dataclass(frozen=True, slots=True)
class PlanDraft:
    """plan.md 与 todo.md 的成对草稿正文。"""

    plan_body: str
    todo_body: str


class PlanDriver(Protocol):
    """plan 模型回合 seam；生产实现绑定原版 planning-and-task-breakdown。"""

    async def draft_plan(self, context: PlanDraftContext) -> PlanDraft:
        """返回 plan.md 与 todo.md 的成对正文（不含 front matter）。"""


class PlanGateActivity:
    """plan.md + todo.md 双文件草稿与联合 typed 门禁。"""

    def __init__(
        self,
        *,
        store: ComposeWorkItemStore,
        documents: ComposeDocumentStore,
        interaction: Any,
        driver: PlanDriver,
        now_ms: Callable[[], int | None] | None = None,
    ) -> None:
        self._store = store
        self._documents = documents
        self._interaction = interaction
        self._driver = driver
        self._now_ms_port = now_ms

    async def run(
        self,
        item: ComposeWorkItem,
        *,
        run_id: str,
    ) -> PlanGateResult:
        """执行或恢复 Plan/Todo gate；所有退出路径收敛 Activity ledger。"""
        activity_id = f"plan:{item.work_item_id}"
        await self._ensure_activity_running(activity_id, item, run_id)
        try:
            return await self._run_draft_and_gate(item, activity_id)
        except asyncio.CancelledError:
            raise
        except PlanGateActivityError:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise
        except Exception as exc:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise PlanGateActivityError("COMPOSE_PLAN_EXECUTION_FAILED") from exc

    async def _ensure_activity_running(
        self,
        activity_id: str,
        item: ComposeWorkItem,
        run_id: str,
    ) -> None:
        """start 或从可恢复状态 restart 本 Activity；running 直接继续。"""
        try:
            existing = await self._store.load_activity(activity_id)
            if existing is None:
                await self._store.start_activity(
                    StartComposeActivity(
                        activity_id=activity_id,
                        work_item_id=item.work_item_id,
                        run_id=run_id,
                        kind=PLAN_ACTIVITY_KIND,
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
        except PlanGateActivityError:
            raise
        except ComposeWorkItemStoreError as exc:
            raise PlanGateActivityError("COMPOSE_PLAN_LEDGER_FAILED") from exc

    async def _finish(
        self,
        activity_id: str,
        status: ComposeActivityStatus,
    ) -> None:
        """收敛 Activity 终态；已结束的 Activity 不重复收敛。"""
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
            raise PlanGateActivityError("COMPOSE_PLAN_LEDGER_FAILED") from exc

    async def _run_draft_and_gate(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> PlanGateResult:
        spec = await self._read_snapshot(item, ComposeDocumentKind.SPEC)
        if spec is None:
            raise PlanGateActivityError("COMPOSE_PLAN_SPEC_MISSING")
        task = await self._read_snapshot(item, ComposeDocumentKind.TASK)
        if task is None:
            raise PlanGateActivityError("COMPOSE_PLAN_TASK_MISSING")
        plan_snapshot = await self._read_snapshot(item, ComposeDocumentKind.PLAN)
        todo_snapshot = await self._read_snapshot(item, ComposeDocumentKind.TODO)
        feedback = ""
        while True:
            if plan_snapshot is None or todo_snapshot is None or feedback:
                plan_snapshot, todo_snapshot = await self._propose_draft(
                    item, task, spec, plan_snapshot, todo_snapshot, feedback
                )
            gate = await self._gate(item, activity_id, plan_snapshot, todo_snapshot)
            if gate.outcome is PlanGateOutcome.REVISE:
                feedback = await self._collect_feedback(item, activity_id)
                continue
            return gate

    async def _propose_draft(
        self,
        item: ComposeWorkItem,
        task: ComposeDocumentSnapshot,
        spec: ComposeDocumentSnapshot,
        plan_snapshot: ComposeDocumentSnapshot | None,
        todo_snapshot: ComposeDocumentSnapshot | None,
        feedback: str,
    ) -> tuple[ComposeDocumentSnapshot, ComposeDocumentSnapshot]:
        """让 driver 成对生成 plan/todo 并提交同一 revision。"""
        context = PlanDraftContext(
            goal=item.goal,
            task_digest=task.digest,
            spec_digest=spec.digest,
            spec_body=spec.content,
            feedback=feedback,
        )
        try:
            draft = await self._driver.draft_plan(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PlanGateActivityError("COMPOSE_PLAN_EXECUTION_FAILED") from exc
        _validate_plan_draft(draft)
        revision = (
            max(
                plan_snapshot.revision if plan_snapshot is not None else 0,
                todo_snapshot.revision if todo_snapshot is not None else 0,
            )
            + 1
        )
        committed_plan = await self._commit(
            item,
            ComposeDocumentKind.PLAN,
            plan_snapshot,
            _render_document(
                work_item_id=item.work_item_id,
                kind=ComposeDocumentKind.PLAN,
                revision=revision,
                status="proposed",
                updated_at_ms=self._now_ms(),
                body=draft.plan_body,
            ),
        )
        await self._sync_reference(item, ComposeDocumentKind.PLAN, committed_plan)
        try:
            committed_todo = await self._commit(
                item,
                ComposeDocumentKind.TODO,
                todo_snapshot,
                _render_document(
                    work_item_id=item.work_item_id,
                    kind=ComposeDocumentKind.TODO,
                    revision=revision,
                    status="proposed",
                    updated_at_ms=self._now_ms(),
                    body=draft.todo_body,
                ),
            )
            await self._sync_reference(item, ComposeDocumentKind.TODO, committed_todo)
        except PlanGateActivityError:
            raise
        return committed_plan, committed_todo

    async def _gate(
        self,
        item: ComposeWorkItem,
        activity_id: str,
        plan_snapshot: ComposeDocumentSnapshot,
        todo_snapshot: ComposeDocumentSnapshot,
    ) -> PlanGateResult:
        """联合门禁：确认绑定 Plan+Todo digest，修改收集 feedback，放弃返回。"""
        choice = await self._choice(
            question_id=_GATE_QUESTION_ID,
            header="确认实施计划",
            body=f"目标：{item.goal}\n请确认 plan.md 与 todo.md 后批准实施。",
            options=(
                ("confirm", "批准实施", "批准当前 Plan/Todo 并进入自动实现"),
                ("revise", "提出修改", "提供反馈并重新生成计划"),
                ("abandon", "放弃", "终结当前 Work Item（文档保留）"),
            ),
        )
        if choice == "confirm":
            await self._store.record_confirmation(
                RecordComposeConfirmation(
                    work_item_id=item.work_item_id,
                    confirmation_id=(
                        f"plan-gate-{self._now_ms()}-{plan_snapshot.digest[:16]}"
                    ),
                    confirmation_kind="plan",
                    document_digests=(plan_snapshot.digest, todo_snapshot.digest),
                    confirmed_at_ms=self._now_ms(),
                )
            )
            await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
            return PlanGateResult(
                PlanGateOutcome.CONFIRMED,
                pending=None,
                revision=plan_snapshot.revision,
            )
        if choice == "abandon":
            await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
            return PlanGateResult(
                PlanGateOutcome.ABANDONED,
                pending=None,
                revision=plan_snapshot.revision,
            )
        return PlanGateResult(
            PlanGateOutcome.REVISE, pending=None, revision=plan_snapshot.revision
        )

    async def _collect_feedback(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> str:
        """收集修改 feedback；空输入视为取消修订，重新进入 gate。"""
        raw = await self._answer(
            question_id=_FEEDBACK_QUESTION_ID,
            header="计划修改反馈",
            body="请说明需要修改的内容：",
            options=(),
        )
        feedback = str(raw or "").strip()
        if not feedback:
            raise PlanGateActivityError("COMPOSE_PLAN_FEEDBACK_EMPTY")
        return feedback

    # ---------- 文档与引用 ----------

    async def _read_snapshot(
        self,
        item: ComposeWorkItem,
        kind: ComposeDocumentKind,
    ) -> ComposeDocumentSnapshot | None:
        try:
            return await self._documents.inspect(item.work_item_id, item.slug, kind)
        except ComposeDocumentStoreError as exc:
            raise PlanGateActivityError("COMPOSE_PLAN_DOCUMENT_INVALID") from exc

    async def _commit(
        self,
        item: ComposeWorkItem,
        kind: ComposeDocumentKind,
        expected: ComposeDocumentSnapshot | None,
        content: str,
    ) -> ComposeDocumentSnapshot:
        try:
            return await self._documents.commit(
                DocumentCommit(
                    work_item_id=item.work_item_id,
                    slug=item.slug,
                    kind=kind,
                    content=content,
                    expected=expected,
                )
            )
        except ComposeDocumentStoreError as exc:
            raise PlanGateActivityError("COMPOSE_PLAN_DRAFT_INVALID") from exc

    async def _sync_reference(
        self,
        item: ComposeWorkItem,
        kind: ComposeDocumentKind,
        snapshot: ComposeDocumentSnapshot,
    ) -> None:
        """把当前文档 identity 同步到 SQLite，供 confirmation 与 readiness 使用。"""
        try:
            await self._store.upsert_document_reference(
                UpsertComposeDocumentReference(
                    work_item_id=item.work_item_id,
                    kind=kind,
                    relative_path=snapshot.relative_path,
                    content_digest=snapshot.digest,
                    revision=snapshot.revision,
                    updated_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise PlanGateActivityError("COMPOSE_PLAN_REFERENCE_FAILED") from exc

    # ---------- Interaction ----------

    async def _choice(
        self,
        *,
        question_id: str,
        header: str,
        body: str,
        options: tuple[tuple[str, str, str], ...],
    ) -> str:
        result = await self._interaction.request_decision(
            self._request(
                question_id=question_id,
                header=header,
                body=body,
                options=options,
            )
        )
        if result.expired:
            raise PlanGateActivityError("COMPOSE_PLAN_DECISION_UNAVAILABLE")
        value = _answer_value(result, question_id)
        if not value:
            raise PlanGateActivityError("COMPOSE_PLAN_DECISION_UNAVAILABLE")
        return str(value)

    async def _answer(
        self,
        *,
        question_id: str,
        header: str,
        body: str,
        options: tuple[tuple[str, str, str], ...],
    ) -> str | None:
        result = await self._interaction.request_decision(
            self._request(
                question_id=question_id,
                header=header,
                body=body,
                options=options,
            )
        )
        if result.expired:
            return None
        return _answer_value(result, question_id)

    def _request(
        self,
        *,
        question_id: str,
        header: str,
        body: str,
        options: tuple[tuple[str, str, str], ...],
    ):
        """构造 typed decision 请求；开放式问题使用 allow_other。"""
        # 延迟导入避免与 work_item_engine 的模块循环依赖。
        from harness_agent.compose.work_item_engine import (
            TypedDecisionOption,
            TypedDecisionRequest,
        )

        interrupt_id = f"compose-{question_id}-{self._now_ms()}"
        return TypedDecisionRequest(
            request_id=interrupt_id,
            interrupt_id=interrupt_id,
            question_id=question_id,
            header=header,
            body=body,
            options=tuple(
                TypedDecisionOption(
                    value=value,
                    label=label,
                    description=description,
                )
                for value, label, description in options
            ),
            allow_other=True,
        )

    def _now_ms(self) -> int:
        now = self._now_ms_port() if self._now_ms_port is not None else None
        return int(now) if now is not None else int(time.time() * 1000)


def _validate_plan_draft(draft: PlanDraft) -> None:
    """确定性校验成对草稿：非空、无 placeholder、todo 可执行。"""
    plan_body = draft.plan_body.strip()
    todo_body = draft.todo_body.strip()
    if not plan_body:
        raise PlanGateActivityError("COMPOSE_PLAN_DRAFT_INVALID")
    if not todo_body:
        raise PlanGateActivityError("COMPOSE_PLAN_TODO_INVALID")
    if has_placeholder(plan_body) or has_placeholder(todo_body):
        raise PlanGateActivityError("COMPOSE_PLAN_PLACEHOLDER_INVALID")
    todo_items = [
        line
        for line in todo_body.splitlines()
        if line.strip().startswith(_TODO_ITEM_LINE)
    ]
    if not todo_items:
        raise PlanGateActivityError("COMPOSE_PLAN_TODO_INVALID")
    if any(term in line for line in todo_items for term in _FORBIDDEN_TODO_TERMS):
        raise PlanGateActivityError("COMPOSE_PLAN_TODO_INVALID")


def _render_document(
    *,
    work_item_id: str,
    kind: ComposeDocumentKind,
    revision: int,
    status: str,
    updated_at_ms: int,
    body: str,
) -> str:
    """渲染带固定 front matter 的 Markdown 文档。"""
    body_text = body.strip()
    return (
        "---\n"
        f"work_item_id: {work_item_id}\n"
        f"kind: {kind.value}\n"
        f"revision: {revision}\n"
        f"status: {status}\n"
        f"updated_at: {updated_at_ms}\n"
        "---\n"
        f"{body_text}\n"
    )


def _answer_value(result: Any, question_id: str) -> str | None:
    """从 InteractionResult 的 answers 形状提取单选值。"""
    answers = result.value.get("answers", {}) if isinstance(result.value, Mapping) else {}
    raw = answers.get(question_id, []) if isinstance(answers, Mapping) else []
    items = raw if isinstance(raw, list) else []
    return str(items[0]) if items else None
