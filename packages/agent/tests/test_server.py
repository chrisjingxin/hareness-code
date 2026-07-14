"""Tests for JSON-RPC dispatch, run lifecycle, and real stdio transport."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest


async def _wait_for_event(events: list[dict[str, Any]], method: str) -> dict[str, Any]:
    for _ in range(100):
        for event in events:
            if event["method"] == method:
                return event
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {method}; received: {events}")


async def test_initialize_handshake_includes_protocol_capabilities():
    """Initialize returns capability metadata without exposing secrets."""
    from za38_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    responses: list[dict[str, Any]] = []

    async def mock_send(message: dict[str, Any]) -> None:
        responses.append(message)

    server.send = mock_send  # type: ignore[method-assign]
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {"client_info": {"name": "test", "version": "0.1.0"}},
            "id": 1,
        }
    )

    result = responses[0]["result"]
    assert result["server_info"]["name"] == "za38-agent"
    assert result["protocol_version"] == 1
    assert result["capabilities"]["cancellation"] is True


async def test_echo_query_emits_ordered_terminal_events():
    """Echo mode exercises the run protocol without a real model credential."""
    from za38_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    notifications: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []

    async def mock_send_notification(method: str, params: dict[str, Any]) -> None:
        notifications.append({"method": method, "params": params})

    async def mock_send(message: dict[str, Any]) -> None:
        responses.append(message)

    server.send_notification = mock_send_notification  # type: ignore[method-assign]
    server.send = mock_send  # type: ignore[method-assign]
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "query",
            "params": {"message": "hello", "thread_id": "thread-1", "run_id": "run-1"},
            "id": 2,
        }
    )

    terminal = await _wait_for_event(notifications, "run/completed")
    assert responses[0]["result"] == {"thread_id": "thread-1", "run_id": "run-1", "accepted": True}
    assert [event["method"] for event in notifications] == ["run/started", "message/delta", "run/completed"]
    assert notifications[1]["params"]["text"] == "hello"
    assert terminal["params"]["sequence"] == 3


async def test_second_query_on_active_thread_is_rejected_and_cancel_is_observed():
    """The server stays responsive while an Agent stream is running."""
    from za38_agent.server import JsonRpcServer

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingAgent:
        async def astream(self, *_: Any, **__: Any):
            started.set()
            yield ("messages", (type("Chunk", (), {"content": "working", "usage_metadata": None, "tool_call_chunks": []})(), {}))
            await release.wait()

    server = JsonRpcServer(agent=BlockingAgent())
    notifications: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []

    async def mock_send_notification(method: str, params: dict[str, Any]) -> None:
        notifications.append({"method": method, "params": params})

    async def mock_send(message: dict[str, Any]) -> None:
        responses.append(message)

    server.send_notification = mock_send_notification  # type: ignore[method-assign]
    server.send = mock_send  # type: ignore[method-assign]
    request = {"jsonrpc": "2.0", "method": "query", "params": {"message": "go", "thread_id": "t", "run_id": "r"}}
    await server.dispatch({**request, "id": 1})
    await asyncio.wait_for(started.wait(), timeout=1)
    await server.dispatch({**request, "id": 2})
    await server.dispatch(
        {"jsonrpc": "2.0", "method": "cancel", "params": {"thread_id": "t", "run_id": "r"}, "id": 3}
    )

    await _wait_for_event(notifications, "run/cancelled")
    assert responses[1]["error"]["code"] == -32000
    assert responses[2]["result"]["cancelled"] is True
    release.set()


async def test_ask_user_interrupt_emits_question_and_resumes_by_stable_interrupt_id():
    """AskUser 的问题事件应带原始 id，respond 必须按该 id 恢复图执行。"""
    from langgraph.types import Command, Interrupt
    from za38_agent.server import JsonRpcServer

    class AskUserAgent:
        def __init__(self) -> None:
            self.inputs: list[object] = []

        async def astream(self, stream_input: object, *_: Any, **__: Any):
            self.inputs.append(stream_input)
            if len(self.inputs) == 1:
                yield (
                    "updates",
                    {
                        "__interrupt__": (
                            Interrupt(
                                {
                                    "type": "ask_user",
                                    "questions": [
                                        {
                                            "question": "选择目标目录",
                                            "type": "multiple_choice",
                                            "choices": [{"value": "src"}, {"value": "tests"}],
                                        }
                                    ],
                                },
                                id="ask-1",
                            ),
                        )
                    },
                )
                return
            assert isinstance(stream_input, Command)
            assert stream_input.resume == {"ask-1": {"status": "answered", "answers": ["src"]}}
            chunk = type("Chunk", (), {"content": "收到回答", "usage_metadata": None, "tool_call_chunks": []})()
            yield ("messages", (chunk, {}))

    agent = AskUserAgent()
    server = JsonRpcServer(agent=agent)
    notifications: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []

    async def mock_send_notification(method: str, params: dict[str, Any]) -> None:
        notifications.append({"method": method, "params": params})

    async def mock_send(message: dict[str, Any]) -> None:
        responses.append(message)

    server.send_notification = mock_send_notification  # type: ignore[method-assign]
    server.send = mock_send  # type: ignore[method-assign]
    await server.dispatch(
        {"jsonrpc": "2.0", "method": "query", "params": {"message": "开始", "thread_id": "t", "run_id": "r"}, "id": 1}
    )
    question = await _wait_for_event(notifications, "question/requested")
    assert question["params"] == {
        "thread_id": "t",
        "run_id": "r",
        "sequence": 2,
        "interrupt_id": "ask-1",
        "question": "选择目标目录",
        "options": [{"label": "src", "value": "src"}, {"label": "tests", "value": "tests"}],
        "questions": [
            {
                "question": "选择目标目录",
                "type": "multiple_choice",
                "choices": [{"value": "src"}, {"value": "tests"}],
            }
        ],
    }

    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "respond",
            "params": {
                "thread_id": "t",
                "run_id": "r",
                "interrupt_id": "ask-1",
                "decisions": {"status": "answered", "answers": ["src"]},
            },
            "id": 2,
        }
    )
    await _wait_for_event(notifications, "run/completed")
    assert responses[-1]["result"] == {"accepted": True, "run_id": "r"}


async def test_real_deepagents_ask_user_interrupt_resumes_through_json_rpc():
    """用真实 deepagents 图覆盖 ask_user interrupt→respond→完成的完整恢复路径。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from za38_agent.agent import create_za38_agent
    from za38_agent.server import JsonRpcServer

    class ToolCallingModel(FakeMessagesListChatModel):
        def bind_tools(self, *_: Any, **__: Any) -> Runnable:
            return self

    model = ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {
                            "questions": [
                                {
                                    "question": "选择目标目录",
                                    "type": "multiple_choice",
                                    "choices": [{"value": "src"}, {"value": "tests"}],
                                }
                            ]
                        },
                        "id": "call-ask-1",
                    }
                ],
            ),
            AIMessage(content="已记录你的选择"),
        ]
    )
    model.profile = {"max_input_tokens": 200_000}
    agent = create_za38_agent(
        model,
        enable_interpreter=False,
        enable_skills=False,
        enable_memory=False,
        auto_approve=True,
    )
    server = JsonRpcServer(agent=agent)
    notifications: list[dict[str, Any]] = []

    async def mock_send_notification(method: str, params: dict[str, Any]) -> None:
        notifications.append({"method": method, "params": params})

    server.send_notification = mock_send_notification  # type: ignore[method-assign]
    server.send = lambda _message: asyncio.sleep(0)  # type: ignore[method-assign]
    await server.dispatch(
        {"jsonrpc": "2.0", "method": "query", "params": {"message": "开始", "thread_id": "real", "run_id": "ask"}, "id": 1}
    )
    question = await _wait_for_event(notifications, "question/requested")
    interrupt_id = question["params"]["interrupt_id"]
    assert question["params"]["options"] == [{"label": "src", "value": "src"}, {"label": "tests", "value": "tests"}]

    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "respond",
            "params": {
                "thread_id": "real",
                "run_id": "ask",
                "interrupt_id": interrupt_id,
                "decisions": {"status": "answered", "answers": ["src"]},
            },
            "id": 2,
        }
    )
    await _wait_for_event(notifications, "run/completed")
    assert any(
        event["method"] == "message/delta" and event["params"].get("text") == "已记录你的选择"
        for event in notifications
    )


