"""Compose 五阶段纯状态机测试：transition、revision、budget 与终态唯一性。"""

from __future__ import annotations

import pytest

from harness_agent.compose.models import (
    ComposeRunStatus,
    ComposeStage,
    ComposeTask,
    EvidenceItem,
    EvidenceStatus,
    StageState,
    TaskStatus,
)
from harness_agent.compose.state_machine import (
    ComposeEvent,
    ComposeStateMachine,
    ComposeTransitionError,
    REVIEW_FIX_BUDGET,
    VERIFY_FIX_BUDGET,
)


def _task(task_id: str = "task-1") -> ComposeTask:
    return ComposeTask(
        id=task_id,
        title="实现搜索",
        kind="behavior",
        acceptance="搜索返回结果",
        verification_commands=("pytest -q tests/test_search.py",),
    )


def _tasks() -> tuple[ComposeTask, ...]:
    return (
        _task("task-1"),
        ComposeTask(
            id="task-2",
            title="补充文档",
            kind="docs",
            acceptance="文档已更新",
            depends_on=("task-1",),
            verification_commands=(),
        ),
    )


def _initial() -> ComposeRunState:
    return ComposeStateMachine.initial(thread_id="thread-1", run_id="run-1")


def test_initial_state_starts_understand_and_has_stable_projection() -> None:
    """初始状态：Understand running、revision 0、projection 与 wire 形状一致。"""
    state = _initial()
    assert state.stage is ComposeStage.UNDERSTAND
    assert state.status is ComposeRunStatus.RUNNING
    assert state.revision == 0
    assert state.stages[ComposeStage.UNDERSTAND] is StageState.RUNNING
    assert state.stages[ComposeStage.REVIEW] is StageState.PENDING

    projection = state.projection()
    assert projection["revision"] == 0
    assert projection["stage"] == "understand"
    assert projection["status"] == "running"
    assert projection["stages"][0] == {"id": "understand", "status": "running", "attempts": 1}
    assert projection["tasks"] == []
    assert projection["evidence"] == []
    assert projection["blocked_reason"] is None


def test_happy_path_reaches_completed_with_monotonic_revision() -> None:
    """Understand→Plan→批准→Build→Verify→Review→completed 全链路合法。"""
    state = _initial()
    state = ComposeStateMachine.apply(state, ComposeEvent.UNDERSTAND_COMPLETE, artifact_id="understanding-1")
    assert state.stage is ComposeStage.PLAN and state.stages[ComposeStage.PLAN] is StageState.RUNNING

    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_COMPLETE, artifact_id="plan-1")
    assert state.status is ComposeRunStatus.WAITING_USER

    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_APPROVE, tasks=_tasks())
    assert state.stage is ComposeStage.BUILD and state.status is ComposeRunStatus.RUNNING
    assert [task.id for task in state.tasks] == ["task-1", "task-2"]

    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-2")
    state = ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)
    assert state.stage is ComposeStage.VERIFY

    state = ComposeStateMachine.apply(
        state,
        ComposeEvent.VERIFY_PASS,
        evidence_id="evidence-1",
        evidence=(EvidenceItem("pytest -q", EvidenceStatus.PASSED),),
    )
    assert state.stage is ComposeStage.REVIEW

    state = ComposeStateMachine.apply(state, ComposeEvent.REVIEW_PASS, report_id="report-1")
    assert state.status is ComposeRunStatus.COMPLETED
    assert state.verification_evidence_id == "evidence-1"
    assert state.review_report_id == "report-1"
    # 每次合法 transition 都递增 revision。
    assert state.revision == 8


def test_stage_attempts_are_counted_per_stage_entry() -> None:
    """阶段每次（重新）进入都递增 attempts。"""
    state = _initial()
    assert state.stage_attempts[ComposeStage.UNDERSTAND] == 1
    state = ComposeStateMachine.apply(state, ComposeEvent.UNDERSTAND_COMPLETE, artifact_id="understanding-1")
    assert state.stage_attempts[ComposeStage.PLAN] == 1
    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_COMPLETE, artifact_id="plan-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_REVISE)
    assert state.stage_attempts[ComposeStage.PLAN] == 2
    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_COMPLETE, artifact_id="plan-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_APPROVE, tasks=_tasks())
    assert state.stage_attempts[ComposeStage.BUILD] == 1


