"""验证 Harness 依赖的 DeepAgents RubricMiddleware 0.6.8 行为边界。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from deepagents import RubricMiddleware
from deepagents.middleware.rubric import RUBRIC_GRADER_MESSAGE_SOURCE
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field


pytestmark = pytest.mark.filterwarnings(
    r"ignore:The middleware `RubricMiddleware` is in beta\..*"
)


class _ToolCallingFakeModel(GenericFakeChatModel):
    """支持 LangChain tool/structured-output 绑定的离线消息模型。"""

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        """测试中保留同一可预测模型，不创建真实 provider client。"""
        return self


class _RecordingFakeModel(_ToolCallingFakeModel):
    """记录主模型输入，证明 Harness context 与 rubric 循环能组合。"""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(
        self,
        messages: list[BaseMessage],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.received.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _grader_call(
    *,
    result: str,
    explanation: str,
    criterion: str,
    passed: bool,
    call_id: str,
) -> AIMessage:
    criterion_result: dict[str, object] = {"name": criterion, "passed": passed}
    if not passed:
        criterion_result["gap"] = explanation
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "GraderResponse",
                "args": {
                    "result": result,
                    "explanation": explanation,
                    "criteria": [criterion_result],
                },
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def test_rubric_mapping_loops_to_model_and_emits_custom_events() -> None:
    """真实 graph 能由 rubric input 激活，并在缺口后回到主模型。"""
    main_model = _ToolCallingFakeModel(
        messages=iter(
            [
                AIMessage(content="第一次结果"),
                AIMessage(content="补齐测试后的结果"),
            ]
        )
    )
    grader_model = _ToolCallingFakeModel(
        messages=iter(
            [
                _grader_call(
                    result="needs_revision",
                    explanation="缺少测试证据",
                    criterion="criterion-1",
                    passed=False,
                    call_id="grade-1",
                ),
                _grader_call(
                    result="satisfied",
                    explanation="条件已满足",
                    criterion="criterion-1",
                    passed=True,
                    call_id="grade-2",
                ),
            ]
        )
    )
    agent = create_agent(
        model=main_model,
        middleware=[RubricMiddleware(model=grader_model, max_iterations=3)],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "rubric-compat-loop"}}

    events = list(
        agent.stream(
            {
                "messages": [HumanMessage(content="完成任务")],
                "rubric": "- criterion-1: 必须包含测试证据",
            },
            config=config,
            stream_mode="custom",
        )
    )

    state = agent.get_state(config).values
    assert state["_rubric_status"] == "satisfied"
    assert state["_rubric_iterations"] == 2
    injected = [
        message
        for message in state["messages"]
        if message.additional_kwargs.get("lc_source") == RUBRIC_GRADER_MESSAGE_SOURCE
    ]
    assert len(injected) == 1
    assert "缺少测试证据" in injected[0].content
    assert [event["type"] for event in events] == [
        "rubric_evaluation_start",
        "rubric_evaluation_end",
        "rubric_evaluation_start",
        "rubric_evaluation_end",
    ]
    assert events[0]["iteration"] == 0
    assert events[-1]["result"] == "satisfied"


def test_iteration_cap_requires_harness_terminal_normalization() -> None:
    """0.6.8 的最终 stream 仍是 needs_revision，最终 state 才表示达到上限。"""
    main_model = _ToolCallingFakeModel(
        messages=iter([AIMessage(content="第一次"), AIMessage(content="第二次")])
    )
    grader_model = _ToolCallingFakeModel(
        messages=iter(
            [
                _grader_call(
                    result="needs_revision",
                    explanation="仍未满足",
                    criterion="criterion-1",
                    passed=False,
                    call_id="grade-1",
                ),
                _grader_call(
                    result="needs_revision",
                    explanation="仍未满足",
                    criterion="criterion-1",
                    passed=False,
                    call_id="grade-2",
                ),
            ]
        )
    )
    agent = create_agent(
        model=main_model,
        middleware=[RubricMiddleware(model=grader_model, max_iterations=2)],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "rubric-compat-cap"}}

    events = list(
        agent.stream(
            {
                "messages": [HumanMessage(content="完成任务")],
                "rubric": "- criterion-1: 必须通过",
            },
            config=config,
            stream_mode="custom",
        )
    )

    state = agent.get_state(config).values
    assert state["_rubric_status"] == "max_iterations_reached"
    assert state["_rubric_iterations"] == 2
    assert events[-1]["result"] == "needs_revision"
    assert events[-1]["iteration"] == 1


def test_rubric_iteration_limit_is_bounded_by_sdk() -> None:
    """Harness 声明的 1～20 上限与当前 SDK 构造门一致。"""
    model = _ToolCallingFakeModel(messages=iter(()))

    with pytest.raises(ValueError, match=r"\[1, 20\]"):
        RubricMiddleware(model=model, max_iterations=21)


async def test_rubric_composes_with_harness_run_context_and_context_window() -> None:
    """共享 Run context、窗口治理和 rubric 可位于同一真实 graph。"""
    from harness_agent.runtime.run_context import RunContext, RunContextSnapshotMiddleware
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
    from harness_agent.threads.context_window import ContextWindowMiddleware

    main_model = _RecordingFakeModel(
        messages=iter(
            [
                AIMessage(content="初稿"),
                AIMessage(content="修订稿"),
            ]
        )
    )
    grader_model = _ToolCallingFakeModel(
        messages=iter(
            [
                _grader_call(
                    result="needs_revision",
                    explanation="缺少结果",
                    criterion="criterion-1",
                    passed=False,
                    call_id="grade-1",
                ),
                _grader_call(
                    result="satisfied",
                    explanation="已经补齐",
                    criterion="criterion-1",
                    passed=True,
                    call_id="grade-2",
                ),
            ]
        )
    )
    snapshot = prepare_embedded_context_snapshot(
        thread_id="rubric-context",
        system_prompt="HARNESS_BASE_CONTEXT",
        workspace=".",
        sandboxed=False,
        provider=None,
        approval_mode="default",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )
    context = RunContext(
        thread_id="rubric-context",
        run_id="run-context",
        context_snapshot=snapshot,
        approval_mode="default",
    )
    agent = create_agent(
        model=main_model,
        middleware=[
            RunContextSnapshotMiddleware(),
            ContextWindowMiddleware(main_model, context_window_tokens=200_000),
            RubricMiddleware(model=grader_model, max_iterations=3),
        ],
        context_schema=RunContext,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "rubric-context"}}

    result = await agent.ainvoke(
        {
            "messages": [HumanMessage(content="完成任务")],
            "rubric": "- criterion-1: 必须给出结果",
        },
        config=config,
        context=context,
    )

    assert [message.content for message in result["messages"] if isinstance(message, AIMessage)] == [
        "初稿",
        "修订稿",
    ]
    assert len(main_model.received) == 2
    for model_messages in main_model.received:
        system_text = "\n".join(
            str(message.content)
            for message in model_messages
            if message.type == "system"
        )
        assert "HARNESS_BASE_CONTEXT" in system_text
        assert "审批模式：默认确认" in system_text
