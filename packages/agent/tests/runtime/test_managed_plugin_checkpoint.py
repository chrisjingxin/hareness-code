"""Managed Plugin child 的真实 SQLite checkpoint 隔离回归测试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import Runnable
from pydantic import Field

from harness_agent.runtime.agent import _compiled_subagent_state, create_harness_agent
from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool
from harness_agent.runtime.agent_engine_profile import AgentEngineProfile, ModelRoleBinding
from harness_agent.runtime.execution_binding import ExecutionMode, ExecutionRef
from harness_agent.runtime.managed_agent_executor import (
    FailClosedManagedObserver,
    FinalOutputGateDecision,
    ManagedAgentExecutionError,
    ManagedAgentExecutor,
    ManagedAgentRequest,
    acquire_pooled_agent_runtime,
)
from harness_agent.runtime.run_context import RunCancellationToken, RunContext
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
from harness_agent.threads.context_projection import ContextProjector
from harness_agent.threads.deferred_store import ThreadDeferredToolStore
from harness_agent.threads.thread_persistence import TranscriptAppend, ThreadPersistence


class _RecordingModel(GenericFakeChatModel):
    """记录真实模型边界收到的消息，响应完全来自离线序列。"""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, _tools: Iterable[Any], **_kwargs: Any) -> Runnable:
        """保留 fake model，使 DeepAgents 仍按真实 bind_tools 路径构图。"""
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        """覆盖非流式 fallback，避免测试遗漏模型输入记录。"""
        self.received.append(list(messages))
        return super()._generate(messages, *args, **kwargs)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        """以单个完整 chunk 模拟离线 provider。"""
        self.received.append(list(messages))
        message = next(self.messages)
        response = message if isinstance(message, AIMessage) else AIMessage(content=message)
        chunk = AIMessageChunk(
            content=response.content,
            tool_calls=response.tool_calls,
            id=response.id,
        )
        chunk.chunk_position = "last"
        yield ChatGenerationChunk(message=chunk)


class _FailingModel(_RecordingModel):
    """在模型边界抛出离线异常，验证失败路径也清理 child checkpoint。"""

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        """记录输入后立即失败，不接触真实 provider。"""
        self.received.append(list(messages))
        raise RuntimeError("CHILD_MODEL_FAILURE_MARKER")
        if False:
            yield ChatGenerationChunk(message=AIMessageChunk(content="unreachable"))


def _profile(project_fingerprint: str) -> AgentEngineProfile:
    """构造不依赖真实 provider 的可复用 Engine Profile。"""
    fingerprint = "a" * 64
    return AgentEngineProfile(
        project_fingerprint=project_fingerprint,
        topology_id="managed-plugin-test",
        topology_version=1,
        model_roles=(ModelRoleBinding("za38-frontend-executor", fingerprint),),
        tool_catalog_fingerprint=fingerprint,
        skill_catalog_fingerprint=fingerprint,
        mcp_config_fingerprint=fingerprint,
        sandbox_config_fingerprint=fingerprint,
        policy_fingerprint=fingerprint,
        middleware_fingerprint=fingerprint,
        prompt_template_fingerprint=fingerprint,
        agent_id="za38-frontend-executor",
        definition_fingerprint=fingerprint,
    )


def _context(
    workspace: Path,
    *,
    thread_id: str,
    run_id: str,
    execution_id: str,
    checkpoint_thread_id: str | None = None,
) -> RunContext:
    """创建带公开父身份的 child RunContext。"""
    return RunContext(
        thread_id=thread_id,
        run_id=run_id,
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id=thread_id,
            system_prompt="managed plugin test",
            workspace=str(workspace),
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
        checkpoint_thread_id=checkpoint_thread_id,
        execution_id=execution_id,
        agent_id="za38-frontend-executor",
        execution_mode=ExecutionMode.MANAGED,
        cancellation_token=RunCancellationToken(),
        deferred_tool_store=ThreadDeferredToolStore(),
    )


async def _seed_parent_checkpoint(
    persistence: ThreadPersistence,
    graph: Any,
    *,
    thread_id: str,
    run_id: str,
) -> None:
    """通过 canonical Transcript → Projector → SQLite saver 写入父历史。"""
    from tests.support.thread_fixtures import accept_thread

    await accept_thread(persistence, thread_id, "PARENT_HISTORY_MARKER", run_id=run_id)
    await persistence.append_transcript_batch(
        (
            TranscriptAppend(
                thread_id=thread_id,
                record_id="parent-assistant",
                kind="assistant",
                content="PARENT_ASSISTANT_MARKER",
                run_id=run_id,
            ),
        )
    )
    projection = await ContextProjector(persistence).project(thread_id)
    await ContextProjector(persistence).sync_cache(graph, thread_id, projection=projection)


async def _checkpoint_rows(
    persistence: ThreadPersistence,
    checkpoint_thread_id: str,
) -> tuple[Any, ...]:
    """读取指定内部 thread 的真实 SQLite checkpoint 行。"""
    rows: list[Any] = []
    async for checkpoint in persistence.checkpointer.alist(
        {
            "configurable": {
                "thread_id": checkpoint_thread_id,
                "checkpoint_ns": persistence.project_fingerprint,
            }
        }
    ):
        rows.append(checkpoint)
    return tuple(rows)


def test_execution_checkpoint_thread_id_is_stable_and_execution_scoped() -> None:
    """内部根图身份稳定且不把公开父身份暴露给 checkpoint 路由。"""
    first = ExecutionRef(
        thread_id="parent-thread",
        run_id="run-a",
        execution_id="child-a",
        parent_execution_id="root-parent",
    )
    same = ExecutionRef(
        thread_id="parent-thread",
        run_id="run-a",
        execution_id="child-a",
        parent_execution_id="root-parent",
    )
    sibling = ExecutionRef(
        thread_id="parent-thread",
        run_id="run-a",
        execution_id="child-b",
        parent_execution_id="root-parent",
    )
    first_id = first.checkpoint_thread_id("project-fingerprint")

    assert first_id == same.checkpoint_thread_id("project-fingerprint")
    assert first_id != sibling.checkpoint_thread_id("project-fingerprint")
    assert "parent-thread" not in first_id
    assert "run-a" not in first_id
    assert "child-a" not in first_id
    assert first_id.startswith("managed-execution-")


async def _execute_child(
    *,
    persistence: ThreadPersistence,
    graph: Any,
    pool: AgentEnginePool,
    profile: AgentEngineProfile,
    workspace: Path,
    thread_id: str,
    run_id: str,
    execution_id: str,
    task: str,
    parent_execution_id: str = "root-parent",
    idempotency_key: str | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    final_output_gate: Callable[[Any], Awaitable[FinalOutputGateDecision]] | None = None,
) -> tuple[Any, list[dict[str, object]], str]:
    """按 Host 的真实 identity/cleanup 方式执行一个离线 Managed child。"""
    child_ref = ExecutionRef(
        thread_id=thread_id,
        run_id=run_id,
        execution_id=execution_id,
        parent_execution_id=parent_execution_id,
    )
    checkpoint_thread_id = child_ref.checkpoint_thread_id(
        persistence.project_fingerprint
    )
    context = _context(
        workspace,
        thread_id=thread_id,
        run_id=run_id,
        execution_id=execution_id,
        checkpoint_thread_id=checkpoint_thread_id,
    )
    graph_configs: list[dict[str, object]] = []

    def graph_config(namespace: str) -> dict[str, object]:
        """记录每个模型回合使用的根图 identity。"""
        config = {
            "configurable": {
                "thread_id": checkpoint_thread_id,
                "checkpoint_ns": namespace,
            }
        }
        graph_configs.append(config)
        return config

    async def runtime_provider():
        """使用真实 AgentEnginePool lease 和 execution checkpoint cleanup。"""
        return await acquire_pooled_agent_runtime(
            pool=pool,
            profile=profile,
            run_context=context,
            graph_config=graph_config,
            checkpoint_cleanup=lambda: persistence.delete_execution_checkpoint(
                checkpoint_thread_id
            ),
        )

    request = ManagedAgentRequest(
        execution_ref=execution_id,
        parent_execution_ref=parent_execution_id,
        run_id=run_id,
        input=task,
        checkpoint_namespace=child_ref.checkpoint_namespace(
            persistence.project_fingerprint
        ),
        output_policy="capture_only",
        runtime_provider=runtime_provider,
        is_cancelled=is_cancelled or (lambda: False),
        idempotency_key=idempotency_key or f"{run_id}-{execution_id}-{task}",
        final_output_gate=final_output_gate,
    )
    result = await ManagedAgentExecutor().execute(
        request,
        FailClosedManagedObserver(),
    )
    return result, graph_configs, checkpoint_thread_id


async def _open_harness_fixture(
    tmp_path: Path,
    model: _RecordingModel,
) -> tuple[Path, ThreadPersistence, Any, AgentEnginePool, AgentEngineProfile]:
    """打开真实 ProjectScopedAsyncSqliteSaver 与离线 shared graph。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(
        project=workspace,
        home=tmp_path / "home",
    )
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(workspace),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        checkpointer=persistence.checkpointer,
        thread_persistence=persistence,
    )
    profile = _profile(persistence.project_fingerprint)
    pool = AgentEnginePool(
        lambda requested: AgentEngine(profile=requested, graph=graph),
    )
    return workspace, persistence, graph, pool, profile


