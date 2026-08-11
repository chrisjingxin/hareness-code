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

from harness_agent.threads.context_window import ContextUpdate
from harness_agent.host.agent_host import AgentHost


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


async def _recv_response(socket: Any, request_id: str) -> dict[str, Any]:
    """读取 socket 直到返回指定 request_id 的 RPC 响应，跳过事件通知。"""
    async def _read() -> dict[str, Any]:
        while True:
            frame = json.loads(await socket.recv())
            if frame.get("id") == request_id:
                return frame

    return await asyncio.wait_for(_read(), timeout=5)


async def test_run_owner_and_observer_receive_identical_events(tmp_path: Path) -> None:
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize("run.cancel"), "owner-init"))

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    attached = host.create_connection(send_attached, attachment_id="att-observer")
    await host.dispatch_connection(
        attached,
        _request(
            "initialize",
            _initialize("host.control", "run.cancel"),
            "web-init",
        ),
    )
    await host.dispatch_connection(
        attached,
        _request("host.control.acquire", {}, "web-acquire"),
    )
    assert attached_frames[-1]["result"]["state"] == "attached"
    host._owner_connection.watched_threads.add("thread-1")
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"mode": "build", "message": "hello", "thread_id": "thread-1", "run_id": "run-1"},
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
    assert [event["sequence"] for event in owner_events] == [1, 2, 3, 4]
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
                {"mode": "build", 
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
        "run.progress",
        "run.progress",
        "context.updated",
        "content.delta",
        "run.completed",
    ]
    assert [event["sequence"] for event in owner_events] == [1, 2, 3, 4, 5, 6]
    await host.close()


async def test_non_run_owner_cannot_cancel_through_holder_gate(tmp_path: Path) -> None:
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    other_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize("run.cancel"), "owner-init"))

    async def send_attached(message: dict[str, Any]) -> None:
        attached_frames.append(message)

    async def send_other(message: dict[str, Any]) -> None:
        other_frames.append(message)

    attached = host.create_connection(send_attached, attachment_id="att-owner")
    await host.dispatch_connection(
        attached,
        _request(
            "initialize",
            _initialize("host.control", "run.cancel"),
            "web-init",
        ),
    )
    await host.dispatch_connection(
        attached,
        _request("host.control.acquire", {}, "web-acquire"),
    )
    other = host.create_connection(send_other, attachment_id="att-other")
    await host.dispatch_connection(
        other,
        _request(
            "initialize",
            _initialize("host.control", "run.cancel"),
            "other-init",
        ),
    )
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"mode": "build", "message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
            "start",
        ),
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await host.dispatch_connection(
        other,
        _request(
            "run.cancel",
            {"thread_id": "thread-1", "run_id": "run-1"},
            "other-cancel",
        ),
    )
    assert other_frames[-1]["error"]["data"]["code"] == "CONTROL_NOT_HOLDER"
    await host.dispatch(
        _request(
            "run.cancel",
            {"thread_id": "thread-1", "run_id": "run-1"},
            "cancel",
        )
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "CONTROL_NOT_HOLDER"
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
            {"mode": "build", "message": "same", "thread_id": "thread-1", "run_id": "run-1"},
            "retry",
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    await host.dispatch(
        _request(
            "run.start",
            {"mode": "build", "message": "same", "thread_id": "thread-1", "run_id": "run-1"},
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
            {"mode": "build", "message": "different", "thread_id": "thread-1", "run_id": "run-1"},
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

    attached = host.create_connection(send_attached, attachment_id="att-watch")
    await host.dispatch_connection(
        attached,
        _request(
            "initialize",
            _initialize("host.control", "threads.read"),
            "web-init",
        ),
    )
    await host.dispatch_connection(
        attached,
        _request("host.control.acquire", {}, "web-acquire"),
    )
    await host.dispatch_connection(
        attached,
        _request(
            "run.start",
            {"mode": "build", "message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
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
            {"mode": "build", "message": "again", "thread_id": "thread-1", "run_id": "run-2"},
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
    assert attachment["attachment_id"]

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



async def test_attached_controlled_operation_without_acquire_is_rejected(
    tmp_path: Path,
) -> None:
    """attached 未 acquire 时受控操作返回 CONTROL_NOT_HOLDER，只读仍可用。"""
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "host.control", "run.cancel"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43211"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize("host.control", "run.cancel"),
                    "web-init",
                )
            )
        )
        status = json.loads(await socket.recv())["result"]
        assert status["capabilities"]["enabled"] == ["host.control", "run.cancel"]
        await socket.send(json.dumps(_request("host.control.status", {}, "web-status")))
        assert json.loads(await socket.recv())["result"]["state"] == "owner"
        await socket.send(
            json.dumps(
                _request(
                    "run.start",
                    {"mode": "build", "message": "hello", "thread_id": "t", "run_id": "r"},
                    "web-start",
                )
            )
        )
        denied = json.loads(await socket.recv())
        assert denied["error"]["code"] == -32008
        assert denied["error"]["data"]["code"] == "CONTROL_NOT_HOLDER"
        assert denied["error"]["data"]["retryable"] is True
    await host.close()


async def test_acquire_release_via_rpc_and_status(tmp_path: Path) -> None:
    """acquire/release/status 走 Protocol RPC，release 不关闭 WebSocket。"""
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "host.control"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43212"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize("host.control"),
                    "web-init",
                )
            )
        )
        await socket.recv()

        await socket.send(json.dumps(_request("host.control.acquire", {}, "acquire")))
        acquired = json.loads(await socket.recv())["result"]
        assert acquired["state"] == "attached"
        assert acquired["holder"]["role"] == "attached"
        assert acquired["holder"]["attachment_id"] == attachment["attachment_id"]

        await socket.send(json.dumps(_request("host.control.release", {}, "release")))
        released = json.loads(await socket.recv())["result"]
        assert released["state"] == "owner"
        assert released["holder"]["role"] == "owner"

        # release 不关闭 socket，attached 可再次 acquire。
        await socket.send(json.dumps(_request("host.control.acquire", {}, "acquire-2")))
        assert json.loads(await socket.recv())["result"]["state"] == "attached"
    await host.close()


