"""ComposeWorkItemEngine：跨 Run 的 Work Item 生命周期与 Turn 路由。

Engine 是 deep module：对外只暴露 execute_turn / inspect / abandon，隐藏
Work Item 定位、文档 reconcile、readiness 计算、意图澄清和 terminal CAS。
Host/CLI 不直接调用 run_task_stage 之类的浅 interface。

本模块不依赖 host：交互、side answer、分类与时间都通过 ComposeTurnPorts
注入，便于 fake 覆盖；意图分类结果永远不能直接造成 abandon、覆盖或创建
Work Item 的副作用——创建必须经过无未终结项 + 目标非空的确定性检查，
abandon 必须经过调用方的 typed confirmation 与 revision CAS。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from harness_agent.compose.document_paths import make_compose_slug
from harness_agent.compose.document_store import (
    ComposeDocumentStore,
    ComposeDocumentStoreError,
)
from harness_agent.compose.models import (
    ComposeDocumentKind,
    ComposeEffectStatus,
    ComposeWorkItem,
    ComposeWorkItemStatus,
    ThreadMode,
)
from harness_agent.compose.readiness import (
    ComposeReadiness,
    ComposeReadinessResolver,
    CompletionReadinessFact,
    ConfirmationGroups,
    DocumentReadinessFact,
    ReportReadinessFact,
    ReviewFreshnessFact,
    WorkspaceFreshnessFact,
)
from harness_agent.compose.turn_intent import (
    TurnIntent,
    TurnIntentClassifierPort,
    TurnIntentContext,
    TurnIntentKind,
    TurnIntentResolver,
    TurnIntentSource,
)
from harness_agent.threads.compose_work_item_store import (
    BindRunToWorkItem,
    ComposeWorkItemStore,
    ComposeWorkItemStoreError,
    CreateComposeWorkItem,
    SetComposeWorkItemStatus,
    TerminalizeComposeWorkItem,
    UpsertComposeDocumentReference,
)
from harness_agent.compose.activities.task import (
    GrillDriver,
    TaskGateActivity,
    TaskGateActivityError,
    TaskGateOutcome,
)
from harness_agent.compose.activities.spec import (
    SpecDriver,
    SpecGateActivity,
    SpecGateActivityError,
    SpecGateOutcome,
)
from harness_agent.compose.activities.plan import (
    PlanDriver,
    PlanGateActivity,
    PlanGateActivityError,
    PlanGateOutcome,
)

from harness_agent.compose.activities.implement import (
    ImplementActivity,
    ImplementActivityError,
    ImplementDriver,
    ImplementItemOutcome,
)
from harness_agent.compose.activities.verify import (
    VerificationPort,
    VerifyActivity,
    VerifyActivityError,
    VerifyOutcome,
)
from harness_agent.compose.activities.review import (
    ReviewActivity,
    ReviewActivityError,
    ReviewDriver,
    ReviewOutcome,
)
from harness_agent.compose.guard import (
    CompletionGuard,
    CompletionGuardError,
    ReportDriver,
)

MAX_PIPELINE_STEPS = 8
"""批准实施后单个 Turn 内自动闭环的 Activity 步数上限。"""

MAX_SLUG_ALLOCATION_ATTEMPTS = 8
"""并发创建持续占用 slug 时的单 Turn 重试上限。"""

MAX_GOAL_CHARS = 400
"""创建 Work Item 时保存的目标文本上限；超出截断，正文仍以 Transcript 为准。"""

MAX_TITLE_CHARS = 80
"""projection 标题展示上限；超长目标只显示摘要前缀。"""

CONFIRMATION_KINDS = ("task", "spec", "plan")
"""readiness 使用的三个 typed confirmation gate 名称。"""

_DOCUMENT_ORDER = (
    ComposeDocumentKind.TASK,
    ComposeDocumentKind.SPEC,
    ComposeDocumentKind.PLAN,
    ComposeDocumentKind.TODO,
    ComposeDocumentKind.REPORT,
)


class ComposeWorkItemEngineError(RuntimeError):
    """Engine 层的稳定错误码；host adapter 负责映射为 RunError。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存可分支的错误码与可选的简短诊断。"""
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class ComposeTurnOutcome(str, Enum):
    """一次 Turn 的收敛结果；Run 终态仍由 RunCoordinator 唯一决定。"""

    WAITING_USER = "waiting_user"
    RETRYABLE_FAILED = "retryable_failed"
    BLOCKED = "blocked"
    TURN_BUDGET = "turn_budget"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ComposeTurnRequest:
    """一次用户 Turn 的受理输入；run_id 是本次执行的稳定身份。"""

    thread_id: str
    run_id: str
    message: str
    explicit_intent: TurnIntent | None = None
    cancelled: bool = False
    amends_work_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class TypedDecisionOption:
    """typed decision 的一个固定选项。"""

    value: str
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class TypedDecisionRequest:
    """一次 typed decision 的完整请求；形状与 host request_question 对齐。"""

    request_id: str
    interrupt_id: str
    question_id: str
    header: str
    body: str
    options: tuple[TypedDecisionOption, ...]
    allow_other: bool = True


@dataclass(frozen=True, slots=True)
class TypedDecisionResult:
    """typed decision 的语言无关结果；expired 表示无法到达活跃客户端。"""

    value: Mapping[str, object]
    expired: bool = False


class ComposeInteractionPort(Protocol):
    """把 typed decision 交给用户并返回选择；不暴露 host Run 细节。"""

    async def request_decision(self, request: TypedDecisionRequest) -> TypedDecisionResult: ...


class SideAnswerPort(Protocol):
    """/btw 式只读临时回答；实现方必须保证不写 Work Item、ledger 或主上下文。"""

    async def answer(self, *, thread_id: str, question: str) -> str: ...


@dataclass(slots=True)
class ComposeTurnPorts:
    """Engine 的全部外部依赖；测试使用 fake 注入，生产由 host 组装。"""

    store: ComposeWorkItemStore
    documents: ComposeDocumentStore
    classifier: TurnIntentClassifierPort
    interaction: ComposeInteractionPort
    side_answer: SideAnswerPort | None = None
    workspace_revision: Callable[[], Awaitable[str | None]] | None = None
    now_ms: Callable[[], int] | None = None
    readiness: ComposeReadinessResolver = field(default_factory=ComposeReadinessResolver)
    task_driver: GrillDriver | None = None
    spec_driver: SpecDriver | None = None
    plan_driver: PlanDriver | None = None
    implement_driver: ImplementDriver | None = None
    verify_port: VerificationPort | None = None
    review_driver: ReviewDriver | None = None
    report_driver: ReportDriver | None = None


@dataclass(frozen=True, slots=True)
class ReadinessProjection:
    """readiness 九个 predicate 的 wire 安全投影。"""

    task_confirmed: bool
    spec_confirmed: bool
    plan_confirmed: bool
    todo_executable: bool
    implementation_current: bool
    verification_fresh: bool
    review_fresh: bool
    report_current: bool
    complete: bool

    @classmethod
    def from_readiness(cls, readiness: ComposeReadiness) -> "ReadinessProjection":
        return cls(
            task_confirmed=readiness.task_confirmed,
            spec_confirmed=readiness.spec_confirmed,
            plan_confirmed=readiness.plan_confirmed,
            todo_executable=readiness.todo_executable,
            implementation_current=readiness.implementation_current,
            verification_fresh=readiness.verification_fresh,
            review_fresh=readiness.review_fresh,
            report_current=readiness.report_current,
            complete=readiness.complete,
        )


@dataclass(frozen=True, slots=True)
class DocumentStatusProjection:
    """一份文档的展示状态；不暴露绝对路径或正文。"""

    kind: str
    present: bool
    current_digest: str
    confirmed: bool


@dataclass(frozen=True, slots=True)
class ComposeWorkItemProjection:
    """Work Item 的非敏感完整投影；UI 只消费此形状。"""

    thread_id: str
    work_item_id: str
    slug: str
    title: str
    revision: int
    status: str
    current_activity: str
    pending_decision: str | None
    blocked_reason: str | None
    readiness: ReadinessProjection
    documents: tuple[DocumentStatusProjection, ...]


@dataclass(frozen=True, slots=True)
class ComposeTurnResult:
    """一次 execute_turn 的收敛结果；不携带 Prompt、Skill 或原始 Tool 输出。"""

    work_item: ComposeWorkItemProjection | None
    status: ComposeTurnOutcome
    pending_decision: str | None
    side_answer: str | None = None
    intent: TurnIntent | None = None


class ComposeWorkItemEngine:
    """跨 Run 持续的 Work Item 生命周期 owner；不拥有 SQLite 连接或 graph。"""

    def __init__(self, ports: ComposeTurnPorts) -> None:
        self._ports = ports
        self._resolver = TurnIntentResolver(ports.classifier)

    async def execute_turn(self, request: ComposeTurnRequest) -> ComposeTurnResult:
        """受理一次用户 Turn：路由意图、定位/创建 Work Item、投影 readiness。"""
        thread_mode = await self._ports.store.load_thread_mode(request.thread_id)
        if thread_mode is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_THREAD_MODE_MISSING",
                "Thread 尚未受理有效 Run，无法进入 Compose 流程",
            )
        if thread_mode is not ThreadMode.COMPOSE:
            raise ComposeWorkItemEngineError(
                "THREAD_MODE_LOCKED",
                "Thread 已锁定为 Build 模式，Compose 不能受理",
            )
        now = self._now_ms()
        if request.cancelled:
            # 取消只中断当前执行，Work Item 生命周期保持原样。
            active = await self._ports.store.load_active(request.thread_id)
            projection = (
                await self._projection_for(active) if active is not None else None
            )
            return ComposeTurnResult(
                projection,
                ComposeTurnOutcome.WAITING_USER,
                None,
            )
        active = await self._ports.store.load_active(request.thread_id)
        intent = request.explicit_intent
        if intent is None:
            intent = await self._resolver.resolve(self._intent_context(request, active))
        return await self._route(request, intent, active, now)

    async def inspect(
        self,
        *,
        thread_id: str,
        work_item_id: str | None = None,
    ) -> ComposeWorkItemProjection | None:
        """只读投影当前 Work Item；不写文档引用、不创建 Run 绑定。"""
        if work_item_id is not None:
            item = await self._ports.store.load(work_item_id)
        else:
            item = await self._ports.store.load_active(thread_id)
        if item is None or item.thread_id != thread_id:
            return None
        readiness, facts = await self._compute_readiness(item)
        return self._projection(item, readiness, pending_decision=None, facts=facts)

    async def abandon(
        self,
        *,
        thread_id: str,
        work_item_id: str,
        expected_revision: int,
        reason: str | None,
    ) -> ComposeWorkItemProjection:
        """以 revision CAS 终结 Work Item；调用方必须先完成 typed confirmation。

        已写文档、代码、测试与 Artifact 全部保留，不自动回滚、不删除目录。
        reason 只用于确认展示，不写入 ledger。
        """
        item = await self._ports.store.load(work_item_id)
        if item is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_WORK_ITEM_NOT_FOUND",
                "Work Item 不存在或已不在当前 project",
            )
        if item.thread_id != thread_id:
            raise ComposeWorkItemEngineError(
                "COMPOSE_WORK_ITEM_THREAD_MISMATCH",
                "Work Item 不属于该 Thread",
            )
        terminalized = await self._ports.store.terminalize(
            TerminalizeComposeWorkItem(
                work_item_id=work_item_id,
                expected_revision=expected_revision,
                status=ComposeWorkItemStatus.ABANDONED,
                terminal_at_ms=self._now_ms(),
            )
        )
        readiness, facts = await self._compute_readiness(terminalized)
        return self._projection(terminalized, readiness, pending_decision=None, facts=facts)

    # ---------- 内部路由 ----------

    async def _route(
        self,
        request: ComposeTurnRequest,
        intent: TurnIntent,
        active: ComposeWorkItem | None,
        now: int,
    ) -> ComposeTurnResult:
        kind = intent.kind
        if kind is TurnIntentKind.SIDE_QUESTION:
            return await self._side_question(request, intent, active)
        if kind is TurnIntentKind.START_NEW_WORK:
            if active is not None:
                return await self._clarify_new_work(request, active, now)
            return await self._create_work_item(request, intent, now)
        if kind in (TurnIntentKind.RESUME_CURRENT, TurnIntentKind.AMEND_CURRENT):
            if active is None:
                return await self._clarify_no_active(request, now)
            return await self._attach(request, intent, active, now)
        return await self._clarify_unclear(request, active, now)

    async def _create_work_item(
        self,
        request: ComposeTurnRequest,
        intent: TurnIntent,
        now: int,
    ) -> ComposeTurnResult:
        goal = _bounded_goal(request.message)
        if not goal:
            # /new-work 空目标：进入等待新目标状态，不创建空 Work Item。
            active = await self._ports.store.load_active(request.thread_id)
            projection = (
                await self._projection_for(active) if active is not None else None
            )
            return ComposeTurnResult(
                projection,
                ComposeTurnOutcome.WAITING_USER,
                None,
                intent=intent,
            )
        work_item_id = f"wi-{uuid.uuid4().hex}"
        for _ in range(MAX_SLUG_ALLOCATION_ATTEMPTS):
            slug = await self._next_slug(goal)
            try:
                item = await self._ports.store.create(
                    CreateComposeWorkItem(
                        thread_id=request.thread_id,
                        work_item_id=work_item_id,
                        slug=slug,
                        goal=goal,
                        created_at_ms=now,
                        amends_work_item_id=request.amends_work_item_id,
                    )
                )
                break
            except ComposeWorkItemStoreError as exc:
                # 不同 Thread 可并发计算出同一 candidate；store 在写事务内
                # 拒绝碰撞，重新读取 project slug 后即可分配下一个目录。
                if exc.code != "COMPOSE_WORK_ITEM_SLUG_CONFLICT":
                    raise
        else:
            raise ComposeWorkItemEngineError(
                "COMPOSE_WORK_ITEM_SLUG_ALLOCATION_FAILED",
                "并发创建持续占用候选文档目录，请重试",
            )
        await self._ports.store.bind_run(
            BindRunToWorkItem(
                thread_id=request.thread_id,
                run_id=request.run_id,
                work_item_id=work_item_id,
                created_at_ms=now,
            )
        )
        readiness, facts = await self._compute_readiness(item)
        if self._ports.task_driver is not None:
            return await self._run_pipeline(request, item, now, readiness, facts)
        return ComposeTurnResult(
            self._projection(item, readiness, pending_decision=None, facts=facts),
            ComposeTurnOutcome.WAITING_USER,
            None,
            intent=intent,
        )

    async def _attach(
        self,
        request: ComposeTurnRequest,
        intent: TurnIntent,
        active: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult:
        """继续/修订当前 Work Item：固定 Run 绑定并重算 readiness。"""
        await self._ports.store.bind_run(
            BindRunToWorkItem(
                thread_id=request.thread_id,
                run_id=request.run_id,
                work_item_id=active.work_item_id,
                created_at_ms=now,
            )
        )
        await self._reconcile_documents(active)
        readiness, facts = await self._compute_readiness(active)
        if intent.kind is TurnIntentKind.AMEND_CURRENT:
            pending = f"amend:{readiness.next_action}"
            outcome = (
                ComposeTurnOutcome.BLOCKED
                if active.status is ComposeWorkItemStatus.BLOCKED
                else ComposeTurnOutcome.WAITING_USER
            )
            return ComposeTurnResult(
                self._projection(active, readiness, pending_decision=pending, facts=facts),
                outcome,
                pending,
                intent=intent,
            )
        if self._ports.task_driver is not None:
            return await self._run_pipeline(request, active, now, readiness, facts)
        outcome = (
            ComposeTurnOutcome.BLOCKED
            if active.status is ComposeWorkItemStatus.BLOCKED
            else ComposeTurnOutcome.WAITING_USER
        )
        return ComposeTurnResult(
            self._projection(active, readiness, pending_decision=None, facts=facts),
            outcome,
            None,
            intent=intent,
        )

    # ---------- Activity 流水线 ----------

    async def _run_pipeline(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
        readiness: ComposeReadiness,
        facts: dict[ComposeDocumentKind, DocumentReadinessFact],
    ) -> ComposeTurnResult:
        """按 readiness gate 顺序执行有界 Activity；批准实施后自动闭环。"""
        if not readiness.task_confirmed:
            return await self._run_task_gate(request, item, now)
        if not readiness.spec_confirmed:
            return await self._run_spec_gate(request, item, now)
        if not readiness.plan_confirmed:
            return await self._run_plan_gate(request, item, now)
        current = item
        steps = 0
        while steps < MAX_PIPELINE_STEPS:
            readiness, facts = await self._compute_readiness(current)
            if not readiness.implementation_current:
                outcome = await self._run_implement(request, current, now)
            elif not readiness.verification_fresh:
                outcome = await self._run_verify(request, current, now)
            elif not readiness.review_fresh:
                outcome = await self._run_review(request, current, now)
            elif not readiness.report_current:
                outcome = await self._run_report(request, current, now)
            elif readiness.complete and not current.terminal:
                return await self._complete(current, now)
            else:
                break
            if outcome is not None:
                return outcome
            current = await self._ports.store.load(current.work_item_id)
            if current is None:
                raise ComposeWorkItemEngineError(
                    "COMPOSE_WORK_ITEM_NOT_FOUND",
                    "Work Item 在 Activity 执行后丢失",
                )
            steps += 1
        readiness, facts = await self._compute_readiness(current)
        outcome = (
            ComposeTurnOutcome.BLOCKED
            if current.status is ComposeWorkItemStatus.BLOCKED
            else ComposeTurnOutcome.WAITING_USER
        )
        return ComposeTurnResult(
            self._projection(current, readiness, pending_decision=None, facts=facts),
            outcome,
            None,
            intent=_explicit_intent(TurnIntentKind.RESUME_CURRENT),
        )

    async def _run_task_gate(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult:
        """执行 grill 访谈与 Task typed gate；错误保持可恢复投影。"""
        return await self._run_gate(
            request,
            item,
            now,
            driver=self._ports.task_driver,
            error_prefix="task",
            activity_class=TaskGateActivity,
            abandoned_outcome=TaskGateOutcome.ABANDONED,
        )

    async def _run_spec_gate(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult:
        """执行 spec.md 草稿与 typed gate；错误保持可恢复投影。"""
        return await self._run_gate(
            request,
            item,
            now,
            driver=self._ports.spec_driver,
            error_prefix="spec",
            activity_class=SpecGateActivity,
            abandoned_outcome=SpecGateOutcome.ABANDONED,
        )

    async def _run_plan_gate(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult:
        """执行 plan.md/todo.md 成对草稿与联合 typed gate。"""
        return await self._run_gate(
            request,
            item,
            now,
            driver=self._ports.plan_driver,
            error_prefix="plan",
            activity_class=PlanGateActivity,
            abandoned_outcome=PlanGateOutcome.ABANDONED,
        )

    async def _run_implement(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult:
        """执行 TDD Implement Activity；错误与 blocked 保持可恢复投影。"""
        driver = self._ports.implement_driver
        if driver is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_ACTIVITY_UNAVAILABLE",
                "implement 驱动未接入",
            )
        if self._ports.workspace_revision is None:
            readiness, facts = await self._compute_readiness(item)
            pending = "implement:COMPOSE_IMPLEMENT_WORKSPACE_REVISION_MISSING"
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.WAITING_USER,
                pending,
                intent=_explicit_intent(TurnIntentKind.RESUME_CURRENT),
            )
        activity = ImplementActivity(
            store=self._ports.store,
            documents=self._ports.documents,
            driver=driver,
            workspace_revision=self._ports.workspace_revision,
            now_ms=self._ports.now_ms,
        )
        resume_intent = _explicit_intent(TurnIntentKind.RESUME_CURRENT)
        try:
            result = await activity.run(item, run_id=request.run_id)
        except Exception as exc:
            code = getattr(exc, "code", None)
            pending = f"implement:{code}" if code else "implement:failed"
            readiness, facts = await self._compute_readiness(item)
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.RETRYABLE_FAILED,
                pending,
                intent=resume_intent,
            )
        if result.outcome is ImplementItemOutcome.BLOCKED:
            blocked = await self._ports.store.set_status(
                SetComposeWorkItemStatus(
                    work_item_id=item.work_item_id,
                    expected_revision=item.revision,
                    status=ComposeWorkItemStatus.BLOCKED,
                    updated_at_ms=now,
                )
            )
            readiness, facts = await self._compute_readiness(blocked)
            return ComposeTurnResult(
                self._projection(
                    blocked, readiness, pending_decision=result.pending, facts=facts
                ),
                ComposeTurnOutcome.BLOCKED,
                result.pending,
                intent=resume_intent,
            )
        updated = await self._ports.store.load(item.work_item_id)
        if updated is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_WORK_ITEM_NOT_FOUND",
                "Work Item 在 Activity 执行后丢失",
            )
        if result.pending is None:
            # 全部完成且无 pending：流水线自动进入下一 Activity。
            return None
        readiness, facts = await self._compute_readiness(updated)
        outcome = (
            ComposeTurnOutcome.BLOCKED
            if updated.status is ComposeWorkItemStatus.BLOCKED
            else ComposeTurnOutcome.WAITING_USER
        )
        return ComposeTurnResult(
            self._projection(
                updated, readiness, pending_decision=result.pending, facts=facts
            ),
            outcome,
            result.pending,
            intent=resume_intent,
        )

    async def _run_verify(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult | None:
        """执行 Verify Activity；失败修复 Todo 后回到 Implement 自动闭环。"""
        port = self._ports.verify_port
        if port is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_ACTIVITY_UNAVAILABLE",
                "verify 端口未接入",
            )
        activity = VerifyActivity(
            store=self._ports.store,
            documents=self._ports.documents,
            port=port,
            workspace_revision=self._ports.workspace_revision,
            now_ms=self._ports.now_ms,
        )
        resume_intent = _explicit_intent(TurnIntentKind.RESUME_CURRENT)
        try:
            result = await activity.run(item, run_id=request.run_id)
        except Exception as exc:
            code = getattr(exc, "code", None)
            pending = f"verify:{code}" if code else "verify:failed"
            readiness, facts = await self._compute_readiness(item)
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.RETRYABLE_FAILED,
                pending,
                intent=resume_intent,
            )
        if result.outcome is VerifyOutcome.FAILED:
            updated = await self._ports.store.load(item.work_item_id)
            if updated is None:
                raise ComposeWorkItemEngineError(
                    "COMPOSE_WORK_ITEM_NOT_FOUND",
                    "Work Item 在 Activity 执行后丢失",
                )
            readiness, facts = await self._compute_readiness(updated)
            return ComposeTurnResult(
                self._projection(
                    updated, readiness, pending_decision=result.pending, facts=facts
                ),
                ComposeTurnOutcome.WAITING_USER,
                result.pending,
                intent=resume_intent,
            )
        return None

    async def _run_review(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult | None:
        """执行双轴只读 Review；Required finding 回到 Implement 自动闭环。"""
        driver = self._ports.review_driver
        if driver is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_ACTIVITY_UNAVAILABLE",
                "review 驱动未接入",
            )
        activity = ReviewActivity(
            store=self._ports.store,
            documents=self._ports.documents,
            driver=driver,
            workspace_revision=self._ports.workspace_revision,
            now_ms=self._ports.now_ms,
        )
        resume_intent = _explicit_intent(TurnIntentKind.RESUME_CURRENT)
        try:
            result = await activity.run(item, run_id=request.run_id)
        except Exception as exc:
            code = getattr(exc, "code", None)
            pending = f"review:{code}" if code else "review:failed"
            readiness, facts = await self._compute_readiness(item)
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.RETRYABLE_FAILED,
                pending,
                intent=resume_intent,
            )
        if result.outcome is ReviewOutcome.FINDINGS:
            updated = await self._ports.store.load(item.work_item_id)
            if updated is None:
                raise ComposeWorkItemEngineError(
                    "COMPOSE_WORK_ITEM_NOT_FOUND",
                    "Work Item 在 Activity 执行后丢失",
                )
            readiness, facts = await self._compute_readiness(updated)
            return ComposeTurnResult(
                self._projection(
                    updated, readiness, pending_decision=result.pending, facts=facts
                ),
                ComposeTurnOutcome.WAITING_USER,
                result.pending,
                intent=resume_intent,
            )
        return None

    async def _run_report(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult | None:
        """生成 report.md 与引用证据；完成后 Guard Kernel 走 complete CAS。"""
        driver = self._ports.report_driver
        if driver is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_ACTIVITY_UNAVAILABLE",
                "report 驱动未接入",
            )
        guard = CompletionGuard(
            store=self._ports.store,
            documents=self._ports.documents,
            driver=driver,
            workspace_revision=self._ports.workspace_revision,
            now_ms=self._ports.now_ms,
        )
        resume_intent = _explicit_intent(TurnIntentKind.RESUME_CURRENT)
        digests = await self._four_document_digests(item)
        verification_digest = await self._latest_evidence_digest(item, "verification")
        review_payload = await self._latest_review_payload(item)
        if verification_digest is None or review_payload is None:
            readiness, facts = await self._compute_readiness(item)
            pending = "report:COMPOSE_REPORT_INPUTS_MISSING"
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.WAITING_USER,
                pending,
                intent=resume_intent,
            )
        try:
            await guard.write_report(
                item,
                task_digest=digests[0],
                spec_digest=digests[1],
                plan_digest=digests[2],
                todo_digest=digests[3],
                verification_digest=verification_digest,
                requirement_review_digest=review_payload[0],
                code_review_digest=review_payload[1],
            )
        except CompletionGuardError as exc:
            readiness, facts = await self._compute_readiness(item)
            pending = f"report:{exc.code}"
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.RETRYABLE_FAILED,
                pending,
                intent=resume_intent,
            )
        return None

    async def _complete(
        self,
        item: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult:
        """Guard Kernel 终态：revision CAS 提交 completed。"""
        guard = CompletionGuard(
            store=self._ports.store,
            documents=self._ports.documents,
            driver=self._ports.report_driver,
            workspace_revision=self._ports.workspace_revision,
            now_ms=self._ports.now_ms,
        )
        try:
            completed = await guard.complete(item, now_ms=now)
        except CompletionGuardError as exc:
            readiness, facts = await self._compute_readiness(item)
            pending = f"complete:{exc.code}"
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.RETRYABLE_FAILED,
                pending,
                intent=_explicit_intent(TurnIntentKind.RESUME_CURRENT),
            )
        readiness, facts = await self._compute_readiness(completed)
        return ComposeTurnResult(
            self._projection(completed, readiness, pending_decision=None, facts=facts),
            ComposeTurnOutcome.COMPLETED,
            None,
            intent=_explicit_intent(TurnIntentKind.RESUME_CURRENT),
        )

    async def _four_document_digests(self, item: ComposeWorkItem) -> tuple[str, ...]:
        """读取 Task/Spec/Plan/Todo 四份当前文档 digest。"""
        digests: list[str] = []
        for kind in (
            ComposeDocumentKind.TASK,
            ComposeDocumentKind.SPEC,
            ComposeDocumentKind.PLAN,
            ComposeDocumentKind.TODO,
        ):
            snapshot = await self._inspect_optional(item, kind)
            if snapshot is None:
                raise ComposeWorkItemEngineError(
                    "COMPOSE_DOCUMENT_MISSING",
                    "完成流程缺少必要文档",
                )
            digests.append(snapshot.digest)
        return tuple(digests)

    async def _latest_evidence_digest(self, item: ComposeWorkItem, kind: str) -> str | None:
        """读取某类证据的最新 content digest。"""
        try:
            records = await self._ports.store.load_evidence(item.work_item_id, kind)
        except ComposeWorkItemStoreError:
            return None
        return records[-1].content_digest if records else None

    async def _latest_review_payload(self, item: ComposeWorkItem):
        """读取最新 review 证据的双轴 digest 引用。"""
        try:
            records = await self._ports.store.load_evidence(
                item.work_item_id, "review"
            )
        except ComposeWorkItemStoreError:
            return None
        if not records:
            return None
        return (
            str(records[-1].content_digest),
            str(records[-1].content_digest),
        )

    async def _run_gate(
        self,
        request: ComposeTurnRequest,
        item: ComposeWorkItem,
        now: int,
        *,
        driver: Any,
        error_prefix: str,
        activity_class: Any,
        abandoned_outcome: Any,
    ) -> ComposeTurnResult:
        """通用门禁执行：Activity ledger 与 abandoned CAS 语义统一。"""
        if driver is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_ACTIVITY_UNAVAILABLE",
                f"{error_prefix} gate 驱动未接入",
            )
        gate = activity_class(
            store=self._ports.store,
            documents=self._ports.documents,
            interaction=self._ports.interaction,
            driver=driver,
            now_ms=self._ports.now_ms,
        )
        resume_intent = _explicit_intent(TurnIntentKind.RESUME_CURRENT)
        try:
            result = await gate.run(item, run_id=request.run_id)
        except Exception as exc:
            code = getattr(exc, "code", None)
            # 生产诊断：gate 失败只收敛为 pending，根因必须可见（限流/解析/网络）。
            logging.getLogger(__name__).warning(
                "Compose gate %s failed for %s: %r (cause: %r)",
                error_prefix,
                item.work_item_id,
                exc,
                exc.__cause__,
            )
            pending = f"{error_prefix}:{code}" if code else f"{error_prefix}:failed"
            readiness, facts = await self._compute_readiness(item)
            return ComposeTurnResult(
                self._projection(item, readiness, pending_decision=pending, facts=facts),
                ComposeTurnOutcome.RETRYABLE_FAILED,
                pending,
                intent=resume_intent,
            )
        if result.outcome is abandoned_outcome:
            terminalized = await self._ports.store.terminalize(
                TerminalizeComposeWorkItem(
                    work_item_id=item.work_item_id,
                    expected_revision=item.revision,
                    status=ComposeWorkItemStatus.ABANDONED,
                    terminal_at_ms=now,
                )
            )
            readiness, facts = await self._compute_readiness(terminalized)
            return ComposeTurnResult(
                self._projection(
                    terminalized, readiness, pending_decision=None, facts=facts
                ),
                ComposeTurnOutcome.WAITING_USER,
                None,
                intent=resume_intent,
            )
        updated = await self._ports.store.load(item.work_item_id)
        if updated is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_WORK_ITEM_NOT_FOUND",
                "Work Item 在 Activity 执行后丢失",
            )
        readiness, facts = await self._compute_readiness(updated)
        outcome = (
            ComposeTurnOutcome.BLOCKED
            if updated.status is ComposeWorkItemStatus.BLOCKED
            else ComposeTurnOutcome.WAITING_USER
        )
        return ComposeTurnResult(
            self._projection(
                updated, readiness, pending_decision=result.pending, facts=facts
            ),
            outcome,
            result.pending,
            intent=resume_intent,
        )

    async def _side_question(
        self,
        request: ComposeTurnRequest,
        intent: TurnIntent,
        active: ComposeWorkItem | None,
    ) -> ComposeTurnResult:
        """临时只读回答：不写 Work Item、不改变 readiness、不进后续 ContextPack。"""
        if self._ports.side_answer is None:
            raise ComposeWorkItemEngineError(
                "COMPOSE_SIDE_ANSWER_UNAVAILABLE",
                "临时问答能力未接入",
            )
        question = intent.detail or request.message
        answer = await self._ports.side_answer.answer(
            thread_id=request.thread_id,
            question=question,
        )
        projection = (
            await self._projection_for(active) if active is not None else None
        )
        return ComposeTurnResult(
            projection,
            ComposeTurnOutcome.WAITING_USER,
            None,
            side_answer=answer,
            intent=intent,
        )

    async def _clarify_unclear(
        self,
        request: ComposeTurnRequest,
        active: ComposeWorkItem | None,
        now: int,
    ) -> ComposeTurnResult:
        """歧义/分类失败：让用户在四个语义中选择，不猜测执行。"""
        body = "无法确定你的意图，请选择："
        if active is not None:
            body = f"当前目标：{_bounded_title(active.goal)}\n{body}"
        decision = await self._typed_decision(
            request,
            question_id="intent-clarify",
            header="请确认你的意图",
            body=body,
            options=(
                TypedDecisionOption(
                    "amend_current",
                    "修改当前任务",
                    "把本条消息作为对当前目标的修订输入",
                ),
                TypedDecisionOption(
                    "start_new_work",
                    "开始新任务",
                    "结束当前目标并创建一个新的研发目标"
                    if active is not None
                    else "创建一个新的研发目标",
                ),
                TypedDecisionOption(
                    "side_question",
                    "临时提问",
                    "只读回答，不进入任务记录与后续上下文",
                ),
                TypedDecisionOption("cancel", "取消", "不改变当前状态"),
            ),
        )
        return await self._after_choice(request, decision, active, now)

    async def _clarify_new_work(
        self,
        request: ComposeTurnRequest,
        active: ComposeWorkItem,
        now: int,
    ) -> ComposeTurnResult:
        """已有未终结项时新目标必须澄清：继续 / 放弃后新建 / 取消。"""
        decision = await self._typed_decision(
            request,
            question_id="new-work-clarify",
            header="已有进行中的研发目标",
            body=(
                f"当前目标：{_bounded_title(active.goal)}。"
                "是否放弃当前目标并开始新任务？"
            ),
            options=(
                TypedDecisionOption(
                    "resume_current",
                    "继续当前目标",
                    "回到当前 Work Item 的下一步",
                ),
                TypedDecisionOption(
                    "abandon_then_new",
                    "放弃当前目标并新建",
                    "终结当前项（文档保留）并创建新目标",
                ),
                TypedDecisionOption("cancel", "取消", "不改变当前状态"),
            ),
        )
        value = self._choice_value(decision, "new-work-clarify")
        if value == "resume_current":
            return await self._attach(
                request,
                _explicit_intent(TurnIntentKind.RESUME_CURRENT),
                active,
                now,
            )
        if value == "abandon_then_new":
            await self._ports.store.terminalize(
                TerminalizeComposeWorkItem(
                    work_item_id=active.work_item_id,
                    expected_revision=active.revision,
                    status=ComposeWorkItemStatus.ABANDONED,
                    terminal_at_ms=now,
                )
            )
            return await self._create_work_item(
                request,
                _explicit_intent(TurnIntentKind.START_NEW_WORK),
                now,
            )
        return await self._noop_result(request, active)

    async def _clarify_no_active(
        self,
        request: ComposeTurnRequest,
        now: int,
    ) -> ComposeTurnResult:
        """没有未终结项时“继续/修订”无对象，必须让用户选择下一步。"""
        decision = await self._typed_decision(
            request,
            question_id="no-active-clarify",
            header="当前没有进行中的研发目标",
            body="没有可继续的 Work Item，请选择下一步：",
            options=(
                TypedDecisionOption(
                    "start_new_work",
                    "开始新任务",
                    "创建一个新的研发目标",
                ),
                TypedDecisionOption(
                    "side_question",
                    "临时提问",
                    "只读回答，不进入任务记录",
                ),
                TypedDecisionOption("cancel", "取消", "不改变当前状态"),
            ),
        )
        value = self._choice_value(decision, "no-active-clarify")
        if value == "start_new_work":
            return await self._create_work_item(
                request,
                _explicit_intent(TurnIntentKind.START_NEW_WORK),
                now,
            )
        if value == "side_question":
            return await self._side_question(
                request,
                _explicit_intent(TurnIntentKind.SIDE_QUESTION, request.message),
                None,
            )
        return await self._noop_result(request, None)

    async def _after_choice(
        self,
        request: ComposeTurnRequest,
        decision: TypedDecisionResult,
        active: ComposeWorkItem | None,
        now: int,
    ) -> ComposeTurnResult:
        """把 unclear 澄清选择映射回受限意图并重新路由；未知值保持现状。"""
        value = self._choice_value(decision, "intent-clarify")
        if value == "cancel":
            return await self._noop_result(request, active)
        explicit = _explicit_intent_from_choice(value, detail=request.message)
        if explicit is None:
            # allow_other 自定义输入无法映射到固定语义，不猜测执行。
            return await self._noop_result(request, active)
        return await self._route(request, explicit, active, now)

    async def _noop_result(
        self,
        request: ComposeTurnRequest,
        active: ComposeWorkItem | None,
    ) -> ComposeTurnResult:
        projection = (
            await self._projection_for(active) if active is not None else None
        )
        return ComposeTurnResult(
            projection,
            ComposeTurnOutcome.WAITING_USER,
            None,
        )

    # ---------- 文档与 readiness ----------

    async def _reconcile_documents(self, item: ComposeWorkItem) -> None:
        """把工作空间当前 Markdown identity 同步到 SQLite 文档引用。"""
        refs = {
            ref.kind: ref
            for ref in await self._ports.store.load_document_references(
                item.work_item_id
            )
        }
        for kind in ComposeDocumentKind:
            snapshot = await self._inspect_optional(item, kind)
            if snapshot is None:
                continue
            ref = refs.get(kind)
            if (
                ref is None
                or ref.current_digest != snapshot.digest
                or ref.relative_path != snapshot.relative_path
            ):
                await self._ports.store.upsert_document_reference(
                    UpsertComposeDocumentReference(
                        work_item_id=item.work_item_id,
                        kind=kind,
                        relative_path=snapshot.relative_path,
                        content_digest=snapshot.digest,
                        revision=snapshot.revision,
                        updated_at_ms=self._now_ms(),
                    )
                )

    async def _compute_readiness(
        self,
        item: ComposeWorkItem,
    ) -> tuple[ComposeReadiness, dict[ComposeDocumentKind, DocumentReadinessFact]]:
        """只读计算九项 readiness；缺失/畸形文档一律按未满足处理。"""
        refs = {
            ref.kind: ref
            for ref in await self._ports.store.load_document_references(
                item.work_item_id
            )
        }
        facts: dict[ComposeDocumentKind, DocumentReadinessFact] = {}
        for kind in ComposeDocumentKind:
            snapshot = await self._inspect_optional(item, kind)
            ref = refs.get(kind)
            facts[kind] = DocumentReadinessFact(
                kind=kind,
                current_digest=snapshot.digest if snapshot is not None else "",
                recorded_digest=ref.current_digest if ref is not None else "",
            )
        confirmations: dict[str, ConfirmationGroups] = {}
        for kind_name in CONFIRMATION_KINDS:
            confirmations[kind_name] = (
                await self._ports.store.load_confirmation_groups(
                    item.work_item_id,
                    kind_name,
                )
            )
        revision = (
            await self._ports.workspace_revision()
            if self._ports.workspace_revision is not None
            else None
        )
        implementation = await self._implementation_fact(item)
        verification = await self._verification_fact(item)
        review = await self._review_fact(item)
        report = await self._report_fact(item)
        completion = await self._completion_fact(item)
        readiness = self._ports.readiness.resolve(
            facts,
            confirmations,
            workspace_revision=revision,
            implementation=implementation,
            verification=verification,
            review=review,
            report=report,
            completion=completion,
        )
        return readiness, facts

    async def _verification_fact(self, item: ComposeWorkItem):
        """把最新 verification 证据还原为 resolver 可消费的新鲜度事实。"""
        return await self._workspace_fact(item, "verification")

    async def _workspace_fact(self, item: ComposeWorkItem, kind: str):
        """implementation/verification 共用：最新 passed 证据 → freshness 事实。"""
        try:
            records = await self._ports.store.load_evidence(item.work_item_id, kind)
        except ComposeWorkItemStoreError:
            return None
        if not records:
            return None
        payload = records[-1].payload
        try:
            digests = frozenset(str(digest) for digest in payload["document_digests"])
            return WorkspaceFreshnessFact(
                workspace_revision=str(payload["workspace_revision"]),
                document_digests=digests,
                evidence_digest=records[-1].content_digest,
                execution_id=str(payload["execution_id"]),
                passed=bool(payload.get("passed", True)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def _review_fact(self, item: ComposeWorkItem):
        """把最新 review 证据还原为双轴新鲜度事实；任一轴缺失即 None。"""
        try:
            records = await self._ports.store.load_evidence(
                item.work_item_id, "review"
            )
        except ComposeWorkItemStoreError:
            return None
        if not records:
            return None
        payload = records[-1].payload
        try:
            digests = frozenset(str(digest) for digest in payload["document_digests"])
            requirement = payload["requirement"]
            code = payload["code"]
            revision = str(payload["workspace_revision"])
            return ReviewFreshnessFact(
                requirement=WorkspaceFreshnessFact(
                    workspace_revision=revision,
                    document_digests=digests,
                    evidence_digest=records[-1].content_digest,
                    execution_id=str(requirement["execution_id"]),
                    passed=bool(requirement.get("passed", True)),
                ),
                code=WorkspaceFreshnessFact(
                    workspace_revision=revision,
                    document_digests=digests,
                    evidence_digest=records[-1].content_digest,
                    execution_id=str(code["execution_id"]),
                    passed=bool(code.get("passed", True)),
                ),
                no_required_findings=bool(payload.get("no_required_findings", True)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def _report_fact(self, item: ComposeWorkItem):
        """把最新 report 证据还原为 ReportReadinessFact。"""
        try:
            records = await self._ports.store.load_evidence(
                item.work_item_id, "report"
            )
        except ComposeWorkItemStoreError:
            return None
        if not records:
            return None
        payload = records[-1].payload
        try:
            return ReportReadinessFact(
                document_digest=str(payload["document_digest"]),
                source_digests=frozenset(
                    str(digest) for digest in payload["source_digests"]
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def _completion_fact(self, item: ComposeWorkItem):
        """Guard Kernel 前置事实：存在 pending/unknown effect 时不能完成。"""
        try:
            effects = await self._ports.store.load_effects(item.work_item_id)
        except ComposeWorkItemStoreError:
            return CompletionReadinessFact(
                no_pending_effects=False,
                no_unknown_effects=False,
            )
        from harness_agent.compose.models import ComposeEffectStatus

        return CompletionReadinessFact(
            no_pending_effects=not any(
                effect.status is ComposeEffectStatus.INTENT for effect in effects
            ),
            no_unknown_effects=not any(
                effect.status is ComposeEffectStatus.UNKNOWN for effect in effects
            ),
        )

    async def _implementation_fact(self, item: ComposeWorkItem):
        """把最新 implementation 证据还原为 resolver 可消费的新鲜度事实。"""
        return await self._workspace_fact(item, "implementation")

    async def _inspect_optional(
        self,
        item: ComposeWorkItem,
        kind: ComposeDocumentKind,
    ):
        """读取一份文档；缺失或 schema 无效按尚未满足处理，不阻断流程。"""
        try:
            return await self._ports.documents.inspect(
                item.work_item_id,
                item.slug,
                kind,
            )
        except ComposeDocumentStoreError:
            return None

    async def _projection_for(
        self,
        item: ComposeWorkItem,
    ) -> ComposeWorkItemProjection:
        readiness, facts = await self._compute_readiness(item)
        return self._projection(item, readiness, pending_decision=None, facts=facts)

    def _projection(
        self,
        item: ComposeWorkItem,
        readiness: ComposeReadiness,
        *,
        pending_decision: str | None,
        facts: dict[ComposeDocumentKind, DocumentReadinessFact],
    ) -> ComposeWorkItemProjection:
        current_activity = (
            item.status.value if item.terminal else readiness.next_action
        )
        return ComposeWorkItemProjection(
            thread_id=item.thread_id,
            work_item_id=item.work_item_id,
            slug=item.slug,
            title=_bounded_title(item.goal),
            revision=item.revision,
            status=item.status.value,
            current_activity=current_activity,
            pending_decision=pending_decision,
            blocked_reason=None,
            readiness=ReadinessProjection.from_readiness(readiness),
            documents=tuple(
                DocumentStatusProjection(
                    kind=kind.value,
                    present=bool(facts[kind].current_digest),
                    current_digest=facts[kind].current_digest,
                    confirmed=_document_confirmed(kind, readiness),
                )
                for kind in _DOCUMENT_ORDER
            ),
        )

    # ---------- 交互辅助 ----------

    async def _typed_decision(
        self,
        request: ComposeTurnRequest,
        *,
        question_id: str,
        header: str,
        body: str,
        options: tuple[TypedDecisionOption, ...],
    ) -> TypedDecisionResult:
        interrupt_id = f"compose-{question_id}-{request.run_id}"
        result = await self._ports.interaction.request_decision(
            TypedDecisionRequest(
                request_id=interrupt_id,
                interrupt_id=interrupt_id,
                question_id=question_id,
                header=header,
                body=body,
                options=options,
            )
        )
        if result.expired:
            raise ComposeWorkItemEngineError(
                "COMPOSE_INTERACTION_UNAVAILABLE",
                "typed decision 过期或无法到达活跃客户端",
            )
        return result

    def _choice_value(
        self,
        result: TypedDecisionResult,
        question_id: str,
    ) -> str:
        """从 InteractionResult 的 answers 形状提取单选值；无答案返回空串。"""
        answers = (
            result.value.get("answers", {})
            if isinstance(result.value, Mapping)
            else {}
        )
        raw = answers.get(question_id, []) if isinstance(answers, Mapping) else []
        items = raw if isinstance(raw, list) else []
        return str(items[0]) if items else ""

    async def _next_slug(self, goal: str) -> str:
        """生成稳定 slug，并在整个 project 范围解决文档目录冲突。"""
        base = make_compose_slug(goal)
        existing = await self._ports.store.load_slugs()
        candidate = base
        index = 2
        while candidate in existing:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def _intent_context(
        self,
        request: ComposeTurnRequest,
        active: ComposeWorkItem | None,
    ) -> TurnIntentContext:
        return TurnIntentContext(
            message=request.message,
            goal_summary=active.goal if active is not None else "",
            scope_summary="",
            pending_decision=None,
            current_activity=None,
            has_active_work_item=active is not None,
        )

    def _now_ms(self) -> int:
        return (
            self._ports.now_ms()
            if self._ports.now_ms is not None
            else int(time.time() * 1000)
        )


def _bounded_goal(message: str) -> str:
    """裁剪目标文本到有界长度；只按字符截断，不改变语义判断。"""
    text = message.strip()
    return text[:MAX_GOAL_CHARS] if len(text) > MAX_GOAL_CHARS else text


def _bounded_title(goal: str) -> str:
    """生成 projection 展示用的短标题；超过上限加省略号。"""
    text = " ".join(goal.split())
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[:MAX_TITLE_CHARS] + "…"


def _explicit_intent(
    kind: TurnIntentKind,
    detail: str = "",
) -> TurnIntent:
    """构造来自命令/UI/澄清选择的显式意图。"""
    return TurnIntent(kind=kind, detail=detail, source=TurnIntentSource.EXPLICIT)


def _explicit_intent_from_choice(value: str, *, detail: str) -> TurnIntent | None:
    """把澄清选择值映射为受限意图；未知值返回 None 表示不能猜测。"""
    try:
        kind = TurnIntentKind(value)
    except ValueError:
        return None
    if kind is TurnIntentKind.UNCLEAR:
        return None
    return _explicit_intent(kind, detail=detail)


def _document_confirmed(kind: ComposeDocumentKind, readiness: ComposeReadiness) -> bool:
    """一份文档是否被其 gate 确认；Plan/Todo 与 Report 使用联合语义。"""
    if kind is ComposeDocumentKind.TASK:
        return readiness.task_confirmed
    if kind is ComposeDocumentKind.SPEC:
        return readiness.spec_confirmed
    if kind in (ComposeDocumentKind.PLAN, ComposeDocumentKind.TODO):
        return readiness.plan_confirmed
    return readiness.report_current
