"""Task gate：grill 访谈、task.md 草稿与 typed confirmation。

访谈一次只问一个决策问题；每个回答立即以 ``status: draft`` 落盘到 task.md，
因此 429、Esc 取消、崩溃或 Host 重启后都能从最后一条已答问题继续，而不是
重新开始。访谈结束后由 GrillDriver 生成正式正文（``status: proposed``），
随后进入 [确认目标与范围 | 提出修改 | 放弃] typed gate。只有用户确认当前
digest 后才写入 confirmation 事实；修改 feedback 会生成新 revision 并使旧
确认 stale。模型输出不能推进确认，畸形草稿 fail closed。
"""

from __future__ import annotations

import asyncio
import re
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

TASK_ACTIVITY_KIND = "task"
"""grill/task Activity 的稳定 ledger kind。"""

MAX_INTERVIEW_ROUNDS_PER_RUN = 8
"""单个 Run 内最多完成的问答轮次；超出收敛为 waiting_answer。"""

MAX_INTERVIEW_ANSWERS = 64
"""访谈记录的最大持久化问答对数，防止草稿无界增长。"""

DRAFT_STATUS = "draft"
PROPOSED_STATUS = "proposed"

_INTERVIEW_MARKER = "## 访谈记录"
_QA_LINE = re.compile(r"^- (问|答): (.*)$")
_GATE_QUESTION_ID = "task-gate"
_FEEDBACK_QUESTION_ID = "task-feedback"
_INTERVIEW_QUESTION_ID = "task-interview"


