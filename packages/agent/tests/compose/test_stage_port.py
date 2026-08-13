"""ManagedStageAgentPort 集成测试：真实 AgentDelegator 校验路径。

覆盖真实 Host 组装链（resolve spec → DelegationTarget → AgentDelegator →
delegation policy 校验 → Managed invoke），验证内置 stage id 不被
DELEGATION_TARGET_FORBIDDEN 拒绝，且 stage 不能再委派。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from harness_agent.compose.stage_agents import ManagedStageAgentPort, StageRequest
from harness_agent.runtime.agent_delegation import AgentDelegationError
from harness_agent.runtime.agent_execution import AgentExecutionRegistry
from harness_agent.runtime.execution_binding import (
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
)
from harness_agent.runtime.run_context import RunCancellationToken


class _FakeEngine:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    @property
    def graph(self) -> Any:
        return self

    async def ainvoke(self, messages: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"messages": [AIMessage(content=self.output)]}

    async def astream(self, stream_input: Any, **kwargs: Any):
        """共享 execution stream 路径：产出与 ainvoke 等价的最终消息。"""
        self.calls.append({"messages": stream_input, "kwargs": kwargs})
        yield ("messages", (AIMessage(content=self.output), {}))


class _FakeRunLease:
    def __init__(self) -> None:
        self.released = False

    async def release(self) -> None:
        self.released = True


class _FakeLease:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.released = False

    async def run(self) -> _FakeRunLease:
        return _FakeRunLease()

    async def release(self) -> None:
        self.released = True


class _FakePool:
    def __init__(self, lease: _FakeLease) -> None:
        self.lease = lease
        self.profiles: list[Any] = []

    async def acquire(self, profile: Any) -> _FakeLease:
        self.profiles.append(profile)
        return self.lease

    async def finalize_draining(self, profile_key: str) -> None:
        return None


class _FakeSpec:
    """满足 ManagedStageAgentPort 访问的最小 spec 形状。"""

    def __init__(self) -> None:
        from harness_agent.runtime.agent_catalog import EffectiveExecutionPolicy, DelegationPolicy

        self.project_fingerprint = "a" * 64
        self.agent_id = "main"
        self.prompt = "fake stage prompt"
        self.tools = ()
        self.enable_memory = False
        self.enable_skills = False
        self.definition_fingerprint = "b" * 64
        self.model_view = None
        self.policy_fingerprint = "c" * 64
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


async def _registry_with_root() -> tuple[AgentExecutionRegistry, ExecutionRef]:
    """注册并启动一个 root execution，作为 stage 的 parent。"""
    registry = AgentExecutionRegistry()
    from harness_agent.runtime.agent_delegation import AgentExecutionBinding

    root = ExecutionRef.root("thread-1", "run-1")
    await registry.accept(AgentExecutionBinding(
        ref=root,
        agent_id="main",
        mode=ExecutionMode.MANAGED,
        depth=0,
    ))
    await registry.start(root)
    return registry, root


async def test_managed_stage_port_runs_understand_through_real_delegator() -> None:
    """真实 AgentDelegator 路径：内置 stage id 不被 DELEGATION_TARGET_FORBIDDEN 拒绝。"""
    engine = _FakeEngine(output='{"goal": "实现搜索", "acceptance": ["可排序"], "open_decisions": [], "change_kind": "feature"}')
    pool = _FakePool(_FakeLease(engine))
    registry, root = await _registry_with_root()
    spec = _FakeSpec()

    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=lambda _key, *, headless=False, readonly=False, planning=False: spec,  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )
    result = await port.run(StageRequest(
        stage="work-item-task",
        task="实现搜索功能",
        parent_ref=root,
        profile_key="stage-profile",
        cancellation_token=RunCancellationToken(),
    ))
    assert result.status == "completed"
    assert result.output["goal"] == "实现搜索"
    checkpoint_ns = engine.calls[0]["kwargs"]["config"]["configurable"]["checkpoint_ns"]
    # child checkpoint namespace：fingerprint:thread:run:child-execution-id
    assert checkpoint_ns.startswith(spec.project_fingerprint)
    assert "child-" in checkpoint_ns


async def test_managed_stage_port_uses_readonly_planning_spec() -> None:
    """Understand/Plan 请求只读 planning spec，Build 才使用可写 headless spec。"""
    engine = _FakeEngine(
        output='{"goal": "实现搜索", "acceptance": ["可排序"], "open_decisions": [], "change_kind": "feature"}'
    )
    pool = _FakePool(_FakeLease(engine))
    registry, root = await _registry_with_root()
    spec = _FakeSpec()
    requests: list[tuple[bool, bool, bool]] = []

    def resolve_spec(
        _key: str,
        *,
        headless: bool = False,
        readonly: bool = False,
        planning: bool = False,
    ) -> _FakeSpec:
        requests.append((headless, readonly, planning))
        return spec

    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=resolve_spec,
        config_home=Path("."),
        workspace=Path("."),
    )
    await port.run(
        StageRequest(
            stage="work-item-task",
            task="实现搜索功能",
            parent_ref=root,
            profile_key="stage-profile",
            cancellation_token=RunCancellationToken(),
        )
    )
    assert requests == [(True, False, True)]


async def test_managed_stage_port_rejects_unbound_plugin_role_before_spec_resolution() -> None:
    """Activity 只接受固定内置角色，不能把 Plugin ID 当作 stage 自动执行。"""
    registry, root = await _registry_with_root()

    def resolve_spec(*_args: Any, **_kwargs: Any) -> _FakeSpec:
        raise AssertionError("unbound role must not resolve an Agent spec")

    port = ManagedStageAgentPort(
        registry=registry,
        pool=_FakePool(_FakeLease(_FakeEngine(output="{}"))),  # type: ignore[arg-type]
        resolve_spec=resolve_spec,
        config_home=Path("."),
        workspace=Path("."),
    )

    with pytest.raises(ValueError, match="COMPOSE_ROLE_NOT_FOUND"):
        await port.run(
            StageRequest(
                stage="plugin-reviewer",
                task="审查当前变更",
                parent_ref=root,
                profile_key="stage-profile",
                cancellation_token=RunCancellationToken(),
            )
        )


async def test_managed_stage_port_schema_retry_gets_fresh_execution() -> None:
    """同一 stage/task 重试必须重新调用模型，而不是重启已终结 execution。"""
    engine = _FakeEngine(output='{"goal": "实现搜索"}')
    pool = _FakePool(_FakeLease(engine))
    registry, root = await _registry_with_root()
    spec = _FakeSpec()
    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=lambda _key, **_kwargs: spec,  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )

    first = await port.run(
        StageRequest(
            stage="work-item-task",
            task="相同的有界 ContextPack",
            parent_ref=root,
            profile_key="stage-profile",
            cancellation_token=RunCancellationToken(),
        )
    )
    second = await port.run(
        StageRequest(
            stage="work-item-task",
            task="相同的有界 ContextPack",
            parent_ref=root,
            profile_key="stage-profile",
            cancellation_token=RunCancellationToken(),
        )
    )
    assert first.execution_id != second.execution_id
    assert len(engine.calls) == 2
    checkpoint_threads = [
        call["kwargs"]["config"]["configurable"]["thread_id"]
        for call in engine.calls
    ]
    assert checkpoint_threads[0] != checkpoint_threads[1]
    assert all(
        thread_id.startswith("thread-1:run-1:child-")
        for thread_id in checkpoint_threads
    )


class _RecordingObserver:
    """记录 bind/emit 的测试 observer。"""

    def __init__(self) -> None:
        self.execution_id: str | None = None
        self.parent_execution_id: str | None = None
        self.agent_id: str | None = None
        self.events: list[tuple[str, dict[str, object]]] = []
        self.tool_terminals: list[tuple[str, str]] = []

    def bind_execution(
        self,
        *,
        execution_id: str,
        parent_execution_id: str | None,
        agent_id: str,
    ) -> None:
        self.execution_id = execution_id
        self.parent_execution_id = parent_execution_id
        self.agent_id = agent_id

    def emit(self, event_type: str, payload: dict[str, object]) -> None:
        self.events.append((event_type, dict(payload)))

    def record_tool_terminal(self, *, tool_name: str, status: str) -> None:
        self.tool_terminals.append((tool_name, status))

    async def interact(self, request: object) -> object:
        raise AssertionError("test should not request interaction")


class _StreamingEngine:
    """产出公开 Reasoning + 正文 JSON，验证 capture_only 不发 content.delta。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def graph(self) -> Any:
        return self

    async def astream(self, stream_input: Any, **kwargs: Any):
        from langchain_core.messages import AIMessageChunk

        self.calls.append({"input": stream_input, "kwargs": kwargs})
        yield (
            "messages",
            (
                AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "公开思考"},
                ),
                {},
            ),
        )
        yield (
            "messages",
            (
                AIMessage(
                    content='{"goal": "实现搜索", "acceptance": ["可排序"], "open_decisions": [], "change_kind": "feature"}'
                ),
                {},
            ),
        )


