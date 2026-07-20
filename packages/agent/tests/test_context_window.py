"""上下文窗口阈值、归档、摘要和熔断回归测试。"""

from __future__ import annotations

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


async def _store(tmp_path):
    """创建隔离 project 的真实 SQLite，验证归档不写进工作区。"""
    from harness_agent.thread_store import ThreadStore

    project = tmp_path / "project"
    project.mkdir()
    return await ThreadStore.open(project=project, home=tmp_path / "home")


async def test_context_window_reports_and_soft_dehydrates_old_tool_results(tmp_path):
    """50% 只报告；60% 将旧工具结果归档为可恢复虚拟文件并保留最近两轮。"""
    from harness_agent.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    model = FakeMessagesListChatModel(responses=[AIMessage(content="unused")])
    middleware = ContextWindowMiddleware(model, context_window_tokens=16_384, thread_store=store)
    messages = [
        HumanMessage(content="第一轮"),
        ToolMessage(content="x" * 33_000, tool_call_id="tool-old"),
        HumanMessage(content="第二轮"),
        HumanMessage(content="第三轮"),
    ]

    reported = await middleware._prepare("thread", messages, 7_000)
    dehydrated = await middleware._prepare("thread", messages, 8_000)

    assert reported[1] == "report" and reported[3] is False
    assert dehydrated[1] == "soft_dehydration" and dehydrated[3] is True
    assert dehydrated[2]
    assert "/.harness/history/" in str(dehydrated[0][1].content)
    artifact = await store.read_context_artifact("thread", dehydrated[2][0])
    assert artifact and "x" * 100 in artifact.content
    await store.close()


async def test_context_window_summarizes_at_80_and_opens_circuit_after_failures(tmp_path):
    """80% 生成结构化摘要；空摘要连续三次不能改写历史且会打开熔断。"""
    from harness_agent.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    messages = [
        HumanMessage(content="目标"),
        AIMessage(content="已检查 " + "x" * 33_000),
        HumanMessage(content="继续"),
        HumanMessage(content="现在执行"),
    ]
    good = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="## 目标\n完成\n## 已确认事实\n有证据\n## 决策\n无\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")]),
        context_window_tokens=16_384,
        thread_store=store,
    )
    summarized = await good._prepare("thread", messages, 10_000)
    assert summarized[1] == "summary" and summarized[3] is True
    assert "harness_context_summary" in str(summarized[0][0].content)

    forced = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="## 目标\n完成\n## 已确认事实\n有证据\n## 决策\n无\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")]),
        context_window_tokens=16_384,
        thread_store=store,
    )
    forced_result = await forced._prepare("forced", messages, 11_500)
    assert forced_result[1] == "forced_summary" and forced_result[3] is True
    assert [message.content for message in forced_result[0] if isinstance(message, HumanMessage)][-1] == "现在执行"

    bad = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="")]),
        context_window_tokens=16_384,
        thread_store=store,
    )
    for _ in range(3):
        result = await bad._prepare("broken", messages, 10_000)
        assert result[3] is False
    final = await bad._prepare("broken", messages, 10_000)
    assert final[1] == "circuit_open"
    assert (await store.context_state("broken")).circuit_open is True
    await store.close()


async def test_context_window_overflow_recovery_archives_before_single_retry(tmp_path):
    """溢出恢复优先脱水旧工具结果，并只产生可恢复的归档指针。"""
    from harness_agent.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
        context_window_tokens=16_384,
        thread_store=store,
    )
    messages = [
        HumanMessage(content="旧请求"),
        ToolMessage(content="z" * 33_000, tool_call_id="tool-overflow"),
        HumanMessage(content="保留请求"),
    ]
    recovered, artifacts, changed = await middleware._overflow_recovery("overflow", messages)

    assert changed is True and artifacts
    assert "/.harness/history/" in str(recovered[1].content)
    assert (await store.read_context_artifact("overflow", artifacts[0])) is not None
    await store.close()


