"""Goal 领域值与严格输入上限；不包含传输或 SQLite 细节。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Mapping

GOAL_MAX_OBJECTIVE_CHARS = 8_000
GOAL_MAX_ASSUMPTIONS = 16
GOAL_MAX_ASSUMPTION_CHARS = 1_000
GOAL_MAX_CRITERIA = 32
GOAL_MAX_CRITERION_CHARS = 2_000
GOAL_MAX_PROPOSAL_CHARS = 12_000
GOAL_DEFAULT_MAX_ITERATIONS = 3


class GoalStoreError(RuntimeError):
    """Goal 存储的稳定错误；公开消息只包含错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GoalCriterion:
    """带稳定 ID 的一条验收条件。"""

    criterion_id: str
    text: str


@dataclass(frozen=True, slots=True)
class GoalGrader:
    """用户配置与最近一次实际 grader 的安全投影。"""

    selection: Literal["inherit", "profile"] = "inherit"
    configured_profile_id: str | None = None
    actual_profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class Goal:
    """某个 Thread 当前 Goal 的完整 projection。"""

    goal_id: str
    revision: int
    status: Literal["active", "paused", "blocked", "complete"]
    objective: str
    assumptions: tuple[str, ...]
    criteria: tuple[GoalCriterion, ...]
    note: str | None
    prior_blocker: str | None
    grader: GoalGrader
    max_iterations: int
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class GoalPending:
    """尚未由用户确认的 proposal 请求。"""

    request_id: str
    kind: Literal["create", "replace", "amend"]
    status: Literal["queued", "drafting", "clarifying", "reviewing", "ready", "failed"]
    base_goal_id: str | None
    base_revision: int | None
    input_text: str
    proposed_objective: str | None
    proposed_assumptions: tuple[str, ...]
    proposed_criteria: tuple[str, ...]
    created_at_ms: int
    updated_at_ms: int
    error_code: str | None


@dataclass(frozen=True, slots=True)
class GoalActivity:
    """不会进入 Transcript 的有界 Goal 时间线事实。"""

    activity_id: str
    kind: Literal["proposal", "lifecycle", "evaluation"]
    summary: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class GoalContinuation:
    """接受目标后 exactly-once 启动的持久续跑身份。"""

    continuation_id: str
    goal_id: str
    goal_revision: int
    reason: Literal["accepted", "amended", "resumed"]


@dataclass(frozen=True, slots=True)
class GoalInspection:
    """一次一致读取返回的 Goal 状态。"""

    goal: Goal | None
    pending: GoalPending | None
    latest_evaluation: Mapping[str, object] | None
    activities: tuple[GoalActivity, ...] = ()


@dataclass(frozen=True, slots=True)
class GoalRequestResult:
    """goal.request 的持久结果。"""

    disposition: Literal["ready", "queued"]
    pending: GoalPending


@dataclass(frozen=True, slots=True)
class GoalApplyResult:
    """proposal 被接受后的 Goal 与续跑令牌。"""

    goal: Goal
    continuation: GoalContinuation


@dataclass(frozen=True, slots=True)
class GoalMutationResult:
    """goal.mutate 的权威结果，可先 queued 后在安全边界 applied。"""

    disposition: Literal["applied", "queued"]
    goal: Goal | None
    pending: GoalPending | None
    continuation: GoalContinuation | None
    changed: bool


@dataclass(frozen=True, slots=True)
class GoalReconcileResult:
    """Run 终态或 Host 恢复后完成的单次持久收敛。"""

    goal: Goal | None
    pending: GoalPending | None
    continuation: GoalContinuation | None = None
    proposal_ready: bool = False
    changed: bool = False
    reason: str | None = None


def validate_goal_text(value: str, *, code: str = "GOAL_OBJECTIVE_INVALID") -> str:
    """校验非空且有界的用户文本，不做静默截断。"""
    if not isinstance(value, str) or not value.strip() or len(value) > GOAL_MAX_OBJECTIVE_CHARS:
        raise GoalStoreError(code)
    return value.strip()


def validate_goal_items(
    values: tuple[str, ...],
    *,
    allow_empty: bool,
    max_items: int = GOAL_MAX_CRITERIA,
    max_chars: int = GOAL_MAX_CRITERION_CHARS,
) -> tuple[str, ...]:
    """校验 proposal 列表；criteria 由调用方要求至少一项。"""
    if len(values) > max_items or (not allow_empty and not values):
        raise GoalStoreError("GOAL_CRITERIA_INVALID")
    normalized = tuple(item.strip() for item in values)
    if any(not item or len(item) > max_chars for item in normalized):
        raise GoalStoreError("GOAL_CRITERIA_INVALID")
    return normalized


def validate_goal_payload(
    objective: str,
    assumptions: tuple[str, ...],
    criteria: tuple[str, ...],
) -> None:
    """拒绝原文合计超过 12,000 字符的 proposal，不做静默截断。"""
    if len(objective) + sum(map(len, assumptions)) + sum(map(len, criteria)) > GOAL_MAX_PROPOSAL_CHARS:
        raise GoalStoreError("GOAL_CRITERIA_INVALID")


def criterion_digest(criteria: tuple[GoalCriterion, ...]) -> str:
    """按稳定 ID 与顺序计算 Run binding 使用的摘要。"""
    encoded = json.dumps(
        [asdict(item) for item in criteria],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def goal_to_wire(goal: Goal | None) -> dict[str, object] | None:
    """将 Goal 转成 Protocol projection。"""
    if goal is None:
        return None
    value = asdict(goal)
    value["assumptions"] = list(goal.assumptions)
    value["criteria"] = [asdict(item) for item in goal.criteria]
    return value


def pending_to_wire(pending: GoalPending | None) -> dict[str, object] | None:
    """将 pending 转成 Protocol projection。"""
    if pending is None:
        return None
    value = asdict(pending)
    value["proposed_assumptions"] = list(pending.proposed_assumptions)
    value["proposed_criteria"] = list(pending.proposed_criteria)
    return value


def activity_to_wire(activity: GoalActivity) -> dict[str, object]:
    """将 activity 转成 Protocol projection。"""
    return asdict(activity)