def test_plan_revise_returns_to_plan_and_cancel_is_terminal() -> None:
    """修改返回 Plan；取消产生唯一 cancelled 终态。"""
    state = _initial()
    state = ComposeStateMachine.apply(state, ComposeEvent.UNDERSTAND_COMPLETE, artifact_id="understanding-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_COMPLETE, artifact_id="plan-1")
    assert state.status is ComposeRunStatus.WAITING_USER

    cancelled = ComposeStateMachine.apply(state, ComposeEvent.PLAN_CANCEL)
    assert cancelled.status is ComposeRunStatus.CANCELLED
    with pytest.raises(ComposeTransitionError):
        ComposeStateMachine.apply(cancelled, ComposeEvent.PLAN_APPROVE)


def test_verify_fail_rounds_then_blocks_after_budget() -> None:
    """Verify 失败携带 fix task 回到 Build；超过 VERIFY_FIX_BUDGET 轮后 blocked。"""
    state = _enter_verify()

    for fix_round in range(1, VERIFY_FIX_BUDGET + 1):
        fix = _task(f"fix-verify-{fix_round}")
        state = ComposeStateMachine.apply(
            state,
            ComposeEvent.VERIFY_FAIL,
            evidence=(EvidenceItem("pytest -q", EvidenceStatus.FAILED),),
            fix_tasks=(fix,),
        )
        assert state.status is ComposeRunStatus.RUNNING, f"round {fix_round} should fix"
        assert state.stage is ComposeStage.BUILD
        assert state.verify_fix_round == fix_round
        assert fix.id in {task.id for task in state.tasks}
        state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id=fix.id)
        state = ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)

    # 预算已用完：下一次失败必须 blocked。
    state = ComposeStateMachine.apply(
        state, ComposeEvent.VERIFY_FAIL, evidence=(EvidenceItem("pytest -q", EvidenceStatus.FAILED),)
    )
    assert state.status is ComposeRunStatus.BLOCKED
    assert state.blocked_reason is not None
    with pytest.raises(ComposeTransitionError):
        ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)


def test_verify_fail_without_fix_task_is_rejected() -> None:
    """Verify 失败必须携带来源明确的 fix task，不能空手回 Build。"""
    state = _enter_verify()
    with pytest.raises(ComposeTransitionError, match="COMPOSE_FIX_TASKS_MISSING"):
        ComposeStateMachine.apply(
            state, ComposeEvent.VERIFY_FAIL, evidence=(EvidenceItem("pytest -q", EvidenceStatus.FAILED),)
        )
    with pytest.raises(ComposeTransitionError, match="COMPOSE_FIX_TASK_DUPLICATE"):
        ComposeStateMachine.apply(
            state,
            ComposeEvent.VERIFY_FAIL,
            evidence=(EvidenceItem("pytest -q", EvidenceStatus.FAILED),),
            fix_tasks=(_task("task-1"),),  # 与既有任务重复
        )


def test_verify_pass_requires_all_passed_evidence() -> None:
    """无 fresh pass evidence 不能进入 Review。"""
    state = _enter_verify()
    with pytest.raises(ComposeTransitionError, match="COMPOSE_EVIDENCE_NOT_PASSED"):
        ComposeStateMachine.apply(state, ComposeEvent.VERIFY_PASS, evidence_id="e", evidence=())
    with pytest.raises(ComposeTransitionError, match="COMPOSE_EVIDENCE_NOT_PASSED"):
        ComposeStateMachine.apply(
            state,
            ComposeEvent.VERIFY_PASS,
            evidence_id="e",
            evidence=(EvidenceItem("pytest -q", EvidenceStatus.FAILED),),
        )


