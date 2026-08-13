"""Spec gate：spec-driven-development 草稿生成与 typed confirmation。

Task 已确认后才可执行本 Activity；driver 只消费 confirmed Task 正文摘要、
相关源码 pointer 与用户 feedback，不继承 grill 全对话。生成 `spec.md`
（``status: proposed``）后进入 [确认行为与 interface | 提出修改 | 放弃]
typed gate；确认记录绑定当前 Task+Spec digest 组合，Task 或 Spec 任一修改
都会使旧确认 stale。模型输出不能推进确认，schema 无效 fail closed。
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

SPEC_ACTIVITY_KIND = "spec"
"""spec Activity 的稳定 ledger kind。"""

_GATE_QUESTION_ID = "spec-gate"
_FEEDBACK_QUESTION_ID = "spec-feedback"


class SpecGateActivityError(RuntimeError):
    """Spec gate 的稳定错误码；上层映射为可恢复投影或 blocked。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class SpecGateOutcome(str, Enum):
    """一次 Spec gate 执行的收敛结果。"""

    WAITING_GATE = "waiting_gate"
    CONFIRMED = "confirmed"
    ABANDONED = "abandoned"
    REVISE = "revise"


@dataclass(frozen=True, slots=True)
class SpecGateResult:
    """gate 收敛结果与展示用 pending decision。"""

    outcome: SpecGateOutcome
    pending: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class SpecDraftContext:
    """spec driver 一次草稿生成所需的受限上下文。"""

    goal: str
    task_digest: str
    task_body: str
    feedback: str = ""


class SpecDriver(Protocol):
    """spec 模型回合 seam；生产实现绑定原版 spec-driven-development
    与 codebase-design，并按显式任务指令覆盖通用输出路径。"""

    async def draft_spec(self, context: SpecDraftContext) -> str:
        """返回 spec.md 正文（不含 front matter）。"""


