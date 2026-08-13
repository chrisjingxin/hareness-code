"""受控 Inline/Managed delegation 的权限、状态与资源释放测试。"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from pydantic import Field

from harness_agent.runtime.agent_catalog import DelegationPolicy
from harness_agent.runtime.agent_delegation import (
    AgentDelegationError,
    AgentDelegator,
    DelegateAgent,
    DelegationTarget,
    child_execution_ref,
)
from harness_agent.runtime.agent_execution import AgentExecutionRegistry
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
)
from harness_agent.runtime.run_context import RunCancellationToken
from harness_agent.runtime.managed_agent_executor import (
    FailClosedManagedObserver,
    ManagedAgentExecutor,
    ManagedAgentRequest,
    acquire_pooled_agent_runtime,
)


class _ToolCallingModel(GenericFakeChatModel):
    """支持 DeepAgents bind_tools 的离线模型。"""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, _tools, **_kwargs) -> Runnable:
        """测试不执行真实 provider，只保留预置响应序列。"""
        return self

    def _generate(self, messages: list[BaseMessage], *args, **kwargs):
        """记录主、子 Agent 的实际消息，验证文件 Snapshot 的公开 Thread scope。"""
        self.received.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


async def _registry() -> tuple[AgentExecutionRegistry, ExecutionRef]:
    """建立一个 running 根 execution。"""
    registry = AgentExecutionRegistry()
    root = ExecutionRef.root("thread-1", "run-1")
    await registry.accept(
        AgentExecutionBinding(
            ref=root,
            agent_id="main",
            mode=ExecutionMode.MANAGED,
            depth=0,
        )
    )
    await registry.start(root)
    return registry, root


def _command(
    root: ExecutionRef,
    *,
    target: str = "general-purpose",
    token: RunCancellationToken | None = None,
    timeout: float = 1,
) -> DelegateAgent:
    """构造允许一个一层子 Agent 的派发命令。"""
    return DelegateAgent(
        parent_ref=root,
        target_agent_id=target,
        task="检查代码并返回结论",
        idempotency_key=f"call-{target}",
        delegation_policy=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose", "reviewer"),
            max_depth=1,
            max_parallelism=1,
        ),
        cancellation_token=token or RunCancellationToken(),
        timeout_seconds=timeout,
    )


async def test_system_selects_inline_and_records_child_execution() -> None:
    """模型只选 Agent ID；Inline 模式由可信 target 固定且不创建 Engine lease。"""
    registry, root = await _registry()
    calls: list[str] = []

    async def inline(command: DelegateAgent):
        calls.append(command.task)
        return {"final": "ok"}

    delegator = AgentDelegator(
        registry,
        targets=(
            DelegationTarget(
                agent_id="general-purpose",
                mode=ExecutionMode.INLINE,
                runner=inline,
                policy_fingerprint="a" * 64,
            ),
        ),
    )
    result = await delegator.execute(_command(root))

    assert result.mode is ExecutionMode.INLINE
    assert result.status is ExecutionStatus.COMPLETED
    assert result.output == {"final": "ok"}
    child = await registry.get(result.ref)
    assert child is not None
    assert child.ref.parent_execution_id == root.execution_id
    assert child.policy_fingerprint == "a" * 64
    assert child.engine_profile_key is None
    assert calls == ["检查代码并返回结论"]


async def test_managed_runner_releases_lease_on_failure() -> None:
    """Managed adapter 的异常路径仍必须由 runner finally 释放实际 Engine lease。"""
    registry, root = await _registry()
    released = asyncio.Event()

    async def managed(_command: DelegateAgent):
        try:
            raise RuntimeError("managed failed")
        finally:
            released.set()

    delegator = AgentDelegator(
        registry,
        targets=(
            DelegationTarget(
                agent_id="reviewer",
                mode=ExecutionMode.MANAGED,
                runner=managed,
                engine_profile_key="b" * 64,
                policy_fingerprint="c" * 64,
            ),
        ),
    )
    with pytest.raises(AgentDelegationError, match="RuntimeError") as caught:
        await delegator.execute(_command(root, target="reviewer"))
    assert caught.value.code == "DELEGATION_EXECUTION_FAILED"
    assert released.is_set()
    children = await registry.list(root)
    assert children[-1].status is ExecutionStatus.FAILED


async def test_managed_adapter_reuses_profile_engine_and_releases_each_run() -> None:
    """相同 Managed spec 经 executor 复用 Pool 图，并释放每次 delegation lease。"""
    from dataclasses import replace

    from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool
    from harness_agent.runtime.agent_engine_profile import AgentEngineProfile, ModelRoleBinding

    registry, root = await _registry()
    fingerprint = "e" * 64
    profile = AgentEngineProfile(
        project_fingerprint=fingerprint,
        topology_id="agent",
        topology_version=1,
        model_roles=(ModelRoleBinding("reviewer", fingerprint),),
        tool_catalog_fingerprint=fingerprint,
        skill_catalog_fingerprint=fingerprint,
        mcp_config_fingerprint=fingerprint,
        sandbox_config_fingerprint=fingerprint,
        policy_fingerprint=fingerprint,
        middleware_fingerprint=fingerprint,
        prompt_template_fingerprint=fingerprint,
        agent_id="reviewer",
        definition_fingerprint=fingerprint,
    )
    builds = 0

    class _StreamingGraph:
        async def astream(self, *_args, **_kwargs):
            yield ("messages", (AIMessage(content="reviewed"), {}))

    def build(requested):
        nonlocal builds
        builds += 1
        return AgentEngine(profile=requested, graph=_StreamingGraph())

    pool = AgentEnginePool(build)

    async def invoke(command: DelegateAgent):
        child_ref = child_execution_ref(command)

        async def acquire_runtime():
            return await acquire_pooled_agent_runtime(
                pool=pool,
                profile=profile,
                run_context=None,
                graph_config=lambda namespace: {
                    "configurable": {
                        "thread_id": child_ref.thread_id,
                        "checkpoint_ns": namespace,
                    }
                },
            )

        result = await ManagedAgentExecutor().execute(
            ManagedAgentRequest(
                execution_ref=child_ref.execution_id,
                parent_execution_ref=child_ref.parent_execution_id,
                run_id=child_ref.run_id,
                input=command.task,
                checkpoint_namespace=child_ref.checkpoint_namespace(fingerprint),
                output_policy="capture_only",
                runtime_provider=acquire_runtime,
                is_cancelled=lambda: command.cancellation_token.cancelled,
                idempotency_key=command.idempotency_key,
                timeout_seconds=command.timeout_seconds,
            ),
            FailClosedManagedObserver(),
        )
        return {"task": command.task, "final": result.final_content}

    delegator = AgentDelegator(
        registry,
        targets=(
            DelegationTarget(
                agent_id="reviewer",
                mode=ExecutionMode.MANAGED,
                runner=invoke,
                engine_profile_key=profile.profile_key,
            ),
        ),
    )
    first = _command(root, target="reviewer")
    second = replace(first, idempotency_key="call-reviewer-2")
    assert (await delegator.execute(first)).status is ExecutionStatus.COMPLETED
    assert (await delegator.execute(second)).status is ExecutionStatus.COMPLETED
    assert builds == 1
    diagnostics = await pool.diagnostics()
    assert diagnostics.active_leases == 0
    assert diagnostics.active_runs == 0
    await pool.aclose()


async def test_parent_cancellation_cancels_child_and_runner() -> None:
    """父 Run token 取消必须终止 child runner 并记录 cancelled。"""
    registry, root = await _registry()
    token = RunCancellationToken()
    cancelled = asyncio.Event()

    async def worker(_command: DelegateAgent):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {}

    delegator = AgentDelegator(
        registry,
        targets=(
            DelegationTarget(
                agent_id="general-purpose",
                mode=ExecutionMode.INLINE,
                runner=worker,
            ),
        ),
    )
    task = asyncio.create_task(delegator.execute(_command(root, token=token)))
    await asyncio.sleep(0)
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    children = await registry.list(root)
    assert children[-1].status is ExecutionStatus.CANCELLED


async def test_delegation_policy_rejects_target_and_depth_before_runner() -> None:
    """allowedAgents 与 maxDepth 不能被 target 或 Prompt 放宽。"""
    registry, root = await _registry()

    async def runner(_command: DelegateAgent):
        raise AssertionError("forbidden target reached runner")

    delegator = AgentDelegator(
        registry,
        targets=(
            DelegationTarget(
                agent_id="reviewer",
                mode=ExecutionMode.MANAGED,
                runner=runner,
                engine_profile_key="d" * 64,
            ),
        ),
    )
    forbidden = _command(root, target="reviewer")
    forbidden = DelegateAgent(
        parent_ref=forbidden.parent_ref,
        target_agent_id=forbidden.target_agent_id,
        task=forbidden.task,
        idempotency_key=forbidden.idempotency_key,
        delegation_policy=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose",),
            max_depth=0,
            max_parallelism=1,
        ),
        cancellation_token=forbidden.cancellation_token,
        timeout_seconds=forbidden.timeout_seconds,
    )
    with pytest.raises(AgentDelegationError) as caught:
        await delegator.execute(forbidden)
    assert caught.value.code == "DELEGATION_TARGET_FORBIDDEN"


async def test_production_task_tool_routes_through_execution_registry(tmp_path) -> None:
    """Inline child 使用父 Thread 调用文件工具，并登记独立 child execution。"""
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.runtime.agent_catalog import EffectiveExecutionPolicy
    from harness_agent.policy.capability_policy import (
        BUILTIN_TOOL_NAMES,
        resolve_effective_capability_view,
    )
    from harness_agent.runtime.run_context import RunContext
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
    from harness_agent.threads.snapshots import ThreadSnapshotStore

    registry, root = await _registry()
    policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        tools=None,
        mcp_tools=None,
        skills=None,
        filesystem_read=None,
        filesystem_write=None,
        shell=None,
        network=None,
        isolation="local",
        approval_mode="yolo",
        delegation=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose",),
            max_depth=1,
            max_parallelism=1,
        ),
    )
    view = resolve_effective_capability_view(
        policy,
        available_tools=BUILTIN_TOOL_NAMES,
    )
    target = tmp_path / "child.txt"
    target.write_text("child context\n", encoding="utf-8")
    snapshots = ThreadSnapshotStore()
    model = _ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "只返回 CHILD_OK",
                                "subagent_type": "general-purpose",
                            },
                            "id": "task-call-1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/child.txt", "offset": 0, "limit": 20},
                            "id": "child-read-1",
                        }
                    ],
                ),
                AIMessage(content="CHILD_OK"),
                AIMessage(content="PARENT_OK"),
            ]
        )
    )
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(tmp_path),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        capability_view=view,
        execution_registry=registry,
        snapshot_store=snapshots,
    )
    context = RunContext(
        thread_id=root.thread_id,
        run_id=root.run_id,
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id=root.thread_id,
            system_prompt="test",
            workspace=str(tmp_path),
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
        execution_id=root.execution_id,
        agent_id="main",
        cancellation_token=RunCancellationToken(),
        delegation_policy=policy.delegation,
        snapshot_store=snapshots,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="delegate")]},
        config={"configurable": {"thread_id": root.thread_id}},
        context=context,
    )

    assert result["messages"][-1].content == "PARENT_OK"
    executions = await registry.list(root)
    assert len(executions) == 2
    assert executions[-1].agent_id == "general-purpose"
    assert executions[-1].mode is ExecutionMode.INLINE
    assert executions[-1].status is ExecutionStatus.COMPLETED
    read_message = next(
        message
        for batch in model.received
        for message in batch
        if isinstance(message, ToolMessage) and message.name == "read_file"
    )
    snapshot_id = json.loads(str(read_message.content))["snapshot_id"]
    snapshots.resolve(snapshot_id, root.thread_id, "/child.txt", f"local:{tmp_path.resolve()}")


async def test_production_task_exposes_host_registered_plugin_target(tmp_path) -> None:
    """Host 注册的 Plugin Agent 通过同一个 task schema 走 Managed execution。"""
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.runtime.agent_catalog import EffectiveExecutionPolicy
    from harness_agent.policy.capability_policy import (
        BUILTIN_TOOL_NAMES,
        resolve_effective_capability_view,
    )
    from harness_agent.runtime.run_context import RunContext
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot

    registry, root = await _registry()
    policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        tools=None,
        mcp_tools=None,
        skills=None,
        filesystem_read=None,
        filesystem_write=None,
        shell=None,
        network=None,
        isolation="local",
        approval_mode="yolo",
        delegation=DelegationPolicy(
            enabled=True,
            allowed_agents=("reviewer",),
            max_depth=1,
            max_parallelism=1,
        ),
    )
    view = resolve_effective_capability_view(
        policy,
        available_tools=BUILTIN_TOOL_NAMES,
    )
    calls: list[str] = []

    async def plugin_runner(command: DelegateAgent):
        calls.append(command.task)
        return {"messages": [AIMessage(content="PLUGIN_REVIEW_OK")]}

    model = _ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "review this",
                                "subagent_type": "reviewer",
                            },
                            "id": "plugin-task-1",
                        }
                    ],
                ),
                AIMessage(content="PARENT_OK"),
            ]
        )
    )
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(tmp_path),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        capability_view=view,
        execution_registry=registry,
        delegation_targets=(
            DelegationTarget(
                agent_id="reviewer",
                mode=ExecutionMode.MANAGED,
                runner=plugin_runner,
                engine_profile_key="f" * 64,
            ),
        ),
    )
    context = RunContext(
        thread_id=root.thread_id,
        run_id=root.run_id,
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id=root.thread_id,
            system_prompt="test",
            workspace=str(tmp_path),
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
        execution_id=root.execution_id,
        agent_id="main",
        cancellation_token=RunCancellationToken(),
        delegation_policy=policy.delegation,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="delegate")]},
        config={"configurable": {"thread_id": root.thread_id}},
        context=context,
    )

    assert result["messages"][-1].content == "PARENT_OK"
    assert calls == ["review this"]
    executions = await registry.list(root)
    child = next(item for item in executions if item.agent_id == "reviewer")
    assert child.mode is ExecutionMode.MANAGED
    assert child.status is ExecutionStatus.COMPLETED
