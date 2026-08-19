"""compose.inspect / compose.abandon RPC 与 threads.open 进度投影。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_agent.compose.models import ThreadMode
from harness_agent.compose.session import ComposeSession, ComposeSessionPorts, ComposeTurnRequest
from harness_agent.threads.thread_persistence import AcceptRun
from tests.support.thread_fixtures import test_binding as make_test_binding


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0", "kind": "test"},
        "capabilities": {"requests": ["threads.read"], "handles": []},
    }
    params.update(overrides)
    return params


async def _capture_server(server: Any) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture  # type: ignore[method-assign]
    await server.dispatch(_request("initialize", _initialize_params(), "init"))
    return frames


async def _compose_server(tmp_path: Path, *, thread_id: str) -> tuple[Any, list[dict[str, Any]], Any]:
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

    async def run_grill(request: ComposeTurnRequest, slug: str) -> None:
        del request, slug

    session = ComposeSession(
        ComposeSessionPorts(
            store=persistence.compose_progress_store(),
            workspace=workspace,
            run_grill=run_grill,
        )
    )
    await session.execute_turn(
        ComposeTurnRequest(thread_id=thread_id, run_id=f"{thread_id}-run", message="写搜索")
    )
    return server, frames, persistence


async def _build_server(tmp_path: Path, *, thread_id: str) -> tuple[Any, list[dict[str, Any]]]:
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


async def test_threads_open_exposes_compose_progress(tmp_path: Path) -> None:
    """threads.open 在 Compose Thread 上返回冻结模式与 compose.progress。"""
    thread_id = "compose-thread"
    server, frames, _persistence = await _compose_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(_request("threads.open", {"thread_id": thread_id}, "open"))
        result = frames[-1]["result"]
        assert result["thread_mode"] == "compose"
        assert result["compose_progress"]["slug"]
        assert result["compose_progress"]["status"] == "active"
        assert result["compose_progress"]["current_stage"] == "grill"
        assert "work_item" not in result
    finally:
        await server._close_thread_persistence()


async def test_compose_inspect_returns_same_progress_as_threads_open(tmp_path: Path) -> None:
    """compose.inspect 返回与 threads.open 相同的 progress。"""
    thread_id = "compose-thread"
    server, frames, _persistence = await _compose_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(_request("threads.open", {"thread_id": thread_id}, "open"))
        opened = frames[-1]["result"]["compose_progress"]
        await server.dispatch(_request("compose.inspect", {"thread_id": thread_id}, "inspect"))
        inspected = frames[-1]["result"]["progress"]
        assert inspected == opened
        assert inspected["status"] == "active"
    finally:
        await server._close_thread_persistence()


async def test_compose_abandon_then_nothing_to_abandon(tmp_path: Path) -> None:
    """abandon 只清薄进度；再次 abandon 返回 COMPOSE_NOTHING_TO_ABANDON。"""
    thread_id = "compose-thread"
    server, frames, persistence = await _compose_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(
            _request("compose.abandon", {"thread_id": thread_id, "reason": "放弃"}, "abandon-ok")
        )
        result = frames[-1]["result"]
        assert result["progress"]["status"] == "abandoned"

        await server.dispatch(
            _request("compose.abandon", {"thread_id": thread_id}, "abandon-again")
        )
        error = frames[-1]["error"]
        assert error["message"] == "COMPOSE_NOTHING_TO_ABANDON"
        assert error["data"]["code"] == "COMPOSE_NOTHING_TO_ABANDON"

        inspected = await persistence.compose_progress_store().load(thread_id)
        assert inspected is None
    finally:
        await server._close_thread_persistence()


async def test_compose_inspect_on_build_thread_returns_mode_locked(tmp_path: Path) -> None:
    """Build Thread 上调用 compose.inspect 返回 THREAD_MODE_LOCKED。"""
    thread_id = "build-thread"
    server, frames = await _build_server(tmp_path, thread_id=thread_id)
    try:
        await server.dispatch(_request("compose.inspect", {"thread_id": thread_id}, "inspect"))
        error = frames[-1]["error"]
        assert error["data"]["code"] == "THREAD_MODE_LOCKED"
    finally:
        await server._close_thread_persistence()