async def test_context_window_manual_compaction_bypasses_threshold_but_keeps_savings_guard(tmp_path):
    """用户命令可在未到 80% 时主动摘要，仍拒绝无收益的重写。"""
    from harness_agent.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="## 目标\n压缩\n## 已确认事实\n已完成\n## 决策\n保留两轮\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")]),
        context_window_tokens=16_384,
        thread_store=store,
    )
    messages = [
        HumanMessage(content="第一轮 " + "a" * 9_000),
        AIMessage(content="第一轮结论 " + "b" * 9_000),
        HumanMessage(content="第二轮"),
        HumanMessage(content="第三轮"),
    ]

    compacted, update, rewritten = await middleware.compact_now("manual", messages)

    assert rewritten is True
    assert update.action == "manual_summary"
    assert "harness_context_summary" in str(compacted[0].content)
    assert [message.content for message in compacted if isinstance(message, HumanMessage)][-1] == "第三轮"
    assert (await store.context_state("manual")).last_action == "manual_summary"
    assert middleware.consume_updates("manual") == (update,)
    await store.close()


async def test_manual_compaction_can_replace_persisted_delta_channel_history(tmp_path):
    """`context.compact` 所用 checkpoint 改写必须保留摘要和最近 user turn。"""
    from typing import Any

    from langchain_core.runnables import Runnable
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from harness_agent.agent import create_harness_agent

    class ToolModel(FakeMessagesListChatModel):
        """为 DeepAgents 提供工具绑定和一次摘要响应的离线模型。"""

        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    store = await _store(tmp_path)
    messages = [
        HumanMessage(content="第一轮 " + "a" * 9_000),
        AIMessage(content="第一轮结论 " + "b" * 9_000),
        HumanMessage(content="第二轮"),
        HumanMessage(content="第三轮"),
    ]
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages}
    await store.checkpointer.aput(store.graph_config("manual-checkpoint"), checkpoint, {}, {})
    await store.record_message("manual-checkpoint", "第一轮")

    middlewares: dict[str, Any] = {}
    model = ToolModel(responses=[AIMessage(content="## 目标\n压缩\n## 已确认事实\n已完成\n## 决策\n保留两轮\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")])
    model.profile = {"max_input_tokens": 16_384}
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path / "project"),
        checkpointer=store.checkpointer,
        thread_store=store,
        context_middlewares=middlewares,
        context_window_tokens=16_384,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
    )

    compacted, _update, rewritten = await middlewares["ephemeral"].compact_now("manual-checkpoint", messages)
    assert rewritten is True
    from langchain_core.messages import RemoveMessage

    await agent.aupdate_state(
        {"configurable": {"thread_id": "manual-checkpoint"}},
        {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *compacted]},
        as_node="model",
    )
    await store.refresh_thread("manual-checkpoint")
    opened = await store.open_thread("manual-checkpoint")

    contents = [message.content for message in opened.messages]
    assert any("harness_context_summary" in content for content in contents)
    assert contents[-1] == "第三轮"
    await store.close()


async def test_context_rewrite_keeps_current_model_response_in_checkpoint(tmp_path):
    """模型结果先于附加 Command 写入时，摘要重写仍必须保留本轮最终回答。"""
    from typing import Any

    from langchain_core.runnables import Runnable

    from harness_agent.agent import create_harness_agent

    class ToolModel(FakeMessagesListChatModel):
        """为 DeepAgents 提供工具绑定的最小离线模型。"""

        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    store = await _store(tmp_path)
    model = ToolModel(
        responses=[
            AIMessage(content="## 目标\n完成\n## 已确认事实\n有证据\n## 决策\n无\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无"),
            AIMessage(content="最终回答"),
        ]
    )
    model.profile = {"max_input_tokens": 16_384}
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path / "project"),
        checkpointer=store.checkpointer,
        thread_store=store,
        context_window_tokens=16_384,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
    )
    messages = [
        HumanMessage(content="第一轮"),
        ToolMessage(content="x" * 42_000, tool_call_id="old-tool"),
        HumanMessage(content="第二轮"),
        HumanMessage(content="第三轮"),
    ]
    await store.record_message("rewrite", "第一轮")
    async for _ in agent.astream({"messages": messages}, config=store.graph_config("rewrite"), stream_mode=["messages", "updates"]):
        pass

    # DeepAgents 使用 DeltaChannel，最新 checkpoint 只记录增量版本；必须经
    # ThreadStore 的确定性 reducer 回放后再断言完整历史。
    await store.refresh_thread("rewrite")
    checkpoint = await store.open_thread("rewrite")
    contents = [message.content for message in checkpoint.messages]
    assert "最终回答" in contents
    assert any("harness_context_summary" in content for content in contents)
    await store.close()
