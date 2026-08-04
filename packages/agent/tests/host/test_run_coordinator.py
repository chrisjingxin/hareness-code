"""RunCoordinator 的领域生命周期测试，不依赖 AgentHost 或 JSON-RPC transport。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from harness_agent.host.run_coordinator import (
    ConnectionRef,
    InteractionResult,
    MAX_TOOL_PAYLOAD_BYTES,
    RunCoordinator,
    RunError,
    RunPreparation,
    RunRuntime,
    RunState,
    StartRun,
    _capture_transcript_message,
    _extract_interaction,
    _translate_stream_event,
)
from harness_agent.runtime.execution_binding import ExecutionRef
from harness_agent.threads.thread_persistence import (
    ThreadPersistence,
    ThreadPersistenceError,
    TranscriptAppend,
)
from tests.support.thread_fixtures import test_binding as make_test_binding


class _NoopInteraction:
    """测试用 InteractionPort；本组用例不需要真正的反向请求。"""

    async def request(self, _owner, _run, _interaction) -> InteractionResult:
        return InteractionResult({"decision": "reject"})


def _coordinator(releases: list[str]) -> RunCoordinator:
    async def persistence_provider():
        return None

    async def preparation_provider(_command, _persistence):
        return RunPreparation()

    async def runtime_provider(run) -> RunRuntime:
        async def release() -> None:
            releases.append(run.ref.run_id)

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    return RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )


async def _events(execution) -> list:
    return [event async for event in execution.events]


@pytest.mark.asyncio
async def test_run_coordinator_enforces_owner_busy_and_single_terminal_event() -> None:
    """生命周期边界由 Coordinator interface 负责，不需要构造 Server。"""
    releases: list[str] = []
    coordinator = _coordinator(releases)
    owner = ConnectionRef("owner")
    other = ConnectionRef("other")

    execution = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello"),
        owner,
    )
    with pytest.raises(RunError, match="THREAD_BUSY") as busy:
        await coordinator.start(
            StartRun(thread_id="thread", run_id="run-2", message="busy"),
            owner,
        )
    assert busy.value.code == "THREAD_BUSY"
    with pytest.raises(RunError) as not_owner:
        await coordinator.cancel(execution.ref, other)
    assert not_owner.value.code == "RUN_NOT_OWNER"

    cancelled = await coordinator.cancel(execution.ref, owner)
    assert cancelled.cancelled is True
    events = await _events(execution)
    assert [event.type for event in events] == ["run.cancelled"]
    assert events[0].execution_id == "root-run-1"
    assert await coordinator.execution_registry.list(
        ExecutionRef.root("thread", "run-1")
    ) == ()


@pytest.mark.asyncio
async def test_run_coordinator_releases_runtime_and_completes_once() -> None:
    """正常执行只发一个 completed 终态，并释放本次 Runtime。"""
    releases: list[str] = []
    coordinator = _coordinator(releases)
    execution = await coordinator.start(
        StartRun(thread_id="thread", run_id="run-1", message="hello"),
        ConnectionRef("owner"),
    )

    events = await _events(execution)
    assert [event.type for event in events] == [
        "run.started",
        "content.delta",
        "run.completed",
    ]
    assert {event.execution_id for event in events} == {"root-run-1"}
    assert {event.agent_id for event in events} == {"main"}
    assert events[0].record()["execution_id"] == "root-run-1"
    assert releases == ["run-1"]
    assert await coordinator.is_active("thread") is False
    assert await coordinator.execution_registry.list(
        ExecutionRef.root("thread", "run-1")
    ) == ()


@pytest.mark.asyncio
async def test_close_before_run_task_starts_releases_snapshot_reservation_once() -> None:
    """尚未取得首个时间片的 Run 也必须释放 Host reservation，且只释放一次。"""
    release_calls = 0
    runtime_started = False

    class Reservation:
        async def release(self) -> None:
            nonlocal release_calls
            release_calls += 1

    async def persistence_provider():
        return None

    async def preparation_provider(_command, _persistence):
        return RunPreparation(snapshot_reservation=Reservation())

    async def runtime_provider(_run) -> RunRuntime:
        nonlocal runtime_started
        runtime_started = True
        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=lambda: _noop_async(),
        )

    async def _noop_async() -> None:
        return None

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )

    await coordinator.start(
        StartRun(thread_id="thread-not-started", run_id="run-1", message="hello"),
        ConnectionRef("owner"),
    )
    await coordinator.close()

    assert runtime_started is False
    assert release_calls == 1
    await coordinator.close()
    assert release_calls == 1


@pytest.mark.asyncio
async def test_transcript_capture_keeps_full_tool_text_before_wire_truncation() -> None:
    """语义层先捕获完整 ToolMessage，wire 仍可独立按 1 MiB 截断。"""
    run = RunState(
        start=StartRun(thread_id="thread-transcript", run_id="run-raw", message="读取"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        AIMessageChunk(
            content="完整助手回答",
            id="assistant-1",
            tool_call_chunks=[
                {"index": 0, "id": "call-1", "name": "read_file", "args": ""}
            ],
        ),
    )
    full_tool_text = "x" * (MAX_TOOL_PAYLOAD_BYTES + 1)
    tool = ToolMessage(
        content=full_tool_text,
        tool_call_id="call-1",
        name="read_file",
    )

    _capture_transcript_message(run, tool)
    assert [record.kind for record in run.pending_transcript] == ["assistant", "tool"]
    assert run.pending_transcript[-1].content == full_tool_text

    wire_events = list(_translate_stream_event(("messages", (tool, {})), run))
    result = next(payload["result"] for kind, payload in wire_events if kind == "tool.completed")
    assert result["truncated"] is True
    assert result["original_bytes"] == len(full_tool_text.encode("utf-8"))
    assert result["content"] != full_tool_text


@pytest.mark.asyncio
async def test_transcript_capture_groups_chunks_without_stable_ids_into_one_assistant() -> None:
    """无 ID 或变 ID 的 assistant delta 仍属于同一完整助手消息。"""
    run = RunState(
        start=StartRun(thread_id="thread-chunks", run_id="run-chunks", message="分片"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    for chunk in (
        AIMessageChunk(
            content="第一段",
            tool_call_chunks=[
                {"index": 0, "id": "call-1", "name": "read_file", "args": ""}
            ],
        ),
        AIMessageChunk(content="第二段"),
        AIMessageChunk(content="第三段", id="provider-id-changed"),
    ):
        _capture_transcript_message(run, chunk)

    _capture_transcript_message(
        run,
        ToolMessage(content="边界", tool_call_id="call-1", name="read_file"),
    )
    assert [(record.kind, record.content) for record in run.pending_transcript] == [
        ("assistant", "第一段第二段第三段"),
        ("tool", "边界"),
    ]


def test_idless_tool_call_index_is_reset_between_model_rounds() -> None:
    """同一 index 的无 ID 工具调用跨回合必须使用不同的 Run ordinal。"""
    run = RunState(
        start=StartRun(thread_id="thread-rounds", run_id="run-rounds", message="连续执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    first_call = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": None, "name": "execute", "args": "pwd"}
            ],
        },
    )()
    second_call = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": None, "name": "execute", "args": "ls"}
            ],
        },
    )()
    first_result = type(
        "ToolMessage",
        (),
        {"content": "first", "tool_call_id": "", "name": "execute", "status": "success"},
    )()
    second_result = type(
        "ToolMessage",
        (),
        {"content": "second", "tool_call_id": "", "name": "execute", "status": "success"},
    )()

    _capture_transcript_message(run, first_call)
    _capture_transcript_message(run, first_result)
    _capture_transcript_message(run, second_call)
    _capture_transcript_message(run, second_result)

    tool_records = [record for record in run.pending_transcript if record.kind == "tool"]
    assert [record.content for record in tool_records] == ["first", "second"]
    assert [record.tool_call_id for record in tool_records] == [
        "tool-run-rounds-1",
        "tool-run-rounds-2",
    ]
    assert [record.record_id for record in tool_records] == [
        "run:run-rounds:tool:tool-run-rounds-1",
        "run:run-rounds:tool:tool-run-rounds-2",
    ]


def test_parallel_idless_tool_results_fail_closed() -> None:
    """并行无 ID 结果无法可靠归属时不能静默合并到一个记录。"""
    run = RunState(
        start=StartRun(thread_id="thread-parallel", run_id="run-parallel", message="并行执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        type(
            "AIMessageChunk",
            (),
            {
                "content": "",
                "usage_metadata": None,
                "tool_call_chunks": [
                    {"index": 0, "id": None, "name": "one", "args": ""},
                    {"index": 1, "id": None, "name": "two", "args": ""},
                ],
            },
        )(),
    )
    with pytest.raises(RunError, match="cannot be associated safely"):
        _capture_transcript_message(
            run,
            type(
                "ToolMessage",
                (),
                {
                    "content": "ambiguous",
                    "tool_call_id": "",
                    "name": "tool",
                    "status": "success",
                },
            )(),
        )


def test_tool_result_provider_id_mismatch_does_not_guess_unique_stable_call() -> None:
    """已有 provider ID=A 时，结果 ID=B 不能借唯一候选猜配。"""
    run = RunState(
        start=StartRun(thread_id="thread-id-mismatch", run_id="run-id-mismatch", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        AIMessage(
            content="",
            tool_calls=[{"id": "provider-a", "name": "execute", "args": {"cmd": "pwd"}}],
        ),
    )
    with pytest.raises(RunError, match="does not match"):
        _capture_transcript_message(
            run,
            ToolMessage(content="结果", tool_call_id="provider-b", name="execute"),
        )
    assert [record.kind for record in run.pending_transcript] == ["assistant"]
    assert run.pending_transcript[0].tool_calls[0]["id"] == "provider-a"


def test_orphan_tool_result_does_not_create_transcript_call() -> None:
    """没有前置 assistant tool call 时，结果不能凭空生成孤儿 ID。"""
    run = RunState(
        start=StartRun(thread_id="thread-orphan-result", run_id="run-orphan-result", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    with pytest.raises(RunError, match="preceding assistant"):
        _capture_transcript_message(
            run,
            ToolMessage(content="孤儿结果", tool_call_id="provider-b", name="execute"),
        )
    assert run.pending_transcript == []


def test_idless_assistant_allows_late_provider_result_id_binding() -> None:
    """无 provider ID 的唯一 assistant 候选允许结果 ID 到达时绑定内部 ID。"""
    run = RunState(
        start=StartRun(thread_id="thread-late-id", run_id="run-late-id", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": None, "name": "execute", "args": '{"cmd":"pwd"}'}
            ],
        ),
    )
    _capture_transcript_message(
        run,
        ToolMessage(content="结果", tool_call_id="late-provider-id", name="execute"),
    )
    assistant = next(record for record in run.pending_transcript if record.kind == "assistant")
    tool = next(record for record in run.pending_transcript if record.kind == "tool")
    assert assistant.tool_calls[0]["id"] == tool.tool_call_id
    assert assistant.tool_calls[0]["id"] != "late-provider-id"


def test_idless_unindexed_second_named_call_fails_closed() -> None:
    """无 ID、无 index 的第二个明确 call start 不能静默并入 current。"""
    run = RunState(
        start=StartRun(thread_id="thread-unindexed-calls", run_id="run-unindexed-calls", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    chunk = lambda name, args: type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [{"index": None, "id": None, "name": name, "args": args}],
        },
    )()
    _capture_transcript_message(run, chunk("execute", "pwd"))
    with pytest.raises(RunError, match="without index"):
        _capture_transcript_message(run, chunk("execute", "ls"))


def test_tool_chunk_id_change_with_same_index_stays_one_call() -> None:
    """同一回合的 provider ID 变化不能把一个 index 拆成两次工具调用。"""
    run = RunState(
        start=StartRun(thread_id="thread-id-change", run_id="run-id-change", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    first = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": "provider-a", "name": "execute", "args": ""}
            ],
        },
    )()
    changed = type(
        "AIMessageChunk",
        (),
        {
            "content": "",
            "usage_metadata": None,
            "tool_call_chunks": [
                {"index": 0, "id": "provider-b", "name": None, "args": "pwd"}
            ],
        },
    )()
    result = type(
        "ToolMessage",
        (),
        {
            "content": "done",
            "tool_call_id": "provider-b",
            "name": "execute",
            "status": "success",
        },
    )()

    _capture_transcript_message(run, first)
    _capture_transcript_message(run, changed)
    _capture_transcript_message(run, result)

    tools = [record for record in run.pending_transcript if record.kind == "tool"]
    assert len(tools) == 1
    assert tools[0].tool_call_id == "provider-a"


def test_tool_call_chunks_round_trip_json_arguments_and_invalid_raw() -> None:
    """AIMessageChunk 参数分片聚合后可恢复，半条 JSON 保留 raw/status。"""
    complete = RunState(
        start=StartRun(thread_id="thread-chunk-args", run_id="run-chunk-args", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    for chunk in (
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": "chunk-call", "name": "execute", "args": '{"cmd":'}
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": None, "name": None, "args": '"pwd"}'}
            ],
        ),
    ):
        _capture_transcript_message(complete, chunk)
    _capture_transcript_message(
        complete,
        ToolMessage(content="done", tool_call_id="chunk-call", name="execute"),
    )
    assert complete.pending_transcript[0].tool_calls[0]["arguments"] == {"cmd": "pwd"}
    assert complete.pending_transcript[0].tool_calls[0]["arguments_status"] == "valid"
    assert complete.pending_transcript[0].tool_calls[0]["arguments_raw"] == '{"cmd":"pwd"}'

    invalid = RunState(
        start=StartRun(thread_id="thread-chunk-invalid", run_id="run-chunk-invalid", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        invalid,
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"index": 0, "id": "invalid-call", "name": "execute", "args": '{"cmd":'}
            ],
        ),
    )
    _capture_transcript_message(
        invalid,
        ToolMessage(content="done", tool_call_id="invalid-call", name="execute"),
    )
    assert invalid.pending_transcript[0].tool_calls[0]["arguments_status"] == "partial"
    assert invalid.pending_transcript[0].tool_calls[0]["arguments_raw"] == '{"cmd":'


@pytest.mark.parametrize("value", [{1}, float("nan"), float("inf"), -float("inf")])
def test_non_json_provider_chunk_arguments_are_invalid_not_stringified_valid(
    value,
) -> None:
    """非 JSON/非有限 provider 参数不能由字符串化伪装成 valid。"""
    run = RunState(
        start=StartRun(thread_id="thread-non-json", run_id="run-non-json", message="执行"),
        owner=ConnectionRef("owner"),
        persistence=None,
        preparation=RunPreparation(),
    )
    _capture_transcript_message(
        run,
        type(
            "AIMessageChunk",
            (),
            {
                "content": "",
                "usage_metadata": None,
                "tool_call_chunks": [
                    {"index": 0, "id": "non-json", "name": "execute", "args": value}
                ],
            },
        )(),
    )
    _capture_transcript_message(
        run,
        ToolMessage(content="done", tool_call_id="non-json", name="execute"),
    )
    call = run.pending_transcript[0].tool_calls[0]
    assert call["arguments_status"] == "invalid"
    assert "arguments_raw" in call
    assert "arguments" not in call


def test_child_namespace_interrupt_does_not_trigger_root_interaction() -> None:
    """Protocol v3 无 provenance 时，child interrupt 不能提升为 root request。"""
    child_event = (
        ("child", "approval-node"),
        "updates",
        {
            "__interrupt__": [
                {
                    "id": "child-interrupt",
                    "value": {
                        "type": "ask_user",
                        "questions": [{"question": "子图问题", "choices": []}],
                    },
                }
            ]
        },
    )
    assert _extract_interaction(child_event) is None

    root_event = (
        "updates",
        {
            "__interrupt__": [
                {
                    "id": "root-interrupt",
                    "value": {
                        "type": "ask_user",
                        "questions": [{"question": "根问题", "choices": []}],
                    },
                }
            ]
        },
    )
    assert _extract_interaction(root_event) is not None


@pytest.mark.asyncio
async def test_tool_call_only_assistant_round_trips_arguments_and_conflicts_on_change(
    tmp_path,
) -> None:
    """无正文 assistant 也持久化完整参数，幂等重试不能吞掉参数差异。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-tool-calls", "读取 README", run_id="run-tool-calls")
    try:
        run = RunState(
            start=StartRun(thread_id="thread-tool-calls", run_id="run-tool-calls", message="读取 README"),
            owner=ConnectionRef("owner"),
            persistence=store,
            preparation=RunPreparation(),
        )
        _capture_transcript_message(
            run,
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-read",
                        "name": "read_file",
                        "args": {"path": "README.md"},
                    }
                ],
            ),
        )
        _capture_transcript_message(
            run,
            ToolMessage(content="README 内容", tool_call_id="call-read", name="read_file"),
        )
        await _coordinator([])._flush_transcript(run)

        records = await store.load_transcript("thread-tool-calls")
        assistant = records[1]
        assert assistant.kind == "assistant"
        assert assistant.payload["content"] == ""
        assert assistant.payload["tool_calls"] == [
            {
                "id": "call-read",
                "name": "read_file",
                "type": "tool_call",
                "arguments": {"path": "README.md"},
                "arguments_json": '{"path":"README.md"}',
                "arguments_status": "valid",
            }
        ]
        assert records[2].payload["tool_call_id"] == "call-read"

        with pytest.raises(ThreadPersistenceError, match="TRANSCRIPT_RECORD_CONFLICT"):
            await store.append_transcript(
                TranscriptAppend(
                    thread_id="thread-tool-calls",
                    record_id=assistant.record_id,
                    kind="assistant",
                    content="",
                    run_id="run-tool-calls",
                    execution_id="root-run-tool-calls",
                    tool_calls=(
                        {
                            "id": "call-read",
                            "name": "read_file",
                            "arguments": {"path": "OTHER.md"},
                            "arguments_json": '{"path":"OTHER.md"}',
                            "arguments_status": "valid",
                        },
                    ),
                )
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_parallel_stable_tool_calls_keep_result_pairing(tmp_path) -> None:
    """并行稳定调用 ID 按结果到达顺序也保持各自的 Transcript 归属。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-parallel-calls", "并行读取", run_id="run-parallel-calls")
    try:
        run = RunState(
            start=StartRun(
                thread_id="thread-parallel-calls",
                run_id="run-parallel-calls",
                message="并行读取",
            ),
            owner=ConnectionRef("owner"),
            persistence=store,
            preparation=RunPreparation(),
        )
        _capture_transcript_message(
            run,
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-a", "name": "read_file", "args": {"path": "a"}},
                    {"id": "call-b", "name": "read_file", "args": {"path": "b"}},
                ],
            ),
        )
        coordinator = _coordinator([])
        _capture_transcript_message(
            run,
            ToolMessage(content="A", tool_call_id="call-a", name="read_file"),
        )
        await coordinator._flush_transcript(run)
        partial_records = await store.load_transcript("thread-parallel-calls")
        partial_assistant = next(
            record for record in partial_records if record.kind == "assistant"
        )
        partial_tool_ids = [
            record.payload["tool_call_id"]
            for record in partial_records
            if record.kind == "tool"
        ]
        declared_ids = {
            call["id"] for call in partial_assistant.payload["tool_calls"]
        }
        assert declared_ids == {"call-a", "call-b"}
        assert partial_tool_ids == ["call-a"]
        assert declared_ids - set(partial_tool_ids) == {"call-b"}

        _capture_transcript_message(
            run,
            ToolMessage(content="B", tool_call_id="call-b", name="read_file"),
        )
        await coordinator._flush_transcript(run)

        records = await store.load_transcript("thread-parallel-calls")
        assert [record.payload["tool_call_id"] for record in records if record.kind == "tool"] == [
            "call-a",
            "call-b",
        ]
        assistant = next(record for record in records if record.kind == "assistant")
        assert [call["id"] for call in assistant.payload["tool_calls"]] == ["call-a", "call-b"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_subgraph_messages_are_not_flattened_into_root_transcript(tmp_path) -> None:
    """无 provenance 时显式抑制非空 namespace 的 child live 事件，不写 root 事实。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-root", "根请求", run_id="run-root")
    try:
        class RootAndSubgraphAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    ("child", "tool-node"),
                    "messages",
                    (
                        AIMessageChunk(
                            content="子图回答",
                            tool_call_chunks=[
                                {
                                    "index": 0,
                                    "id": "child-call",
                                    "name": "child_tool",
                                    "args": '{"secret":true}',
                                }
                            ],
                        ),
                        {},
                    ),
                )
                yield (
                    ("child", "tool-node"),
                    "messages",
                    (ToolMessage(content="子图工具结果", tool_call_id="child-call"), {}),
                )
                yield (
                    "messages",
                    (
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "root-call",
                                    "name": "read_file",
                                    "args": {"path": "root.txt"},
                                }
                            ],
                        ),
                        {},
                    ),
                )
                yield (
                    "messages",
                    (
                        ToolMessage(
                            content="根工具结果",
                            tool_call_id="root-call",
                            name="read_file",
                        ),
                        {},
                    ),
                )

        run = RunState(
            start=StartRun(thread_id="thread-root", run_id="run-root", message="根请求"),
            owner=ConnectionRef("owner"),
            persistence=store,
            preparation=RunPreparation(),
            runtime=RunRuntime(
                agent=RootAndSubgraphAgent(),
                run_context=None,
                graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
                release=lambda: _noop_async(),
            ),
        )
        await _coordinator([])._stream_agent(run, None)

        records = await store.load_transcript("thread-root")
        assert [record.kind for record in records] == ["user", "assistant", "tool"]
        assert records[1].payload["tool_calls"][0]["id"] == "root-call"
        assert records[2].payload["tool_call_id"] == "root-call"
        assert all("子图" not in str(record.payload) for record in records)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_completed_tool_batch_is_readable_while_run_waits_for_next_model_stage(
    tmp_path,
) -> None:
    """Tool 边界提交后，独立连接可在后续模型阶段阻塞时恢复已完成语义。"""
    from tests.support.thread_fixtures import accept_thread

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    store = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(store, "thread-durable", "读取文件", run_id="run-durable")

    blocked = asyncio.Event()
    release = asyncio.Event()

    class BlockingAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    type(
                        "AIMessageChunk",
                        (),
                        {
                            "content": "正在读取",
                            "tool_call_chunks": [
                                {"index": 0, "id": None, "name": "read_file", "args": ""}
                            ],
                            "usage_metadata": None,
                        },
                    )(),
                    {},
                ),
            )
            yield (
                "messages",
                (
                    type(
                        "ToolMessage",
                        (),
                        {
                            "content": "完整工具结果",
                            "tool_call_id": "",
                            "name": "read_file",
                            "status": "success",
                        },
                    )(),
                    {},
                ),
            )
            blocked.set()
            await release.wait()
            yield (
                "messages",
                (
                    type(
                        "AIMessageChunk",
                        (),
                        {
                            "content": "最终回答",
                            "tool_call_chunks": [],
                            "usage_metadata": None,
                        },
                    )(),
                    {},
                ),
            )

    run = RunState(
        start=StartRun(thread_id="thread-durable", run_id="run-durable", message="读取文件"),
        owner=ConnectionRef("owner"),
        persistence=store,
        preparation=RunPreparation(execution_binding=make_test_binding("thread-durable", "run-durable")),
        runtime=RunRuntime(
            agent=BlockingAgent(),
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=lambda: _noop_async(),
        ),
    )
    coordinator = _coordinator([])
    stream_task = asyncio.create_task(coordinator._stream_agent(run, None))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        independent = await ThreadPersistence.open(project=project, home=home)
        try:
            records = await independent.load_transcript("thread-durable")
            assert [(record.kind, record.payload["content"]) for record in records] == [
                ("user", "读取文件"),
                ("assistant", "正在读取"),
                ("tool", "完整工具结果"),
            ]
        finally:
            await independent.close()
    finally:
        release.set()
        try:
            await stream_task
            final_records = await store.load_transcript("thread-durable")
            assert final_records[-1].payload["content"] == "最终回答"
        finally:
            await store.close()


