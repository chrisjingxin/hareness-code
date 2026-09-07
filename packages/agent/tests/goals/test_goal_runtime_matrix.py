"""goal-backed 矩阵、blocked 恢复、配置冻结与 undo 失效。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_agent.config.config import ConfigError, GoalSettings, ModelProfile, ModelSettings
from harness_agent.goals.context import (
    goal_run_binding,
    is_goal_backed_run,
    resolve_goal_grader_identity,
)
from harness_agent.goals.models import GoalCriterion, evaluation_to_wire
from harness_agent.threads.thread_persistence import ThreadPersistence


@pytest.mark.parametrize(
    ("mode", "input_kind", "status", "plan", "expected"),
    [
        ("build", "user", "active", False, True),
        ("build", "goal_continuation", "active", False, True),
        ("build", "goal_proposal", "active", False, False),
        ("build", "user", "active", True, False),
        ("build", "user", "paused", False, False),
        ("build", "user", "blocked", False, False),
        ("build", "user", "complete", False, False),
        ("compose", "user", "active", False, False),
        ("direct_shell", "user", "active", False, False),
        ("build", "goal_continuation", "active", True, False),
    ],
)
def test_goal_backed_matrix(
    mode: str,
    input_kind: str,
    status: str,
    plan: bool,
    expected: bool,
) -> None:
    assert is_goal_backed_run(
        mode=mode,
        input_kind=input_kind,
        goal_status=status,
        plan_constrained=plan,
    ) is expected


def test_goal_run_binding_consumes_config_iterations_and_grader_profile() -> None:
    from harness_agent.goals.models import Goal, GoalGrader

    goal = Goal(
        goal_id="goal-1",
        revision=1,
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
    binding = goal_run_binding(
        goal,
        actual_primary_profile_id="fast",
        settings=GoalSettings(grader_model="pro", max_iterations=7),
        actual_grader_profile_id="pro",
        grader_fingerprint="a" * 64,
        goal_backed=True,
    )
    assert binding.max_iterations == 7
    assert binding.grader_selection == "profile"
    assert binding.actual_grader_profile_id == "pro"
    assert binding.grader_fingerprint == "a" * 64


def _profile(profile_id: str, model: str, *, api_key: str | None = "secret") -> ModelProfile:
    return ModelProfile(
        profile_id=profile_id,
        settings=ModelSettings(model, "https://gateway.example/v1", api_key=api_key),
        source="test",
    )


def test_resolve_goal_grader_inherit_uses_actual_primary_fingerprint() -> None:
    config = SimpleNamespace(
        goal=GoalSettings(),
        require_model_profile=lambda _profile_id=None: (_ for _ in ()).throw(
            AssertionError("inherit 不得解析独立 grader profile")
        ),
    )
    selection, profile_id, fingerprint = resolve_goal_grader_identity(
        config,
        actual_primary_profile_id="fast",
        actual_primary_fingerprint="b" * 64,
    )
    assert selection == "inherit"
    assert profile_id == "fast"
    assert fingerprint == "b" * 64


def test_resolve_goal_grader_profile_does_not_leak_endpoint_or_key() -> None:
    pro = _profile("pro", "pro-model")
    config = SimpleNamespace(
        goal=GoalSettings(grader_model="pro", max_iterations=4),
        require_model_profile=lambda profile_id=None: pro if profile_id == "pro" else (_ for _ in ()).throw(
            ConfigError(f"MODEL_PROFILE_NOT_FOUND: {profile_id}")
        ),
    )
    selection, profile_id, fingerprint = resolve_goal_grader_identity(
        config,
        actual_primary_profile_id="fast",
        actual_primary_fingerprint="b" * 64,
    )
    assert selection == "profile"
    assert profile_id == "pro"
    assert fingerprint != "b" * 64
    assert len(fingerprint) == 64
    assert "gateway.example" not in fingerprint
    assert "secret" not in fingerprint


def test_resolve_goal_grader_unavailable_does_not_fallback_to_primary() -> None:
    config = SimpleNamespace(
        goal=GoalSettings(grader_model="missing"),
        require_model_profile=lambda _profile_id=None: (_ for _ in ()).throw(
            ConfigError("MODEL_PROFILE_NOT_FOUND: missing")
        ),
    )
    with pytest.raises(ConfigError, match="GOAL_GRADER_MODEL_UNAVAILABLE"):
        resolve_goal_grader_identity(
            config,
            actual_primary_profile_id="fast",
            actual_primary_fingerprint="b" * 64,
        )


def test_resolve_goal_grader_missing_api_key_is_unavailable() -> None:
    config = SimpleNamespace(
        goal=GoalSettings(grader_model="pro"),
        require_model_profile=lambda _profile_id=None: _profile("pro", "pro-model", api_key=None),
    )
    with pytest.raises(ConfigError, match="GOAL_GRADER_MODEL_UNAVAILABLE"):
        resolve_goal_grader_identity(
            config,
            actual_primary_profile_id="fast",
            actual_primary_fingerprint="b" * 64,
        )


def test_evaluation_wire_strips_stale_flag() -> None:
    assert evaluation_to_wire({"evaluation_id": "eval-1", "result": "satisfied", "stale": True}) == {
        "evaluation_id": "eval-1",
        "result": "satisfied",
    }


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
async def test_blocked_user_message_reactivates_and_projects_prior_blocker(tmp_path: Path) -> None:
    persistence, store, goal = await _accepted_goal(tmp_path)
    try:
        blocked = await store.block_goal(
            thread_id="thread-1",
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            note="缺少部署凭据",
            now_ms=10,
        )
        activated = await store.activate_from_blocked(
            thread_id="thread-1",
            goal_id=blocked.goal_id,
            goal_revision=blocked.revision,
            now_ms=11,
        )
        assert activated.status == "active"
        assert activated.prior_blocker == "缺少部署凭据"
        cleared = await store.clear_prior_blocker(
            thread_id="thread-1",
            goal_id=activated.goal_id,
            goal_revision=activated.revision,
            now_ms=12,
        )
        assert cleared.prior_blocker is None
        assert cleared.status == "active"
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_undo_stales_evaluations_and_reopens_complete_goal(tmp_path: Path) -> None:
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
            now_ms=20,
        )
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
            now_ms=21,
        )
        assert completed is not None
        await store.invalidate_for_undo(thread_id="thread-1", after_ms=15, now_ms=30)
        snapshot = await store.inspect("thread-1")
        assert snapshot.goal is not None
        assert snapshot.goal.status == "active"
        assert snapshot.latest_evaluation is not None
        assert snapshot.latest_evaluation.get("stale") is True
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
            now_ms=31,
        ) is None
    finally:
        await persistence.close()


@pytest.mark.asyncio
async def test_undo_transaction_stales_goal_and_redo_does_not_restore_satisfied(
    tmp_path: Path,
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
            now_ms=20,
        )
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
            now_ms=21,
        )
        assert completed is not None
        await persistence.set_thread_reverted_turn(
            "thread-1",
            "turn-1",
            undo_mode="both",
            goal_invalidate_after_ms=15,
            now_ms=30,
        )
        snapshot = await store.inspect("thread-1")
        assert snapshot.goal is not None
        assert snapshot.goal.status == "active"
        assert snapshot.latest_evaluation is not None
        assert snapshot.latest_evaluation.get("stale") is True
        assert await persistence.get_thread_reverted_turn("thread-1") == "turn-1"

        await persistence.set_thread_reverted_turn("thread-1", None)
        restored = await store.inspect("thread-1")
        assert restored.goal is not None
        assert restored.goal.status == "active"
        assert restored.latest_evaluation is not None
        assert restored.latest_evaluation.get("stale") is True
        assert restored.goal.completed_at_ms is None
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
            now_ms=40,
        ) is None
    finally:
        await persistence.close()
