"""诊断回归：stage Agent 返回空/非 JSON 输出时 wire 上的错误形态。

真实 ManagedStageAgentPort（fake engine）+ 真实 workflow + RunCoordinator：
验证用户看到的 `Expecting value: line 1 column 1 (char 0)` 来自哪里，
以及修复后 wire 上不再出现裸 JSONDecodeError 文本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from harness_agent.compose.stage_agents import ManagedStageAgentPort, StageRequest
from harness_agent.compose.workflow import ComposeServices
from harness_agent.host.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    RunCoordinator,
    RunPreparation,
    RunRuntime,
    StartRun,
)
from harness_agent.runtime.agent_execution import AgentExecutionRegistry
from harness_agent.runtime.execution_binding import ExecutionMode
from harness_agent.runtime.run_context import RunCancellationToken
from harness_agent.threads.thread_persistence import ThreadPersistence
from tests.support.thread_fixtures import test_binding as make_test_binding


class _EmptyFinalEngine:
    """模拟真实图返回空正文的最后一条消息（模型未产出文本）。"""

    async def ainvoke(self, messages: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"messages": [AIMessage(content="")]}

    @property
    def graph(self) -> "_EmptyFinalEngine":
        return self


class _FakeRunLease:
    async def release(self) -> None:
        return None


class _FakeLease:
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    async def run(self) -> _FakeRunLease:
        return _FakeRunLease()

    async def release(self) -> None:
        return None


class _FakePool:
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    async def acquire(self, profile: Any) -> _FakeLease:
        return _FakeLease(self.engine)

    async def finalize_draining(self, profile_key: str) -> None:
        return None


class _FakeSpec:
    """ManagedStageAgentPort 需要的最小 spec 形状。"""

    def __init__(self) -> None:
        from harness_agent.runtime.agent_catalog import (
            DelegationPolicy,
            EffectiveExecutionPolicy,
        )

        self.project_fingerprint = "a" * 64
        self.agent_id = "main"
        self.prompt = "fake stage prompt"
        self.tools = ()
        self.enable_memory = False
        self.enable_skills = False
        self.definition_fingerprint = "b" * 64
        self.model_view = None
        self.runtime_profile = type("Profile", (), {
            "profile_key": "stage-profile",
            "mcp_config_fingerprint": "d" * 64,
            "policy_fingerprint": "c" * 64,
            "definition_fingerprint": "b" * 64,
        })()
        self.skill_registry = type("Registry", (), {
            "snapshot_id": "e" * 64,
            "system_prompt_fragment": lambda: "skills",
        })()
        self.mcp_snapshot = None
        self.workspace = Path(".")
        self.execution = type("Exec", (), {
            "approval_mode": "default",
            "mode": "local",
            "sandbox_enabled": False,
        })()
        self.effective_policy = EffectiveExecutionPolicy(
            policy_ids=("builtin-main",),
            tools=None, mcp_tools=None, skills=None,
            filesystem_read=None, filesystem_write=None, shell=None, network=None,
            isolation="local", approval_mode="default",
            delegation=DelegationPolicy(
                enabled=True,
                allowed_agents=("general-purpose",),
                max_depth=1,
                max_parallelism=4,
            ),
        )


async def _run_compose_with_real_port(tmp_path: Path, engine: Any):
    """真实 ManagedStageAgentPort + workflow + coordinator 完整跑一次 Compose Run。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    persistence = await ThreadPersistence.open(project=project, home=home)

    class _Interaction:
        async def request(self, _owner, _run, interaction) -> InteractionResult:
            return InteractionResult({"answers": {"question-1": ["approve"]}})

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

    spec = _FakeSpec()
    registry = AgentExecutionRegistry()
    stage_port = ManagedStageAgentPort(
        registry=registry,
        pool=_FakePool(engine),  # type: ignore[arg-type]
        resolve_spec=lambda _key, *, headless=False, readonly=False: spec,  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )

    async def compose_services() -> ComposeServices | None:
        return ComposeServices(
            stage_agent=stage_port,
            method_assets={
                "understand": "u", "plan": "p", "build": "B",
                "tdd": "T", "debug": "D", "code-review": "R",
            },
            workspace_root=str(project),
            now_ms=lambda: 42,
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=noop_runtime,
        interaction_port=_Interaction(),
        compose_services_provider=compose_services,
        execution_registry=registry,
    )
    execution = await coordinator.start(
        StartRun(thread_id="thread-1", run_id="run-1", message="实现搜索", mode="compose"),
        ConnectionRef("owner"),
    )
    events = [event async for event in execution.events]
    await coordinator.close()
    await persistence.close()
    return events


async def test_empty_stage_output_converges_readable_failure(tmp_path: Path) -> None:
    """空 final 不把裸 JSONDecodeError 文本放到 wire 上。"""
    events = await _run_compose_with_real_port(tmp_path, _EmptyFinalEngine())
    failed = [event for event in events if event.type == "run.failed"]
    assert len(failed) == 1
    error = failed[0].payload["error"]
    # 稳定错误码 + 可读消息；不得包含裸解析器文本。
    assert error["code"] == "COMPOSE_ARTIFACT_INVALID"
    assert "Expecting value" not in error["message"]
    assert "输出为空" in error["message"]


class _ChattyFinalEngine:
    """模拟模型在 JSON 前后附加了解释文字（真实常见形态）。"""

    async def ainvoke(self, messages: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"messages": [AIMessage(content='好的，我来实现搜索功能：\n{"goal": "实现搜索"}')]}

    @property
    def graph(self) -> "_ChattyFinalEngine":
        return self


async def test_chatty_non_json_output_converges_readable_failure(tmp_path: Path) -> None:
    """带解释文字的非 JSON 输出给出可读错误，且不含输出原文。"""
    events = await _run_compose_with_real_port(tmp_path, _ChattyFinalEngine())
    failed = [event for event in events if event.type == "run.failed"]
    assert len(failed) == 1
    error = failed[0].payload["error"]
    assert error["code"] == "COMPOSE_ARTIFACT_INVALID"
    assert "Expecting value" not in error["message"]
    assert "不是有效 JSON" in error["message"]
    # 输出原文（含解释文字）不越过 wire。
    assert "我来实现搜索功能" not in error["message"]