async def _noop_async() -> None:
    return None


@pytest.mark.asyncio
async def test_failed_run_flushes_completed_semantics_but_discards_partial_assistant() -> None:
    """失败收尾保留已完成 assistant/tool，流式半条 assistant 不进入 Transcript。"""

    class Persistence:
        def __init__(self) -> None:
            self.records = []
            self.completed = False

        async def accept_run(self, _command):
            return type("Acceptance", (), {"created": True})()

        async def append_transcript_batch(self, records):
            self.records.extend(records)

        async def complete_run(self, _thread_id: str) -> None:
            self.completed = True

    class FailingAgent:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="已完成回答",
                        id="assistant-1",
                        tool_call_chunks=[
                            {"index": 0, "id": "call-1", "name": "read_file", "args": ""}
                        ],
                    ),
                    {},
                ),
            )
            yield (
                "messages",
                (
                    ToolMessage(content="工具已完成", tool_call_id="call-1", name="read_file"),
                    {},
                ),
            )
            yield ("messages", (AIMessageChunk(content="流式半条", id="assistant-2"), {}))
            raise RuntimeError("injected agent failure")

    persistence = Persistence()

    async def persistence_provider():
        return persistence

    async def preparation_provider(_command, _persistence):
        return RunPreparation(
            execution_binding=make_test_binding("thread-failed", "run-failed")
        )

    async def runtime_provider(_run) -> RunRuntime:
        async def release() -> None:
            return None

        return RunRuntime(
            agent=FailingAgent(),
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    coordinator = RunCoordinator(
        persistence_provider=persistence_provider,
        preparation_provider=preparation_provider,
        runtime_provider=runtime_provider,
        interaction_port=_NoopInteraction(),
    )
    execution = await coordinator.start(
        StartRun(thread_id="thread-failed", run_id="run-failed", message="失败测试"),
        ConnectionRef("owner"),
    )
    events = await _events(execution)

    assert events[-1].type == "run.failed"
    assert [(record.kind, record.content) for record in persistence.records] == [
        ("assistant", "已完成回答"),
        ("tool", "工具已完成"),
    ]
    assert persistence.completed is True
