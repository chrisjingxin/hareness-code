"""thread 恢复 RPC：能力协商、project 范围和不可恢复错误回归测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    """构造最小 JSON-RPC request，保持测试帧与 stdio wire 一致。"""
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


async def test_thread_rpc_requires_capability_and_only_lists_current_project(tmp_path: Path) -> None:
    """未协商时拒绝读取；协商后只返回当前 project 的 thread 索引。"""
    from harness_agent.server import JsonRpcServer

    project = tmp_path / "project"
    project.mkdir()
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = JsonRpcServer(allow_echo=False, config_home=tmp_path / "home")
    server.send = capture
    await server.dispatch(
        _request(
            "initialize",
            {
                "protocol": {"major": 2, "min_minor": 0, "max_minor": 2},
                "client": {"name": "test", "version": "0.1.0"},
                "capabilities": ["threads.read"],
                "cwd": str(project),
            },
            "initialize",
        )
    )
    store = await server._ensure_thread_store()
    await store.record_message("thread-1", "恢复这个 thread")

    await server.dispatch(_request("threads.list", {}, "list"))
    listed = frames[-1]["result"]["threads"]
    assert listed == [{
        "thread_id": "thread-1",
        "created_at_ms": listed[0]["created_at_ms"],
        "updated_at_ms": listed[0]["updated_at_ms"],
        "first_message": "恢复这个 thread",
        "latest_message": "恢复这个 thread",
        "message_count": 0,
    }]

    await server.dispatch(_request("threads.open", {"thread_id": "thread-1"}, "open"))
    assert frames[-1]["error"]["code"] == -32004
    assert frames[-1]["error"]["message"] == "THREAD_NOT_RECOVERABLE"
    await server.dispatch(_request("shutdown", {}, "shutdown"))


async def test_thread_rpc_rejects_unnegotiated_reads(tmp_path: Path) -> None:
    """没有 `threads.read` 的客户端不能把普通 JSON-RPC 当作本地 thread 浏览器。"""
    from harness_agent.server import JsonRpcServer

    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = JsonRpcServer(allow_echo=False, config_home=tmp_path / "home")
    server.send = capture
    await server.dispatch(
        _request(
            "initialize",
            {
                "protocol": {"major": 2, "min_minor": 0, "max_minor": 2},
                "client": {"name": "test", "version": "0.1.0"},
                "capabilities": [],
                "cwd": str(tmp_path),
            },
            "initialize",
        )
    )
    await server.dispatch(_request("threads.list", {}, "list"))
    assert frames[-1]["error"] == {"code": -32002, "message": "THREADS_CAPABILITY_REQUIRED"}
