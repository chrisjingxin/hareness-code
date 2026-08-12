"""Compose 跨包 E2E：fake StageAgent/Verification 走真实 RunCoordinator 全流程。

覆盖 happy path、真实决策、Plan revise、RED/GREEN、Verify fix、Review fix、
retry exhausted、cancel 与 Build 回归；不使用任何真实模型凭据。
"""

from __future__ import annotations

import asyncio
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


def _understanding(**overrides: Any) -> dict[str, Any]:
    return {
        "goal": "实现搜索",
        "constraints": [],
        "acceptance": ["搜索结果可排序"],
        "out_of_scope": [],
        "open_decisions": [],
        "change_kind": "feature",
        **overrides,
    }


def _plan(tasks: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
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
            }
        ],
        "relevant_pointers": ["src/search.py"],
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


def _reviewer(verdict: str = "pass", findings: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"verdict": verdict, "findings": findings or []}


def _evidence(exit_code: int = 0) -> VerificationEvidence:
    return VerificationEvidence(
        command="pytest -q tests/test_search.py",
        working_dir=".",
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

    async def run(self, request: StageRequest, observer=None) -> StageResult:
        self.calls.append(request.stage)
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

    async def run(self, request: VerificationRequest) -> VerificationEvidence:
        return self.script.pop(0)

    async def capture_workspace_changes(
        self, _resource_key: str
    ) -> WorkspaceChangesSnapshot:
        return WorkspaceChangesSnapshot(
            status_summary=" M src/search.py",
            diff="diff --git a/src/search.py b/src/search.py\n+change",
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
    *,
    stage_agent_cls: type = _FakeStageAgent,
):
    """构造真实 coordinator + 持久化 + fake 依赖并完整运行一次 Compose Run。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    stage_agent = stage_agent_cls(stage_script)
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


async def test_e2e_happy_path_completes_with_single_terminal(tmp_path: Path) -> None:
    """Understand→Plan→批准→Build(RED/GREEN)→Verify→Review→completed 全流程。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer("pass"),
            _reviewer("pass"),
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert stage_agent.calls == [
        "understand", "plan", "build",
        "requirement-reviewer", "code-reviewer",
    ]
    types = [event.type for event in events]
    assert types.count("run.completed") == 1
    assert types.count("run.failed") == 0
    assert events[-1].type == "run.completed"
    # 每个合法 transition 都有一帧 compose.state，revision 单调。
    frames = [event.payload for event in events if event.type == "compose.state"]
    revisions = [frame["revision"] for frame in frames]
    assert revisions == sorted(revisions) and revisions[-1] >= 5
    assert frames[-1]["status"] == "completed"
    summaries = [event.payload["text"] for event in events if event.type == "content.delta"]
    assert len(summaries) == 1
    assert "改动文件：1" in summaries[0]
    assert "验证：1/1 通过" in summaries[0]
    assert "Review：需求 pass；代码 pass" in summaries[0]
    assert "未解决风险：无" in summaries[0]


async def test_e2e_decision_revise_and_verify_fix_journey(tmp_path: Path) -> None:
    """产品决策提问、Plan 修改、Verify 失败→fix→Review finding→fix→完成。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(open_decisions=["数据存储用 SQLite 还是 JSON？"]),
            _understanding(),
            _plan(),
            _plan(solution="修订后的方案"),
            _task_result(task_id="task-1"),
            _task_result(task_id="fix-verify-1"),
            _reviewer("pass"),
            _reviewer("fail", [{"severity": "required", "message": "缺少大小写测试", "location": "acceptance-1"}]),
            _task_result(task_id="fix-review-1-1"),
            _reviewer("pass"),
            _reviewer("pass"),
        ],
        [_evidence(exit_code=1), _evidence(exit_code=0), _evidence(exit_code=0)],
        [
            {"answers": {"question-1": ["使用 SQLite"]}},
            {"answers": {"question-1": ["增加端到端测试"]}},
            {"answers": {"question-1": ["approve"]}},
        ],
    )
    assert events[-1].type == "run.completed"
    assert [event.type for event in events].count("run.completed") == 1
    assert [r.type for r in interactions.requests] == ["question", "question", "question"]
    assert stage_agent.calls.count("build") == 3
    assert stage_agent.calls.count("requirement-reviewer") == 2
    assert stage_agent.calls.count("code-reviewer") == 2


async def test_e2e_budget_exhausted_blocks_with_evidence(tmp_path: Path) -> None:
    """Verify 与 Review 预算耗尽各自 blocked，保留失败 evidence。"""
    events, stage_agent, verification, interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _task_result(task_id="fix-verify-1"),
            _task_result(task_id="fix-verify-2"),
        ],
        [_evidence(exit_code=1), _evidence(exit_code=1), _evidence(exit_code=1)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_BLOCKED"
    frames = [event.payload for event in events if event.type == "compose.state"]
    assert frames[-1]["status"] == "blocked"
    assert frames[-1]["evidence"][-1]["status"] == "failed"


async def test_e2e_build_mode_regression(tmp_path: Path) -> None:
    """Build 模式在加入 Compose 后行为无回归。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)

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

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=noop_runtime,
        interaction_port=_ScriptedInteraction([]),
    )
    execution = await coordinator.start(
        StartRun(thread_id="thread-1", run_id="run-1", message="hello", mode="build"),
        ConnectionRef("owner"),
    )
    events = [event async for event in execution.events]
    await coordinator.close()
    await persistence.close()
    assert [event.type for event in events] == [
        "run.started", "run.progress", "content.delta", "run.completed",
    ]
    assert events[0].payload["mode"] == "build"
    assert [event.type for event in events].count("run.completed") == 1


