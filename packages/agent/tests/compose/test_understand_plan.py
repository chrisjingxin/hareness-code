"""Compose Understand → Plan → 用户确认 tracer bullet 测试。

使用 fake StageAgent 与 scripted Interaction 走真实 RunCoordinator：
验证 fresh stage execution、artifact 校验/重试、question 回写、
批准/修改/取消门禁、revision 递增 projection 与唯一终态。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness_agent.compose.models import ComposeTask, PlanArtifact, UnderstandingArtifact
from harness_agent.compose.stage_agents import StageRequest, StageResult
from harness_agent.compose.state_machine import ComposeStateMachine
from harness_agent.compose.workflow import ComposeServices
from harness_agent.host.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    RunCoordinator,
    RunError,
    RunPreparation,
    RunRuntime,
    StartRun,
)
from harness_agent.threads.thread_persistence import ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding


def _understanding(**overrides: Any) -> dict[str, Any]:
    return {
        "goal": "实现搜索",
        "constraints": ["不引入新依赖"],
        "acceptance": ["搜索结果可排序", "搜索不区分大小写"],
        "out_of_scope": ["索引构建"],
        "open_decisions": [],
        "change_kind": "feature",
        **overrides,
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


def _plan(**overrides: Any) -> dict[str, Any]:
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
            },
            {
                "id": "task-2",
                "title": "补充文档",
                "kind": "docs",
                "acceptance": "文档已更新",
                "depends_on": ["task-1"],
                "verification_commands": [],
            },
        ],
        "relevant_pointers": ["src/search.py"],
        **overrides,
    }


class _FakeStageAgent:
    """按脚本依次返回 stage 输出；记录每次 stage 调用。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[str] = []
        self.tasks: list[str] = []

    async def run(self, request: StageRequest) -> StageResult:
        self.calls.append(request.stage)
        self.tasks.append(request.task)
        item = self.script.pop(0)
        if not isinstance(item, dict):
            # 与真实 port 一致：不可解析输出在 stage 边界抛 ValueError。
            raise ValueError("STAGE_OUTPUT_NOT_OBJECT")
        return StageResult(
            execution_id=f"exec-{len(self.calls)}",
            agent_id=request.stage,
            status="completed",
            output=item,
        )


class _ScriptedInteraction:
    """按脚本依次回答 question/approval，并记录收到的请求。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    async def request(self, _owner, _run, interaction) -> InteractionResult:
        self.requests.append(interaction)
        response = self.responses.pop(0)
        return InteractionResult(response)


async def _run_compose(
    tmp_path: Path,
    stage_script: list[dict[str, Any]],
    interaction_responses: list[dict[str, Any]],
):
    """构造真实 coordinator + 持久化 + fake stage/interaction 并运行一次 Compose Run。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    stage_agent = _FakeStageAgent(stage_script)
    interactions = _ScriptedInteraction(interaction_responses)
    now = iter(range(1_000_000, 1_000_000 + 100))

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
                "understand": "测试方法资产",
                "plan": "测试方法资产",
                "build": "BUILD-METHOD",
                "tdd": "TDD-METHOD",
                "debug": "DEBUG-METHOD",
            },
            workspace_root=str(project),
            now_ms=lambda: next(now),
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=noop_runtime,
        interaction_port=interactions,
        compose_services_provider=compose_services,
    )
    execution = await coordinator.start(
        StartRun(
            thread_id="thread-1",
            run_id="run-1",
            message="实现搜索功能",
            mode="compose",
        ),
        ConnectionRef("owner"),
    )
    events = [event async for event in execution.events]
    await coordinator.close()
    await persistence.close()
    return events, stage_agent, interactions


def _state_frames(events) -> list[dict[str, Any]]:
    """取出全部 compose.state payload。"""
    return [
        event.payload
        for event in events
        if event.type == "compose.state"
    ]


