"""v2 JSON-RPC 握手、并发运行、双向交互和真实 stdio 回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "protocol": {"major": 2, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0"},
        "capabilities": ["run.cancel", "run.multithread", "interactive.approval", "interactive.question"],
    }
    params.update(overrides)
    return params


async def _capture_server(server: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(_request("initialize", _initialize_params(), "init-1"))
    return frames


async def _wait_for(frames: list[dict[str, Any]], predicate: Any) -> dict[str, Any]:
    for _ in range(200):
        for frame in frames:
            if predicate(frame):
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out; received: {frames}")


def _event_types(frames: list[dict[str, Any]]) -> list[str]:
    return [frame["params"]["type"] for frame in frames if frame.get("method") == "event"]


async def test_initialize_negotiates_v2_and_capabilities():
    """握手返回选定 minor、能力交集、限制和脱敏配置摘要。"""
    from harness_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    frames = await _capture_server(server)
    result = frames[0]["result"]
    assert result["protocol"] == {"major": 2, "minor": 0}
    assert "run.multithread" in result["enabled_capabilities"]
    assert result["limits"]["max_frame_bytes"] == 8 * 1024 * 1024
    assert result["config_summary"]["security"]["mode"] == "local"


async def test_initialize_rejects_incompatible_major_and_pre_initialize_calls():
    """不兼容 Major 和握手前业务调用必须被结构化拒绝。"""
    from harness_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    frames: list[dict[str, Any]] = []
    server.send = lambda message: _append(frames, message)  # type: ignore[method-assign]
    await server.dispatch(_request("run.start", {"message": "x"}, "run-early"))
    await server.dispatch(_request("initialize", _initialize_params(protocol={"major": 9, "min_minor": 0, "max_minor": 0}), "init-bad"))
    assert [frame["error"]["code"] for frame in frames] == [-32000, -32003]


async def test_echo_run_response_precedes_ordered_terminal_events():
    """run.start 响应必须早于 sequence 连续的统一事件。"""
    from harness_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "hello", "thread_id": "t", "run_id": "r"}, "run-1"))
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
    run_frames = frames[1:]
    assert run_frames[0]["result"]["accepted"] is True
    assert _event_types(run_frames) == ["run.started", "content.delta", "run.completed"]
    assert [frame["params"]["sequence"] for frame in run_frames if frame.get("method") == "event"] == [1, 2, 3]


def test_stream_translation_prefers_normalized_content_blocks():
    """首轮仅提供 content_blocks 时仍必须产生正文事件。"""
    from types import SimpleNamespace

    from harness_agent.server import ActiveRun, JsonRpcServer

    chunk = SimpleNamespace(
        content="",
        content_blocks=[{"type": "text", "text": "首轮回复"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 4},
        tool_call_chunks=[],
    )
    run = ActiveRun(thread_id="thread", run_id="run", message="你好")
    events = list(JsonRpcServer(allow_echo=True)._translate_stream_event(((), "messages", (chunk, {})), run))

    assert events == [("content.delta", {"text": "首轮回复"})]
    assert run.usage == {"input_tokens": 10, "output_tokens": 4}


def test_tool_fragments_with_missing_ids_are_merged_by_index():
    """工具名和参数分片缺少重复 id 时仍应归并为同一协议工具。"""
    from types import SimpleNamespace

    from harness_agent.server import ActiveRun, JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    run = ActiveRun(thread_id="thread", run_id="run", message="执行 pwd")
    first = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": "call-1", "name": "execute", "args": ""}])
    second = SimpleNamespace(content="", usage_metadata=None, tool_call_chunks=[{"index": 0, "id": None, "name": None, "args": '{"command":"pwd"}'}])
    result = type("ToolMessage", (), {"content": "/workspace", "tool_call_id": "call-1", "status": "success", "tool_call_chunks": [], "usage_metadata": None})()

    events = [
        *server._translate_stream_event(((), "messages", (first, {})), run),
        *server._translate_stream_event(((), "messages", (second, {})), run),
        *server._translate_stream_event(((), "messages", (result, {})), run),
    ]

    assert [payload["tool_call_id"] for _, payload in events] == ["call-1", "call-1", "call-1"]
    assert [event_type for event_type, _ in events] == ["tool.started", "tool.delta", "tool.completed"]


async def test_multiple_threads_run_concurrently_but_same_thread_is_rejected():
    """不同 thread 可并发，同一 thread 的第二个活动 run 被拒绝。"""
    from harness_agent.server import JsonRpcServer

    releases = {"t1": asyncio.Event(), "t2": asyncio.Event()}

    class BlockingAgent:
        async def astream(self, _input: Any, *, config: dict[str, Any], **_kwargs: Any):
            thread_id = config["configurable"]["thread_id"]
            yield ("messages", (type("Chunk", (), {"content": thread_id, "usage_metadata": None, "tool_call_chunks": []})(), {}))
            await releases[thread_id].wait()

    server = JsonRpcServer(agent=BlockingAgent())
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "a", "thread_id": "t1", "run_id": "r1"}, "start-1"))
    await server.dispatch(_request("run.start", {"message": "b", "thread_id": "t2", "run_id": "r2"}, "start-2"))
    await server.dispatch(_request("run.start", {"message": "c", "thread_id": "t1", "run_id": "r3"}, "start-3"))
    assert any(frame.get("id") == "start-3" and frame.get("error", {}).get("code") == -32000 for frame in frames)
    await server.dispatch(_request("run.cancel", {"thread_id": "t1", "run_id": "r1"}, "cancel-1"))
    await server.dispatch(_request("run.cancel", {"thread_id": "t2", "run_id": "r2"}, "cancel-2"))
    await _wait_for(frames, lambda frame: _event_count(frames, "run.cancelled") == 2)


async def test_question_request_uses_standard_response_and_stable_question_id():
    """AskUser interrupt 通过 Agent→Client request 恢复，不再调用 respond 方法。"""
    from langgraph.types import Command, Interrupt
    from harness_agent.server import JsonRpcServer

    class AskAgent:
        def __init__(self) -> None:
            self.inputs: list[object] = []

        async def astream(self, stream_input: object, **_kwargs: Any):
            self.inputs.append(stream_input)
            if len(self.inputs) == 1:
                yield ("updates", {"__interrupt__": (Interrupt({"type": "ask_user", "questions": [{"question": "目录？", "type": "multiple_choice", "choices": [{"value": "src"}]}]}, id="ask-1"),)})
                return
            assert isinstance(stream_input, Command)
            assert stream_input.resume == {"ask-1": {"status": "answered", "answers": ["src"]}}
            yield ("messages", (type("Chunk", (), {"content": "完成", "usage_metadata": None, "tool_call_chunks": []})(), {}))

    server = JsonRpcServer(agent=AskAgent())
    frames = await _capture_server(server)
    await server.dispatch(_request("run.start", {"message": "开始", "thread_id": "t", "run_id": "r"}, "start"))
    interaction = await _wait_for(frames, lambda frame: frame.get("method") == "request")
    assert interaction["id"] == interaction["params"]["request_id"] == "ask-1"
    assert interaction["params"]["payload"]["questions"][0]["id"] == "question-1"
    await server.dispatch({"jsonrpc": "2.0", "id": "ask-1", "result": {"type": "question", "request_id": "ask-1", "answers": {"question-1": ["src"]}}})
    await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
    assert "interaction.resolved" in _event_types(frames)


async def test_real_hitl_rejection_prevents_file_write():
    """真实 deepagents 写入审批被拒绝后不得落盘。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        destination = Path(workspace) / "blocked.txt"
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": str(destination), "content": "x"}, "id": "call-1"}]), AIMessage(content="已拒绝")])
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(model, cwd=workspace, enable_interpreter=False, enable_skills=False, enable_memory=False, enable_ask_user=False, approval_mode="default")
        server = JsonRpcServer(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(_request("run.start", {"message": "写入", "thread_id": "t", "run_id": "r"}, "start"))
        interaction = await _wait_for(frames, lambda frame: frame.get("method") == "request")
        await server.dispatch({"jsonrpc": "2.0", "id": interaction["id"], "result": {"type": "approval", "request_id": interaction["id"], "decision": "reject"}})
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert not destination.exists()


async def test_workspace_rejection_precedes_default_approval_request():
    """越界文件调用在 HITL 前被拒绝，避免用户看到无法改变边界的审批框。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace, TemporaryDirectory() as outside:
        destination = Path(outside) / "must-not-write.md"
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": str(destination), "content": "blocked"},
                            "id": "call-outside",
                        }
                    ],
                ),
                AIMessage(content="越界已拒绝"),
            ]
        )
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(
            model,
            cwd=workspace,
            approval_mode="default",
            enable_interpreter=False,
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        server = JsonRpcServer(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(
            _request("run.start", {"message": "越界写入", "thread_id": "outside", "run_id": "outside-run"}, "outside-start")
        )
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")

        assert not destination.exists()
        assert not any(frame.get("method") == "request" for frame in frames)


async def test_auto_edit_writes_without_interruption_but_shell_still_requires_approval():
    """自动编辑模式只跳过 write_file；execute 仍必须由客户端明确拒绝或批准。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        write_model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": str(Path(workspace) / "auto.txt"),
                                "content": "written",
                            },
                            "id": "call-write",
                        }
                    ],
                ),
                AIMessage(content="写入完成"),
            ]
        )
        write_model.profile = {"max_input_tokens": 200_000}
        write_agent = create_harness_agent(
            write_model,
            cwd=workspace,
            approval_mode="auto-edit",
            enable_interpreter=False,
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        write_server = JsonRpcServer(agent=write_agent)
        write_frames = await _capture_server(write_server)
        await write_server.dispatch(
            _request("run.start", {"message": "写入", "thread_id": "write", "run_id": "write-run"}, "write-start")
        )
        await _wait_for(write_frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")
        assert (Path(workspace) / "auto.txt").read_text(encoding="utf-8") == "written"
        assert not any(frame.get("method") == "request" for frame in write_frames)

        shell_model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "execute", "args": {"command": "pwd"}, "id": "call-shell"}
                    ],
                ),
                AIMessage(content="命令被拒绝"),
            ]
        )
        shell_model.profile = {"max_input_tokens": 200_000}
        shell_agent = create_harness_agent(
            shell_model,
            cwd=workspace,
            approval_mode="auto-edit",
            enable_interpreter=False,
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        shell_server = JsonRpcServer(agent=shell_agent)
        shell_frames = await _capture_server(shell_server)
        await shell_server.dispatch(
            _request("run.start", {"message": "执行", "thread_id": "shell", "run_id": "shell-run"}, "shell-start")
        )
        interaction = await _wait_for(shell_frames, lambda frame: frame.get("method") == "request")
        assert interaction["params"]["type"] == "approval"
        await shell_server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": interaction["id"],
                "result": {"type": "approval", "request_id": interaction["id"], "decision": "reject"},
            }
        )
        await _wait_for(shell_frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")


async def test_plan_mode_returns_tool_message_without_writing_or_requesting_approval():
    """计划模式写工具调用必须由内核硬拒绝，不能先交给 TUI 或落盘。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from harness_agent.agent import create_harness_agent
    from harness_agent.server import JsonRpcServer

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": str(Path(workspace) / "plan.txt"),
                                "content": "must not write",
                            },
                            "id": "call-plan-write",
                        }
                    ],
                ),
                AIMessage(content="已提供计划"),
            ]
        )
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(
            model,
            cwd=workspace,
            approval_mode="plan",
            enable_interpreter=False,
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
        )
        server = JsonRpcServer(agent=agent)
        frames = await _capture_server(server)
        await server.dispatch(
            _request("run.start", {"message": "写入", "thread_id": "plan", "run_id": "plan-run"}, "plan-start")
        )
        await _wait_for(frames, lambda frame: frame.get("params", {}).get("type") == "run.completed")

        assert not (Path(workspace) / "plan.txt").exists()
        assert not any(frame.get("method") == "request" for frame in frames)


async def test_missing_interaction_capability_fails_closed_without_reverse_request():
    """无头客户端不声明交互能力时，服务端直接返回拒绝而不发送 request。"""
    from harness_agent.server import ActiveRun, InteractionSpec, JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(
        _request(
            "initialize",
            _initialize_params(capabilities=["run.cancel", "run.multithread", "config.read"]),
            "init-headless",
        )
    )
    result = await server._request_interaction(
        ActiveRun(thread_id="headless", run_id="headless-run", message="test"),
        InteractionSpec(
            request_id="approval-headless",
            type="approval",
            payload={},
            interrupt_id="approval-headless",
        ),
    )

    assert result == {"type": "approval", "request_id": "approval-headless", "decision": "reject"}
    assert not any(frame.get("method") == "request" for frame in frames)


def test_tool_output_is_utf8_safely_truncated():
    """超限工具输出携带截断标记和原始字节数。"""
    from harness_agent.protocol_generated import MAX_TOOL_PAYLOAD_BYTES
    from harness_agent.server import _truncate_text

    original = "界" * (MAX_TOOL_PAYLOAD_BYTES // 2)
    clipped, truncated, original_bytes = _truncate_text(original)
    assert truncated is True
    assert len(clipped.encode()) <= MAX_TOOL_PAYLOAD_BYTES
    assert original_bytes == len(original.encode())


async def test_stdio_subprocess_end_to_end_echo_mode():
    """真实 sidecar 完成 v2 initialize、run.start、event 与 shutdown。"""
    package_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(package_root), "HARNESS_ECHO_MODE": "1"}
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "harness_agent", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    assert process.stdin and process.stdout
    process.stdin.write((json.dumps(_request("initialize", _initialize_params(), "init")) + "\n" + json.dumps(_request("run.start", {"message": "hello", "thread_id": "t", "run_id": "r"}, "start")) + "\n").encode())
    await process.stdin.drain()
    frames: list[dict[str, Any]] = []
    while not any(frame.get("params", {}).get("type") == "run.completed" for frame in frames):
        frames.append(json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=2)))
    process.stdin.write((json.dumps(_request("shutdown", {}, "stop")) + "\n").encode())
    await process.stdin.drain()
    await asyncio.wait_for(process.wait(), timeout=2)
    assert "content.delta" in _event_types(frames)


async def _append(frames: list[dict[str, Any]], message: dict[str, Any]) -> None:
    frames.append(message)


def _event_count(frames: list[dict[str, Any]], event_type: str) -> int:
    return sum(frame.get("params", {}).get("type") == event_type for frame in frames)
