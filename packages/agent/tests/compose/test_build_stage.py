"""Compose Build stage 测试：task 依赖顺序、TDD/direct 选择、Debug 注入与预算。

使用 fake StageAgent 走真实 RunCoordinator：验证 Builder 只拿到当前 task
的 ContextPack、行为/Bug/refactor 必须记录 RED evidence、失败后注入
Debug 方法资产、attempt 耗尽收敛 blocked，模型不能自行进入 Verify。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harness_agent.compose.stage_agents import StageRequest, StageResult
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
        "acceptance": ["搜索结果可排序"],
        "out_of_scope": [],
        "open_decisions": [],
        "change_kind": "feature",
    }


def _plan(*, tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "solution": "新增 search module",
        "tasks": tasks
        or [
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


class _FakeStageAgent:
    """按脚本依次返回 stage 输出；记录 stage 与任务正文。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[str] = []
        self.tasks: list[str] = []

    async def run(self, request: StageRequest, observer=None) -> StageResult:
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


class _ScriptedInteraction:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    async def request(self, _owner, _run, interaction) -> InteractionResult:
        self.requests.append(interaction)
        return InteractionResult(self.responses.pop(0))


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
    return [event.payload for event in events if event.type == "compose.state"]


def _task_ids(task_text: str) -> list[str]:
    """从任务正文中找出被执行的 task id。"""
    found = [line for line in task_text.splitlines() if line.strip().startswith("## 任务")]
    return found


async def test_build_runs_tasks_in_order_and_stops_at_verify_boundary(tmp_path: Path) -> None:
    """behavior 任务强制 RED、docs 任务 direct；全部完成后停在 Verify 边界。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),          # behavior + RED
            _task_result(task_id="task-2", red_evidence=""),  # docs direct
        ],
        [{"answers": {"question-1": ["approve"]}}],
    )
    # Builder 只执行了 build stage 的 task-1/task-2，顺序正确。
    assert stage_agent.calls == ["understand", "plan", "build", "build"]
    assert "task-1" in stage_agent.tasks[2] and "task-2" in stage_agent.tasks[3]
    # TDD 方法只注入 behavior 任务，direct 任务不注入。
    assert "TDD-METHOD" in stage_agent.tasks[2]
    assert "TDD-METHOD" not in stage_agent.tasks[3]

    frames = _state_frames(events)
    assert frames[-1]["stage"] == "verify"
    assert frames[-1]["tasks"] == [
        {"id": "task-1", "title": "实现搜索", "status": "passed"},
        {"id": "task-2", "title": "补充文档", "status": "passed"},
    ]
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_VERIFICATION_UNAVAILABLE"


async def test_tdd_task_without_red_evidence_gets_debug_retry(tmp_path: Path) -> None:
    """行为任务缺 RED evidence 视为失败；重试注入 Debug 方法。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(tasks=[
                {
                    "id": "task-1",
                    "title": "修复搜索",
                    "kind": "bug",
                    "acceptance": "搜索不崩溃",
                    "depends_on": [],
                    "verification_commands": ["pytest -q tests/test_search.py"],
                },
            ]),
            _task_result(task_id="task-1", red_evidence=""),  # 缺 RED
            _task_result(task_id="task-1"),                   # Debug 后成功
        ],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert stage_agent.calls == ["understand", "plan", "build", "build"]
    assert "DEBUG-METHOD" in stage_agent.tasks[3]
    assert "TDD-METHOD" in stage_agent.tasks[2]
    frames = _state_frames(events)
    assert frames[-1]["tasks"][0]["status"] == "passed"


