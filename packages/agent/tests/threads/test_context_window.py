"""上下文窗口阈值、归档、摘要和熔断回归测试。"""

from __future__ import annotations

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.support.thread_fixtures import accept_thread


async def _store(tmp_path):
    """创建隔离 project 的真实 SQLite，验证归档不写进工作区。"""
    from harness_agent.threads.thread_persistence import ThreadPersistence

    project = tmp_path / "project"
    project.mkdir()
    return await ThreadPersistence.open(project=project, home=tmp_path / "home")


async def test_context_window_reports_and_soft_dehydrates_old_tool_results(tmp_path):
    """50% 只报告；60% 将旧工具结果归档为可恢复虚拟文件并保留最近两轮。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    await accept_thread(store, "thread", "第一轮")
    model = FakeMessagesListChatModel(responses=[AIMessage(content="unused")])
    middleware = ContextWindowMiddleware(model, context_window_tokens=16_384, thread_persistence=store)
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-old", "name": "read", "args": {}}],
        ),
        ToolMessage(content="x" * 33_000, tool_call_id="tool-old"),
        HumanMessage(content="第二轮"),
        HumanMessage(content="第三轮"),
    ]

    reported = await middleware._prepare("thread", messages, 7_000)
    dehydrated = await middleware._prepare("thread", messages, 8_000)

    assert reported[1] == "report" and reported[3] is False
    assert dehydrated[1] == "soft_dehydration" and dehydrated[3] is True
    assert dehydrated[2]
    assert "/.harness/history/" in str(dehydrated[0][2].content)
    artifact = await store.load_context_artifact("thread", dehydrated[2][0])
    assert artifact and "x" * 100 in artifact.content
    await store.close()


async def test_consecutive_rewrites_declare_all_artifacts_and_restart_from_latest(tmp_path):
    """连续脱水、摘要、再脱水必须继承旧指针，重启后 latest checkpoint 仍有效。"""
    from harness_agent.context_projection import ContextProjector, artifact_references
    from harness_agent.context_window import ContextWindowMiddleware
    from harness_agent.thread_persistence import ThreadPersistence, TranscriptAppend

    store = await _store(tmp_path)
    await accept_thread(store, "thread", "第一轮")
    first_middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    original = [
        HumanMessage(content="第一轮"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-first", "name": "read", "args": {}}],
        ),
        ToolMessage(content="a" * 33_000, tool_call_id="tool-first"),
        AIMessage(content="旧结论 " + "b" * 33_000),
        HumanMessage(content="第二轮"),
    ]
    first_messages, first_new_ids, first_changed = await first_middleware._dehydrate(
        "thread", original, keep_turns=1
    )
    assert first_changed is True and len(first_new_ids) == 1
    first_artifact_id = first_new_ids[0]

    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="summary-boundary",
            kind="user",
            content="开始摘要",
        )
    )
    summary_text = (
        "## 目标\n压缩\n## 已确认事实\n已归档工具输出\n## 决策\n保留指针"
        "\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n"
        f"/.harness/history/{first_artifact_id}.md"
    )
    summary_middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content=summary_text)]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    second_messages, second_new_ids, second_changed = await summary_middleware._summarize(
        "thread", first_messages, keep_turns=1
    )
    assert second_changed is True and len(second_new_ids) == 1
    second_checkpoint = await store.load_latest_valid_compression_checkpoint("thread")
    assert second_checkpoint is not None
    assert second_checkpoint.artifact_ids == artifact_references(second_messages)
    assert set(second_checkpoint.artifact_ids) == {
        first_artifact_id,
        second_new_ids[0],
    }

    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="dehydrate-boundary",
            kind="user",
            content="继续脱水",
        )
    )
    third_input = [
        *second_messages,
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-third", "name": "read", "args": {}}],
        ),
        ToolMessage(content="c" * 33_000, tool_call_id="tool-third"),
        HumanMessage(content="第三轮"),
    ]
    third_messages, third_new_ids, third_changed = await first_middleware._dehydrate(
        "thread", third_input, keep_turns=1
    )
    assert third_changed is True and len(third_new_ids) == 1
    latest = await store.load_latest_valid_compression_checkpoint("thread")
    assert latest is not None
    assert latest.artifact_ids == artifact_references(third_messages)
    assert set(latest.artifact_ids) == {
        first_artifact_id,
        second_new_ids[0],
        third_new_ids[0],
    }
    assert set(third_new_ids).isdisjoint(first_new_ids + second_new_ids)

    await store.close()
    reopened = await ThreadPersistence.open(
        project=tmp_path / "project", home=tmp_path / "home"
    )
    recovered = await ContextProjector(reopened).project("thread")
    assert recovered.checkpoint is not None
    assert recovered.checkpoint.checkpoint_id == latest.checkpoint_id
    assert recovered.checkpoint.artifact_ids == latest.artifact_ids
    assert [message.content for message in recovered.messages] == [
        message.content for message in third_messages
    ]
    await reopened.close()


async def test_context_window_summarizes_at_80_and_opens_circuit_after_failures(tmp_path):
    """80% 生成结构化摘要；空摘要连续三次不能改写历史且会打开熔断。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    await accept_thread(store, "thread", "目标")
    await accept_thread(store, "forced", "目标")
    await accept_thread(store, "broken", "目标")
    messages = [
        HumanMessage(content="目标"),
        AIMessage(content="已检查 " + "x" * 33_000),
        HumanMessage(content="继续"),
        HumanMessage(content="现在执行"),
    ]
    good = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="## 目标\n完成\n## 已确认事实\n有证据\n## 决策\n无\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    summarized = await good._prepare("thread", messages, 10_000)
    assert summarized[1] == "summary" and summarized[3] is True
    assert "harness_context_summary" in str(summarized[0][0].content)

    forced = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="## 目标\n完成\n## 已确认事实\n有证据\n## 决策\n无\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    forced_result = await forced._prepare("forced", messages, 11_500)
    assert forced_result[1] == "forced_summary" and forced_result[3] is True
    assert [message.content for message in forced_result[0] if isinstance(message, HumanMessage)][-1] == "现在执行"

    bad = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="")]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    for _ in range(3):
        result = await bad._prepare("broken", messages, 10_000)
        assert result[3] is False
    final = await bad._prepare("broken", messages, 10_000)
    assert final[1] == "circuit_open"
    assert (await store.load_context("broken")).state.circuit_open is True
    await store.close()


