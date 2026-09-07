"""独立 grader 验收、覆盖校验与 Completion Guard。"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_agent.goals.context import goal_run_binding
from harness_agent.goals.models import GoalCriterion, GoalStoreError, evaluation_to_wire
from harness_agent.goals.rubric_adapter import (
    canonical_rubric,
    grader_tool_names,
    normalize_rubric_event,
    validate_criterion_coverage,
)
from harness_agent.threads.thread_persistence import ThreadPersistence


def test_canonical_rubric_uses_stable_criterion_ids() -> None:
    text = canonical_rubric(
        (
            GoalCriterion("criterion-1", "改代码"),
            GoalCriterion("criterion-2", "跑测试"),
        )
    )
    assert text == "- criterion-1: 改代码\n- criterion-2: 跑测试"


def test_coverage_rejects_missing_duplicate_unknown_and_contradiction() -> None:
    expected = ("criterion-1", "criterion-2")
    assert validate_criterion_coverage(
        expected,
        [{"name": "criterion-1", "passed": True}, {"name": "criterion-2", "passed": True}],
        result="satisfied",
    ) == (
        ("criterion-1", True, None),
        ("criterion-2", True, None),
    )
    with pytest.raises(GoalStoreError, match="GOAL_EVALUATION_INVALID"):
        validate_criterion_coverage(
            expected,
            [{"name": "criterion-1", "passed": True}],
            result="satisfied",
        )
    with pytest.raises(GoalStoreError, match="GOAL_EVALUATION_INVALID"):
        validate_criterion_coverage(
            expected,
            [
                {"name": "criterion-1", "passed": True},
                {"name": "criterion-1", "passed": True},
                {"name": "criterion-2", "passed": True},
            ],
            result="satisfied",
        )
    with pytest.raises(GoalStoreError, match="GOAL_EVALUATION_INVALID"):
        validate_criterion_coverage(
            expected,
            [
                {"name": "criterion-1", "passed": True},
                {"name": "unknown", "passed": True},
            ],
            result="satisfied",
        )
    with pytest.raises(GoalStoreError, match="GOAL_EVALUATION_INVALID"):
        validate_criterion_coverage(
            expected,
            [
                {"name": "criterion-1", "passed": True, "gap": "仍缺测试"},
                {"name": "criterion-2", "passed": True},
            ],
            result="satisfied",
        )


def test_coverage_accepts_descriptive_and_numbered_names() -> None:
    expected = ("criterion-1", "criterion-2")
    criteria_objs = (
        GoalCriterion("criterion-1", "实现泛型 LRUCache 核心类"),
        GoalCriterion("criterion-2", "编写全面的单元测试用例"),
    )

    # 1. 带 criterion-X 前缀的描述
    assert validate_criterion_coverage(
        expected,
        [
            {"name": "criterion-1: 实现泛型 LRUCache 核心类", "passed": True},
            {"name": "[criterion-2] 编写单元测试", "passed": True},
        ],
        result="satisfied",
    ) == (
        ("criterion-1", True, None),
        ("criterion-2", True, None),
    )

    # 2. 带序号前缀的描述 (1., 2.)
    assert validate_criterion_coverage(
        expected,
        [
            {"name": "1. 实现泛型 LRUCache 核心类", "passed": True},
            {"name": "2: 编写全面的单元测试用例", "passed": True},
        ],
        result="satisfied",
    ) == (
        ("criterion-1", True, None),
        ("criterion-2", True, None),
    )

    # 3. 纯文本描述（通过 expected_criteria 匹配）
    assert validate_criterion_coverage(
        expected,
        [
            {"name": "实现泛型 LRUCache 核心类", "passed": True},
            {"name": "编写全面的单元测试用例", "passed": True},
        ],
        result="satisfied",
        expected_criteria=criteria_objs,
    ) == (
        ("criterion-1", True, None),
        ("criterion-2", True, None),
    )


def test_evaluation_to_wire_strips_internal_digest() -> None:
    internal = {
        "evaluation_id": "eval-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "run_id": "run-1",
        "grading_run_id": "grade-1",
        "iteration": 1,
        "result": "satisfied",
        "explanation": "验收通过",
        "criteria": [{"criterion_id": "criterion-1", "passed": True, "gap": None}],
        "grader_profile_id": "fast",
        "created_at_ms": 1000,
        "criteria_digest": "abcdef123456",
        "internal_secret": "do_not_leak",
    }
    wire = evaluation_to_wire(internal)
    assert wire is not None
    assert "criteria_digest" not in wire
    assert "internal_secret" not in wire
    assert wire["evaluation_id"] == "eval-1"
    assert wire["result"] == "satisfied"
    assert evaluation_to_wire(None) is None


def test_normalize_rubric_event_maps_iteration_and_terminal_cap() -> None:
    checking = normalize_rubric_event(
        {
            "type": "rubric_evaluation_start",
            "grading_run_id": "grade-1",
            "iteration": 0,
        },
        goal_id="goal-1",
        goal_revision=1,
        max_iterations=3,
        expected_ids=("criterion-1",),
    )
    assert checking is not None
    assert checking["phase"] == "checking"
    assert checking["iteration"] == 1
    assert "result" not in checking

    terminal = normalize_rubric_event(
        {
            "type": "rubric_evaluation_end",
            "grading_run_id": "grade-1",
            "iteration": 2,
            "result": "needs_revision",
            "explanation": "仍未满足",
            "criteria": [{"name": "criterion-1", "passed": False, "gap": "缺测试"}],
        },
        goal_id="goal-1",
        goal_revision=1,
        max_iterations=3,
        expected_ids=("criterion-1",),
    )
    assert terminal is not None
    assert terminal["phase"] == "result"
    assert terminal["iteration"] == 3
    assert terminal["result"] == "max_iterations_reached"
    assert terminal["criteria"] == [
        {"criterion_id": "criterion-1", "passed": False, "gap": "缺测试"}
    ]


def test_grader_error_event_is_sanitized() -> None:
    event = normalize_rubric_event(
        {
            "type": "rubric_evaluation_end",
            "grading_run_id": "grade-1",
            "iteration": 0,
            "result": "grader_error",
            "explanation": "Grader raised TimeoutError (HTTP 401): sk-secret sqlite:///tmp/db",
            "criteria": [],
        },
        goal_id="goal-1",
        goal_revision=1,
        max_iterations=3,
        expected_ids=("criterion-1",),
    )
    assert event is not None
    assert event["result"] == "grader_error"
    assert event["explanation"] == "独立验收执行失败"
    assert "sk-secret" not in event["explanation"]


def test_grader_tools_are_a_subset_of_primary_and_exclude_mutation() -> None:
    names = grader_tool_names(
        ("ls", "read_file", "glob", "grep", "write_file", "edit_file", "execute", "task", "memory_save")
    )
    assert names <= {"ls", "read_file", "glob", "grep", "rerun_verification"}
    assert "write_file" not in names
    assert "task" not in names
    assert "memory_save" not in names
    assert "rerun_verification" in names


def test_goal_run_binding_freezes_grader_identity_and_iterations() -> None:
    from harness_agent.goals.models import Goal, GoalGrader

    goal = Goal(
        goal_id="goal-1",
        revision=2,
        status="active",
        objective="完成登录",
        assumptions=(),
        criteria=(GoalCriterion("criterion-1", "测试通过"),),
        note=None,
        prior_blocker=None,
        grader=GoalGrader(),
        max_iterations=3,
        created_at_ms=1,
        updated_at_ms=2,
        completed_at_ms=None,
    )
    binding = goal_run_binding(goal, actual_primary_profile_id="fast")
    assert binding.goal_backed is True
    assert binding.grader_selection == "inherit"
    assert binding.actual_grader_profile_id == "fast"
    assert binding.max_iterations == 3
    assert len(binding.grader_fingerprint) == 64


async def _accepted_goal(tmp_path: Path, thread_id: str = "thread-1"):
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=tmp_path)
    store = persistence.goal_store()
    await store.request(
        thread_id=thread_id,
        request_id="request-1",
        kind="create",
        input_text="完成任务",
        expected_goal_id=None,
        expected_revision=None,
        ready=True,
        now_ms=1,
    )
    await store.save_proposal(
        request_id="request-1",
        objective="完成任务",
        assumptions=(),
        criteria=("改代码", "跑测试"),
        now_ms=2,
    )
    applied = await store.apply_proposal(
        request_id="request-1",
        criteria=("改代码", "跑测试"),
        now_ms=3,
    )
    return persistence, store, applied.goal


@pytest.mark.asyncio
async def test_record_evaluation_then_completion_guard_completes(tmp_path: Path) -> None:
    persistence, store, goal = await _accepted_goal(tmp_path)
    try:
        recorded = await store.record_evaluation(
            thread_id="thread-1",
            evaluation_id="eval-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            run_id="run-1",
            grading_run_id="grade-1",
            iteration=1,
            result="satisfied",
            explanation="全部验收条件已通过",
            criteria=(
                {"criterion_id": "criterion-1", "passed": True, "gap": None},
                {"criterion_id": "criterion-2", "passed": True, "gap": None},
            ),
            grader_profile_id="fast",
            criteria_digest=goal_run_binding(goal, actual_primary_profile_id="fast").criteria_digest,
            now_ms=10,
        )
        assert recorded["result"] == "satisfied"
        completed = await store.commit_completion(
            thread_id="thread-1",
            run_id="run-1",
            grading_run_id="grade-1",
            evaluation_id="eval-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            criteria_digest=recorded["criteria_digest"],
            run_completed=True,
            goal_backed=True,
            now_ms=11,
        )
        assert completed is not None
        assert completed.status == "complete"
        assert (await store.inspect("thread-1")).goal.status == "complete"
    finally:
        await persistence.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_completed": False},
        {"goal_backed": False},
        {"goal_revision": 99},
        {"criteria_digest": "0" * 64},
        {"grading_run_id": "other-grade"},
    ],
)
async def test_completion_guard_rejects_each_required_condition(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    persistence, store, goal = await _accepted_goal(tmp_path)
    try:
        recorded = await store.record_evaluation(
            thread_id="thread-1",
            evaluation_id="eval-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            run_id="run-1",
            grading_run_id="grade-1",
            iteration=1,
            result="satisfied",
            explanation="全部验收条件已通过",
            criteria=(
                {"criterion_id": "criterion-1", "passed": True, "gap": None},
                {"criterion_id": "criterion-2", "passed": True, "gap": None},
            ),
            grader_profile_id="fast",
            criteria_digest=goal_run_binding(goal, actual_primary_profile_id="fast").criteria_digest,
            now_ms=10,
        )
        params = {
            "thread_id": "thread-1",
            "run_id": "run-1",
            "grading_run_id": "grade-1",
            "evaluation_id": "eval-1",
            "goal_id": goal.goal_id,
            "goal_revision": goal.revision,
            "criteria_digest": recorded["criteria_digest"],
            "run_completed": True,
            "goal_backed": True,
            "now_ms": 11,
        }
        params.update(kwargs)
        assert await store.commit_completion(**params) is None
        assert (await store.inspect("thread-1")).goal.status == "active"
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_pending_amend_blocks_completion(tmp_path: Path) -> None:
    persistence, store, goal = await _accepted_goal(tmp_path)
    try:
        recorded = await store.record_evaluation(
            thread_id="thread-1",
            evaluation_id="eval-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            run_id="run-1",
            grading_run_id="grade-1",
            iteration=1,
            result="satisfied",
            explanation="全部验收条件已通过",
            criteria=(
                {"criterion_id": "criterion-1", "passed": True, "gap": None},
                {"criterion_id": "criterion-2", "passed": True, "gap": None},
            ),
            grader_profile_id="fast",
            criteria_digest=goal_run_binding(goal, actual_primary_profile_id="fast").criteria_digest,
            now_ms=10,
        )
        await store.request(
            thread_id="thread-1",
            request_id="request-2",
            kind="amend",
            input_text="再加文档",
            expected_goal_id=goal.goal_id,
            expected_revision=goal.revision,
            ready=True,
            now_ms=11,
        )
        assert await store.commit_completion(
            thread_id="thread-1",
            run_id="run-1",
            grading_run_id="grade-1",
            evaluation_id="eval-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            criteria_digest=recorded["criteria_digest"],
            run_completed=True,
            goal_backed=True,
            now_ms=12,
        ) is None
        assert (await store.inspect("thread-1")).goal.status == "active"
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_update_goal_complete_is_request_only_and_blocked_cas(tmp_path: Path) -> None:
    persistence, store, goal = await _accepted_goal(tmp_path)
    try:
        await store.request_completion(
            thread_id="thread-1",
            run_id="run-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            note="我认为完成了",
            now_ms=10,
        )
        assert (await store.inspect("thread-1")).goal.status == "active"
        blocked = await store.block_goal(
            thread_id="thread-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            note="缺少部署凭据",
            now_ms=11,
        )
        assert blocked.status == "blocked"
        assert blocked.note == "缺少部署凭据"
        assert (await store.inspect("thread-1")).goal.status == "blocked"
    finally:
        await persistence.close()


def test_rerun_verification_rejects_unknown_and_unsafe_commands() -> None:
    from harness_agent.goals.verification import VerificationEvidenceRegistry

    registry = VerificationEvidenceRegistry()
    with pytest.raises(GoalStoreError, match="GOAL_VERIFICATION_FORBIDDEN"):
        registry.rerun("missing")
    evidence_id = registry.record(
        command="pytest -q",
        cwd="/workspace",
        sandbox="workspace",
        env=(),
    )
    replay = registry.lookup(evidence_id)
    assert replay is not None
    assert replay.command == "pytest -q"
    with pytest.raises(GoalStoreError, match="GOAL_VERIFICATION_FORBIDDEN"):
        registry.record(command="rm -rf /", cwd="/workspace", sandbox="workspace", env=())


def test_create_rubric_middleware_injects_chinese_system_prompt() -> None:
    from unittest.mock import MagicMock
    from harness_agent.goals.rubric_adapter import (
        GRADER_CHINESE_SYSTEM_PROMPT,
        create_rubric_middleware,
    )

    fake_model = MagicMock()
    middleware = create_rubric_middleware(fake_model, max_iterations=3)
    assert middleware._system_prompt == GRADER_CHINESE_SYSTEM_PROMPT
    assert "请始终使用中文输出" in middleware._system_prompt

