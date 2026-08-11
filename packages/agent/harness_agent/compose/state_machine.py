"""Compose 五阶段纯状态机：阶段只由 typed event 与固定 budget 推进。

状态机不接触 I/O：workflow 负责 artifact 校验、执行 Agent 与用户门禁，
再把确定性结果作为 event 交给状态机。模型不能自行宣称进入下一阶段；
revision 在每次合法 transition 后单调递增，作为 wire projection 帧序。
"""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import Any, Mapping

from harness_agent.compose.models import (
    ComposeRunState,
    ComposeRunStatus,
    ComposeStage,
    ComposeTask,
    EvidenceItem,
    EvidenceStatus,
    StageState,
    TaskStatus,
)

VERIFY_FIX_BUDGET = 2
REVIEW_FIX_BUDGET = 2
SCHEMA_INVALID_RETRY_ALLOWED = 1


class ComposeEvent(str, Enum):
    """状态机的全部合法事件；payload 字段由各 transition 校验。"""

    STAGE_RETRY = "stage_retry"
    UNDERSTAND_COMPLETE = "understand_complete"
    PLAN_COMPLETE = "plan_complete"
    PLAN_APPROVE = "plan_approve"
    PLAN_REVISE = "plan_revise"
    PLAN_CANCEL = "plan_cancel"
    TASK_COMPLETE = "task_complete"
    TASK_FAIL = "task_fail"
    BUILD_COMPLETE = "build_complete"
    VERIFY_PASS = "verify_pass"
    VERIFY_FAIL = "verify_fail"
    REVIEW_PASS = "review_pass"
    REVIEW_FAIL = "review_fail"
    BLOCK = "block"
    FAIL = "fail"
    CANCEL = "cancel"