async def test_managed_stage_port_capture_only_emits_reasoning_not_content() -> None:
    """capture_only：公开 Reasoning 经 observer，content.delta 与 artifact JSON 不进 wire。"""
    engine = _StreamingEngine()
    pool = _FakePool(_FakeLease(engine))
    registry, root = await _registry_with_root()
    spec = _FakeSpec()
    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=lambda _key, **_kwargs: spec,  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )
    observer = _RecordingObserver()
    result = await port.run(
        StageRequest(
            stage="work-item-task",
            task="实现搜索功能",
            parent_ref=root,
            profile_key="stage-profile",
            cancellation_token=RunCancellationToken(),
        ),
        observer,
    )
    assert result.output["goal"] == "实现搜索"
    assert observer.execution_id is not None
    assert observer.execution_id.startswith("child-")
    assert observer.parent_execution_id == root.execution_id
    assert observer.agent_id == "work-item-task"
    types = [event_type for event_type, _ in observer.events]
    assert "run.progress" in types
    assert "reasoning.delta" in types
    assert "content.delta" not in types
    assert all("实现搜索" not in str(payload) for _, payload in observer.events)
    assert all('{"goal"' not in str(payload) for _, payload in observer.events)
    checkpoint_ns = engine.calls[0]["kwargs"]["config"]["configurable"]["checkpoint_ns"]
    assert "child-" in checkpoint_ns


