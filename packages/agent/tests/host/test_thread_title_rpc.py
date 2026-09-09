"""threads.set_title RPC：能力、owner 与空标题错误。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.support.thread_fixtures import accept_thread


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    """构造最小 JSON-RPC request。"""
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


async def test_threads_set_title_writes_summary(tmp_path: Path) -> None:
    """owner 可以把当前 project 的 thread 改成短标题。"""
    from harness_agent.host.agent_host import AgentHost

    project = tmp_path / "project"
    project.mkdir()
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=project)
    server.send = capture
    await server.dispatch(_initialize(["threads.read"]))
    store = await server._ensure_thread_persistence()
    await accept_thread(store, "thread-1", "请检查当前改动")

    await server.dispatch(
        _request("threads.set_title", {"thread_id": "thread-1", "title": "修索引"}, "set"),
    )
    result = frames[-1]["result"]
    assert result["thread"]["title"] == "修索引"
    assert result["thread"]["thread_id"] == "thread-1"
    await server.dispatch(_request("threads.list", {}, "list"))
    assert frames[-1]["result"]["threads"][0]["title"] == "修索引"
    await server.close()


async def test_threads_set_title_rejects_empty_and_missing(tmp_path: Path) -> None:
    """空标题与不存在的 thread 返回稳定错误码。"""
    from harness_agent.host.agent_host import AgentHost

    project = tmp_path / "project"
    project.mkdir()
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = AgentHost(allow_echo=False, config_home=tmp_path / "home", workspace=project)
    server.send = capture
    await server.dispatch(_initialize(["threads.read"]))
    store = await server._ensure_thread_persistence()
    await accept_thread(store, "thread-1", "请检查当前改动")

    await server.dispatch(
        _request("threads.set_title", {"thread_id": "thread-1", "title": "  \n"}, "empty"),
    )
    assert frames[-1]["error"]["message"] == "TITLE_EMPTY"
    await server.dispatch(
        _request("threads.set_title", {"thread_id": "missing", "title": "修索引"}, "missing"),
    )
    assert frames[-1]["error"]["message"] == "THREAD_NOT_FOUND"
    await server.close()
