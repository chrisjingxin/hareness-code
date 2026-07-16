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
    assert "文件工具只允许访问" in local
    assert "不能通过审批绕过" in local
    assert "corp` 远端沙箱" in remote
    assert "`/workspace`" in remote


def test_default_local_subagent_has_its_own_workspace_guard(tmp_path):
    """默认子 Agent 不得因独立 middleware 栈绕过本机文件边界。"""
    from harness_agent.agent import _create_local_subagents
    from harness_agent.workspace_boundary import WorkspaceBoundaryMiddleware

    subagents = _create_local_subagents(tmp_path)
    assert subagents[0]["name"] == "general-purpose"
    assert isinstance(subagents[0]["middleware"][0], WorkspaceBoundaryMiddleware)


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