async def test_context_window_overflow_recovery_archives_before_single_retry(tmp_path):
    """溢出恢复优先脱水旧工具结果，并只产生可恢复的归档指针。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    await accept_thread(store, "overflow", "旧请求")
    middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    messages = [
        HumanMessage(content="旧请求"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-overflow", "name": "read", "args": {}}],
        ),
        ToolMessage(content="z" * 33_000, tool_call_id="tool-overflow"),
        HumanMessage(content="保留请求"),
    ]
    recovered, artifacts, changed = await middleware._overflow_recovery("overflow", messages)

    assert changed is True and artifacts
    assert "/.harness/history/" in str(recovered[2].content)
    assert (await store.load_context_artifact("overflow", artifacts[0])) is not None
    await store.close()


async def test_context_window_manual_compaction_bypasses_threshold_but_keeps_savings_guard(tmp_path):
    """用户命令可在未到 80% 时主动摘要，仍拒绝无收益的重写。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    await accept_thread(store, "manual", "第一轮")
    middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[AIMessage(content="## 目标\n压缩\n## 已确认事实\n已完成\n## 决策\n保留两轮\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")]),
        context_window_tokens=16_384,
        thread_persistence=store,
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
    assert (await store.load_context("manual")).state.last_action == "manual_summary"
    assert middleware.consume_updates("manual") == (update,)
    await store.close()


async def test_manual_compaction_can_replace_persisted_delta_channel_history(tmp_path):
    """`context.compact` 所用 checkpoint 改写必须保留摘要和最近 user turn。"""
    from typing import Any

    from langchain_core.runnables import Runnable
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from harness_agent.runtime.agent import create_harness_agent

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
    await accept_thread(store, "manual-checkpoint", "第一轮")

    model = ToolModel(responses=[AIMessage(content="## 目标\n压缩\n## 已确认事实\n已完成\n## 决策\n保留两轮\n## 改动\n无\n## 测试\n无\n## 未决项\n无\n## 归档\n无")])
    model.profile = {"max_input_tokens": 16_384}
    from harness_agent.threads.context_window import ContextWindowMiddleware

    middleware = ContextWindowMiddleware(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path / "project"),
        checkpointer=store.checkpointer,
        thread_persistence=store,
        context_middleware=middleware,
        context_window_tokens=16_384,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
    )

    compacted, _update, rewritten = await middleware.compact_now("manual-checkpoint", messages)
    assert rewritten is True
    from langchain_core.messages import RemoveMessage

    await agent.aupdate_state(
        {"configurable": {"thread_id": "manual-checkpoint"}},
        {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *compacted]},
        as_node="model",
    )
    await store.complete_run("manual-checkpoint")
    context = await store.load_context("manual-checkpoint")

    contents = [message.content for message in context.messages]
    assert any("harness_context_summary" in content for content in contents)
    assert contents[-1] == "第三轮"
    await store.close()


async def test_context_rewrite_keeps_current_model_response_in_checkpoint(tmp_path):
    """模型结果先于附加 Command 写入时，摘要重写仍必须保留本轮最终回答。"""
    from typing import Any

    from langchain_core.runnables import Runnable

    from harness_agent.runtime.agent import create_harness_agent

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
    from harness_agent.context_window import ContextWindowMiddleware

    middleware = ContextWindowMiddleware(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path / "project"),
        checkpointer=store.checkpointer,
        thread_persistence=store,
        context_middleware=middleware,
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
    await accept_thread(store, "rewrite", "第一轮")
    # 非 shared-engine 的库级图没有 RunContext，中间件使用显式
    # ephemeral 领域；生产 Host 总是由 RunContext 提供真实 thread ID。
    await accept_thread(store, "ephemeral", "第一轮")
    async for _ in agent.astream({"messages": messages}, config=store.graph_config("rewrite"), stream_mode=["messages", "updates"]):
        pass

    # DeepAgents 使用 DeltaChannel，最新 checkpoint 只记录增量版本；必须经
    # ThreadPersistence 的确定性 reducer 回放后再断言完整历史。
    await store.complete_run("rewrite")
    context = await store.load_context("rewrite")
    contents = [message.content for message in context.messages]
    updates = middleware.consume_updates("ephemeral")
    assert "最终回答" in contents
    assert any("harness_context_summary" in content for content in contents), updates
    await store.close()
