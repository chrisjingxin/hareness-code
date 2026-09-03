"""Goal proposal 的 Host Run adapter；复用唯一 Run 生命周期与交互通道。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from harness_agent.goals.models import GoalStoreError, goal_to_wire, validate_goal_text
from harness_agent.goals.proposal import (
    GOAL_MAX_CLARIFICATION_ROUNDS,
    GoalClarification,
    GoalProposalContext,
    GoalProposalServices,
)
from harness_agent.host.run_execution import AdapterOutcome, RUN_PROGRESS, RUN_STARTED
from harness_agent.runtime.interactions import InteractionRequest


class GoalProposalRunAdapter:
    """让 proposal 复用 RunCoordinator 的 owner、取消、Interaction 与唯一终态。"""

    def __init__(self, services_provider: Callable[[Any], Awaitable[GoalProposalServices]]) -> None:
        self._services_provider = services_provider

    async def execute(self, run: Any, port: Any) -> AdapterOutcome | None:
        proposal_input = run.start.input
        if getattr(proposal_input, "kind", None) != "goal_proposal":
            return AdapterOutcome("failed", "GOAL_OBJECTIVE_INVALID", "invalid proposal input")
        services = await self._services_provider(run)
        # Proposal 已冻结实际 primary 与持久化依赖，不会获取 AgentEngine。
        # 在模型调用和人工审核前结束 snapshot 临界区，避免控制面 RPC 阻塞
        # sidecar 的串行读循环，继而饿死反向 Interaction 响应。
        await port.release_preparation_snapshot(run)
        snapshot = await services.store.inspect(run.thread_id)
        pending = snapshot.pending
        if (
            pending is None
            or pending.request_id != proposal_input.request_id
            or pending.status not in {"ready", "reviewing"}
        ):
            return AdapterOutcome("failed", "GOAL_NOT_FOUND", "goal request is not ready")

        port.emit(run, RUN_STARTED, {"resumed": False, "mode": "build"})
        port.mark_running(run)
        await port.start_execution(run)
        context = GoalProposalContext(
            input_text=pending.input_text,
            current_goal=snapshot.goal,
            recent_messages=services.recent_messages,
        )
        clarification_round = 0
        review_round = 0
        needs_draft = pending.status == "ready"
        while True:
            if needs_draft:
                await services.store.set_proposal_status(
                    pending.request_id,
                    status="drafting",
                    now_ms=services.now_ms(),
                )
                port.emit(run, RUN_PROGRESS, {"phase": "model", "elapsed_ms": 0})
                generated = await services.draft(context)
                if isinstance(generated, GoalClarification):
                    if clarification_round >= GOAL_MAX_CLARIFICATION_ROUNDS:
                        await services.store.set_proposal_status(
                            pending.request_id,
                            status="failed",
                            error_code="GOAL_OBJECTIVE_UNCLEAR",
                            now_ms=services.now_ms(),
                        )
                        return AdapterOutcome(
                            "failed",
                            "GOAL_OBJECTIVE_UNCLEAR",
                            "goal remains unclear after two clarification rounds",
                        )
                    clarification_round += 1
                    await services.store.set_proposal_status(
                        pending.request_id,
                        status="clarifying",
                        now_ms=services.now_ms(),
                    )
                    questions = _question_payload(generated, clarification_round)
                    question_interaction_id = f"goal-clarification-{run.run_id}-{clarification_round}"
                    answer = await port.request_question(
                        run,
                        request_id=question_interaction_id,
                        interrupt_id=question_interaction_id,
                        questions=questions,
                    )
                    clarification = _clarification_answers(answer, generated, clarification_round)
                    if clarification is None:
                        await services.store.cancel_proposal(pending.request_id, now_ms=services.now_ms())
                        return AdapterOutcome("cancelled", message="Goal clarification cancelled")
                    context = GoalProposalContext(
                        input_text=context.input_text,
                        current_goal=context.current_goal,
                        recent_messages=context.recent_messages,
                        clarifications=(*context.clarifications, *clarification),
                        feedback=context.feedback,
                    )
                    continue

                pending = await services.store.save_proposal(
                    request_id=pending.request_id,
                    objective=generated.objective,
                    assumptions=generated.assumptions,
                    criteria=generated.criteria,
                    now_ms=services.now_ms(),
                )
                needs_draft = False
            review_round += 1
            interaction_id = f"goal-review-{run.run_id}-{review_round}"
            result = await port.request_interaction(
                run,
                InteractionRequest(
                    request_id=interaction_id,
                    type="goal",
                    interrupt_id=interaction_id,
                    payload={
                        "interrupt_id": interaction_id,
                        "request_id": pending.request_id,
                        "proposal_kind": pending.kind,
                        "base_goal_id": pending.base_goal_id,
                        "base_revision": pending.base_revision,
                        "objective": pending.proposed_objective,
                        "assumptions": list(pending.proposed_assumptions),
                        "criteria": list(pending.proposed_criteria),
                        "decisions": ["accepted", "edited", "rejected", "cancelled"],
                    },
                ),
            )
            response = result.value if isinstance(result.value, Mapping) else {}
            decision = response.get("decision")
            if result.expired or decision == "cancelled":
                await services.store.cancel_proposal(pending.request_id, now_ms=services.now_ms())
                return AdapterOutcome("cancelled", message="Goal proposal cancelled")
            if decision == "rejected":
                raw_feedback = response.get("feedback")
                try:
                    feedback = validate_goal_text(
                        raw_feedback if isinstance(raw_feedback, str) else "",
                    )
                except GoalStoreError:
                    return AdapterOutcome("failed", "GOAL_OBJECTIVE_INVALID", "goal feedback is required")
                context = GoalProposalContext(
                    input_text=context.input_text,
                    current_goal=context.current_goal,
                    recent_messages=context.recent_messages,
                    clarifications=context.clarifications,
                    feedback=feedback,
                )
                needs_draft = True
                continue
            if decision == "edited":
                raw_criteria = response.get("criteria")
                if not isinstance(raw_criteria, list) or not all(isinstance(item, str) for item in raw_criteria):
                    return AdapterOutcome("failed", "GOAL_CRITERIA_INVALID", "invalid edited criteria")
                criteria = tuple(raw_criteria)
            elif decision == "accepted":
                criteria = pending.proposed_criteria
            else:
                return AdapterOutcome("failed", "GOAL_CRITERIA_INVALID", "invalid goal decision")
            break
        applied = await services.store.apply_proposal(
            request_id=pending.request_id,
            criteria=criteria,
            now_ms=services.now_ms(),
        )
        continuation = {
            "continuation_id": applied.continuation.continuation_id,
            "goal_id": applied.continuation.goal_id,
            "goal_revision": applied.continuation.goal_revision,
            "reason": applied.continuation.reason,
        }
        run.context_summary["goal_continuation"] = continuation
        run.context_summary["goal"] = goal_to_wire(applied.goal)
        port.emit(
            run,
            "goal.changed",
            {"reason": "proposal_applied", "goal": goal_to_wire(applied.goal)},
        )
        return None


def _question_payload(
    clarification: GoalClarification,
    round_number: int,
) -> list[dict[str, object]]:
    """把 Criteria 输出收敛为共享 question interaction。"""
    body = "；".join(clarification.missing_information)
    return [
        {
            "id": f"goal-question-{round_number}-{index}",
            "question": question,
            "header": "目标澄清",
            "body": body,
            "options": [],
            "multi_select": False,
            "allow_other": True,
        }
        for index, question in enumerate(clarification.questions, start=1)
    ]


def _clarification_answers(
    result: Any,
    clarification: GoalClarification,
    round_number: int,
) -> tuple[tuple[str, str], ...] | None:
    """校验 question response 并按原问题顺序返回文本答案。"""
    if result.expired or not isinstance(result.value, Mapping):
        return None
    answers = result.value.get("answers")
    if not isinstance(answers, Mapping):
        return None
    resolved: list[tuple[str, str]] = []
    for index, question in enumerate(clarification.questions, start=1):
        raw = answers.get(f"goal-question-{round_number}-{index}")
        if not isinstance(raw, (list, tuple)) or not raw or not all(isinstance(item, str) for item in raw):
            return None
        answer = "；".join(item.strip() for item in raw if item.strip())
        if not answer:
            return None
        resolved.append((question, answer))
    return tuple(resolved)