async def test_real_deepagents_hitl_reject_prevents_write_through_json_rpc():
    """真实 write_file 必须先发审批；拒绝后不得在工作区落盘。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from za38_agent.agent import create_za38_agent
    from za38_agent.server import JsonRpcServer

    class ToolCallingModel(FakeMessagesListChatModel):
        def bind_tools(self, *_: Any, **__: Any) -> Runnable:
            return self

    with TemporaryDirectory() as workspace:
        model = ToolCallingModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "should-not-exist.txt", "content": "blocked"},
                            "id": "call-write-1",
                        }
                    ],
                ),
                AIMessage(content="已处理写入请求"),
            ]
        )
        model.profile = {"max_input_tokens": 200_000}
        agent = create_za38_agent(
            model,
            cwd=workspace,
            enable_interpreter=False,
            enable_skills=False,
            enable_memory=False,
            enable_ask_user=False,
            auto_approve=False,
        )
        server = JsonRpcServer(agent=agent)
        notifications: list[dict[str, Any]] = []

        async def mock_send_notification(method: str, params: dict[str, Any]) -> None:
            notifications.append({"method": method, "params": params})

        server.send_notification = mock_send_notification  # type: ignore[method-assign]
        server.send = lambda _message: asyncio.sleep(0)  # type: ignore[method-assign]
        await server.dispatch(
            {"jsonrpc": "2.0", "method": "query", "params": {"message": "写文件", "thread_id": "hitl", "run_id": "write"}, "id": 1}
        )
        approval = await _wait_for_event(notifications, "approval/requested")
        interrupt_id = approval["params"]["interrupt_id"]
        request = approval["params"]["requests"]["action_requests"][0]
        assert request["name"] == "write_file"
        assert request["args"]["file_path"] == "should-not-exist.txt"

        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "respond",
                "params": {
                    "thread_id": "hitl",
                    "run_id": "write",
                    "interrupt_id": interrupt_id,
                    "decisions": {"decisions": [{"type": "reject"}]},
                },
                "id": 2,
            }
        )
        await _wait_for_event(notifications, "run/completed")
        assert not (Path(workspace) / "should-not-exist.txt").exists()
        assert any(
            event["method"] == "tool/completed" and event["params"].get("error") is True
            for event in notifications
        )


def test_human_in_the_loop_interrupt_keeps_tool_request_details():
    """审批事件应保留 tool 请求，便于 TUI 展示风险说明和参数。"""
    from langgraph.types import Interrupt
    from za38_agent.server import JsonRpcServer

    server = JsonRpcServer(allow_echo=True)
    events = list(
        server._translate_interrupts(  # noqa: SLF001 - 协议转换的定向单元测试。
            (
                Interrupt(
                    {
                        "action_requests": [
                            {"name": "write_file", "args": {"path": "src/a.py"}, "description": "写入源文件"}
                        ],
                        "review_configs": [{"action_name": "write_file", "allowed_decisions": ["approve", "reject"]}],
                    },
                    id="hitl-1",
                ),
            )
        )
    )
    assert events == [
        (
            "approval/requested",
            {
                "interrupt_id": "hitl-1",
                "description": "写入源文件",
                "requests": {
                    "action_requests": [
                        {"name": "write_file", "args": {"path": "src/a.py"}, "description": "写入源文件"}
                    ],
                    "review_configs": [{"action_name": "write_file", "allowed_decisions": ["approve", "reject"]}],
                },
            },
        )
    ]


async def test_stdio_subprocess_end_to_end_echo_mode():
    """A real Python sidecar accepts JSONL and terminates a run."""
    package_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(package_root), "ZA38_ECHO_MODE": "1"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "za38_agent",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdin and process.stdout
    process.stdin.write(
        b'{"jsonrpc":"2.0","method":"initialize","params":{"client_info":{"name":"e2e","version":"1"}},"id":1}\n'
        b'{"jsonrpc":"2.0","method":"query","params":{"message":"hello","thread_id":"t","run_id":"r"},"id":2}\n'
    )
    await process.stdin.drain()

    frames: list[dict[str, Any]] = []
    while not any(frame.get("method") == "run/completed" for frame in frames):
        line = await asyncio.wait_for(process.stdout.readline(), timeout=2)
        assert line
        frames.append(json.loads(line))

    process.stdin.write(b'{"jsonrpc":"2.0","method":"shutdown","params":{},"id":3}\n')
    await process.stdin.drain()
    await asyncio.wait_for(process.wait(), timeout=2)
    assert any(frame.get("method") == "message/delta" for frame in frames)
    assert any(frame.get("result", {}).get("accepted") is True for frame in frames)
