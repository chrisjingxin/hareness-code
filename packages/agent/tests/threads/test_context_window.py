"""上下文窗口阈值、归档、摘要和熔断回归测试。"""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.support.thread_fixtures import accept_thread


async def _store(tmp_path):
    """创建隔离 project 的真实 SQLite，验证归档不写进工作区。"""
    from harness_agent.threads.thread_persistence import ThreadPersistence

    project = tmp_path / "project"
    project.mkdir()
    return await ThreadPersistence.open(project=project, home=tmp_path / "home")


def test_context_updated_payload_redacts_unstable_diagnostics_and_internal_ids():
    """诊断只允许安全短码，不把路径、提示词或内部标识送到 TUI。"""
    from harness_agent.threads.context_window import ContextUpdate

    payload = ContextUpdate(
        thread_id="thread",
        action="/Users/test/project prompt=secret",
        estimated_tokens=10,
        input_cap_tokens=20,
        context_window_tokens=30,
        dynamic_tokens=10,
        cache_status="/tmp/cache",
        miss_reason="ValueError:/private/secret/prompt.txt",
        artifact_ids=("/private/checkpoint-123", "history-safe"),
    ).payload()

    assert payload["action"] == "context_unknown"
    assert payload["cache_status"] == "unknown"
    assert payload["miss_reason"] == "diagnostic_unavailable"
    assert payload["artifact_ids"] == ["artifact_redacted", "history-safe"]
    assert "/private" not in str(payload)
    assert "secret" not in str(payload)


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
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-newer", "name": "read", "args": {}}],
        ),
        ToolMessage(content="y" * 33_000, tool_call_id="tool-newer"),
        HumanMessage(content="第三轮"),
        HumanMessage(content="第四轮"),
    ]

    reported = await middleware._prepare("thread", messages, 7_000)
    dehydrated = await middleware._prepare("thread", messages, 8_000)

    assert reported[1] == "report" and reported[3] is False
    assert dehydrated[1] == "pressure_micro" and dehydrated[3] is True
    assert dehydrated[2]
    assert "/.harness/history/" in str(dehydrated[0][2].content)
    assert "[tool=read]" in str(dehydrated[0][2].content)
    assert "sha256=" in str(dehydrated[0][2].content)
    assert "y" * 100 in str(dehydrated[0][5].content)
    artifact = await store.load_context_artifact("thread", dehydrated[2][0])
    assert artifact and "x" * 100 in artifact.content
    await store.close()


async def test_consecutive_rewrites_declare_all_artifacts_and_restart_from_latest(tmp_path):
    """连续脱水、摘要、再脱水必须继承旧指针，重启后 latest checkpoint 仍有效。"""
    from harness_agent.threads.context_projection import ContextProjector, artifact_references
    from harness_agent.threads.context_window import ContextWindowMiddleware
    from harness_agent.threads.thread_persistence import ThreadPersistence, TranscriptAppend

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
            ToolMessage(content="a" * 22_000, tool_call_id="tool-first"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-second", "name": "read", "args": {}}],
        ),
            ToolMessage(content="b" * 22_000, tool_call_id="tool-second"),
            AIMessage(content="旧结论 " + "c" * 22_000),
        HumanMessage(content="第二轮"),
        HumanMessage(content="第三轮"),
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
            ToolMessage(content="c" * 24_000, tool_call_id="tool-third"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-fourth", "name": "read", "args": {}}],
        ),
            ToolMessage(content="d" * 24_000, tool_call_id="tool-fourth"),
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
        HumanMessage(content="中间请求"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tool-overflow-newer", "name": "read", "args": {}}],
        ),
        ToolMessage(content="q" * 33_000, tool_call_id="tool-overflow-newer"),
        HumanMessage(content="保留请求"),
    ]
    recovered, artifacts, changed = await middleware._overflow_recovery("overflow", messages)

    assert changed is True and artifacts
    assert "/.harness/history/" in str(recovered[2].content)
    assert (await store.load_context_artifact("overflow", artifacts[0])) is not None
    await store.close()


