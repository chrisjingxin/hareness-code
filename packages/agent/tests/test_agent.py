"""Agent factory tests using a fake model that supports tool binding."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Sequence

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable
from pydantic import Field


class ToolCallingFakeChatModel(GenericFakeChatModel):
    """Generic fake model with the minimal bind_tools contract deepagents needs."""

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self


class RecordingFakeChatModel(ToolCallingFakeChatModel):
    """记录模型实际收到的消息，用于验证共享图的动态 PromptEpoch 注入。"""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        """保存模型输入后继续使用 GenericFakeChatModel 的离线响应。"""
        self.received.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _make_fake_model() -> ToolCallingFakeChatModel:
    model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="ok")]))
    model.profile = {"max_input_tokens": 200000}
    return model


def _create_agent():
    from harness_agent.agent import create_harness_agent

    return create_harness_agent(
        model=_make_fake_model(),
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
    )


def test_create_harness_agent_returns_compiled_graph():
    agent = _create_agent()
    assert hasattr(agent, "astream")
    assert hasattr(agent, "ainvoke")


def test_execution_context_prompt_marks_local_and_remote_boundaries():
    """提示词必须如实说明本机默认模式与远端逻辑工作目录。"""
    from harness_agent.agent import _with_execution_context

    local = _with_execution_context(
        "base", workspace="/tmp/work", sandboxed=False, provider=None, approval_mode="default"
    )
    remote = _with_execution_context(
        "base", workspace="/workspace", sandboxed=True, provider="corp", approval_mode="yolo"
    )

    assert "本机工作目录是：`/tmp/work`" in local
    assert "文件工具只允许访问" in local
    assert "不能通过审批绕过" in local
    assert "corp` 远端沙箱" in remote
    assert "`/workspace`" in remote


def test_default_local_subagent_has_its_own_workspace_guard(tmp_path):
    """默认子 Agent 不得因独立 middleware 栈绕过本机文件边界。"""
    from harness_agent.agent import _create_default_subagents
    from harness_agent.workspace_boundary import WorkspaceBoundaryMiddleware

    subagents = _create_default_subagents(workspace=tmp_path, approval_mode="default")
    assert subagents[0]["name"] == "general-purpose"
    assert isinstance(subagents[0]["middleware"][0], WorkspaceBoundaryMiddleware)


def test_plan_subagent_has_its_own_plan_guard(tmp_path):
    """子 Agent 的独立栈必须重复计划模式守卫，不能借 task 绕过。"""
    from harness_agent.agent import _create_default_subagents
    from harness_agent.approval_policy import PlanModeMiddleware

    subagents = _create_default_subagents(workspace=tmp_path, approval_mode="plan")
    assert isinstance(subagents[0]["middleware"][0], PlanModeMiddleware)


async def test_agent_streams_events():
    agent = _create_agent()
    events = [
        event
        async for event in agent.astream(
            {"messages": [HumanMessage(content="hi")]},
            config={"configurable": {"thread_id": "test-1"}},
            stream_mode=["messages", "updates"],
        )
    ]
    assert events


async def test_shared_agent_injects_prompt_epoch_per_run_without_thread_state_leakage():
    """同一编译图服务两个 thread 时，模型输入和 checkpoint 必须彼此隔离。"""
    from langgraph.checkpoint.memory import MemorySaver

    from harness_agent.agent import create_harness_agent, create_prompt_epoch
    from harness_agent.run_context import RunContext

    model = RecordingFakeChatModel(
        messages=iter([AIMessage(content="A 完成"), AIMessage(content="B 完成")])
    )
    model.profile = {"max_input_tokens": 200_000}
    agent = create_harness_agent(
        model,
        checkpointer=MemorySaver(),
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
        shared_runtime=True,
    )

    def run_context(thread_id: str, marker: str) -> RunContext:
        return RunContext(
            thread_id=thread_id,
            run_id=f"run-{thread_id}",
            prompt_epoch=create_prompt_epoch(
                thread_id=thread_id,
                system_prompt=marker,
                workspace=".",
                sandboxed=False,
                provider=None,
                approval_mode="yolo",
                skill_registry=None,
                enable_memory=False,
                enable_skills=False,
            ),
            approval_mode="yolo",
        )

    await asyncio.gather(
        agent.ainvoke(
            {"messages": [HumanMessage(content="thread A request")]},
            config={"configurable": {"thread_id": "thread-a"}},
            context=run_context("thread-a", "PROMPT_EPOCH_A"),
        ),
        agent.ainvoke(
            {"messages": [HumanMessage(content="thread B request")]},
            config={"configurable": {"thread_id": "thread-b"}},
            context=run_context("thread-b", "PROMPT_EPOCH_B"),
        ),
    )

    system_inputs = [
        "\n".join(str(message.content) for message in messages if message.type == "system")
        for messages in model.received
    ]
    assert any("PROMPT_EPOCH_A" in prompt and "PROMPT_EPOCH_B" not in prompt for prompt in system_inputs)
    assert any("PROMPT_EPOCH_B" in prompt and "PROMPT_EPOCH_A" not in prompt for prompt in system_inputs)

    first = await agent.aget_state({"configurable": {"thread_id": "thread-a"}})
    second = await agent.aget_state({"configurable": {"thread_id": "thread-b"}})
    assert [message.content for message in first.values["messages"] if isinstance(message, HumanMessage)] == ["thread A request"]
    assert [message.content for message in second.values["messages"] if isinstance(message, HumanMessage)] == ["thread B request"]


def test_run_context_rejects_mismatched_langgraph_thread_id():
    """共享图配置与 RunContext 指向不同 thread 时必须 fail closed。"""
    from harness_agent.agent import create_prompt_epoch
    from harness_agent.run_context import RunContext, RunContextError, thread_id_for_runtime

    context = RunContext(
        thread_id="thread-a",
        run_id="run-a",
        prompt_epoch=create_prompt_epoch(
            thread_id="thread-a",
            system_prompt="test prompt",
            workspace=".",
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
        ),
        approval_mode="yolo",
    )
    runtime = SimpleNamespace(
        context=context,
        config={"configurable": {"thread_id": "thread-b"}},
    )

    with pytest.raises(RunContextError, match="RUN_CONTEXT_CONFIG_THREAD_MISMATCH"):
        thread_id_for_runtime(runtime)