class ComposeTransitionError(RuntimeError):
    """非法 transition；code 是稳定错误标识，message 面向诊断。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def _illegal(state: ComposeRunState, event: ComposeEvent, detail: str = "") -> ComposeTransitionError:
    message = f"{event.value} illegal from {state.stage.value}/{state.status.value}"
    if detail:
        message += f" ({detail})"
    return ComposeTransitionError("COMPOSE_ILLEGAL_TRANSITION", message)


def _require_stage(state: ComposeRunState, stage: ComposeStage, event: ComposeEvent) -> None:
    if state.stage is not stage or state.stages.get(stage) is not StageState.RUNNING:
        raise _illegal(state, event)


def _copy(state: ComposeRunState) -> ComposeRunState:
    """浅拷贝可变 dict 字段，保持状态机实例不可变语义。"""
    return replace(
        state,
        stages=dict(state.stages),
        stage_attempts=dict(state.stage_attempts),
        schema_retry_used=dict(state.schema_retry_used),
        tasks=tuple(state.tasks),
    )


def _on_stage_retry(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """同一阶段的结构化重试（schema invalid 只允许一次）。"""
    if state.stages.get(state.stage) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.STAGE_RETRY)
    if state.schema_retry_used.get(state.stage, False):
        raise ComposeTransitionError(
            "COMPOSE_SCHEMA_RETRY_USED",
            f"schema-invalid retry already used for {state.stage.value}",
        )
    next_state = _copy(state)
    next_state.schema_retry_used[state.stage] = True
    next_state.stage_attempts[state.stage] += 1
    next_state.revision += 1
    return next_state


def _on_understand_complete(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """Understand 完成（open_decisions 为空由 workflow 校验后发送）。"""
    _require_stage(state, ComposeStage.UNDERSTAND, ComposeEvent.UNDERSTAND_COMPLETE)
    next_state = _copy(state)
    next_state.stages[ComposeStage.UNDERSTAND] = StageState.PASSED
    next_state.stage = ComposeStage.PLAN
    next_state.stages[ComposeStage.PLAN] = StageState.RUNNING
    next_state.stage_attempts[ComposeStage.PLAN] += 1
    next_state.revision += 1
    return next_state


def _on_plan_complete(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """Plan 就绪；进入用户确认门禁（等待整体方案批准）。"""
    _require_stage(state, ComposeStage.PLAN, ComposeEvent.PLAN_COMPLETE)
    next_state = _copy(state)
    next_state.stages[ComposeStage.PLAN] = StageState.PASSED
    next_state.status = ComposeRunStatus.WAITING_USER
    next_state.revision += 1
    return next_state


def _on_plan_approve(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """用户批准整体方案；批准的任务清单冻结在 Run 状态里。"""
    if state.status is not ComposeRunStatus.WAITING_USER or state.stage is not ComposeStage.PLAN:
        raise _illegal(state, ComposeEvent.PLAN_APPROVE)
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, (tuple, list)) or not all(
        isinstance(task, ComposeTask) for task in raw_tasks
    ):
        raise ComposeTransitionError(
            "COMPOSE_PLAN_TASKS_MISSING", "plan approve requires approved tasks"
        )
    next_state = _copy(state)
    next_state.stage = ComposeStage.BUILD
    next_state.stages[ComposeStage.BUILD] = StageState.RUNNING
    next_state.stage_attempts[ComposeStage.BUILD] += 1
    next_state.status = ComposeRunStatus.RUNNING
    next_state.tasks = tuple(raw_tasks)
    next_state.revision += 1
    return next_state


def _on_plan_revise(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """用户要求修改方案；回到 Plan 重做并丢弃旧 artifact 引用。"""
    if state.status is not ComposeRunStatus.WAITING_USER or state.stage is not ComposeStage.PLAN:
        raise _illegal(state, ComposeEvent.PLAN_REVISE)
    next_state = _copy(state)
    next_state.stages[ComposeStage.PLAN] = StageState.RUNNING
    next_state.stage_attempts[ComposeStage.PLAN] += 1
    next_state.status = ComposeRunStatus.RUNNING
    next_state.plan_artifact_id = None
    next_state.revision += 1
    return next_state


def _on_plan_cancel(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """用户取消整体方案；Run 直接收敛到唯一 cancelled 终态。"""
    if state.status is not ComposeRunStatus.WAITING_USER or state.stage is not ComposeStage.PLAN:
        raise _illegal(state, ComposeEvent.PLAN_CANCEL)
    next_state = _copy(state)
    next_state.stages[ComposeStage.PLAN] = StageState.CANCELLED
    next_state.status = ComposeRunStatus.CANCELLED
    next_state.revision += 1
    return next_state


def _task(state: ComposeRunState, task_id: str) -> ComposeTask:
    for task in state.tasks:
        if task.id == task_id:
            return task
    raise ComposeTransitionError("COMPOSE_TASK_UNKNOWN", f"unknown task {task_id}")


def _on_task_complete(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """单个 Build task 通过；依赖未通过或已通过的 task 不能再次完成。"""
    if state.stage is not ComposeStage.BUILD or state.stages.get(ComposeStage.BUILD) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.TASK_COMPLETE)
    task_id = payload.get("task_id")
    if not isinstance(task_id, str):
        raise ComposeTransitionError("COMPOSE_TASK_ID_MISSING", "task complete requires task_id")
    task = _task(state, task_id)
    if task.status is TaskStatus.PASSED:
        raise _illegal(state, ComposeEvent.TASK_COMPLETE, f"task {task_id} already passed")
    by_id = {current.id: current for current in state.tasks}
    for dependency in task.depends_on:
        dependency_task = by_id.get(dependency)
        if dependency_task is None or dependency_task.status is not TaskStatus.PASSED:
            raise _illegal(
                state,
                ComposeEvent.TASK_COMPLETE,
                f"task {task_id} depends on unfinished {dependency}",
            )
    next_state = _copy(state)
    updated = list(next_state.tasks)
    for index, current in enumerate(updated):
        if current.id == task_id:
            updated[index] = replace(current, status=TaskStatus.PASSED)
            break
    next_state.tasks = tuple(updated)
    next_state.revision += 1
    return next_state


def _on_task_fail(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """Build task 最终失败：收敛为 blocked，不能由模型自行跳到 Verify。"""
    if state.stage is not ComposeStage.BUILD or state.stages.get(ComposeStage.BUILD) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.TASK_FAIL)
    task_id = payload.get("task_id")
    if not isinstance(task_id, str):
        raise ComposeTransitionError("COMPOSE_TASK_ID_MISSING", "task fail requires task_id")
    task = _task(state, task_id)
    if task.status is TaskStatus.PASSED:
        raise _illegal(state, ComposeEvent.TASK_FAIL, f"task {task_id} already passed")
    next_state = _copy(state)
    updated = list(next_state.tasks)
    for index, current in enumerate(updated):
        if current.id == task_id:
            updated[index] = replace(current, status=TaskStatus.FAILED)
            break
    next_state.tasks = tuple(updated)
    next_state.stages[ComposeStage.BUILD] = StageState.FAILED
    next_state.status = ComposeRunStatus.BLOCKED
    next_state.blocked_reason = f"task {task_id} failed"
    next_state.revision += 1
    return next_state


def _on_build_complete(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """全部任务完成后进入 Verify；未完成的任务阻止进入。"""
    if state.stage is not ComposeStage.BUILD or state.stages.get(ComposeStage.BUILD) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.BUILD_COMPLETE)
    if not state.tasks or any(task.status is not TaskStatus.PASSED for task in state.tasks):
        raise _illegal(state, ComposeEvent.BUILD_COMPLETE, "tasks incomplete")
    next_state = _copy(state)
    next_state.stages[ComposeStage.BUILD] = StageState.PASSED
    next_state.stage = ComposeStage.VERIFY
    next_state.stages[ComposeStage.VERIFY] = StageState.RUNNING
    next_state.stage_attempts[ComposeStage.VERIFY] += 1
    next_state.revision += 1
    return next_state


def _on_verify_pass(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """全部 required command fresh pass；缺 evidence 不能进入 Review。"""
    if state.stage is not ComposeStage.VERIFY or state.stages.get(ComposeStage.VERIFY) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.VERIFY_PASS)
    evidence_id = payload.get("evidence_id")
    evidence = payload.get("evidence", ())
    if not isinstance(evidence_id, str) or not isinstance(evidence, (tuple, list)):
        raise ComposeTransitionError("COMPOSE_EVIDENCE_MISSING", "verify pass requires evidence")
    if not evidence or any(
        not isinstance(item, EvidenceItem) or item.status is not EvidenceStatus.PASSED
        for item in evidence
    ):
        raise ComposeTransitionError(
            "COMPOSE_EVIDENCE_NOT_PASSED",
            "verify pass requires non-empty all-passed evidence",
        )
    next_state = _copy(state)
    next_state.stages[ComposeStage.VERIFY] = StageState.PASSED
    next_state.verification_evidence_id = evidence_id
    next_state.evidence = tuple(evidence)
    next_state.stage = ComposeStage.REVIEW
    next_state.stages[ComposeStage.REVIEW] = StageState.RUNNING
    next_state.stage_attempts[ComposeStage.REVIEW] += 1
    next_state.revision += 1
    return next_state


def _on_verify_fail(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """Verify 失败：来源明确的 fix task 回到 Build；预算耗尽后 blocked。"""
    if state.stage is not ComposeStage.VERIFY or state.stages.get(ComposeStage.VERIFY) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.VERIFY_FAIL)
    evidence = payload.get("evidence", ())
    next_state = _copy(state)
    next_state.stages[ComposeStage.VERIFY] = StageState.FAILED
    if isinstance(evidence, (tuple, list)) and evidence:
        next_state.evidence = tuple(evidence)
    next_state.verify_fix_round += 1
    if next_state.verify_fix_round > VERIFY_FIX_BUDGET:
        next_state.status = ComposeRunStatus.BLOCKED
        next_state.blocked_reason = "verify fix budget exhausted"
    else:
        fix_tasks = payload.get("fix_tasks")
        if not isinstance(fix_tasks, (tuple, list)) or not all(
            isinstance(task, ComposeTask) for task in fix_tasks
        ):
            raise ComposeTransitionError(
                "COMPOSE_FIX_TASKS_MISSING",
                "verify fail requires a fix task for the next build round",
            )
        existing_ids = {task.id for task in next_state.tasks}
        for fix_task in fix_tasks:
            if fix_task.id in existing_ids:
                raise ComposeTransitionError(
                    "COMPOSE_FIX_TASK_DUPLICATE",
                    f"fix task {fix_task.id} already exists",
                )
        # 原任务保持 PASSED 事实；fix task 作为新的 pending 项进入 Build。
        next_state.tasks = next_state.tasks + tuple(fix_tasks)
        next_state.stage = ComposeStage.BUILD
        next_state.stages[ComposeStage.BUILD] = StageState.RUNNING
        next_state.stage_attempts[ComposeStage.BUILD] += 1
        next_state.status = ComposeRunStatus.RUNNING
    next_state.revision += 1
    return next_state


def _on_review_pass(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """两轴 Review 都 pass：Run 唯一 completed 终态。"""
    if state.stage is not ComposeStage.REVIEW or state.stages.get(ComposeStage.REVIEW) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.REVIEW_PASS)
    report_id = payload.get("report_id")
    if not isinstance(report_id, str):
        raise ComposeTransitionError("COMPOSE_REPORT_MISSING", "review pass requires report_id")
    next_state = _copy(state)
    next_state.stages[ComposeStage.REVIEW] = StageState.PASSED
    next_state.review_report_id = report_id
    next_state.status = ComposeRunStatus.COMPLETED
    next_state.revision += 1
    return next_state


def _on_review_fail(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """Review 存在 Required finding：来源明确的 fix task 回到 Build；预算耗尽后 blocked。"""
    if state.stage is not ComposeStage.REVIEW or state.stages.get(ComposeStage.REVIEW) is not StageState.RUNNING:
        raise _illegal(state, ComposeEvent.REVIEW_FAIL)
    report_id = payload.get("report_id")
    next_state = _copy(state)
    next_state.stages[ComposeStage.REVIEW] = StageState.FAILED
    if isinstance(report_id, str):
        next_state.review_report_id = report_id
    next_state.review_fix_round += 1
    if next_state.review_fix_round > REVIEW_FIX_BUDGET:
        next_state.status = ComposeRunStatus.BLOCKED
        next_state.blocked_reason = "review fix budget exhausted"
    else:
        fix_tasks = payload.get("fix_tasks")
        if not isinstance(fix_tasks, (tuple, list)) or not all(
            isinstance(task, ComposeTask) for task in fix_tasks
        ):
            raise ComposeTransitionError(
                "COMPOSE_FIX_TASKS_MISSING",
                "review fail requires a fix task for the next build round",
            )
        existing_ids = {task.id for task in next_state.tasks}
        for fix_task in fix_tasks:
            if fix_task.id in existing_ids:
                raise ComposeTransitionError(
                    "COMPOSE_FIX_TASK_DUPLICATE",
                    f"fix task {fix_task.id} already exists",
                )
        next_state.tasks = next_state.tasks + tuple(fix_tasks)
        next_state.stage = ComposeStage.BUILD
        next_state.stages[ComposeStage.BUILD] = StageState.RUNNING
        next_state.stage_attempts[ComposeStage.BUILD] += 1
        next_state.status = ComposeRunStatus.RUNNING
    next_state.revision += 1
    return next_state


def _on_block(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """环境缺失/预算耗尽：blocked 终态，保留原因供用户接管。"""
    reason = payload.get("reason")
    next_state = _copy(state)
    if state.stages.get(state.stage) in (StageState.RUNNING, StageState.WAITING_USER):
        next_state.stages[state.stage] = StageState.BLOCKED
    next_state.status = ComposeRunStatus.BLOCKED
    next_state.blocked_reason = str(reason) if reason else "blocked"
    next_state.revision += 1
    return next_state


def _on_fail(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """schema invalid 重试耗尽/阶段执行失败：failed 终态。"""
    next_state = _copy(state)
    if state.stages.get(state.stage) in (StageState.RUNNING, StageState.WAITING_USER):
        next_state.stages[state.stage] = StageState.FAILED
    next_state.status = ComposeRunStatus.FAILED
    next_state.revision += 1
    return next_state


def _on_cancel(state: ComposeRunState, payload: Mapping[str, Any]) -> ComposeRunState:
    """用户/父级取消：唯一 cancelled 终态。"""
    next_state = _copy(state)
    if state.stages.get(state.stage) in (StageState.RUNNING, StageState.WAITING_USER):
        next_state.stages[state.stage] = StageState.CANCELLED
    next_state.status = ComposeRunStatus.CANCELLED
    next_state.revision += 1
    return next_state


_HANDLERS: dict[ComposeEvent, Any] = {
    ComposeEvent.STAGE_RETRY: _on_stage_retry,
    ComposeEvent.UNDERSTAND_COMPLETE: _on_understand_complete,
    ComposeEvent.PLAN_COMPLETE: _on_plan_complete,
    ComposeEvent.PLAN_APPROVE: _on_plan_approve,
    ComposeEvent.PLAN_REVISE: _on_plan_revise,
    ComposeEvent.PLAN_CANCEL: _on_plan_cancel,
    ComposeEvent.TASK_COMPLETE: _on_task_complete,
    ComposeEvent.TASK_FAIL: _on_task_fail,
    ComposeEvent.BUILD_COMPLETE: _on_build_complete,
    ComposeEvent.VERIFY_PASS: _on_verify_pass,
    ComposeEvent.VERIFY_FAIL: _on_verify_fail,
    ComposeEvent.REVIEW_PASS: _on_review_pass,
    ComposeEvent.REVIEW_FAIL: _on_review_fail,
    ComposeEvent.BLOCK: _on_block,
    ComposeEvent.FAIL: _on_fail,
    ComposeEvent.CANCEL: _on_cancel,
}


class ComposeStateMachine:
    """纯 transition 函数集合；无 I/O、无时间依赖。"""

    @staticmethod
    def initial(thread_id: str, run_id: str) -> ComposeRunState:
        """创建初始状态：Understand running，其余阶段 pending。"""
        stages = {stage: StageState.PENDING for stage in ComposeStage}
        stages[ComposeStage.UNDERSTAND] = StageState.RUNNING
        attempts = {stage: 0 for stage in ComposeStage}
        attempts[ComposeStage.UNDERSTAND] = 1
        return ComposeRunState(
            thread_id=thread_id,
            run_id=run_id,
            stages=stages,
            stage_attempts=attempts,
            schema_retry_used={stage: False for stage in ComposeStage},
        )

    @staticmethod
    def apply(
        state: ComposeRunState,
        event: ComposeEvent,
        **payload: Any,
    ) -> ComposeRunState:
        """应用一个事件；非法 transition 抛出 ComposeTransitionError。"""
        handler = _HANDLERS.get(event)
        if handler is None:
            raise ComposeTransitionError("COMPOSE_EVENT_UNKNOWN", f"unknown event {event}")
        if state.terminal:
            raise ComposeTransitionError(
                "COMPOSE_TERMINAL_IMMUTABLE",
                f"COMPOSE_TERMINAL_IMMUTABLE: terminal state {state.status.value} is immutable",
            )
        return handler(state, payload)