async def test_task_attempt_budget_exhausted_blocks_run(tmp_path: Path) -> None:
    """task 连续失败耗尽 attempt 预算后收敛 blocked，不能进入 Verify。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(tasks=[
                {
                    "id": "task-1",
                    "title": "实现搜索",
                    "kind": "behavior",
                    "acceptance": "搜索返回结果",
                    "depends_on": [],
                    "verification_commands": ["pytest -q tests/test_search.py"],
                },
            ]),
            _task_result(task_id="task-1", remaining_issue="实现失败"),
            _task_result(task_id="task-1", remaining_issue="仍然失败"),
        ],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert stage_agent.calls == ["understand", "plan", "build", "build"]
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_BLOCKED"
    frames = _state_frames(events)
    assert frames[-1]["status"] == "blocked"
    assert frames[-1]["tasks"][0]["status"] == "failed"


async def test_malformed_builder_output_retries_then_fails_attempt(tmp_path: Path) -> None:
    """malformed result 结构化重试一次；仍无效则消耗 attempt 并 Debug 重试。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(tasks=[
                {
                    "id": "task-1",
                    "title": "实现搜索",
                    "kind": "behavior",
                    "acceptance": "搜索返回结果",
                    "depends_on": [],
                    "verification_commands": ["pytest -q tests/test_search.py"],
                },
            ]),
            "不是 JSON",
            "仍然不是 JSON",
            _task_result(task_id="task-1"),
        ],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert stage_agent.calls == ["understand", "plan", "build", "build", "build"]
    assert "DEBUG-METHOD" in stage_agent.tasks[4]
    frames = _state_frames(events)
    assert frames[-1]["tasks"][0]["status"] == "passed"


async def test_dependency_order_prevents_second_task_without_first(tmp_path: Path) -> None:
    """task-1 失败时依赖它的 task-2 不会被启动。"""
    events, stage_agent, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1", remaining_issue="失败"),
            _task_result(task_id="task-1", remaining_issue="失败"),
        ],
        [{"answers": {"question-1": ["approve"]}}],
    )
    build_calls = [
        text for text in stage_agent.tasks if "## 任务" in text and "BUILD-METHOD" in text
    ]
    assert "task-2" not in "".join(build_calls)
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_BLOCKED"


async def test_cancel_during_build_produces_single_cancelled_terminal(tmp_path: Path) -> None:
    """Build 执行中取消产生唯一 cancelled 终态。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    gate = __import__("asyncio").Event()

    class _BlockingBuilder:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.tasks: list[str] = []

        async def run(self, request: StageRequest, observer=None) -> StageResult:
            self.calls.append(request.stage)
            self.tasks.append(request.task)
            if request.stage == "build":
                await gate.wait()  # 模拟 Builder 长时间执行
                output: dict[str, Any] = _task_result(task_id="task-1")
            elif request.stage == "understand":
                output = _understanding()
            else:
                output = _plan()
            return StageResult(
                execution_id=f"exec-{len(self.calls)}",
                agent_id=request.stage,
                status="completed",
                output=output,
            )

    stage_agent = _BlockingBuilder()
    interactions = _ScriptedInteraction([{"answers": {"question-1": ["approve"]}}])

    async def persistence_provider() -> ThreadPersistence:
        return persistence

    async def preparation_provider(_command, _persistence) -> RunPreparation:
        return RunPreparation(execution_binding=make_test_binding("thread-1", "run-1"))

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
            },
            workspace_root=str(project),
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
        StartRun(thread_id="thread-1", run_id="run-1", message="实现搜索", mode="compose"),
        ConnectionRef("owner"),
    )
    # 等待进入 Build 且 Builder 已阻塞。
    for _ in range(200):
        if gate.is_set() or stage_agent.calls.count("build") > 0:
            break
        await __import__("asyncio").sleep(0.01)
    assert stage_agent.calls.count("build") == 1
    cancelled = await coordinator.cancel(execution.ref, ConnectionRef("owner"))
    assert cancelled.cancelled is True
    events = [event async for event in execution.events]
    await coordinator.close()
    await persistence.close()
    assert [event.type for event in events].count("run.cancelled") == 1
    assert events[-1].type == "run.cancelled"
    frames = [event.payload for event in events if event.type == "compose.state"]
    assert frames[-1]["stage"] == "build"
    assert frames[-1]["status"] == "cancelled"
    summaries = [event.payload["text"] for event in events if event.type == "content.delta"]
    assert len(summaries) == 1
    assert "Compose cancelled" in summaries[0]
