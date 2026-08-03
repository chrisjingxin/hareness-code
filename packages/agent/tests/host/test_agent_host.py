"""Project-scoped AgentHost 的多 Connection、owner 与 attachment 回归测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from harness_agent.context_window import ContextUpdate
from harness_agent.server import AgentHost


class _BlockingAgent:
    """让协议测试通过 public run.start 保持一个可观察的 active Run。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def astream(self, *_args: Any, **_kwargs: Any):
        self.started.set()
        await asyncio.Event().wait()
        if False:
            yield None


class _StreamingAgent:
    """只产生一条 mock 模型消息，验证 transport 不复制 Run 生命周期。"""

    async def astream(self, *_args: Any, **_kwargs: Any):
        yield ((), "messages", (AIMessage(content="fixture response"), {}))


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize(*requests: str) -> dict[str, Any]:
    return {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "1", "kind": "test"},
        "capabilities": {"requests": list(requests), "handles": []},
    }


async def test_run_owner_and_observer_receive_identical_events(tmp_path: Path) -> None:
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize("run.cancel"), "owner-init"))

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    attached = host.create_connection(send_attached)
    await host.dispatch_connection(
        attached,
        _request("initialize", _initialize("run.cancel"), "web-init"),
    )
    host._owner_connection.watched_threads.add("thread-1")
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"message": "hello", "thread_id": "thread-1", "run_id": "run-1"},
            "start",
        ),
    )
    for _ in range(100):
        if any(frame.get("params", {}).get("type") == "run.completed" for frame in attached_frames):
            break
        await asyncio.sleep(0.01)

    owner_events = [frame["params"] for frame in owner_frames if frame.get("method") == "event"]
    attached_events = [frame["params"] for frame in attached_frames if frame.get("method") == "event"]
    assert owner_events == attached_events
    assert [event["sequence"] for event in owner_events] == [1, 2, 3]
    await host.close()


async def test_stdio_owner_and_websocket_observer_share_context_updated_sequence(
    tmp_path: Path,
) -> None:
    """stdio owner 与真实 WebSocket attachment 看到同一 context.updated/终态序列。"""
    owner_frames: list[dict[str, Any]] = []
    host = AgentHost(
        agent=_StreamingAgent(),
        config_home=tmp_path / "home",
        workspace=tmp_path,
    )
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "run.multithread"),
            "owner-init",
        )
    )
    import socket

    probe = socket.socket()
    try:
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            await host.close()
            pytest.skip("sandbox forbids loopback WebSocket bind")
    finally:
        probe.close()
    await host.dispatch(
        _request("host.attachment.create", {"origin": "http://127.0.0.1:43210"}, "attach")
    )
    attachment_response = owner_frames[-1]
    if "result" not in attachment_response:
        await host.close()
        raise AssertionError(f"WebSocket attachment unexpectedly failed: {attachment_response}")
    grant = attachment_response["result"]
    origin = "http://127.0.0.1:43210"
    websocket_frames: list[dict[str, Any]] = []

    async with connect(grant["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": grant["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize("run.multithread"),
                    "web-init",
                )
            )
        )
        initialized = json.loads(await socket.recv())
        assert initialized["result"]["connection"]["role"] == "attached"
        attached_connection = next(
            connection
            for connection in host._connections.values()
            if connection is not host._owner_connection
        )
        attached_connection.watched_threads.add("thread-transport")
        host._context_updates["thread-transport"] = [
            ContextUpdate(
                thread_id="thread-transport",
                action="report",
                estimated_tokens=100,
                input_cap_tokens=200,
                context_window_tokens=256,
                dynamic_tokens=100,
            )
        ]
        await host.dispatch(
            _request(
                "run.start",
                {
                    "message": "transport continuity",
                    "thread_id": "thread-transport",
                    "run_id": "run-transport",
                },
                "run-start",
            )
        )
        for _ in range(100):
            frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=1))
            websocket_frames.append(frame)
            if (
                frame.get("method") == "event"
                and frame.get("params", {}).get("type") == "run.completed"
            ):
                break
        else:
            raise AssertionError(f"WebSocket run did not complete: {websocket_frames}")

    owner_events = [frame["params"] for frame in owner_frames if frame.get("method") == "event"]
    websocket_events = [
        frame["params"] for frame in websocket_frames if frame.get("method") == "event"
    ]
    assert owner_events == websocket_events
    assert [event["type"] for event in owner_events] == [
        "run.started",
        "context.updated",
        "content.delta",
        "run.completed",
    ]
    assert [event["sequence"] for event in owner_events] == [1, 2, 3, 4]
    await host.close()


