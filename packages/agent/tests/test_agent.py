"""Agent factory tests using a fake model that supports tool binding."""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable


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


def _make_fake_model() -> ToolCallingFakeChatModel:
    model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="ok")]))
    model.profile = {"max_input_tokens": 200000}
    return model


def _create_agent():
    from harness_agent.agent import create_harness_agent

    return create_harness_agent(
        model=_make_fake_model(),
        enable_interpreter=False,
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

    local = _with_execution_context("base", workspace="/tmp/work", sandboxed=False, provider=None)
    remote = _with_execution_context(
        "base", workspace="/workspace", sandboxed=True, provider="corp"
    )

    assert "本机工作目录是：`/tmp/work`" in local
    assert "不是操作系统安全边界" in local
    assert "corp` 远端沙箱" in remote
    assert "`/workspace`" in remote


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
