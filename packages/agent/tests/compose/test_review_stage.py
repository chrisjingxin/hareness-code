"""Compose Review 阶段测试：双轴独立 Reviewer、finding 修复回路与完成判定。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harness_agent.compose.models import VerificationEvidence
from harness_agent.compose.stage_agents import StageRequest, StageResult
from harness_agent.compose.verification import (
    VerificationRequest,
    WorkspaceChangesSnapshot,
)
from harness_agent.compose.workflow import ComposeServices
from harness_agent.host.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    RunCoordinator,
    RunPreparation,
    RunRuntime,
    StartRun,
)
from harness_agent.threads.thread_persistence import ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding


def _understanding() -> dict[str, Any]:
    return {
        "goal": "实现搜索",
        "constraints": [],
        "acceptance": ["搜索结果可排序", "搜索不区分大小写"],
        "out_of_scope": [],
        "open_decisions": [],
        "change_kind": "feature",
    }


def _plan() -> dict[str, Any]:
    return {
        "solution": "新增 search module",
        "tasks": [
            {
                "id": "task-1",
                "title": "实现搜索",
                "kind": "behavior",
                "acceptance": "搜索返回结果",
                "depends_on": [],
                "verification_commands": ["pytest -q tests/test_search.py"],
            }
        ],
        "relevant_pointers": ["src/search.py"],
    }


def _task_result(*, task_id: str = "task-1", **overrides: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "changed_paths": ["src/search.py"],
        "focused_test_evidence": "pytest -q tests/test_search.py 通过",
        "red_evidence": "先写测试：test_search 失败（RED）",
        "remaining_issue": "",
        **overrides,
    }


def _reviewer(*, verdict: str = "pass", findings: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"verdict": verdict, "findings": findings or []}


def _required(severity: str = "required", message: str = "缺验收证据") -> dict[str, str]:
    return {"severity": severity, "message": message, "location": "acceptance-1"}


def _evidence(exit_code: int = 0) -> VerificationEvidence:
    return VerificationEvidence(
        command="pytest -q tests/test_search.py",
        working_dir="packages/agent",
        started_at_ms=1,
        finished_at_ms=2,
        exit_code=exit_code,
        output_digest="a" * 64,
        output_summary="3 passed" if exit_code == 0 else "1 failed",
        truncated=False,
    )


class _FakeStageAgent:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[str] = []
        self.tasks: list[str] = []

    async def run(self, request: StageRequest) -> StageResult:
        self.calls.append(request.stage)
        self.tasks.append(request.task)
        item = self.script.pop(0)
        if not isinstance(item, dict):
            raise ValueError("STAGE_OUTPUT_NOT_OBJECT")
        return StageResult(
            execution_id=f"exec-{len(self.calls)}",
            agent_id=request.stage,
            status="completed",
            output=item,
        )


class _FakeVerification:
    def __init__(self, script: list[VerificationEvidence]) -> None:
        self.script = list(script)
        self.workspace_change_requests: list[str] = []

    async def run(self, request: VerificationRequest) -> VerificationEvidence:
        return self.script.pop(0)

    async def capture_workspace_changes(
        self, resource_key: str
    ) -> WorkspaceChangesSnapshot:
        self.workspace_change_requests.append(resource_key)
        return WorkspaceChangesSnapshot(
            status_summary=" M src/search.py\n?? src/unreported.py",
            diff="diff --git a/src/search.py b/src/search.py\n+actual change",
        )


class _ScriptedInteraction:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    async def request(self, _owner, _run, interaction) -> InteractionResult:
        self.requests.append(interaction)
        return InteractionResult(self.responses.pop(0))


async def _run_compose(
    tmp_path: Path,
    stage_script: list[Any],
    verification_script: list[VerificationEvidence],
    interaction_responses: list[dict[str, Any]],
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    stage_agent = _FakeStageAgent(stage_script)
    verification = _FakeVerification(verification_script)
    interactions = _ScriptedInteraction(interaction_responses)

    async def persistence_provider() -> ThreadPersistence:
        return persistence

    async def preparation_provider(_command, _persistence) -> RunPreparation:
        return RunPreparation(
            execution_binding=make_test_binding("thread-1", "run-1"),
        )

    async def noop_runtime(_run) -> RunRuntime:
        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    async def compose_services() -> ComposeServices | None:
        return ComposeServices(
            stage_agent=stage_agent,
            method_assets={
                "understand": "u", "plan": "p", "build": "BUILD-METHOD",
                "tdd": "TDD-METHOD", "debug": "DEBUG-METHOD",
                "code-review": "REVIEW-METHOD",
            },
            workspace_root=str(project),
            verification=verification,
            now_ms=lambda: 42,
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=noop_runtime,
        interaction_port=interactions,
        compose_services_provider=compose_services,
    )
    execution = await coordinator.start(
        StartRun(thread_id="thread-1", run_id="run-1", message="实现搜索功能", mode="compose"),
        ConnectionRef("owner"),
    )
    events = [event async for event in execution.events]
    await coordinator.close()
    await persistence.close()
    return events, stage_agent, verification, interactions


def _state_frames(events) -> list[dict[str, Any]]:
    return [event.payload for event in events if event.type == "compose.state"]


async def test_review_pass_completes_run_with_single_terminal(tmp_path: Path) -> None:
    """双轴 Review 通过后 Run 进入唯一 completed 终态。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="pass"),
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert stage_agent.calls == [
        "understand", "plan", "build",
        "requirement-reviewer", "code-reviewer",
    ]
    assert events[-1].type == "run.completed"
    assert events[-1].payload["finish_reason"] == "completed"
    assert [event.type for event in events].count("run.completed") == 1
    frames = _state_frames(events)
    assert frames[-1]["status"] == "completed"
    assert frames[-1]["stage"] == "review"
    reviewer_packs = [
        task
        for stage, task in zip(stage_agent.calls, stage_agent.tasks, strict=True)
        if stage in {"requirement-reviewer", "code-reviewer"}
    ]
    assert all("?? src/unreported.py" in pack for pack in reviewer_packs)
    assert all("+actual change" in pack for pack in reviewer_packs)
    assert len(verification.workspace_change_requests) == 1