class SpecGateActivity:
    """spec.md 草稿 + typed 门禁；只依赖 Task 已确认的事实。"""

    def __init__(
        self,
        *,
        store: ComposeWorkItemStore,
        documents: ComposeDocumentStore,
        interaction: Any,
        driver: SpecDriver,
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
    ) -> SpecGateResult:
        """执行或恢复 Spec gate；所有退出路径收敛 Activity ledger。"""
        activity_id = f"spec:{item.work_item_id}"
        await self._ensure_activity_running(activity_id, item, run_id)
        try:
            return await self._run_draft_and_gate(item, activity_id)
        except asyncio.CancelledError:
            raise
        except SpecGateActivityError:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise
        except Exception as exc:
            await self._finish(activity_id, ComposeActivityStatus.RETRYABLE_FAILED)
            raise SpecGateActivityError("COMPOSE_SPEC_EXECUTION_FAILED") from exc

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
                        kind=SPEC_ACTIVITY_KIND,
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
        except SpecGateActivityError:
            raise
        except ComposeWorkItemStoreError as exc:
            raise SpecGateActivityError("COMPOSE_SPEC_LEDGER_FAILED") from exc

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
            raise SpecGateActivityError("COMPOSE_SPEC_LEDGER_FAILED") from exc

    async def _run_draft_and_gate(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> SpecGateResult:
        task = await self._read_snapshot(item, ComposeDocumentKind.TASK)
        if task is None:
            raise SpecGateActivityError("COMPOSE_SPEC_TASK_MISSING")
        snapshot = await self._read_snapshot(item, ComposeDocumentKind.SPEC)
        feedback = ""
        while True:
            if snapshot is None or feedback:
                snapshot = await self._propose_draft(item, task, snapshot, feedback)
            gate = await self._gate(item, activity_id, task, snapshot)
            if gate.outcome is SpecGateOutcome.REVISE:
                feedback = await self._collect_feedback(item, activity_id)
                snapshot = await self._read_snapshot(item, ComposeDocumentKind.SPEC)
                continue
            return gate

    async def _propose_draft(
        self,
        item: ComposeWorkItem,
        task: ComposeDocumentSnapshot,
        snapshot: ComposeDocumentSnapshot | None,
        feedback: str,
    ) -> ComposeDocumentSnapshot:
        """让 driver 生成 spec.md 正式正文并提交 proposed revision。"""
        context = SpecDraftContext(
            goal=item.goal,
            task_digest=task.digest,
            task_body=task.content,
            feedback=feedback,
        )
        try:
            body = await self._driver.draft_spec(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SpecGateActivityError("COMPOSE_SPEC_EXECUTION_FAILED") from exc
        revision = (snapshot.revision if snapshot is not None else 0) + 1
        content = _render_document(
            work_item_id=item.work_item_id,
            kind=ComposeDocumentKind.SPEC,
            revision=revision,
            status="proposed",
            updated_at_ms=self._now_ms(),
            body=body,
        )
        committed = await self._commit(item, ComposeDocumentKind.SPEC, snapshot, content)
        await self._sync_reference(item, ComposeDocumentKind.SPEC, committed)
        return committed

    async def _gate(
        self,
        item: ComposeWorkItem,
        activity_id: str,
        task: ComposeDocumentSnapshot,
        snapshot: ComposeDocumentSnapshot,
    ) -> SpecGateResult:
        """typed gate：确认绑定 Task+Spec digest，修改收集 feedback，放弃返回。"""
        choice = await self._choice(
            question_id=_GATE_QUESTION_ID,
            header="确认行为规格",
            body=f"目标：{item.goal}\n请确认 spec.md 的行为与 interface 后继续。",
            options=(
                ("confirm", "确认行为与 interface", "批准当前 spec.md 并进入下一阶段"),
                ("revise", "提出修改", "提供反馈并重新生成规格"),
                ("abandon", "放弃", "终结当前 Work Item（文档保留）"),
            ),
        )
        if choice == "confirm":
            await self._store.record_confirmation(
                RecordComposeConfirmation(
                    work_item_id=item.work_item_id,
                    confirmation_id=(
                        f"spec-gate-{self._now_ms()}-{snapshot.digest[:16]}"
                    ),
                    confirmation_kind="spec",
                    document_digests=(task.digest, snapshot.digest),
                    confirmed_at_ms=self._now_ms(),
                )
            )
            await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
            return SpecGateResult(
                SpecGateOutcome.CONFIRMED, pending=None, revision=snapshot.revision
            )
        if choice == "abandon":
            await self._finish(activity_id, ComposeActivityStatus.COMPLETED)
            return SpecGateResult(
                SpecGateOutcome.ABANDONED, pending=None, revision=snapshot.revision
            )
        return SpecGateResult(
            SpecGateOutcome.REVISE, pending=None, revision=snapshot.revision
        )

    async def _collect_feedback(
        self,
        item: ComposeWorkItem,
        activity_id: str,
    ) -> str:
        """收集修改 feedback；空输入视为取消修订，重新进入 gate。"""
        raw = await self._answer(
            question_id=_FEEDBACK_QUESTION_ID,
            header="规格修改反馈",
            body="请说明需要修改的内容：",
            options=(),
        )
        feedback = str(raw or "").strip()
        if not feedback:
            raise SpecGateActivityError("COMPOSE_SPEC_FEEDBACK_EMPTY")
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
            raise SpecGateActivityError("COMPOSE_SPEC_DOCUMENT_INVALID") from exc

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
            raise SpecGateActivityError("COMPOSE_SPEC_DRAFT_INVALID") from exc

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
            raise SpecGateActivityError("COMPOSE_SPEC_REFERENCE_FAILED") from exc

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
            raise SpecGateActivityError("COMPOSE_SPEC_DECISION_UNAVAILABLE")
        value = _answer_value(result, question_id)
        if not value:
            raise SpecGateActivityError("COMPOSE_SPEC_DECISION_UNAVAILABLE")
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