def _messages_for_task(
    model: _RecordingModel,
    task: str,
) -> list[BaseMessage]:
    """取包含指定 task 的实际模型输入，避免断言 system prompt 细节。"""
    return next(
        messages
        for messages in reversed(model.received)
        if any(
            isinstance(message, HumanMessage) and message.content == task
            for message in messages
        )
    )


@pytest.mark.asyncio
async def test_managed_plugin_child_does_not_load_parent_sqlite_checkpoint(
    tmp_path: Path,
) -> None:
    """真实 Managed child 首次模型输入只能含本次 task，不得继承父 checkpoint。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persistence = await ThreadPersistence.open(project=workspace, home=tmp_path / "home")
    model = _RecordingModel(messages=iter([AIMessage(content="CHILD_RESULT")]))
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(workspace),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        checkpointer=persistence.checkpointer,
        thread_persistence=persistence,
    )
    thread_id = "parent-thread"
    run_id = "parent-run"
    await _seed_parent_checkpoint(
        persistence,
        graph,
        thread_id=thread_id,
        run_id=run_id,
    )

    profile = _profile(persistence.project_fingerprint)
    pool = AgentEnginePool(
        lambda requested: AgentEngine(profile=requested, graph=graph),
    )
    child_ref = ExecutionRef(
        thread_id=thread_id,
        run_id=run_id,
        execution_id="child-isolation",
        parent_execution_id="root-parent",
    )
    child_context = _context(
        workspace,
        thread_id=thread_id,
        run_id=run_id,
        execution_id=child_ref.execution_id,
        checkpoint_thread_id=child_ref.checkpoint_thread_id(
            persistence.project_fingerprint
        ),
    )
    child_checkpoint_thread_id = child_ref.checkpoint_thread_id(
        persistence.project_fingerprint
    )

    async def runtime_provider():
        """使用真实 AgentEnginePool lease 进入 ManagedAgentExecutor。"""
        return await acquire_pooled_agent_runtime(
            pool=pool,
            profile=profile,
            run_context=child_context,
            graph_config=lambda namespace: {
                "configurable": {
                    "thread_id": child_checkpoint_thread_id,
                    "checkpoint_ns": namespace,
                }
            },
            checkpoint_cleanup=lambda: persistence.delete_execution_checkpoint(
                child_checkpoint_thread_id
            ),
        )

    request = ManagedAgentRequest(
        execution_ref=child_ref.execution_id,
        parent_execution_ref=child_ref.parent_execution_id,
        run_id=child_ref.run_id,
        input="CHILD_TASK_MARKER",
        checkpoint_namespace=child_ref.checkpoint_namespace(
            persistence.project_fingerprint
        ),
        output_policy="capture_only",
        runtime_provider=runtime_provider,
        is_cancelled=lambda: False,
        idempotency_key="managed-child-isolation",
    )
    try:
        result = await ManagedAgentExecutor().execute(
            request,
            FailClosedManagedObserver(),
        )
        assert result.final_content == "CHILD_RESULT"
        child_messages = next(
            messages
            for messages in model.received
            if any(
                isinstance(message, HumanMessage)
                and message.content == "CHILD_TASK_MARKER"
                for message in messages
            )
        )
        assert [
            message.content
            for message in child_messages
            if isinstance(message, HumanMessage)
        ] == ["CHILD_TASK_MARKER"]
        assert all(
            "PARENT_HISTORY_MARKER" not in str(message.content)
            and "PARENT_ASSISTANT_MARKER" not in str(message.content)
            for message in child_messages
        )
        assert await _checkpoint_rows(persistence, child_checkpoint_thread_id) == ()
        parent_rows = await _checkpoint_rows(persistence, thread_id)
        assert parent_rows
        assert all(
            row.config["configurable"]["thread_id"] == thread_id
            for row in parent_rows
        )
    finally:
        await pool.aclose()
        await persistence.close()


@pytest.mark.asyncio
async def test_managed_plugin_child_continue_reuses_only_its_checkpoint(
    tmp_path: Path,
) -> None:
    """SubagentStop continue 在同一 child 内保留 child 历史但不带入父历史。"""
    model = _RecordingModel(
        messages=iter(
            [
                AIMessage(content="CHILD_FIRST_RESULT"),
                AIMessage(content="CHILD_FINAL_RESULT"),
            ]
        )
    )
    workspace, persistence, graph, pool, profile = await _open_harness_fixture(
        tmp_path,
        model,
    )
    thread_id = "parent-thread"
    await _seed_parent_checkpoint(
        persistence,
        graph,
        thread_id=thread_id,
        run_id="parent-run",
    )
    decisions = [
        FinalOutputGateDecision(
            action="continue",
            continuation_prompt="CHILD_CONTINUATION_MARKER",
        ),
        FinalOutputGateDecision(action="allow"),
    ]

    async def gate(_final: Any) -> FinalOutputGateDecision:
        """第一次提交继续，第二次允许同一 execution 完成。"""
        return decisions.pop(0)

    try:
        result, graph_configs, checkpoint_thread_id = await _execute_child(
            persistence=persistence,
            graph=graph,
            pool=pool,
            profile=profile,
            workspace=workspace,
            thread_id=thread_id,
            run_id="child-run",
            execution_id="child-continue",
            task="CHILD_TASK_MARKER",
            final_output_gate=gate,
        )

        assert result.final_content == "CHILD_FINAL_RESULT"
        assert len(graph_configs) == 2
        assert [
            config["configurable"]["thread_id"]  # type: ignore[index]
            for config in graph_configs
        ] == [checkpoint_thread_id, checkpoint_thread_id]
        resumed_messages = _messages_for_task(
            model,
            "CHILD_CONTINUATION_MARKER",
        )
        assert [
            message.content
            for message in resumed_messages
            if isinstance(message, HumanMessage)
        ] == ["CHILD_TASK_MARKER", "CHILD_CONTINUATION_MARKER"]
        assert any(
            message.content == "CHILD_FIRST_RESULT"
            for message in resumed_messages
            if isinstance(message, AIMessage)
        )
        assert all(
            marker not in str(message.content)
            for message in resumed_messages
            for marker in ("PARENT_HISTORY_MARKER", "PARENT_ASSISTANT_MARKER")
        )
        assert await _checkpoint_rows(persistence, checkpoint_thread_id) == ()
    finally:
        await pool.aclose()
        await persistence.close()


@pytest.mark.asyncio
async def test_managed_plugin_completed_child_cannot_be_reused_by_sibling_or_new_run(
    tmp_path: Path,
) -> None:
    """终态 child 清理后，同 ID 重用、sibling 和新 Run 都从空状态开始。"""
    tasks = (
        "CHILD_A_TASK",
        "CHILD_REUSED_AFTER_TERMINAL_TASK",
        "CHILD_SIBLING_TASK",
        "CHILD_NEW_RUN_TASK",
    )
    model = _RecordingModel(
        messages=iter([AIMessage(content=f"RESULT_{index}") for index in range(4)])
    )
    workspace, persistence, graph, pool, profile = await _open_harness_fixture(
        tmp_path,
        model,
    )
    thread_id = "parent-thread"
    await _seed_parent_checkpoint(
        persistence,
        graph,
        thread_id=thread_id,
        run_id="parent-run",
    )
    executions = (
        ("child-run", "child-a", tasks[0]),
        ("child-run", "child-a", tasks[1]),
        ("child-run", "child-sibling", tasks[2]),
        ("new-run", "child-new-run", tasks[3]),
    )
    checkpoint_ids: list[str] = []
    try:
        for run_id, execution_id, task in executions:
            result, _configs, checkpoint_thread_id = await _execute_child(
                persistence=persistence,
                graph=graph,
                pool=pool,
                profile=profile,
                workspace=workspace,
                thread_id=thread_id,
                run_id=run_id,
                execution_id=execution_id,
                task=task,
            )
            assert result.final_content.startswith("RESULT_")
            checkpoint_ids.append(checkpoint_thread_id)
            messages = _messages_for_task(model, task)
            assert [
                message.content
                for message in messages
                if isinstance(message, HumanMessage)
            ] == [task]
            assert all(
                marker not in str(message.content)
                for message in messages
                for marker in (
                    "PARENT_HISTORY_MARKER",
                    "CHILD_A_TASK",
                    "CHILD_REUSED_AFTER_TERMINAL_TASK",
                    "CHILD_SIBLING_TASK",
                )
                if marker != task
            )
            assert await _checkpoint_rows(persistence, checkpoint_thread_id) == ()

        assert checkpoint_ids[0] == checkpoint_ids[1]
        assert checkpoint_ids[0] != checkpoint_ids[2]
        assert checkpoint_ids[0] != checkpoint_ids[3]
        parent_rows = await _checkpoint_rows(persistence, thread_id)
        assert parent_rows
        transcript = await persistence.load_transcript(thread_id)
        assert [record.payload["content"] for record in transcript] == [
            "PARENT_HISTORY_MARKER",
            "PARENT_ASSISTANT_MARKER",
        ]
    finally:
        await pool.aclose()
        await persistence.close()


@pytest.mark.asyncio
async def test_managed_plugin_failed_child_cleans_execution_checkpoint(
    tmp_path: Path,
) -> None:
    """模型失败后不留下可被未来 Managed child 复用的 SQLite 状态。"""
    model = _FailingModel(messages=iter([]))
    workspace, persistence, graph, pool, profile = await _open_harness_fixture(
        tmp_path,
        model,
    )
    try:
        with pytest.raises(RuntimeError, match="CHILD_MODEL_FAILURE_MARKER"):
            await _execute_child(
                persistence=persistence,
                graph=graph,
                pool=pool,
                profile=profile,
                workspace=workspace,
                thread_id="parent-thread",
                run_id="failed-run",
                execution_id="failed-child",
                task="FAILED_CHILD_TASK",
            )
        failed_id = ExecutionRef(
            thread_id="parent-thread",
            run_id="failed-run",
            execution_id="failed-child",
            parent_execution_id="root-parent",
        ).checkpoint_thread_id(persistence.project_fingerprint)
        assert await _checkpoint_rows(persistence, failed_id) == ()
    finally:
        await pool.aclose()
        await persistence.close()


@pytest.mark.asyncio
async def test_managed_plugin_cancelled_child_cleans_execution_checkpoint(
    tmp_path: Path,
) -> None:
    """取消在 stream 边界生效时也清理 child checkpoint。"""
    model = _RecordingModel(messages=iter([AIMessage(content="UNUSED_RESULT")]))
    workspace, persistence, graph, pool, profile = await _open_harness_fixture(
        tmp_path,
        model,
    )
    try:
        with pytest.raises(ManagedAgentExecutionError, match="Run was cancelled") as error:
            await _execute_child(
                persistence=persistence,
                graph=graph,
                pool=pool,
                profile=profile,
                workspace=workspace,
                thread_id="parent-thread",
                run_id="cancelled-run",
                execution_id="cancelled-child",
                task="CANCELLED_CHILD_TASK",
                is_cancelled=lambda: bool(model.received),
            )
        assert error.value.code == "RUN_CANCELLED"
        cancelled_id = ExecutionRef(
            thread_id="parent-thread",
            run_id="cancelled-run",
            execution_id="cancelled-child",
            parent_execution_id="root-parent",
        ).checkpoint_thread_id(persistence.project_fingerprint)
        assert await _checkpoint_rows(persistence, cancelled_id) == ()
    finally:
        await pool.aclose()
        await persistence.close()


def test_compiled_subagent_state_keeps_only_final_message_contract() -> None:
    """父图接收 child 结果时仍只得到最终 AIMessage，不写入私有历史。"""
    from harness_agent.runtime.agent_delegation import AgentResult
    from harness_agent.runtime.execution_binding import ExecutionStatus

    state = _compiled_subagent_state(
        AgentResult(
            ref=ExecutionRef.root("parent-thread", "parent-run"),
            agent_id="za38-frontend-executor",
            mode=ExecutionMode.MANAGED,
            status=ExecutionStatus.COMPLETED,
            output={"final": "CHILD_FINAL_RESULT"},
        )
    )

    assert [message.content for message in state["messages"]] == [
        "CHILD_FINAL_RESULT"
    ]