async def test_release_is_blocked_while_attached_run_is_active(tmp_path: Path) -> None:
    """active Run 阻止 release，status 不会提前恢复 owner。"""
    owner_frames: list[dict[str, Any]] = []
    attached_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "host.control", "run.cancel"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43213"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize("host.control", "run.cancel"),
                    "web-init",
                )
            )
        )
        await socket.recv()
        await socket.send(json.dumps(_request("host.control.acquire", {}, "acquire")))
        await _recv_response(socket, "acquire")
        await socket.send(
            json.dumps(
                _request(
                    "run.start",
                    {"mode": "build", "message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
                    "web-start",
                )
            )
        )
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        await socket.send(json.dumps(_request("host.control.release", {}, "release")))
        blocked = await _recv_response(socket, "release")
        assert blocked["error"]["data"]["code"] == "CONTROL_RELEASE_BLOCKED"
        await socket.send(json.dumps(_request("host.control.status", {}, "status")))
        assert (await _recv_response(socket, "status"))["result"]["state"] == "attached"
    await host.close()


async def test_owner_revoke_connected_attachment_cancels_run_and_restores_owner(
    tmp_path: Path,
) -> None:
    """owner revoke 已连接 Web 时：socket 关闭、Run 取消、控制权归还 owner。"""
    owner_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    host._owner_connection.watched_threads.add("thread-1")
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "host.control", "run.cancel"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43214"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    socket = await connect(attachment["endpoint"], origin=origin, proxy=None)
    await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
    assert json.loads(await socket.recv()) == {"type": "ready"}
    await socket.send(
        json.dumps(
            _request(
                "initialize",
                _initialize("host.control", "run.cancel"),
                "web-init",
            )
        )
    )
    await socket.recv()
    await socket.send(json.dumps(_request("host.control.acquire", {}, "acquire")))
    await _recv_response(socket, "acquire")
    await socket.send(
        json.dumps(
            _request(
                "run.start",
                {"mode": "build", "message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
                "web-start",
            )
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await host.dispatch(
        _request(
            "host.attachment.revoke",
            {"attachment_id": attachment["attachment_id"]},
            "revoke",
        )
    )
    revoke_result = owner_frames[-1]["result"]
    assert revoke_result["attachment_id"] == attachment["attachment_id"]
    assert revoke_result["revoked"] is True
    assert revoke_result["control"]["state"] == "owner"
    assert revoke_result["control"]["holder"]["role"] == "owner"

    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(_drain_until_closed(socket), timeout=5)
    for _ in range(200):
        if any(
            frame.get("params", {}).get("type") == "run.cancelled"
            for frame in owner_frames
        ):
            break
        await asyncio.sleep(0.01)
    cancelled = [
        frame["params"]
        for frame in owner_frames
        if frame.get("params", {}).get("type") == "run.cancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0]["payload"]["reason"] == "Cancelled by client"
    await host.close()


async def test_revoke_unconsumed_token_invalidates_it(tmp_path: Path) -> None:
    """未消费 token 的 revoke 直接完成，token 无法再认证。"""
    owner_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "host.control"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43215"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]
    await host.dispatch(
        _request(
            "host.attachment.revoke",
            {"attachment_id": attachment["attachment_id"]},
            "revoke",
        )
    )
    assert owner_frames[-1]["result"]["control"]["state"] == "owner"

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        with pytest.raises(ConnectionClosed):
            await socket.recv()

    await host.dispatch(
        _request(
            "host.attachment.revoke",
            {"attachment_id": attachment["attachment_id"]},
            "revoke-again",
        )
    )
    assert owner_frames[-1]["result"]["revoked"] is True
    await host.dispatch(
        _request(
            "host.attachment.revoke",
            {"attachment_id": "missing-attachment"},
            "revoke-missing",
        )
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "ATTACHMENT_NOT_FOUND"
    await host.close()


async def test_attached_disconnect_cancels_run_and_restores_owner(
    tmp_path: Path,
) -> None:
    """WebSocket 自然断线进入相同收敛路径：取消 Run 并归还 owner。"""
    owner_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "host.control", "run.cancel"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43216"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    socket = await connect(attachment["endpoint"], origin=origin, proxy=None)
    await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
    assert json.loads(await socket.recv()) == {"type": "ready"}
    await socket.send(
        json.dumps(
            _request(
                "initialize",
                _initialize("host.control", "run.cancel"),
                "web-init",
            )
        )
    )
    await socket.recv()
    await socket.send(json.dumps(_request("host.control.acquire", {}, "acquire")))
    await socket.recv()
    await socket.send(
        json.dumps(
            _request(
                "run.start",
                {"mode": "build", "message": "slow", "thread_id": "thread-1", "run_id": "run-1"},
                "web-start",
            )
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    await socket.close()

    for _ in range(200):
        await host.dispatch(
            _request("host.control.status", {}, "owner-status")
        )
        if owner_frames[-1].get("result", {}).get("state") == "owner":
            break
        await asyncio.sleep(0.01)
    assert owner_frames[-1]["result"]["state"] == "owner"
    assert await host._run_coordinator.is_active("thread-1") is False
    await host.close()


async def test_attached_capability_ceiling_excludes_attach_and_multithread(
    tmp_path: Path,
) -> None:
    """Web ceiling 不含 host.attach/run.multithread，请求它们也不会提升。"""
    owner_frames: list[dict[str, Any]] = []
    host = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize(
                "host.attach",
                "run.multithread",
                "host.control",
                "run.cancel",
            ),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43217"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize(
                        "host.attach",
                        "run.multithread",
                        "host.control",
                        "run.cancel",
                    ),
                    "web-init",
                )
            )
        )
        initialized = json.loads(await socket.recv())["result"]
        available = set(initialized["capabilities"]["available"])
        enabled = set(initialized["capabilities"]["enabled"])
        assert "host.attach" not in available
        assert "run.multithread" not in available
        assert "host.attach" not in enabled
        assert "run.multithread" not in enabled
        assert {"host.control", "run.cancel"} <= enabled
    await host.close()


async def test_connection_run_busy_without_multithread(tmp_path: Path) -> None:
    """无 run.multithread 时，同一 Connection 的第二个 starting/active Run 被拒。"""
    owner_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(_request("initialize", _initialize(), "owner-init"))
    await host.dispatch(
        _request(
            "run.start",
            {"mode": "build", "message": "first", "thread_id": "thread-1", "run_id": "run-1"},
            "start-1",
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    await host.dispatch(
        _request(
            "run.start",
            {"mode": "build", "message": "second", "thread_id": "thread-2", "run_id": "run-2"},
            "start-2",
        )
    )
    assert owner_frames[-1]["error"]["data"]["code"] == "CONNECTION_RUN_BUSY"
    assert owner_frames[-1]["error"]["code"] == -32000
    await host.close()


async def test_multithread_owner_can_run_parallel_threads(tmp_path: Path) -> None:
    """有 run.multithread 时，同一 Connection 可在不同 Thread 并发 Run。"""
    owner_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request("initialize", _initialize("run.multithread"), "owner-init")
    )
    await host.dispatch(
        _request(
            "run.start",
            {"mode": "build", "message": "first", "thread_id": "thread-1", "run_id": "run-1"},
            "start-1",
        )
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    await host.dispatch(
        _request(
            "run.start",
            {"mode": "build", "message": "second", "thread_id": "thread-2", "run_id": "run-2"},
            "start-2",
        )
    )
    assert owner_frames[-1]["result"]["accepted"] is True
    await host.close()


async def test_acquire_and_owner_run_start_race_has_single_winner(
    tmp_path: Path,
) -> None:
    """owner run.start 与 attached acquire 并发竞争时恰好一方被受理。"""
    owner_frames: list[dict[str, Any]] = []
    agent = _BlockingAgent()
    host = AgentHost(agent=agent, config_home=tmp_path / "home", workspace=tmp_path)
    host.send = lambda message: _append(owner_frames, message)  # type: ignore[method-assign]
    await host.dispatch(
        _request(
            "initialize",
            _initialize("host.attach", "host.control", "run.cancel"),
            "owner-init",
        )
    )
    origin = "http://127.0.0.1:43218"
    await host.dispatch(
        _request("host.attachment.create", {"origin": origin}, "attachment")
    )
    attachment = owner_frames[-1]["result"]

    async with connect(attachment["endpoint"], origin=origin, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": attachment["token"]}))
        assert json.loads(await socket.recv()) == {"type": "ready"}
        await socket.send(
            json.dumps(
                _request(
                    "initialize",
                    _initialize("host.control", "run.cancel"),
                    "web-init",
                )
            )
        )
        await socket.recv()
        async def send_request(payload: dict[str, Any]) -> dict[str, Any]:
            await socket.send(json.dumps(payload))
            return await _recv_response(socket, payload["id"])

        owner_start = asyncio.create_task(
            host.dispatch(
                _request(
                    "run.start",
                    {"mode": "build", "message": "race", "thread_id": "t", "run_id": "race-run"},
                    "owner-start",
                )
            )
        )
        web_acquire = asyncio.create_task(
            send_request(_request("host.control.acquire", {}, "web-acquire"))
        )
        await asyncio.gather(owner_start, web_acquire)
        start_accepted = any(
            frame.get("id") == "owner-start" and "result" in frame
            for frame in owner_frames
        )
        acquire_accepted = "result" in web_acquire.result()
        assert start_accepted != acquire_accepted
    await host.close()


class TestBuildApprovalClassifier:
    """AUTO 模式分类器装配：profile 解析失败时优雅降级而不是崩溃。"""

    @staticmethod
    def _fake_host(config: Any) -> Any:
        """构造只带 _config 属性的伪 host，供未绑定方法调用。"""
        from types import SimpleNamespace

        return SimpleNamespace(_config=config)

    @staticmethod
    def _model_settings(api_key: str | None) -> Any:
        """构造分类器 profile 用的模型设置；使用独立环境变量避免串用。"""
        from harness_agent.config.config import ModelSettings

        return ModelSettings(
            name="small-fast",
            base_url="https://gateway.example.internal/v1",
            api_key_env="HARNESS_CLASSIFIER_TEST_KEY",
            api_key=api_key,
            timeout_seconds=120.0,
        )

    def test_returns_classifier_when_profile_available(self, monkeypatch: pytest.MonkeyPatch):
        """profile 存在且密钥可用时返回 SafetyClassifier，并使用收紧的超时。"""
        from types import SimpleNamespace

        from harness_agent.config.config import ModelProfile
        from harness_agent.policy.classifier import SafetyClassifier

        monkeypatch.delenv("HARNESS_CLASSIFIER_TEST_KEY", raising=False)
        settings = self._model_settings(api_key="test-key")
        profile = ModelProfile(
            profile_id="small-fast", settings=settings, is_default=False, source="test"
        )
        config = SimpleNamespace(
            model_catalog=SimpleNamespace(require_profile=lambda _id: profile)
        )

        classifier = AgentHost._build_approval_classifier(self._fake_host(config), "small-fast")

        assert isinstance(classifier, SafetyClassifier)

    def test_missing_profile_degrades_to_none(self):
        """profile 不存在时返回 None（回退人工审批），不抛异常。"""
        from types import SimpleNamespace

        from harness_agent.config.config import ConfigError

        def require_profile(_id: str) -> Any:
            raise ConfigError("MODEL_PROFILE_NOT_FOUND: nope")

        config = SimpleNamespace(model_catalog=SimpleNamespace(require_profile=require_profile))

        assert AgentHost._build_approval_classifier(self._fake_host(config), "nope") is None

    def test_missing_api_key_degrades_to_none(self, monkeypatch: pytest.MonkeyPatch):
        """profile 无可用密钥时返回 None（回退人工审批），不抛异常。"""
        from types import SimpleNamespace

        from harness_agent.config.config import ModelProfile

        monkeypatch.delenv("HARNESS_CLASSIFIER_TEST_KEY", raising=False)
        profile = ModelProfile(
            profile_id="small-fast",
            settings=self._model_settings(api_key=None),
            is_default=False,
            source="test",
        )
        config = SimpleNamespace(
            model_catalog=SimpleNamespace(require_profile=lambda _id: profile)
        )

        assert (
            AgentHost._build_approval_classifier(self._fake_host(config), "small-fast") is None
        )

    def test_missing_model_catalog_degrades_to_none(self):
        """配置未加载模型目录时返回 None，不阻断引擎构建。"""
        assert AgentHost._build_approval_classifier(self._fake_host(None), "small-fast") is None



async def _append(frames: list[dict[str, Any]], message: dict[str, Any]) -> None:
    frames.append(message)


async def _drain_until_closed(socket: Any) -> None:
    """持续读取 socket；预期在连接关闭时抛 ConnectionClosed。"""
    while True:
        await socket.recv()
