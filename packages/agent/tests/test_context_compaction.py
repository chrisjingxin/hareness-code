"""ZC-103 typed full compression、overflow 和运行态恢复测试。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import PrivateAttr

from harness_agent.context_compaction import (
    CompressionRequest,
    ContextCompactor,
    SUMMARY_INPUT_SAFETY_MARGIN_TOKENS,
    _render_message,
    _select_complete_summary_input,
)
from harness_agent.context_projection import ContextProjector
from harness_agent.context_window import ContextWindowMiddleware
from harness_agent.prompting import estimate_tokens
from harness_agent.runtime_state import (
    RuntimeExecutionPolicy,
    RuntimeStateError,
    RuntimeStateRehydrator,
    RuntimeStateSnapshot,
)
from harness_agent.thread_persistence import (
    AcceptRun,
    CommitContextRewrite,
    ContextState,
    ThreadPersistence,
    TranscriptAppend,
)
from thread_fixtures import test_binding as make_binding


SUMMARY = (
    "## 目标\n完成当前任务\n"
    "## 已确认事实\n输入中的事实已经核对\n"
    "## 决策\n保留可审计投影\n"
    "## 改动\n完成上下文闭环\n"
    "## 测试\n使用 fake 模型验证\n"
    "## 未决项\n无\n"
    "## 归档\n无"
)


async def _store(tmp_path: Path) -> ThreadPersistence:
    project = tmp_path / "project"
    project.mkdir()
    return await ThreadPersistence.open(project=project, home=tmp_path / "home")


async def _tool_thread(store: ThreadPersistence, thread_id: str) -> None:
    """建立四个完整 user/tool 原子组，供 micro/full 测试共同使用。"""
    await store.accept_run(
        AcceptRun(
            message="第一轮",
            binding=make_binding(thread_id, f"run-{thread_id}"),
        )
    )
    for suffix, user in (("a", "第二轮"), ("b", "第三轮")):
        call_id = f"call-{suffix}"
        await store.append_transcript(
            TranscriptAppend(
                thread_id=thread_id,
                record_id=f"assistant-{suffix}",
                kind="assistant",
                content="",
                tool_calls=({"id": call_id, "name": "read", "args": {}},),
            )
        )
        await store.append_transcript(
            TranscriptAppend(
                thread_id=thread_id,
                record_id=f"tool-{suffix}",
                kind="tool",
                content=(suffix * 20_000),
                tool_call_id=call_id,
                tool_name="read",
            )
        )
        await store.append_transcript(
            TranscriptAppend(
                thread_id=thread_id,
                record_id=f"user-{suffix}",
                kind="user",
                content=user,
            )
        )
    await store.append_transcript(
        TranscriptAppend(
            thread_id=thread_id,
            record_id="user-final",
            kind="user",
            content="第四轮",
        )
    )


async def _long_thread(store: ThreadPersistence, thread_id: str) -> None:
    """建立有完整前缀但没有工具调用的长历史，供 manual full 使用。"""
    await store.accept_run(
        AcceptRun(
            message="第一轮 " + "x" * 16_000,
            binding=make_binding(thread_id, f"run-{thread_id}"),
        )
    )
    await store.append_transcript(
        TranscriptAppend(
            thread_id=thread_id,
            record_id="assistant-long",
            kind="assistant",
            content="已确认 " + "y" * 16_000,
        )
    )
    for record_id, content in (
        ("user-two", "第二轮"),
        ("user-three", "第三轮"),
        ("user-four", "第四轮"),
    ):
        await store.append_transcript(
            TranscriptAppend(
                thread_id=thread_id,
                record_id=record_id,
                kind="user",
                content=content,
            )
        )


class CountingModel(FakeMessagesListChatModel):
    """离线摘要模型，记录调用次数，禁止真实 provider。"""

    calls: ClassVar[int] = 0

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        type(self).calls += 1
        return await super().ainvoke(*args, **kwargs)


class RecordingModel(CountingModel):
    """记录摘要 side query 的完整输入，验证真实 cap 和 framing。"""

    _inputs: list[tuple[Any, ...]] = PrivateAttr(default_factory=list)

    @property
    def inputs(self) -> list[tuple[Any, ...]]:
        """返回 fake model 收到的 typed message 列表。"""
        return self._inputs

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            self._inputs.append(tuple(args[0]))
        return await super().ainvoke(*args, **kwargs)


def _policy(
    execution_mode: str = "remote-sandbox",
    approval_mode: str = "yolo",
    capability_fingerprint: str = "current-policy",
) -> RuntimeExecutionPolicy:
    """构造测试使用的当前 typed 执行策略。"""
    return RuntimeExecutionPolicy(
        execution_mode=execution_mode,
        approval_mode=approval_mode,
        capability_fingerprint=capability_fingerprint,
    )


def _request(thread_id: str, projection: Any, trigger: str, estimated: int | None = None) -> CompressionRequest:
    """构造生产 service 使用的 typed request。"""
    return CompressionRequest(
        thread_id=thread_id,
        trigger=trigger,  # type: ignore[arg-type]
        projection=projection,
        estimated_tokens=estimated,
    )


def _message_with_summary_payload_tokens(
    service: ContextCompactor, target: int
) -> HumanMessage:
    """生成指定摘要正文预算的单一 user 原子组，不依赖厂商 tokenizer。"""
    measure = service._summary_payload_tokens
    upper = max(128, target * 4 + 4_096)
    low = 0
    high = upper
    while low < high:
        middle = (low + high) // 2
        if measure((HumanMessage(content="x" * middle),)) >= target:
            high = middle
        else:
            low = middle + 1
    message = HumanMessage(content="x" * low)
    assert measure((message,)) == target
    return message


@pytest.mark.asyncio
async def test_auto_micro_after_measure_does_not_call_summary_or_create_full_checkpoint(tmp_path: Path) -> None:
    """micro 释放足够空间时摘要模型不调用，且只提交 micro checkpoint。"""
    store = await _store(tmp_path)
    await _tool_thread(store, "auto-micro")
    projection = await ContextProjector(store).project("auto-micro")
    CountingModel.calls = 0
    model = CountingModel(responses=[])
    service = ContextCompactor(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )

    result = await service.compress(
        _request("auto-micro", projection, "auto", estimated=8_000)
    )

    assert result.outcome == "compressed"
    assert result.action == "auto_micro"
    assert result.checkpoint is not None and result.checkpoint.mode == "micro"
    assert CountingModel.calls == 0
    assert await store.load_latest_valid_compression_checkpoint("auto-micro") is not None
    async with store._lock:
        cursor = await store._connection.execute(
            "SELECT COUNT(*) FROM harness_compression_checkpoints "
            "WHERE project_fingerprint = ? AND thread_id = ? AND mode = 'full'",
            (store.project_fingerprint, "auto-micro"),
        )
        assert (await cursor.fetchone())[0] == 0
        await cursor.close()
    await store.close()


@pytest.mark.asyncio
async def test_auto_micro_full_commits_only_final_full_checkpoint(tmp_path: Path) -> None:
    """micro 后仍超过 full 时只在同一事务提交最终 full。"""
    store = await _store(tmp_path)
    await _tool_thread(store, "auto-full")
    projection = await ContextProjector(store).project("auto-full")
    CountingModel.calls = 0
    model = CountingModel(responses=[AIMessage(content=SUMMARY)])
    service = ContextCompactor(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )

    result = await service.compress(
        _request("auto-full", projection, "auto", estimated=30_000)
    )

    assert result.outcome == "compressed"
    assert result.action == "auto_full"
    assert result.checkpoint is not None and result.checkpoint.mode == "full"
    assert CountingModel.calls == 1
    async with store._lock:
        cursor = await store._connection.execute(
            "SELECT mode FROM harness_compression_checkpoints "
            "WHERE project_fingerprint = ? AND thread_id = ? ORDER BY created_at_ms",
            (store.project_fingerprint, "auto-full"),
        )
        assert [str(row["mode"]) for row in await cursor.fetchall()] == ["full"]
        await cursor.close()
    recovered = await ContextProjector(store).project("auto-full")
    assert recovered.messages == result.projected_messages
    await store.close()


@pytest.mark.asyncio
async def test_manual_force_full_bypasses_auto_circuit_but_short_history_is_typed_skip(tmp_path: Path) -> None:
    """manual 不受 auto breaker 阻断，但短历史不能假成功。"""
    store = await _store(tmp_path)
    await store.accept_run(
        AcceptRun(message="短历史", binding=make_binding("short", "run-short"))
    )
    await store.commit_context(
        CommitContextRewrite(
            thread_id="short",
            state=ContextState(failures=3, circuit_open=True, last_action="auto_failed"),
        )
    )
    short_projection = await ContextProjector(store).project("short")
    CountingModel.calls = 0
    short_service = ContextCompactor(
        CountingModel(responses=[AIMessage(content=SUMMARY)]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    skipped = await short_service.compress(_request("short", short_projection, "manual"))
    assert skipped.outcome == "skipped"
    assert skipped.action == "manual_skipped"
    assert skipped.reason == "short_history"
    assert CountingModel.calls == 0

    await _long_thread(store, "manual-full")
    long_projection = await ContextProjector(store).project("manual-full")
    CountingModel.calls = 0
    manual_service = ContextCompactor(
        CountingModel(responses=[AIMessage(content=SUMMARY)]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    full = await manual_service.compress(_request("manual-full", long_projection, "manual"))
    assert full.outcome == "compressed"
    assert full.action == "manual_full"
    assert full.state is not None and full.state.failures == 0
    assert CountingModel.calls == 1
    await store.close()


@pytest.mark.asyncio
async def test_invalid_summary_keeps_previous_valid_projection_without_partial_rows(tmp_path: Path) -> None:
    """空摘要失败不写 Artifact/Summary/Checkpoint，旧投影保持可恢复。"""
    store = await _store(tmp_path)
    await _long_thread(store, "invalid-summary")
    projection = await ContextProjector(store).project("invalid-summary")
    service = ContextCompactor(
        CountingModel(responses=[AIMessage(content="")]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )

    result = await service.compress(_request("invalid-summary", projection, "manual"))

    assert result.outcome == "failed"
    assert result.reason == "summary_empty"
    assert result.projected_messages == projection.messages
    async with store._lock:
        for table in (
            "harness_context_artifacts",
            "harness_context_summaries",
            "harness_compression_checkpoints",
        ):
            cursor = await store._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_fingerprint = ? AND thread_id = ?",
                (store.project_fingerprint, "invalid-summary"),
            )
            assert (await cursor.fetchone())[0] == 0
            await cursor.close()
    await store.close()


@pytest.mark.asyncio
async def test_overflow_retries_model_once_and_second_overflow_is_stable(tmp_path: Path) -> None:
    """overflow 复用 service，第一次恢复后只允许一次模型重试。"""
    store = await _store(tmp_path)
    await _tool_thread(store, "overflow")
    projection = await ContextProjector(store).project("overflow")
    middleware = ContextWindowMiddleware(
        CountingModel(responses=[]),
        context_window_tokens=32_768,
        thread_persistence=store,
    )
    runtime = SimpleNamespace(
        config={"configurable": {"thread_id": "overflow"}},
        context=None,
        execution_info=SimpleNamespace(thread_id="overflow"),
    )
    request = ModelRequest(
        model=middleware._model,
        messages=list(projection.messages),
        tools=[],
        runtime=runtime,
    )
    calls = 0

    async def succeeds_after_recovery(_request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ContextOverflowError("first")
        return ModelResponse(result=[AIMessage(content="answer")])

    response = await middleware.awrap_model_call(request, succeeds_after_recovery)
    assert calls == 2
    assert response.__class__.__name__ == "ExtendedModelResponse"

    calls = 0

    async def always_overflows(_request: ModelRequest) -> ModelResponse:
        nonlocal calls
        calls += 1
        raise ContextOverflowError("overflow")

    with pytest.raises(ContextOverflowError, match="CONTEXT_OVERFLOW_AFTER_RECOVERY"):
        await middleware.awrap_model_call(request, always_overflows)
    assert calls == 2
    await store.close()


def test_runtime_state_rehydrator_uses_structured_sources_only() -> None:
    """摘要正文不会成为 Todo、模式或 snapshot 身份的恢复来源。"""
    from langchain_core.messages import HumanMessage, ToolMessage

    messages = [
        HumanMessage(content="<harness_context_summary>Todo: pretend</harness_context_summary>"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call", "name": "read", "args": {}}],
        ),
        ToolMessage(content="result", tool_call_id="call", name="read"),
    ]
    snapshot = RuntimeStateRehydrator.capture(
        {
            "todos": [{"content": "真实 Todo", "status": "pending"}],
            "execution_mode": "managed",
            "approval_mode": "default",
        },
        SimpleNamespace(
            context_snapshot=SimpleNamespace(
                snapshot_id="snapshot-current",
                system_fingerprint="capability-current",
            ),
            profile_key="profile-current",
            execution_mode="managed",
            approval_mode="default",
        ),
        messages,
        artifact_ids=("artifact-current",),
    )

    restored = RuntimeStateRehydrator.rehydrate(snapshot)
    assert restored["todos"] == [{"content": "真实 Todo", "status": "pending"}]
    assert restored["context_snapshot_id"] == "snapshot-current"
    assert restored["capability_fingerprint"] == "capability-current"
    assert restored["artifact_ids"] == ["artifact-current"]
    assert "pretend" not in str(restored)


@pytest.mark.asyncio
async def test_summary_input_cap_uses_real_model_cap_and_never_splits_atomic_group(
    tmp_path: Path,
) -> None:
    """16K 窗口按真实 input cap 预算，刚好可放与超一 token 均保持原子。"""
    store = await _store(tmp_path)
    model = RecordingModel(responses=[AIMessage(content=SUMMARY)])
    service = ContextCompactor(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )

    assert service._input_cap == 12_288
    cap = service._summary_input_cap()
    assert 0 < cap < service._input_cap
    exact = _message_with_summary_payload_tokens(service, cap)
    over = _message_with_summary_payload_tokens(service, cap + 1)

    assert _select_complete_summary_input(
        (exact,), cap, measure=service._summary_payload_tokens
    ) == (exact,)
    assert _select_complete_summary_input(
        (over,), cap, measure=service._summary_payload_tokens
    ) is None
    assert _select_complete_summary_input(
        (HumanMessage(content="older"), over),
        cap,
        measure=service._summary_payload_tokens,
    ) is None
    ordered = (HumanMessage(content="older"), HumanMessage(content="newer"))
    assert _select_complete_summary_input(
        ordered, cap, measure=service._summary_payload_tokens
    ) == ordered

    await store.close()


@pytest.mark.asyncio
async def test_summary_model_input_including_prompt_and_framing_stays_below_real_cap(
    tmp_path: Path,
) -> None:
    """摘要 side query 的完整 System/Human 输入不能超过真实模型 cap。"""
    store = await _store(tmp_path)
    await _long_thread(store, "summary-cap")
    projection = await ContextProjector(store).project("summary-cap")
    RecordingModel.calls = 0
    model = RecordingModel(responses=[AIMessage(content=SUMMARY)])
    service = ContextCompactor(
        model,
        context_window_tokens=16_384,
        thread_persistence=store,
    )

    result = await service.compress(_request("summary-cap", projection, "manual"))

    assert result.outcome == "compressed"
    assert RecordingModel.calls == 1
    assert len(model.inputs) == 1
    sent_messages = model.inputs[0]
    sent_tokens = sum(estimate_tokens(_render_message(message)) for message in sent_messages)
    assert sent_tokens <= service._input_cap
    assert (
        sent_tokens + SUMMARY_INPUT_SAFETY_MARGIN_TOKENS <= service._input_cap
    )
    await store.close()


@pytest.mark.asyncio
async def test_exhausted_input_cap_is_typed_skip_without_auto_failure(tmp_path: Path) -> None:
    """输出预留耗尽输入预算时不调用模型，也不推进自动熔断。"""
    store = await _store(tmp_path)
    await _long_thread(store, "cap-exhausted")
    projection = await ContextProjector(store).project("cap-exhausted")
    CountingModel.calls = 0
    service = ContextCompactor(
        CountingModel(responses=[]),
        context_window_tokens=4_096,
        thread_persistence=store,
    )

    result = await service.compress(
        _request("cap-exhausted", projection, "auto", estimated=20_000)
    )

    assert result.outcome == "skipped"
    assert result.reason == "input_cap_exhausted"
    assert CountingModel.calls == 0
    assert result.state is not None and result.state.failures == 0
    assert not result.state.circuit_open
    await store.close()


@pytest.mark.asyncio
async def test_manual_full_uses_current_policy_and_snapshot_over_persisted_runtime_state(
    tmp_path: Path,
) -> None:
    """手动 full 使用当前 typed 策略和 snapshot，不沿用旧权限或串到别的 Thread。"""
    store = await _store(tmp_path)
    await _long_thread(store, "manual-current-policy")
    await store.accept_run(
        AcceptRun(
            message="另一个 Thread",
            binding=make_binding("other-thread", "run-other"),
        )
    )
    old_runtime = RuntimeStateSnapshot(
        todos=({"content": "old todo", "status": "pending"},),
        execution_mode="old-local",
        approval_mode="old-default",
        context_snapshot_id="old-snapshot",
        capability_fingerprint="old-policy",
    )
    await store.commit_context(
        CommitContextRewrite(
            thread_id="manual-current-policy",
            state=ContextState(runtime_state=old_runtime),
        )
    )
    other_before = await store.load_context_state("other-thread")
    projection = await ContextProjector(store).project("manual-current-policy")
    current_policy = _policy(
        execution_mode="remote-sandbox",
        approval_mode="yolo",
        capability_fingerprint="current-policy",
    )
    current_snapshot = SimpleNamespace(
        thread_id="manual-current-policy",
        snapshot_id="current-snapshot",
        system_fingerprint="current-snapshot-capability",
    )
    service = ContextCompactor(
        CountingModel(responses=[AIMessage(content=SUMMARY)]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )

    result = await service.compress(
        CompressionRequest(
            thread_id="manual-current-policy",
            trigger="manual",
            projection=projection,
            runtime_state=old_runtime,
            current_execution_policy=current_policy,
            run_context_snapshot=current_snapshot,
        )
    )

    assert result.outcome == "compressed"
    assert result.state is not None and result.state.runtime_state is not None
    runtime = result.state.runtime_state
    assert runtime.todos == old_runtime.todos
    assert runtime.execution_mode == "remote-sandbox"
    assert runtime.approval_mode == "yolo"
    assert runtime.context_snapshot_id == "current-snapshot"
    assert runtime.capability_fingerprint == "current-snapshot-capability"
    assert await store.load_context_state("other-thread") == other_before
    await store.close()


@pytest.mark.parametrize("field", (
    "execution_mode",
    "approval_mode",
    "context_snapshot_id",
    "capability_fingerprint",
))
@pytest.mark.parametrize(
    "malformed",
    (
        pytest.param(True, id="bool"),
        pytest.param(1, id="int"),
        pytest.param([], id="list"),
        pytest.param({}, id="dict"),
        pytest.param(float("nan"), id="nan"),
    ),
)
def test_runtime_state_record_rejects_non_string_fields(
    field: str, malformed: object
) -> None:
    """持久化 decode 不把恶意非字符串字段洗成空权限。"""
    with pytest.raises(RuntimeStateError):
        RuntimeStateSnapshot.from_record({field: malformed})


@pytest.mark.parametrize("field", (
    "execution_mode",
    "approval_mode",
    "context_snapshot_id",
    "capability_fingerprint",
))
@pytest.mark.parametrize(
    "malformed",
    (
        pytest.param(True, id="bool"),
        pytest.param(1, id="int"),
        pytest.param([], id="list"),
        pytest.param({}, id="dict"),
        pytest.param(float("nan"), id="nan"),
    ),
)
def test_runtime_state_capture_rejects_non_string_langgraph_fields(
    field: str, malformed: object
) -> None:
    """capture 也拒绝 LangGraph channel 中存在但类型错误的权限/身份字段。"""
    with pytest.raises(RuntimeStateError):
        RuntimeStateRehydrator.capture(
            {field: malformed},
            None,
            (HumanMessage(content="当前消息"),),
        )


def test_runtime_state_missing_none_and_str_enum_remain_compatible() -> None:
    """字段缺失/None 保持兼容默认，合法 str Enum 正常归一化。"""
    class ApprovalLabel(str, Enum):
        DEFAULT = "default"

    record = RuntimeStateSnapshot.from_record(
        {
            "execution_mode": None,
            "approval_mode": ApprovalLabel.DEFAULT,
        }
    )
    assert record.execution_mode == ""
    assert record.approval_mode == "default"
    captured = RuntimeStateRehydrator.capture(
        {
            "execution_mode": None,
            "approval_mode": ApprovalLabel.DEFAULT,
        },
        None,
        (HumanMessage(content="当前消息"),),
    )
    assert captured.execution_mode == ""
    assert captured.approval_mode == "default"


@pytest.mark.asyncio
async def test_malformed_runtime_capture_keeps_previous_latest_valid_state_and_rows(
    tmp_path: Path,
) -> None:
    """运行态 decode 失败时不留下摘要、Artifact、checkpoint 或新 state。"""
    store = await _store(tmp_path)
    await _long_thread(store, "runtime-fail-closed")
    projection = await ContextProjector(store).project("runtime-fail-closed")
    valid = ContextCompactor(
        CountingModel(responses=[AIMessage(content=SUMMARY)]),
        context_window_tokens=16_384,
        thread_persistence=store,
    )
    committed = await valid.compress(
        _request("runtime-fail-closed", projection, "manual")
    )
    assert committed.outcome == "compressed"
    previous_checkpoint = await store.load_latest_valid_compression_checkpoint(
        "runtime-fail-closed"
    )
    assert previous_checkpoint is not None
    previous_state = await store.load_context_state("runtime-fail-closed")

    async def malformed_runtime_state(
        _thread_id: str, _run_context: object, messages: Any
    ) -> RuntimeStateSnapshot:
        return RuntimeStateRehydrator.capture(
            {"execution_mode": float("nan")},
            None,
            messages,
        )

    async def counts() -> tuple[int, int, int, int]:
        async with store._lock:
            values: list[int] = []
            for table in (
                "harness_context_artifacts",
                "harness_context_summaries",
                "harness_compression_checkpoints",
                "harness_context_state",
            ):
                cursor = await store._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_fingerprint = ? AND thread_id = ?",
                    (store.project_fingerprint, "runtime-fail-closed"),
                )
                values.append(int((await cursor.fetchone())[0]))
                await cursor.close()
            return tuple(values)  # type: ignore[return-value]

    previous_counts = await counts()
    failing = ContextCompactor(
        CountingModel(responses=[AIMessage(content=SUMMARY)]),
        context_window_tokens=16_384,
        thread_persistence=store,
        runtime_state_provider=malformed_runtime_state,
    )
    result = await failing.compress(
        _request("runtime-fail-closed", projection, "manual")
    )

    assert result.outcome == "failed"
    assert "RUNTIME_STATE_EXECUTION_MODE_TYPE_INVALID" in (result.reason or "")
    current_checkpoint = await store.load_latest_valid_compression_checkpoint(
        "runtime-fail-closed"
    )
    assert current_checkpoint is not None
    assert current_checkpoint.checkpoint_id == previous_checkpoint.checkpoint_id
    assert current_checkpoint.projected_messages == previous_checkpoint.projected_messages
    assert await store.load_context_state("runtime-fail-closed") == previous_state
    assert await counts() == previous_counts
    await store.close()