class TaskGateActivityError(RuntimeError):
    """Task gate 的稳定错误码；上层映射为可恢复投影或 blocked。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class TaskGateOutcome(str, Enum):
    """一次 Task gate 执行的收敛结果。"""

    WAITING_ANSWER = "waiting_answer"
    WAITING_GATE = "waiting_gate"
    CONFIRMED = "confirmed"
    ABANDONED = "abandoned"
    REVISE = "revise"


@dataclass(frozen=True, slots=True)
class TaskGateResult:
    """gate 收敛结果与展示用 pending decision。"""

    outcome: TaskGateOutcome
    pending: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class TaskInterviewContext:
    """grill 模型一次决策所需的受限上下文；不携带完整对话。"""

    goal: str
    scope_summary: str
    answers: tuple[tuple[str, str], ...]
    feedback: str = ""


class GrillDriver(Protocol):
    """grill 模型回合 seam；生产实现绑定原版 grill-me Skill。"""

    async def next_question(self, context: TaskInterviewContext) -> str | None:
        """返回下一个决策问题；``None`` 表示访谈结束可以成稿。"""

    async def draft_task(self, context: TaskInterviewContext) -> str:
        """返回 task.md 正文（不含 front matter）。"""


class TaskGateActivity:
    """grill 访谈 + task.md 草稿 + typed 门禁；所有状态可跨 Run 恢复。"""

    def __init__(
        self,
        *,
        store: ComposeWorkItemStore,
        documents: ComposeDocumentStore,
        interaction: Any,
        driver: GrillDriver,
        now_ms: Callable[[], int | None] | None = None,
        max_rounds_per_run: int = MAX_INTERVIEW_ROUNDS_PER_RUN,
    ) -> None:
        self._store = store
        self._documents = documents
        self._interaction = interaction
        self._driver = driver
        self._now_ms_port = now_ms
        self._max_rounds = max_rounds_per_run

    async def run(
        self,
        item: ComposeWorkItem,
        *,
        run_id: str,
    ) -> TaskGateResult:
        """执行或恢复 Task gate；所有退出路径收敛 Activity ledger。"""
        activity_id = f"task:{item.work_item_id}"
        await self._ensure_activity_running(activity_id, item, run_id)
        try:
            return await self._run_interview_and_gate(item, activity_id)
        except asyncio.CancelledError:
            raise
        except TaskGateActivityError:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise
        except Exception as exc:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise TaskGateActivityError("COMPOSE_TASK_EXECUTION_FAILED") from exc

    # ---------- Activity ledger ----------

    async def _ensure_activity_running(
        self,
        activity_id: str,
        item: ComposeWorkItem,
        run_id: str,
    ) -> None:
        """start 或从可恢复状态 restart 本 Activity；running 直接继续。"""
        try:
            existing = await self._store.load_activity(activity_id)
        except ComposeWorkItemStoreError as exc:
            raise TaskGateActivityError("COMPOSE_TASK_LEDGER_FAILED") from exc
        try:
            if existing is None:
                await self._store.start_activity(
                    StartComposeActivity(
                        activity_id=activity_id,
                        work_item_id=item.work_item_id,
                        run_id=run_id,
                        kind=TASK_ACTIVITY_KIND,
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
        except TaskGateActivityError:
            raise
        except ComposeWorkItemStoreError as exc:
            raise TaskGateActivityError("COMPOSE_TASK_LEDGER_FAILED") from exc

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
            raise TaskGateActivityError("COMPOSE_TASK_LEDGER_FAILED") from exc

    # ---------- 访谈与门禁 ----------

    async def _run_interview_and_gate(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> TaskGateResult:
        snapshot = await self._read_snapshot(item)
        if snapshot is None:
            answers: tuple[tuple[str, str], ...] = ()
        elif snapshot.status == DRAFT_STATUS:
            answers = _parse_interview_answers(snapshot.content)
        elif snapshot.status == PROPOSED_STATUS:
            answers = ()
        else:
            raise TaskGateActivityError("COMPOSE_TASK_STATE_INVALID")
        revision = snapshot.revision if snapshot is not None else 0
        feedback = ""
        while True:
            if snapshot is None or snapshot.status == DRAFT_STATUS or feedback:
                interview = await self._interview(
                    item,
                    snapshot,
                    answers,
                    feedback,
                    revision,
                )
                if interview.outcome is TaskGateOutcome.WAITING_ANSWER:
                    await self._finish(activity_id, ComposeActivityStatus.WAITING_USER)
                    return interview
                snapshot = await self._read_snapshot(item)
                if snapshot is None:
                    raise TaskGateActivityError("COMPOSE_TASK_STATE_INVALID")
                revision = snapshot.revision
                answers = ()
            gate = await self._gate(item, activity_id, snapshot)
            if gate.outcome is TaskGateOutcome.REVISE:
                feedback = await self._collect_feedback(item, activity_id)
                snapshot = await self._read_snapshot(item)
                if snapshot is None:
                    raise TaskGateActivityError("COMPOSE_TASK_STATE_INVALID")
                revision = snapshot.revision
                answers = ()
                continue
            return gate

    async def _interview(
        self,
        item: ComposeWorkItem,
        snapshot: ComposeDocumentSnapshot | None,
        answers: tuple[tuple[str, str], ...],
        feedback: str,
        revision: int,
    ) -> TaskGateResult:
        """一次有界问答循环；每轮回答先落盘草稿再继续。"""
        rounds = 0
        current_snapshot = snapshot
        while rounds < self._max_rounds:
            context = TaskInterviewContext(
                goal=item.goal,
                scope_summary="",
                answers=answers,
                feedback=feedback,
            )
            try:
                question = await self._driver.next_question(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise TaskGateActivityError("COMPOSE_TASK_EXECUTION_FAILED") from exc
            if question is None:
                return await self._propose_draft(
                    item, current_snapshot, context, revision
                )
            answer = await self._ask_question(item, question)
            if answer is None:
                return TaskGateResult(
                    TaskGateOutcome.WAITING_ANSWER,
                    pending=_INTERVIEW_QUESTION_ID,
                    revision=revision,
                )
            answers = answers + ((question, answer),)
            revision += 1
            current_snapshot = await self._commit_draft(
                item,
                expected=current_snapshot,
                revision=revision,
                answers=answers,
                feedback=feedback,
            )
            rounds += 1
        return TaskGateResult(
            TaskGateOutcome.WAITING_ANSWER,
            pending=_INTERVIEW_QUESTION_ID,
            revision=revision,
        )

    async def _propose_draft(
        self,
        item: ComposeWorkItem,
        snapshot: ComposeDocumentSnapshot | None,
        context: TaskInterviewContext,
        revision: int,
    ) -> TaskGateResult:
        """让 driver 生成正式正文并提交 proposed revision。"""
        try:
            body = await self._driver.draft_task(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TaskGateActivityError("COMPOSE_TASK_EXECUTION_FAILED") from exc
        revision += 1
        content = _render_document(
            work_item_id=item.work_item_id,
            revision=revision,
            status=PROPOSED_STATUS,
            updated_at_ms=self._now_ms(),
            body=body,
        )
        committed = await self._commit(item, expected=snapshot, content=content)
        await self._sync_reference(item, committed)
        return TaskGateResult(
            TaskGateOutcome.WAITING_GATE,
            pending=_GATE_QUESTION_ID,
            revision=revision,
        )

    async def _gate(
        self,
        item: ComposeWorkItem,
        activity_id: str,
        snapshot: ComposeDocumentSnapshot,
    ) -> TaskGateResult:
        """typed gate：确认绑定当前 digest，修改收集 feedback，放弃返回。"""
        choice = await self._choice(
            question_id=_GATE_QUESTION_ID,
            header="确认研发任务",
            body=f"目标：{item.goal}\n请确认任务范围后继续。",
            options=(
                ("confirm", "确认目标与范围", "批准当前 task.md 并进入下一阶段"),
                ("revise", "提出修改", "提供反馈并重新生成任务草稿"),
                ("abandon", "放弃", "终结当前 Work Item（文档保留）"),
            ),
        )
        if choice == "confirm":
            await self._store.record_confirmation(
                RecordComposeConfirmation(
                    work_item_id=item.work_item_id,
                    confirmation_id=f"task-gate-{self._now_ms()}-{snapshot.digest[:16]}",
                    confirmation_kind="task",
                    document_digests=(snapshot.digest,),
                    confirmed_at_ms=self._now_ms(),
                )
            )
            await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
            return TaskGateResult(
                TaskGateOutcome.CONFIRMED, pending=None, revision=snapshot.revision
            )
        if choice == "abandon":
            await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
            return TaskGateResult(
                TaskGateOutcome.ABANDONED, pending=None, revision=snapshot.revision
            )
        return TaskGateResult(
            TaskGateOutcome.REVISE, pending=None, revision=snapshot.revision
        )

    async def _collect_feedback(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> str:
        """收集修改 feedback；空输入视为取消修订，重新进入 gate。"""
        raw = await self._answer(
            question_id=_FEEDBACK_QUESTION_ID,
            header="任务修改反馈",
            body="请说明需要修改的内容：",
            options=(),
        )
        feedback = str(raw or "").strip()
        if not feedback:
            raise TaskGateActivityError("COMPOSE_TASK_FEEDBACK_EMPTY")
        return feedback

    # ---------- 文档与引用 ----------

    async def _read_snapshot(
        self,
        item: ComposeWorkItem,
    ) -> ComposeDocumentSnapshot | None:
        try:
            return await self._documents.inspect(
                item.work_item_id,
                item.slug,
                ComposeDocumentKind.TASK,
            )
        except ComposeDocumentStoreError as exc:
            raise TaskGateActivityError("COMPOSE_TASK_DOCUMENT_INVALID") from exc

    async def _commit_draft(
        self,
        item: ComposeWorkItem,
        *,
        expected: ComposeDocumentSnapshot | None,
        revision: int,
        answers: tuple[tuple[str, str], ...],
        feedback: str,
    ) -> ComposeDocumentSnapshot:
        """把当前访谈进度原子写入 task.md（draft status）。"""
        content = _render_document(
            work_item_id=item.work_item_id,
            revision=revision,
            status=DRAFT_STATUS,
            updated_at_ms=self._now_ms(),
            body=_render_interview_draft(item.goal, answers, feedback),
        )
        return await self._commit(item, expected=expected, content=content)

    async def _commit(
        self,
        item: ComposeWorkItem,
        *,
        expected: ComposeDocumentSnapshot | None,
        content: str,
    ) -> ComposeDocumentSnapshot:
        try:
            return await self._documents.commit(
                DocumentCommit(
                    work_item_id=item.work_item_id,
                    slug=item.slug,
                    kind=ComposeDocumentKind.TASK,
                    content=content,
                    expected=expected,
                )
            )
        except ComposeDocumentStoreError as exc:
            raise TaskGateActivityError("COMPOSE_TASK_DRAFT_INVALID") from exc

    async def _sync_reference(
        self,
        item: ComposeWorkItem,
        snapshot: ComposeDocumentSnapshot,
    ) -> None:
        """把当前 task.md identity 同步到 SQLite，供 confirmation 与 readiness 使用。"""
        try:
            await self._store.upsert_document_reference(
                UpsertComposeDocumentReference(
                    work_item_id=item.work_item_id,
                    kind=ComposeDocumentKind.TASK,
                    relative_path=snapshot.relative_path,
                    content_digest=snapshot.digest,
                    revision=snapshot.revision,
                    updated_at_ms=self._now_ms(),
                )
            )
        except ComposeWorkItemStoreError as exc:
            raise TaskGateActivityError("COMPOSE_TASK_REFERENCE_FAILED") from exc

    # ---------- Interaction ----------

    async def _ask_question(
        self,
        item: ComposeWorkItem,
        question: str,
    ) -> str | None:
        """发出一个开放式访谈问题；过期或空答案返回 ``None`` 保持 pending。"""
        result = await self._interaction.request_decision(
            self._request(
                question_id=_INTERVIEW_QUESTION_ID,
                header="任务澄清",
                body=question,
                options=(),
            )
        )
        if result.expired:
            return None
        value = _answer_value(result, _INTERVIEW_QUESTION_ID)
        text = str(value).strip()
        return text if text else None

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
            raise TaskGateActivityError("COMPOSE_TASK_DECISION_UNAVAILABLE")
        value = _answer_value(result, question_id)
        if not value:
            raise TaskGateActivityError("COMPOSE_TASK_DECISION_UNAVAILABLE")
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


# ---------- 渲染与解析 ----------


def _render_document(
    *,
    work_item_id: str,
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
        "kind: task\n"
        f"revision: {revision}\n"
        f"status: {status}\n"
        f"updated_at: {updated_at_ms}\n"
        "---\n"
        f"{body_text}\n"
    )


def _render_interview_draft(
    goal: str,
    answers: tuple[tuple[str, str], ...],
    feedback: str,
) -> str:
    """渲染访谈进行中的草稿正文；正文与问答记录都只落在 task.md。"""
    parts = [f"# 目标\n\n{goal.strip()}"]
    if feedback:
        parts.append(f"## 修改反馈\n\n{feedback.strip()}")
    if answers:
        lines = ["## 访谈记录"]
        for question, answer in answers:
            lines.append(f"- 问: {question}")
            lines.append(f"- 答: {answer}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _parse_interview_answers(content: str) -> tuple[tuple[str, str], ...]:
    """从草稿正文还原已答问题；残缺问答对 fail closed 丢弃。"""
    marker = content.find(_INTERVIEW_MARKER)
    if marker < 0:
        return ()
    pairs: list[tuple[str, str]] = []
    pending_question: str | None = None
    for raw in content[marker:].splitlines():
        matched = _QA_LINE.match(raw)
        if matched is None:
            continue
        kind, text = matched.group(1), matched.group(2)
        if kind == "问":
            pending_question = text
        elif kind == "答" and pending_question is not None:
            pairs.append((pending_question, text))
            pending_question = None
            if len(pairs) >= MAX_INTERVIEW_ANSWERS:
                break
    return tuple(pairs)


def _answer_value(result: Any, question_id: str) -> str | None:
    """从 InteractionResult 的 answers 形状提取单选值。"""
    answers = result.value.get("answers", {}) if isinstance(result.value, Mapping) else {}
    raw = answers.get(question_id, []) if isinstance(answers, Mapping) else []
    items = raw if isinstance(raw, list) else []
    return str(items[0]) if items else None