def test_task_dependency_order_is_enforced() -> None:
    """依赖未通过的 task 不能先完成。"""
    state = _enter_build()  # task-2 depends on task-1
    with pytest.raises(ComposeTransitionError, match="depends on unfinished"):
        ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-2")
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-2")
    assert all(task.status is TaskStatus.PASSED for task in state.tasks)


def test_review_fail_rounds_then_blocks_after_budget() -> None:
    """Review 失败携带 fix task 重新 Build→Verify→Review；预算耗尽后 blocked。"""
    state = _enter_review()
    fix = _task("fix-review-1")

    # 一轮修复后通过：证明 fix task 流程完整可用。
    state = ComposeStateMachine.apply(state, ComposeEvent.REVIEW_FAIL, report_id="r", fix_tasks=(fix,))
    assert state.status is ComposeRunStatus.RUNNING
    assert state.stage is ComposeStage.BUILD
    assert state.review_fix_round == 1
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id=fix.id)
    state = ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)
    state = ComposeStateMachine.apply(
        state, ComposeEvent.VERIFY_PASS, evidence_id="e", evidence=(EvidenceItem("pytest -q", EvidenceStatus.PASSED),)
    )
    state = ComposeStateMachine.apply(state, ComposeEvent.REVIEW_PASS, report_id="r")
    assert state.status is ComposeRunStatus.COMPLETED

    # 连续失败直到预算耗尽
    state = _enter_review()
    for round_index in range(REVIEW_FIX_BUDGET + 1):
        state = ComposeStateMachine.apply(
            state,
            ComposeEvent.REVIEW_FAIL,
            report_id="r",
            fix_tasks=(ComposeTask(
                id=f"fix-review-{round_index + 1}",
                title="修复 finding",
                kind="behavior",
                acceptance="finding 已修复",
                verification_commands=("pytest -q tests/fix.py",),
            ),),
        )
        if state.status is ComposeRunStatus.BLOCKED:
            break
        state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id=f"fix-review-{round_index + 1}")
        state = ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)
        state = ComposeStateMachine.apply(
            state, ComposeEvent.VERIFY_PASS, evidence_id="e", evidence=(EvidenceItem("pytest -q", EvidenceStatus.PASSED),)
        )
    assert state.status is ComposeRunStatus.BLOCKED
    assert "review" in (state.blocked_reason or "")


def test_schema_invalid_retry_allowed_once_then_must_fail() -> None:
    """stage schema invalid 只允许一次结构化重试；重试后再次失败必须 failed。"""
    state = _initial()
    state = ComposeStateMachine.apply(state, ComposeEvent.STAGE_RETRY)
    assert state.stage_attempts[ComposeStage.UNDERSTAND] == 2
    with pytest.raises(ComposeTransitionError):
        ComposeStateMachine.apply(state, ComposeEvent.STAGE_RETRY)

    failed = ComposeStateMachine.apply(state, ComposeEvent.FAIL, reason="artifact invalid")
    assert failed.status is ComposeRunStatus.FAILED
    assert failed.stages[ComposeStage.UNDERSTAND] is StageState.FAILED


def test_task_fail_blocks_build_with_reason() -> None:
    """Build 中任务失败收敛为 blocked，不能跳到 Verify。"""
    state = _enter_build()
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_FAIL, task_id="task-2")
    assert state.status is ComposeRunStatus.BLOCKED
    assert state.tasks[1].status is TaskStatus.FAILED
    with pytest.raises(ComposeTransitionError):
        ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)