async def test_only_run_owner_can_cancel(tmp_path: Path) -> None:
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize("run.cancel"), "owner-init"))

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    attached = host.create_connection(send_attached)
    await host.dispatch_connection(
        attached,
        _request("initialize", _initialize("run.cancel"), "web-init"),
    )
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
            "start",
        ),
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await host.dispatch(
        _request(
            "run.cancel",
            {"thread_id": "thread-1", "run_id": "run-1"},
            "cancel",
        )
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "RUN_NOT_OWNER"
    await host.close()


async def test_run_id_retry_is_idempotent_and_conflicting_content_is_rejected(
    tmp_path: Path,
) -> None:
    """活动 Run 的相同请求可重试，复用 ID 的不同内容稳定冲突。"""
    owner_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize(), "owner-init"))
    await host.dispatch(
        _request(
            "run.start",
            {"message": "same", "thread_id": "thread-1", "run_id": "run-1"},
            "retry",
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    await host.dispatch(
        _request(
            "run.start",
            {"message": "same", "thread_id": "thread-1", "run_id": "run-1"},
            "retry",
        )
    )
    assert owner_frames[-1]["result"] == {
        "thread_id": "thread-1",
        "run_id": "run-1",
        "accepted": True,
    }

    await host.dispatch(
        _request(
            "run.start",
            {"message": "different", "thread_id": "thread-1", "run_id": "run-1"},
            "conflict",
        )
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "RUN_ID_CONFLICT"
    await host.close()


async def test_watch_rejects_active_thread_and_attached_disconnect_cancels_only_owned_run(
    tmp_path: Path,
) -> None:
    """active Thread 不允许新增 watch，attached EOF 只取消自己的 Run。"""
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request("initialize", _initialize("threads.read"), "owner-init")
    )

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    attached = host.create_connection(send_attached)
    await host.dispatch_connection(
        attached,
        _request("initialize", _initialize("threads.read"), "web-init"),
    )
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
            "start",
        ),
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await host.dispatch(
        _request("threads.watch", {"thread_id": "thread-1"}, "watch")
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "THREAD_BUSY"

    await host.close_connection(attached)
    assert host._owner_connection.closed is False
    await host.dispatch(
        _request(
            "run.start",
            {"message": "again", "thread_id": "thread-1", "run_id": "run-2"},
            "owner-start",
        )
    )
    assert owner_frames[-1]["result"]["accepted"] is True
    await host.close()


async def test_attachment_token_is_origin_bound_single_use_and_capability_limited(
    tmp_path: Path,
) -> None:
    owner_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "run.cancel"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43210"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    async with connect(attachment["endpoint"], origin="http://127.0.0.1:1", proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        with pytest.raises(ConnectionClosed):
            await socket.recv()

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize("host.attach", "run.cancel"),
                    "web-init",
                )
            )
        )
        initialized = json.loads(await socket.recv())["result"]
        assert initialized["connection"]["role"] == "attached"
        assert initialized["capabilities"]["enabled"] == ["run.cancel"]

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        with pytest.raises(ConnectionClosed):
            await socket.recv()
    await host.close()


async def _append(frames: list[dict[str, Any]], message: dict[str, Any]) -> None:
    frames.append(message)