async def test_e2e_run_projection_persisted_with_single_terminal(tmp_path: Path) -> None:
    """每次 transition 的 projection 持久化到审计表，终态计数唯一。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    stage_agent = _FakeStageAgent([
        _understanding(),
        _plan(),
        _task_result(task_id="task-1"),
        _reviewer("pass"),
        _reviewer("pass"),
    ])
    verification = _FakeVerification([_evidence(exit_code=0)])
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

    store = persistence.compose_artifact_store()
    persisted = await store.load_run("run-1")
    assert persisted is not None
    assert persisted.status.value == "completed"
    frames = [event.payload for event in events if event.type == "compose.state"]
    assert persisted.revision == frames[-1]["revision"]  # 最后一帧已持久化
    assert await store.terminal_count("run-1") == 1
    # 终态摘要进入 Transcript（用户消息 + 最终可见 assistant 结果）。
    records = await persistence.load_transcript("thread-1")
    kinds = [record.kind for record in records]
    assert kinds == ["user", "assistant"]
    assert "Compose completed" in records[-1].payload["content"]
    await persistence.close()


async def test_e2e_stage_infrastructure_failure_converges_stable_code(tmp_path: Path) -> None:
    """stage 执行基础设施失败收敛为 COMPOSE_STAGE_EXECUTION_FAILED，不泄漏原始异常。"""
    class _ExplodingAgent:
        async def run(self, request: StageRequest, observer=None) -> StageResult:
            raise RuntimeError("engine exploded: secret detail")

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)

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
            stage_agent=_ExplodingAgent(),
            method_assets={"understand": "u", "plan": "p"},
            workspace_root=str(project),
            now_ms=lambda: 42,
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=noop_runtime,
        interaction_port=_ScriptedInteraction([]),
        compose_services_provider=compose_services,
    )
    execution = await coordinator.start(
        StartRun(thread_id="thread-1", run_id="run-1", message="实现搜索功能", mode="compose"),
        ConnectionRef("owner"),
    )
    events = [event async for event in execution.events]
    await coordinator.close()
    await persistence.close()
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error"]["code"] == "COMPOSE_STAGE_EXECUTION_FAILED"
    assert "secret detail" not in str(events[-1].payload["error"])


async def test_e2e_cancel_during_understand_keeps_completed_activity(tmp_path: Path) -> None:
    """早期阶段取消：唯一 cancelled 终态，已完成阶段活动保留，无迟到事件。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)
    gate = asyncio.Event()

    class _BlockingUnderstand:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, request: StageRequest, observer=None) -> StageResult:
            self.calls.append(request.stage)
            if request.stage == "understand":
                await gate.wait()  # 模拟 Understand 长时间处理
            return StageResult(
                execution_id=f"exec-{len(self.calls)}",
                agent_id=request.stage,
                status="completed",
                output=_understanding(),
            )

    stage_agent = _BlockingUnderstand()
    interactions = _ScriptedInteraction([])

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
                "code-review": "REVIEW-METHOD",
            },
            workspace_root=str(project),
            verification=None,
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
    for _ in range(200):
        if gate.is_set() or stage_agent.calls.count("understand") > 0:
            break
        await asyncio.sleep(0.01)
    assert stage_agent.calls.count("understand") == 1
    cancelled = await coordinator.cancel(execution.ref, ConnectionRef("owner"))
    assert cancelled.cancelled is True
    events = [event async for event in execution.events]
    await coordinator.close()

    # 唯一 cancelled 终态，之后没有迟到事件。
    assert [event.type for event in events].count("run.cancelled") == 1
    assert events[-1].type == "run.cancelled"
    assert [event.type for event in events].count("run.completed") == 0
    frames = [event.payload for event in events if event.type == "compose.state"]
    assert frames[-1]["status"] == "cancelled"
    assert frames[-1]["stage"] == "understand"

    # 已完成阶段的活动保留（有界审计不随取消清空）。
    store = persistence.compose_artifact_store()
    persisted = await store.load_run("run-1")
    assert persisted is not None and persisted.status.value == "cancelled"
    assert await store.terminal_count("run-1") == 1
    # Understand 未产出任何已校验结果：审计不伪造阶段摘要或 Tool 记录。
    records = await persistence.compose_activity_store().list_for_run("run-1")
    assert tuple(records) == ()
    # 终态摘要进入 Transcript 但仅此一条，取消不产生迟到活动。
    transcript = await persistence.load_transcript("thread-1")
    assert [record.kind for record in transcript] == ["user", "assistant"]
    assert "Compose cancelled" in transcript[-1].payload["content"]
    await persistence.close()