async def test_happy_path_understands_plans_and_waits_at_gate(tmp_path: Path) -> None:
    """简单请求产出短 artifact、无访谈；批准后进入 Build 边界（WP4 停点）。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _task_result(task_id="task-2", red_evidence=""),
        ],
        [{"decision": "approve_once"}],
    )
    types = [event.type for event in events]
    assert types[0] == "run.started"
    assert events[0].payload["mode"] == "compose"
    assert types[-1] == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_VERIFICATION_UNAVAILABLE"
    assert types.count("run.completed") == 0
    assert types.count("run.failed") == 1

    # 批准前只有 understand/plan；批准后才出现 Builder 写入。
    assert stage_agent.calls == ["understand", "plan", "build", "build"]
    assert [request.type for request in interactions.requests] == ["approval"]
    assert interactions.requests[0].payload["decisions"] == [
        "approve_once",
        "reject_with_feedback",
        "reject",
    ]

    frames = _state_frames(events)
    revisions = [frame["revision"] for frame in frames]
    assert revisions == sorted(revisions)
    assert frames[0] == {
        "revision": 0,
        "stage": "understand",
        "status": "running",
        "stages": [
            {"id": "understand", "status": "running", "attempts": 1},
            {"id": "plan", "status": "pending", "attempts": 0},
            {"id": "build", "status": "pending", "attempts": 0},
            {"id": "verify", "status": "pending", "attempts": 0},
            {"id": "review", "status": "pending", "attempts": 0},
        ],
        "tasks": [],
        "evidence": [],
        "blocked_reason": None,
    }
    assert frames[-1]["stage"] == "verify"
    assert frames[-1]["tasks"][0] == {"id": "task-1", "title": "实现搜索", "status": "passed"}
    assert "goal" not in frames[-1] and "prompt" not in str(frames[-1])


async def test_open_decisions_are_asked_and_rebuilt_into_artifact(tmp_path: Path) -> None:
    """真实产品决策走 question，答案回写后重建 artifact，不消耗 schema retry。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(open_decisions=["数据存储用 SQLite 还是 JSON 文件？"]),
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _task_result(task_id="task-2", red_evidence=""),
        ],
        [
            {"answers": {"question-1": ["使用 SQLite"]}},
            {"decision": "approve_once"},
        ],
    )
    assert [request.type for request in interactions.requests] == ["question", "approval"]
    question = interactions.requests[0]
    assert question.payload["questions"][0]["question"] == "数据存储用 SQLite 还是 JSON 文件？"
    assert "使用 SQLite" in stage_agent.tasks[1]

    # question 回写不消耗 schema-invalid 重试：attempts 保持 1。
    frames = _state_frames(events)
    understand_attempts = [frame["stages"][0]["attempts"] for frame in frames]
    assert max(understand_attempts) == 1
    assert stage_agent.calls == ["understand", "understand", "plan", "build", "build"]


async def test_plan_revise_with_feedback_returns_to_plan(tmp_path: Path) -> None:
    """修改必须携带 feedback 并回到 Plan；修订后的方案再次进入门禁。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _plan(solution="修订后的方案"),
            _task_result(task_id="task-1"),
            _task_result(task_id="task-2", red_evidence=""),
        ],
        [
            {"decision": "reject_with_feedback", "feedback": "增加端到端测试"},
            {"decision": "approve_once"},
        ],
    )
    assert stage_agent.calls == ["understand", "plan", "plan", "build", "build"]
    assert "增加端到端测试" in stage_agent.tasks[2]
    frames = _state_frames(events)
    assert any(frame["status"] == "waiting_user" for frame in frames)
    assert frames[-1]["stage"] == "verify"
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_VERIFICATION_UNAVAILABLE"


async def test_plan_reject_cancels_run_with_single_terminal(tmp_path: Path) -> None:
    """拒绝整体方案产生唯一 cancelled 终态。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [_understanding(), _plan()],
        [{"decision": "reject"}],
    )
    assert events[-1].type == "run.cancelled"
    frames = _state_frames(events)
    assert frames[-1]["status"] == "cancelled"
    assert frames[-1]["stage"] == "plan"
    assert [event.type for event in events].count("run.cancelled") == 1


async def test_schema_invalid_retries_once_then_fails(tmp_path: Path) -> None:
    """artifact schema invalid 只重试一次；仍无效以稳定错误码 failed。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            {"goal": "", "acceptance": []},  # 非法 Understanding
            {"goal": "", "acceptance": []},
        ],
        [],
    )
    assert stage_agent.calls == ["understand", "understand"]
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_ARTIFACT_INVALID"
    frames = _state_frames(events)
    assert [frame["stages"][0]["attempts"] for frame in frames] == [1, 2, 2]


async def test_malformed_stage_output_is_treated_as_schema_invalid(tmp_path: Path) -> None:
    """stage Agent 输出不可解析 JSON 时按 schema invalid 处理并重试。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            "这不是 JSON",
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _task_result(task_id="task-2", red_evidence=""),
        ],
        [{"decision": "approve_once"}],
    )
    assert stage_agent.calls == ["understand", "understand", "plan", "build", "build"]
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_VERIFICATION_UNAVAILABLE"


async def test_plan_artifact_with_placeholder_is_rejected(tmp_path: Path) -> None:
    """Plan 含占位符视为 schema invalid，一次重试后仍无效则 failed。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(solution="先用 {{TODO}} 实现"),
            _plan(solution="先用 {{TODO}} 实现"),
        ],
        [],
    )
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_ARTIFACT_INVALID"