async def test_automatic_compaction_without_persistence_fails_closed(tmp_path):
    """没有 ThreadPersistence 时中低/高水位和 overflow 都不伪造 Artifact。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    class CountingModel(FakeMessagesListChatModel):
        """确认不可持久化路径不会调用摘要模型。"""

        calls: ClassVar[int] = 0

        async def ainvoke(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            type(self).calls += 1
            return await super().ainvoke(*args, **kwargs)

    model = CountingModel(
        responses=[AIMessage(content="## 目标\n不应调用摘要模型")]
    )
    middleware = ContextWindowMiddleware(model, context_window_tokens=16_384)
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(content="", tool_calls=[{"id": "no-persist-a", "name": "read", "args": {}}]),
        ToolMessage(content="a" * 33_000, tool_call_id="no-persist-a"),
        HumanMessage(content="第二轮"),
        AIMessage(content="", tool_calls=[{"id": "no-persist-b", "name": "read", "args": {}}]),
        ToolMessage(content="b" * 33_000, tool_call_id="no-persist-b"),
        HumanMessage(content="第三轮"),
    ]
    original_contents = [message.content for message in messages]

    low = await middleware._prepare("no-persist", messages, 7_000)
    middle = await middleware._prepare("no-persist", messages, 8_000)
    high = await middleware._prepare("no-persist", messages, 12_000)
    overflow, overflow_ids, overflow_changed = await middleware._overflow_recovery(
        "no-persist", messages
    )

    assert low[1] == "report" and low[3] is False
    assert middle[1] == "micro_skipped" and middle[3] is False
    assert high[1] == "micro_skipped" and high[3] is False
    assert overflow_ids == () and overflow_changed is False
    assert [message.content for message in middle[0]] == original_contents
    assert [message.content for message in high[0]] == original_contents
    assert [message.content for message in overflow] == original_contents
    assert not any("/.harness/history/" in str(content) for content in original_contents)
    assert all(not update.artifact_ids for update in middleware.consume_updates("no-persist"))
    assert model.calls == 0


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
    assert (await store.load_context("manual")).state.last_action == "manual_full"
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
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(
            content="",
            tool_calls=[{"id": "old-tool", "name": "read", "args": {}}],
        ),
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


async def test_full_pressure_micro_remeasures_and_skips_summary_model(tmp_path):
    """达到 full 先归档旧工具；pressure_after 足够时只提交 micro。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    class CountingModel(FakeMessagesListChatModel):
        """记录是否错误地调用了完整摘要模型。"""

        calls: ClassVar[int] = 0

        async def ainvoke(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            type(self).calls += 1
            return await super().ainvoke(*args, **kwargs)

    store = await _store(tmp_path)
    await accept_thread(store, "full-micro", "第一轮")
    before_records = await store.load_transcript("full-micro")
    model = CountingModel(responses=[])
    middleware = ContextWindowMiddleware(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(content="", tool_calls=[{"id": "call-a", "name": "read", "args": {}}]),
        ToolMessage(content="a" * 33_000, tool_call_id="call-a"),
        HumanMessage(content="第二轮"),
        AIMessage(content="", tool_calls=[{"id": "call-b", "name": "read", "args": {}}]),
        ToolMessage(content="b" * 33_000, tool_call_id="call-b"),
        HumanMessage(content="第三轮"),
        HumanMessage(content="第四轮"),
    ]

    projected, action, artifact_ids, rewritten = await middleware._prepare(
        "full-micro", messages, 10_000
    )

    assert (action, rewritten) == ("pressure_micro", True)
    assert artifact_ids and model.calls == 0
    checkpoint = await store.load_latest_valid_compression_checkpoint("full-micro")
    assert checkpoint is not None and checkpoint.mode == "micro"
    assert await store.load_transcript("full-micro") == before_records
    assert any("/.harness/history/" in str(message.content) for message in projected)
    await store.close()


async def test_idle_micro_requires_explicit_top_level_call_type(tmp_path):
    """同一空闲时长只在显式顶层首调触发，工具续跑不重复触发。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    await accept_thread(store, "idle-boundary", "第一轮")
    middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(content="", tool_calls=[{"id": "idle-a", "name": "read", "args": {}}]),
        ToolMessage(content="a" * 12_000, tool_call_id="idle-a"),
        HumanMessage(content="第二轮"),
        AIMessage(content="", tool_calls=[{"id": "idle-b", "name": "read", "args": {}}]),
        ToolMessage(content="b" * 12_000, tool_call_id="idle-b"),
        HumanMessage(content="第三轮"),
        HumanMessage(content="第四轮"),
    ]

    initial = await middleware._prepare(
        "idle-boundary",
        messages,
        4_000,
        call_type="top_level_initial",
        idle_duration_ms=900_000,
    )
    continuation = await middleware._prepare(
        "idle-boundary",
        messages,
        4_000,
        call_type="tool_continuation",
        idle_duration_ms=900_000,
    )

    assert initial[1] == "idle_micro" and initial[3] is True
    assert continuation[1] == "within_budget" and continuation[3] is False
    await store.close()


async def test_full_pressure_keeps_micro_in_memory_before_full_checkpoint(tmp_path):
    """微压缩后仍超过 full 时只留下最终 full checkpoint。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    class CountingModel(FakeMessagesListChatModel):
        """记录完整摘要调用次数。"""

        calls: ClassVar[int] = 0

        async def ainvoke(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            type(self).calls += 1
            return await super().ainvoke(*args, **kwargs)

    store = await _store(tmp_path)
    await accept_thread(store, "full-after-micro", "第一轮")
    model = CountingModel(
        responses=[
            AIMessage(
                content=(
                    "## 目标\n完成\n## 已确认事实\n有证据\n## 决策\n保留\n"
                    "## 改动\n无\n## 测试\n通过\n## 未决项\n无\n## 归档\n无"
                )
            )
        ]
    )
    middleware = ContextWindowMiddleware(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(content="", tool_calls=[{"id": "call-a", "name": "read", "args": {}}]),
        ToolMessage(content="a" * 33_000, tool_call_id="call-a"),
        HumanMessage(content="第二轮"),
        AIMessage(content="", tool_calls=[{"id": "call-b", "name": "read", "args": {}}]),
        ToolMessage(content="b" * 33_000, tool_call_id="call-b"),
        AIMessage(content="recent " + "r" * 80_000),
        HumanMessage(content="第三轮"),
        HumanMessage(content="第四轮"),
    ]

    _projected, action, committed_ids, rewritten = await middleware._prepare(
        "full-after-micro", messages, 30_000
    )

    assert rewritten is True and action == "forced_summary" and model.calls == 1
    assert "/.harness/history/" not in str(model.responses[0].content)
    checkpoint = await store.load_latest_valid_compression_checkpoint("full-after-micro")
    assert checkpoint is not None and checkpoint.mode == "full"
    async with store._lock:
        cursor = await store._connection.execute(
            "SELECT mode FROM harness_compression_checkpoints "
            "WHERE project_fingerprint = ? AND thread_id = ? ORDER BY created_at_ms",
            (store.project_fingerprint, "full-after-micro"),
        )
        modes = [str(row["mode"]) for row in await cursor.fetchall()]
        await cursor.close()
    assert modes == ["full"]
    async with store._lock:
        cursor = await store._connection.execute(
            "SELECT artifact_id, kind FROM harness_context_artifacts "
            "WHERE project_fingerprint = ? AND thread_id = ? ORDER BY artifact_id",
            (store.project_fingerprint, "full-after-micro"),
        )
        artifact_rows = await cursor.fetchall()
        await cursor.close()
        cursor = await store._connection.execute(
            "SELECT artifact_ids FROM harness_context_summaries "
            "WHERE project_fingerprint = ? AND thread_id = ?",
            (store.project_fingerprint, "full-after-micro"),
        )
        summary_rows = await cursor.fetchall()
        await cursor.close()
    import json

    stored_artifact_ids = {str(row["artifact_id"]) for row in artifact_rows}
    summary_artifact_ids = set(json.loads(str(summary_rows[0]["artifact_ids"])))
    assert {str(row["kind"]) for row in artifact_rows} == {"tool", "history"}
    assert set(committed_ids) == stored_artifact_ids
    assert set(checkpoint.artifact_ids) == stored_artifact_ids
    assert summary_artifact_ids == stored_artifact_ids

    project = tmp_path / "project"
    await store.close()
    from harness_agent.threads.context_projection import ContextProjector
    from harness_agent.threads.thread_persistence import ThreadPersistence

    reopened = await ThreadPersistence.open(project=project, home=tmp_path / "home")
    recovered = await ContextProjector(reopened).project("full-after-micro")
    assert recovered.checkpoint is not None
    assert recovered.checkpoint.checkpoint_id == checkpoint.checkpoint_id
    assert set(recovered.checkpoint.artifact_ids) == stored_artifact_ids
    await reopened.close()


async def test_full_pressure_after_micro_uses_new_snapshot_for_retention(tmp_path):
    """hard 前 micro 后降到 full/hard 间时，full 改为普通摘要并保留两轮。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    summary = AIMessage(
        content=(
            "## 目标\n完成\n## 已确认事实\n有证据\n## 决策\n保留两轮\n"
            "## 改动\n无\n## 测试\n通过\n## 未决项\n无\n## 归档\n无"
        )
    )
    store = await _store(tmp_path)
    await accept_thread(store, "retention-after-micro", "第一轮")
    middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[summary]),
        context_window_tokens=32_768,
        thread_persistence=store,
    )
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(content="", tool_calls=[{"id": "ret-a", "name": "read", "args": {}}]),
        ToolMessage(content="a" * 26_000, tool_call_id="ret-a"),
        AIMessage(content="旧结论 " + "q" * 30_000),
        HumanMessage(content="第二轮"),
        AIMessage(content="", tool_calls=[{"id": "ret-b", "name": "read", "args": {}}]),
        ToolMessage(content="b" * 9_000, tool_call_id="ret-b"),
        HumanMessage(content="第三轮"),
    ]
    estimated = 30_000
    plan = await middleware._plan_dehydrate(
        messages,
        keep_turns=1,
        keep_recent=1,
        estimated_tokens=estimated,
    )
    assert plan is not None
    expected_after = middleware._measure_pressure(
        list(plan.messages), plan.after_tokens
    )
    assert expected_after.occupancy_ratio >= 0.80
    assert expected_after.occupancy_ratio < 0.90

    projected, action, _artifact_ids, rewritten = await middleware._prepare(
        "retention-after-micro", messages, estimated
    )

    assert action == "summary" and rewritten is True
    human_contents = [
        str(message.content)
        for message in projected
        if isinstance(message, HumanMessage)
    ]
    assert human_contents[-2:] == ["第二轮", "第三轮"]
    checkpoint = await store.load_latest_valid_compression_checkpoint(
        "retention-after-micro"
    )
    assert checkpoint is not None and checkpoint.mode == "full"
    assert checkpoint.pressure_before == expected_after.record()
    assert checkpoint.pressure_before["occupancy_ratio"] < 0.90
    await store.close()


async def test_full_pressure_failure_does_not_leave_micro_artifacts(tmp_path):
    """摘要失败时内存 micro 草稿不产生孤儿 Artifact 或 checkpoint。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    class CountingModel(FakeMessagesListChatModel):
        """记录失败路径是否错误地重复调用或提交。"""

        calls: ClassVar[int] = 0

        async def ainvoke(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            type(self).calls += 1
            return await super().ainvoke(*args, **kwargs)

    store = await _store(tmp_path)
    await accept_thread(store, "full-failure", "第一轮")
    model = CountingModel(responses=[AIMessage(content="")])
    middleware = ContextWindowMiddleware(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(content="", tool_calls=[{"id": "fail-a", "name": "read", "args": {}}]),
        ToolMessage(content="a" * 33_000, tool_call_id="fail-a"),
        HumanMessage(content="第二轮"),
        AIMessage(content="", tool_calls=[{"id": "fail-b", "name": "read", "args": {}}]),
        ToolMessage(content="b" * 33_000, tool_call_id="fail-b"),
        AIMessage(content="recent " + "r" * 80_000),
        HumanMessage(content="第三轮"),
        HumanMessage(content="第四轮"),
    ]

    original, action, artifact_ids, rewritten = await middleware._prepare(
        "full-failure", messages, 30_000
    )

    assert action == "summary_insufficient"
    assert rewritten is False and artifact_ids == () and model.calls == 1
    assert [message.content for message in original] == [
        message.content for message in messages
    ]
    async with store._lock:
        for table in (
            "harness_context_artifacts",
            "harness_context_summaries",
            "harness_compression_checkpoints",
        ):
            cursor = await store._connection.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE project_fingerprint = ? AND thread_id = ?",
                (store.project_fingerprint, "full-failure"),
            )
            assert (await cursor.fetchone())[0] == 0
            await cursor.close()
    await store.close()


async def test_micro_compression_is_idempotent_for_artifact_placeholders(tmp_path):
    """再次处理已替换的工具结果不创建第二份 Artifact。"""
    from harness_agent.threads.context_window import ContextWindowMiddleware

    store = await _store(tmp_path)
    await accept_thread(store, "micro-idempotent", "第一轮")
    middleware = ContextWindowMiddleware(
        FakeMessagesListChatModel(responses=[]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    messages = [
        HumanMessage(content="第一轮"),
        AIMessage(content="", tool_calls=[{"id": "call-a", "name": "read", "args": {}}]),
        ToolMessage(content="a" * 33_000, tool_call_id="call-a"),
        HumanMessage(content="第二轮"),
        AIMessage(content="", tool_calls=[{"id": "call-b", "name": "read", "args": {}}]),
        ToolMessage(content="b" * 33_000, tool_call_id="call-b"),
        HumanMessage(content="第三轮"),
        HumanMessage(content="第四轮"),
    ]
    dehydrated, first_ids, changed = await middleware._dehydrate(
        "micro-idempotent", messages, keep_turns=2
    )
    repeated, second_ids, repeated_changed = await middleware._dehydrate(
        "micro-idempotent", dehydrated, keep_turns=2
    )
    assert changed is True and first_ids
    assert repeated_changed is False and second_ids == ()
    assert [message.content for message in repeated] == [
        message.content for message in dehydrated
    ]
    await store.close()