async def test_optional_findings_do_not_block_completion(tmp_path: Path) -> None:
    """Optional/Nit finding 进入最终报告但不阻止完成。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer(verdict="pass", findings=[_required("nit", "可以拆小函数")]),
            _reviewer(verdict="pass", findings=[_required("optional", "建议补注释")]),
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.completed"
    summaries = [event.payload["text"] for event in events if event.type == "content.delta"]
    assert len(summaries) == 1
    assert "未解决风险：可以拆小函数；建议补注释" in summaries[0]


async def test_fail_verdict_without_required_finding_never_completes(
    tmp_path: Path,
) -> None:
    """Reviewer 明确 fail 时不能因 findings 为空而误判 completed。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer(verdict="fail"),
            _reviewer(verdict="fail"),
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_ARTIFACT_INVALID"
    assert stage_agent.calls.count("requirement-reviewer") == 2
    assert stage_agent.calls.count("code-reviewer") == 0


async def test_required_finding_fixes_and_reloops_to_completion(tmp_path: Path) -> None:
    """Required finding 生成 fix task，重新 Build→Verify→Review 后完成。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="fail", findings=[_required("required", "缺少大小写测试")]),
            _task_result(task_id="fix-review-1-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="pass"),
        ],
        [_evidence(exit_code=0), _evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.completed"
    assert stage_agent.calls.count("build") == 2
    assert stage_agent.calls.count("requirement-reviewer") == 2
    assert stage_agent.calls.count("code-reviewer") == 2
    frames = _state_frames(events)
    assert [task["id"] for task in frames[-1]["tasks"]] == ["task-1", "fix-review-1-1"]


async def test_review_fix_budget_exhausted_blocks(tmp_path: Path) -> None:
    """Review 两轮修复后仍失败进入 blocked。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="fail", findings=[_required()]),
            _task_result(task_id="fix-review-1-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="fail", findings=[_required()]),
            _task_result(task_id="fix-review-2-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="fail", findings=[_required()]),
        ],
        [_evidence(exit_code=0), _evidence(exit_code=0), _evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_BLOCKED"
    frames = _state_frames(events)
    assert frames[-1]["status"] == "blocked"
    assert "review" in (frames[-1]["blocked_reason"] or "")


async def test_reviewer_invalid_output_fails_after_retry(tmp_path: Path) -> None:
    """Reviewer schema invalid 只重试一次；仍无效以稳定错误码 failed。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            {"verdict": "maybe"},  # 非法
            {"verdict": "maybe"},
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_ARTIFACT_INVALID"
    assert stage_agent.calls.count("requirement-reviewer") == 2
    assert stage_agent.calls.count("code-reviewer") == 0


async def test_requirement_and_code_reviewers_are_independent_executions(tmp_path: Path) -> None:
    """Requirement 与 Code Reviewer 是不同 execution identity 的独立调用。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="fail", findings=[_required("critical", "绕过 workspace 边界")]),
            _task_result(task_id="fix-review-1-1"),
            _reviewer(verdict="pass"),
            _reviewer(verdict="pass"),
        ],
        [_evidence(exit_code=0), _evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert stage_agent.calls[-2] == "requirement-reviewer"
    assert stage_agent.calls[-1] == "code-reviewer"
    # critical finding 走 fix 回路，修复后完成。
    assert events[-1].type == "run.completed"


# ---------- 只读 Reviewer spec ----------


def test_restrict_spec_to_read_only_removes_all_writes_and_shell() -> None:
    """只读 spec 的能力交集不含任何写工具、Shell、MCP 或 Skill。"""
    from harness_agent.policy.capability_policy import EffectiveCapabilityView
    from harness_agent.runtime.agent_spec import (
        READ_ONLY_REVIEWER_TOOLS,
        ResolvedAgentSpec,
        restrict_spec_to_read_only,
    )
    from harness_agent.runtime.agent_catalog import EffectiveExecutionPolicy
    from harness_agent.runtime.agent_engine_profile import AgentEngineProfile

    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    policy = EffectiveExecutionPolicy(
        policy_ids=("test",), tools=None, mcp_tools=None, skills=None,
        filesystem_read=None, filesystem_write=None, shell=None, network=None,
        isolation="local", approval_mode="default",
    )
    spec = ResolvedAgentSpec(
        project_fingerprint="a" * 64,
        role="primary",
        agent_id="main",
        definition_fingerprint="b" * 64,
        model_profile_id="pro",
        model_settings=None,
        model_view=None,
        effective_policy=policy,
        capability_view=EffectiveCapabilityView(
            tool_names=("write_file", "execute", "read_file", "ls"),
            mcp_tool_names=("mcp-tool",),
            skill_ids=("skill-1",),
            filesystem_read=None,
            filesystem_write=None,
            shell_commands=None,
            policy_fingerprint=policy.fingerprint,
        ),
        tools=(_Tool("write_file"), _Tool("execute"), _Tool("read_file"), _Tool("ls")),
        skill_registry=None,
        mcp_snapshot=None,
        prompt="x",
        execution=None,
        workspace=Path("."),
        interactive=True,
        tool_view_fingerprint="c" * 64,
        skill_view_fingerprint="d" * 64,
        middleware_fingerprint="e" * 64,
        prompt_template_fingerprint="f" * 64,
        sandbox_config_fingerprint="0" * 64,
        enable_memory=True,
        enable_skills=True,
        enable_ask_user=True,
    )
    restricted = restrict_spec_to_read_only(spec)
    names = {tool.name for tool in restricted.tools}
    assert names == READ_ONLY_REVIEWER_TOOLS & {"write_file", "execute", "read_file", "ls"}
    assert "write_file" not in names and "execute" not in names
    assert restricted.capability_view.mcp_tool_names == ()
    assert restricted.capability_view.skill_ids == ()
    assert restricted.capability_view.filesystem_write is None
    assert restricted.capability_view.shell_commands is None
    assert restricted.enable_memory is False
    assert restricted.enable_skills is False
    assert restricted.enable_ask_user is False
    # 独立身份：能力视图指纹与主 spec 不同（profile key 由指纹派生）。
    assert restricted.tool_view_fingerprint != spec.tool_view_fingerprint

    from harness_agent.runtime.agent_spec import restrict_spec_to_read_only_stage

    planning = restrict_spec_to_read_only_stage(spec)
    planning_names = {tool.name for tool in planning.tools}
    assert planning.role == "stage"
    assert planning.agent_id == "compose-planning"
    assert "write_file" not in planning_names and "execute" not in planning_names
    assert planning.enable_ask_user is False
