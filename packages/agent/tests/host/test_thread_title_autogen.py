"""第一条用户消息后的旁路自动起名：成功通知、失败静默、用户 CAS 优先。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tests.support.thread_fixtures import accept_thread


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize(capabilities: list[str]) -> dict[str, Any]:
    return _request(
        "initialize",
        {
            "protocol": {"major": 3, "min_minor": 0, "max_minor": 9},
            "client": {"name": "test", "version": "0.1.0", "kind": "test"},
            "capabilities": {"requests": capabilities, "handles": []},
        },
        "initialize",
    )


async def _wait_autogen(server: Any) -> None:
    pending = list(getattr(server, "_title_autogen_tasks", ()))
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_auto_title_writes_summary_and_notifies_without_transcript(tmp_path: Path) -> None:
    """自动起名成功后 list 有 title，发出 thread.summary，不写 Transcript。"""
    from harness_agent.host.agent_host import AgentHost

    project = tmp_path / "project"
    project.mkdir()
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=project)
    server.send = capture

    async def draft(*, model_settings: Any, user_message: str) -> str:
        del model_settings
        assert "请检查当前改动" in user_message
        return "修索引"

    server._draft_thread_title = draft  # type: ignore[method-assign]
    server._title_model_settings = lambda _profile=None: object()  # type: ignore[method-assign]
    await server.dispatch(_initialize(["threads.read"]))
    store = await server._ensure_thread_persistence()
    await accept_thread(store, "thread-1", "请检查当前改动")
    server._schedule_thread_title_autogen(
        thread_id="thread-1",
        user_message="请检查当前改动",
        requested_profile_id=None,
        owner_connection_id=server._owner_connection.connection_id,
    )
    await _wait_autogen(server)

    listed = await store.list_threads()
    assert listed[0].title == "修索引"
    notices = [frame for frame in frames if frame.get("method") == "thread.summary"]
    assert len(notices) == 1
    assert notices[0]["params"]["title"] == "修索引"
    assert "run_id" not in notices[0]["params"]
    opened = await store.open_thread("thread-1")
    assert all(message.kind == "user" for message in opened.messages)
    await server.close()


async def test_auto_title_failure_leaves_null_title_and_does_not_notify(tmp_path: Path) -> None:
    """起名失败不写库、不通知、不打断。"""
    from harness_agent.host.agent_host import AgentHost

    project = tmp_path / "project"
    project.mkdir()
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=project)
    server.send = capture

    async def draft(*, model_settings: Any, user_message: str) -> str:
        del model_settings, user_message
        raise RuntimeError("gateway unavailable")

    server._draft_thread_title = draft  # type: ignore[method-assign]
    server._title_model_settings = lambda _profile=None: object()  # type: ignore[method-assign]
    await server.dispatch(_initialize(["threads.read"]))
    store = await server._ensure_thread_persistence()
    await accept_thread(store, "thread-1", "请检查当前改动")
    server._schedule_thread_title_autogen(
        thread_id="thread-1",
        user_message="请检查当前改动",
        requested_profile_id=None,
        owner_connection_id=server._owner_connection.connection_id,
    )
    await _wait_autogen(server)

    assert (await store.list_threads())[0].title is None
    assert [frame for frame in frames if frame.get("method") == "thread.summary"] == []
    await server.close()


async def test_late_auto_title_does_not_override_user_title(tmp_path: Path) -> None:
    """用户 /title 之后，迟到的自动结果必须静默丢弃。"""
    from harness_agent.host.agent_host import AgentHost

    project = tmp_path / "project"
    project.mkdir()
    frames: list[dict[str, Any]] = []
    gate = asyncio.Event()

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=project)
    server.send = capture

    async def draft(*, model_settings: Any, user_message: str) -> str:
        del model_settings, user_message
        await gate.wait()
        return "自动名"

    server._draft_thread_title = draft  # type: ignore[method-assign]
    server._title_model_settings = lambda _profile=None: object()  # type: ignore[method-assign]
    await server.dispatch(_initialize(["threads.read"]))
    store = await server._ensure_thread_persistence()
    await accept_thread(store, "thread-1", "请检查当前改动")
    server._schedule_thread_title_autogen(
        thread_id="thread-1",
        user_message="请检查当前改动",
        requested_profile_id=None,
        owner_connection_id=server._owner_connection.connection_id,
    )
    await server.dispatch(
        _request("threads.set_title", {"thread_id": "thread-1", "title": "我起的名"}, "set"),
    )
    gate.set()
    await _wait_autogen(server)

    assert (await store.list_threads())[0].title == "我起的名"
    auto_notices = [
        frame
        for frame in frames
        if frame.get("method") == "thread.summary" and frame.get("params", {}).get("title") == "自动名"
    ]
    assert auto_notices == []
    await server.close()


async def test_auto_title_skipped_in_echo_mode(tmp_path: Path) -> None:
    """echo 模式不启动自动起名。"""
    from harness_agent.host.agent_host import AgentHost

    project = tmp_path / "project"
    project.mkdir()
    called = False
    server = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=project)
    server.send = lambda _message: None

    async def draft(*, model_settings: Any, user_message: str) -> str:
        del model_settings, user_message
        nonlocal called
        called = True
        return "不该出现"

    server._draft_thread_title = draft  # type: ignore[method-assign]
    server._schedule_thread_title_autogen(
        thread_id="thread-1",
        user_message="请检查当前改动",
        requested_profile_id=None,
        owner_connection_id=server._owner_connection.connection_id,
    )
    await _wait_autogen(server)
    assert called is False
    await server.close()