def test_illegal_transitions_are_rejected_with_stable_code() -> None:
    """跨阶段事件、未完成任务的 BUILD_COMPLETE、终态事件都被拒绝。"""
    state = _initial()
    with pytest.raises(ComposeTransitionError, match="verify_pass"):
        ComposeStateMachine.apply(state, ComposeEvent.VERIFY_PASS, evidence_id="e", evidence=())

    state = _enter_build()
    with pytest.raises(ComposeTransitionError):
        ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)  # 任务未全部完成

    state = _enter_verify()
    with pytest.raises(ComposeTransitionError):
        ComposeStateMachine.apply(state, ComposeEvent.PLAN_APPROVE)

    with pytest.raises(ComposeTransitionError):
        ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-1")

    completed = ComposeStateMachine.apply(
        ComposeStateMachine.apply(
            state,
            ComposeEvent.VERIFY_PASS,
            evidence_id="e",
            evidence=(EvidenceItem("pytest -q", EvidenceStatus.PASSED),),
        ),
        ComposeEvent.REVIEW_PASS,
        report_id="r",
    )
    with pytest.raises(ComposeTransitionError, match="TERMINAL_IMMUTABLE"):
        ComposeStateMachine.apply(completed, ComposeEvent.CANCEL)


def test_block_and_fail_and_cancel_from_any_non_terminal_state() -> None:
    """blocked/failed/cancelled 从任意非终态唯一收敛，且终态不可再变。"""
    for terminal_event in (ComposeEvent.BLOCK, ComposeEvent.FAIL, ComposeEvent.CANCEL):
        state = _enter_verify()
        if terminal_event is ComposeEvent.BLOCK:
            state = ComposeStateMachine.apply(state, terminal_event, reason="env missing")
            assert state.status is ComposeRunStatus.BLOCKED
        elif terminal_event is ComposeEvent.FAIL:
            state = ComposeStateMachine.apply(state, terminal_event, reason="backend down")
            assert state.status is ComposeRunStatus.FAILED
        else:
            state = ComposeStateMachine.apply(state, terminal_event)
            assert state.status is ComposeRunStatus.CANCELLED
        with pytest.raises(ComposeTransitionError):
            ComposeStateMachine.apply(state, ComposeEvent.CANCEL)


def test_projection_reflects_tasks_and_evidence() -> None:
    """projection 的任务/evidence 与状态机事实一致，且不含 artifact 正文。"""
    state = _enter_verify()
    projection = state.projection()
    assert projection["tasks"] == [
        {"id": "task-1", "title": "实现搜索", "status": "passed"},
        {"id": "task-2", "title": "补充文档", "status": "passed"},
    ]
    state = ComposeStateMachine.apply(
        state,
        ComposeEvent.VERIFY_PASS,
        evidence_id="evidence-1",
        evidence=(
            EvidenceItem("pytest -q tests/test_search.py", EvidenceStatus.PASSED),
            EvidenceItem("bun run typecheck", EvidenceStatus.PASSED),
        ),
    )
    projection = state.projection()
    assert projection["evidence"] == [
        {"label": "pytest -q tests/test_search.py", "status": "passed"},
        {"label": "bun run typecheck", "status": "passed"},
    ]
    assert "goal" not in projection and "prompt" not in projection


def _enter_build() -> ComposeRunState:
    """走到 Build running 并装载两个任务的公共状态。"""
    state = _initial()
    state = ComposeStateMachine.apply(state, ComposeEvent.UNDERSTAND_COMPLETE, artifact_id="understanding-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_COMPLETE, artifact_id="plan-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.PLAN_APPROVE, tasks=_tasks())
    return state


def _enter_verify() -> ComposeRunState:
    """走到 Verify running 的公共状态。"""
    state = _enter_build()
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-1")
    state = ComposeStateMachine.apply(state, ComposeEvent.TASK_COMPLETE, task_id="task-2")
    state = ComposeStateMachine.apply(state, ComposeEvent.BUILD_COMPLETE)
    return state


def _enter_review() -> ComposeRunState:
    """走到 Review running 的公共状态。"""
    state = _enter_verify()
    state = ComposeStateMachine.apply(
        state,
        ComposeEvent.VERIFY_PASS,
        evidence_id="evidence-1",
        evidence=(EvidenceItem("pytest -q", EvidenceStatus.PASSED),),
    )
    return state
