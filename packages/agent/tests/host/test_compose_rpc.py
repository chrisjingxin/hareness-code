"""compose.inspect / compose.abandon RPC 与 threads.open Work Item 投影测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_agent.compose.models import ComposeWorkItemStatus, ThreadMode
from harness_agent.threads.compose_work_item_store import CreateComposeWorkItem
from harness_agent.threads.thread_persistence import AcceptRun
from tests.support.thread_fixtures import test_binding as make_test_binding


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params(**overrides: Any) -> dict[str, Any]:
    """仅协商 threads.read 能力的最小握手参数。"""
    params: dict[str, Any] = {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0", "kind": "test"},
        "capabilities": {"requests": ["threads.read"], "handles": []},
    }
    params.update(overrides)
    return params


async def _capture_server(server: Any) -> list[dict[str, Any]]:
    """初始化 server 并捕获 owner 侧所有输出帧。"""
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture  # type: ignore[method-assign]
    await server.dispatch(_request("initialize", _initialize_params(), "init"))
    return frames


async def _compose_server(
    tmp_path: Path, *, thread_id: str
) -> tuple[Any, list[dict[str, Any]], Any, Any]:
    """预置：冻结 compose thread + 一个 active Work Item 的 AgentHost。"""
    from harness_agent.host.agent_host import AgentHost

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    server = AgentHost(config_home=home, workspace=workspace)
    frames = await _capture_server(server)

    persistence = await server._ensure_thread_persistence()
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding(thread_id, f"run-{thread_id}"),
            mode=ThreadMode.COMPOSE,
        )
    )
    item = await persistence.compose_work_item_store().create(
        CreateComposeWorkItem(
            thread_id=thread_id,
            work_item_id=f"{thread_id}-wi",
            slug=f"{thread_id}-wi",
            goal="实现搜索功能",
            created_at_ms=1_700_000_000_000,
        )
    )
    return server, frames, persistence, item


async def _build_server(tmp_path: Path, *, thread_id: str) -> tuple[Any, list[dict[str, Any]]]:
    """预置：冻结 build thread 的 AgentHost。"""
    from harness_agent.host.agent_host import AgentHost

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    server = AgentHost(config_home=home, workspace=workspace)
    frames = await _capture_server(server)
    persistence = await server._ensure_thread_persistence()
    await persistence.accept_run(
        AcceptRun(
            message="受理",
            binding=make_test_binding(thread_id, f"run-{thread_id}"),
            mode=ThreadMode.BUILD,
        )
    )
    return server, frames


async def test_threads_open_exposes_compose_mode_and_work_item(tmp_path: Path) -> None:
    """threads.open 在 Compose Thread 上返回冻结模式与 active Work Item 投影。"""
    thread_id = "compose-thread"
    server, frames, persistence, item = await _compose_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(_request("threads.open", {"thread_id": thread_id}, "open"))
        result = frames[-1]["result"]
        assert result["thread_mode"] == "compose"
        assert result["work_item"]["work_item_id"] == item.work_item_id
        assert result["work_item"]["slug"] == item.slug
        assert result["work_item"]["status"] == "active"
        assert result["work_item"]["revision"] == 0
        assert result["work_item"]["pending_decision"] is None
        assert result["work_item"]["blocked_reason"] is None
    finally:
        await server._close_thread_persistence()


async def test_compose_inspect_returns_same_projection_as_threads_open(tmp_path: Path) -> None:
    """compose.inspect 返回与 threads.open 相同的非敏感 Work Item 投影。"""
    thread_id = "compose-thread"
    server, frames, persistence, item = await _compose_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(_request("threads.open", {"thread_id": thread_id}, "open"))
        opened_work_item = frames[-1]["result"]["work_item"]

        await server.dispatch(_request("compose.inspect", {"thread_id": thread_id}, "inspect"))
        inspect_work_item = frames[-1]["result"]["work_item"]

        assert inspect_work_item == opened_work_item
        assert inspect_work_item["work_item_id"] == item.work_item_id
        assert inspect_work_item["title"] == item.goal
        assert inspect_work_item["status"] == "active"
    finally:
        await server._close_thread_persistence()


async def test_compose_abandon_revision_conflict_then_success(tmp_path: Path) -> None:
    """abandon 以 revision CAS 终结：陈旧 revision 冲突，正确 revision 返回 abandoned。"""
    thread_id = "compose-thread"
    server, frames, persistence, item = await _compose_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(
            _request(
                "compose.abandon",
                {
                    "thread_id": thread_id,
                    "work_item_id": item.work_item_id,
                    "expected_revision": item.revision + 1,
                    "reason": "放弃",
                },
                "abandon-stale",
            )
        )
        error = frames[-1]["error"]
        assert error["message"] == "COMPOSE_WORK_ITEM_REVISION_CONFLICT"
        assert error["data"]["code"] == "COMPOSE_WORK_ITEM_REVISION_CONFLICT"

        await server.dispatch(
            _request(
                "compose.abandon",
                {
                    "thread_id": thread_id,
                    "work_item_id": item.work_item_id,
                    "expected_revision": item.revision,
                    "reason": "放弃",
                },
                "abandon-ok",
            )
        )
        result = frames[-1]["result"]
        assert result["work_item"]["status"] == "abandoned"
        assert result["work_item"]["work_item_id"] == item.work_item_id

        loaded = await persistence.compose_work_item_store().load(item.work_item_id)
        assert loaded is not None
        assert loaded.status is ComposeWorkItemStatus.ABANDONED
        assert loaded.terminal is True
    finally:
        await server._close_thread_persistence()


async def test_compose_inspect_on_build_thread_returns_mode_locked(tmp_path: Path) -> None:
    """Build Thread 上调用 compose.inspect 返回 THREAD_MODE_LOCKED。"""
    thread_id = "build-thread"
    server, frames = await _build_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(_request("compose.inspect", {"thread_id": thread_id}, "inspect"))
        error = frames[-1]["error"]
        assert error["message"] == "THREAD_MODE_LOCKED"
        assert error["data"]["code"] == "THREAD_MODE_LOCKED"
    finally:
        await server._close_thread_persistence()
