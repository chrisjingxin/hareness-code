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
    from za38_agent.agent import create_za38_agent

    return create_za38_agent(
        model=_make_fake_model(),
        enable_interpreter=False,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
    )


def test_create_za38_agent_returns_compiled_graph():
    agent = _create_agent()
    assert hasattr(agent, "astream")
    assert hasattr(agent, "ainvoke")


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