class _ToolEmittingAgent(_FakeStageAgent):
    """Builder 阶段模拟 stream 工具事件，验证 Tool 终态进入 activity 审计。"""

    async def run(self, request: StageRequest, observer=None) -> StageResult:
        self.calls.append(request.stage)
        item = self.script.pop(0)
        if observer is not None:
            observer.bind_execution(
                execution_id=f"exec-{len(self.calls)}",
                parent_execution_id="root-execution",
                agent_id=request.stage,
            )
            observer.emit(
                "tool.started",
                {"tool_call_id": "call-1", "name": "execute"},
            )
            observer.emit(
                "tool.completed",
                {
                    "tool_call_id": "call-1",
                    "result": {
                        "content": "cmd-output-marker",
                        "is_error": False,
                        "truncated": False,
                        "original_bytes": 8,
                    },
                },
            )
            # 与 _StageStreamPorts 在 tool.completed 时的行为一致：有界终态入审计。
            observer.record_tool_terminal(tool_name="execute", status="completed")
        if not isinstance(item, dict):
            raise ValueError("STAGE_OUTPUT_NOT_OBJECT")
        return StageResult(
            execution_id=f"exec-{len(self.calls)}",
            agent_id=request.stage,
            status="completed",
            output=item,
        )


async def test_e2e_thread_reopen_restores_bounded_activities(tmp_path: Path) -> None:
    """Thread reopen 恢复有界阶段/验证摘要，不恢复 artifact 或污染 Transcript。"""
    events, _stage_agent, _verification, _interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer("pass"),
            _reviewer("pass"),
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
    )
    assert events[-1].type == "run.completed"
    home = tmp_path / "home"
    project = tmp_path / "project"
    reopened = await ThreadPersistence.open(project=project, home=home)
    try:
        opened = await reopened.open_thread("thread-1")
        activities = list(opened.compose_activities)
        # 五阶段摘要 + verify tool terminal 按 Host sequence 恢复。
        kinds = [(item["stage"], item["kind"]) for item in activities]
        assert ("understand", "summary") in kinds
        assert ("plan", "summary") in kinds
        assert ("build", "summary") in kinds
        assert ("verify", "summary") in kinds
        assert ("verify", "tool_terminal") in kinds
        assert ("review", "summary") in kinds
        sequences = [int(item["event_sequence"]) for item in activities]
        assert sequences == sorted(sequences)
        # 有界审计：不包含 artifact JSON、diff 或完整 Tool 输出。
        blob = "".join(str(item) for item in activities)
        assert "diff --git" not in blob
        assert '"acceptance"' not in blob
        assert "思考" not in blob and "reasoning" not in blob.lower()
        # 恢复数据不进入模型 context：Transcript 只有用户输入与最终 assistant 结果。
        records = await reopened.load_transcript("thread-1")
        assert [record.kind for record in records] == ["user", "assistant"]
    finally:
        await reopened.close()


async def test_e2e_stage_tool_terminal_persisted_and_reopened(tmp_path: Path) -> None:
    """Builder 工具终态进入有界审计，Thread reopen 可恢复且不带工具正文。"""
    events, _stage_agent, _verification, _interactions = await _run_compose(
        tmp_path,
        [
            _understanding(),
            _plan(),
            _task_result(task_id="task-1"),
            _reviewer("pass"),
            _reviewer("pass"),
        ],
        [_evidence(exit_code=0)],
        [{"answers": {"question-1": ["approve"]}}],
        stage_agent_cls=_ToolEmittingAgent,
    )
    assert events[-1].type == "run.completed"
    home = tmp_path / "home"
    project = tmp_path / "project"
    reopened = await ThreadPersistence.open(project=project, home=home)
    try:
        opened = await reopened.open_thread("thread-1")
        activities = list(opened.compose_activities)
        build_terminals = [
            item for item in activities
            if item["stage"] == "build" and item["kind"] == "tool_terminal"
        ]
        assert len(build_terminals) == 1
        terminal = build_terminals[0]
        assert terminal["label"] == "execute"
        assert terminal["status"] == "completed"
        assert terminal["activity_id"]
        assert terminal["attempt"] >= 1
        # 工具正文（3 passed）与 artifact JSON 不落库。
        blob = "".join(str(item) for item in activities)
        assert "cmd-output-marker" not in blob
        assert "remaining_issue" not in blob
    finally:
        await reopened.close()