async def test_managed_stage_port_uses_post_tool_final_message() -> None:
    """Tool 前说明不污染最终 artifact，且 Tool 终态仍被 observer 记录。"""
    engine = _ToolStreamingEngine()
    pool = _FakePool(_FakeLease(engine))
    registry, root = await _registry_with_root()
    spec = _FakeSpec()
    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=lambda _key, **_kwargs: spec,  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )
    observer = _RecordingObserver()
    result = await port.run(
        StageRequest(
            stage="work-item-implement",
            task="task-1",
            parent_ref=root,
            profile_key="stage-profile",
            cancellation_token=RunCancellationToken(),
        ),
        observer,
    )
    assert result.output["task_id"] == "task-1"
    # 工具名由 TOOL_STARTED 关联；终态只记录名与状态，正文不进入记录。
    assert observer.tool_terminals == [("execute", "completed")]
    assert all("cmd-output" not in text for _, text in observer.tool_terminals)
    types = [event_type for event_type, _ in observer.events]
    assert "tool.started" in types
    assert "tool.completed" in types


class _ToolStreamingEngine:
    """产出调用前说明 → 工具结果 → 最终 artifact，复现跨模型回合正文污染。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def graph(self) -> Any:
        return self

    async def astream(self, stream_input: Any, **kwargs: Any):
        from langchain_core.messages import AIMessageChunk, ToolMessage

        self.calls.append({"input": stream_input, "kwargs": kwargs})
        yield (
            "messages",
            (
                AIMessageChunk(
                    content="我先运行测试确认当前状态。",
                    tool_call_chunks=[
                        {
                            "name": "execute",
                            "args": '{"command": "pytest -q"}',
                            "index": 0,
                            "id": "call-1",
                        }
                    ],
                ),
                {},
            ),
        )
        yield (
            "messages",
            (ToolMessage(content="cmd-output", tool_call_id="call-1", name="execute"), {}),
        )
        yield (
            "messages",
            (
                AIMessage(
                    content='{"task_id": "task-1", "changed_paths": ["src/a.py"], "focused_test_evidence": "ok", "red_evidence": "ok", "remaining_issue": ""}'
                ),
                {},
            ),
        )


async def test_managed_stage_port_rejects_stage_delegating_another_agent() -> None:
    """stage policy 只允许当前 stage id：委派其他 agent 必须被拒绝。"""
    engine = _FakeEngine(output="x")
    pool = _FakePool(_FakeLease(engine))
    registry, root = await _registry_with_root()
    spec = _FakeSpec()

    port = ManagedStageAgentPort(
        registry=registry,
        pool=pool,  # type: ignore[arg-type]
        resolve_spec=lambda _key, *, headless=False, readonly=False, planning=False: spec,  # type: ignore[arg-type]
        config_home=Path("."),
        workspace=Path("."),
    )
    # 模拟 stage 图内模型提交了指向 general-purpose 的委派：用 stage policy
    # 校验路径确认会被 DELEGATION_TARGET_FORBIDDEN 拒绝（stage 不能提权委派）。
    from harness_agent.runtime.agent_catalog import DelegationPolicy
    from harness_agent.runtime.agent_delegation import AgentDelegator, DelegateAgent

    other_target = type("Target", (), {"agent_id": "general-purpose"})()
    delegator = AgentDelegator(registry, targets=(other_target,))  # type: ignore[arg-type]
    command = DelegateAgent(
        parent_ref=root,
        target_agent_id="general-purpose",
        task="帮我写代码",
        idempotency_key="stage-delegation-attempt",
        delegation_policy=DelegationPolicy(
            enabled=True,
            allowed_agents=("work-item-task",),
            max_depth=1,
            max_parallelism=1,
        ),
        cancellation_token=RunCancellationToken(),
    )
    with pytest.raises(AgentDelegationError, match="DELEGATION_TARGET_FORBIDDEN"):
        await delegator.execute(command)
