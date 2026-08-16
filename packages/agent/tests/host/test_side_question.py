"""threads.side_question 临时只读问答端到端与生命周期测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_agent.host.agent_host import AgentHost
from harness_agent.threads.thread_persistence import ThreadPersistence
from tests.support.thread_fixtures import accept_thread


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params(**overrides: Any) -> dict[str, Any]:
    requested = overrides.pop(
        "capabilities",
        [
            "threads.read",
            "models.read",
            "models.select",
        ],
    )
    params: dict[str, Any] = {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0", "kind": "test"},
        "capabilities": {"requests": requested, "handles": []},
    }
    params.update(overrides)
    return params


async def _capture_server(server: Any, init_params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(_request("initialize", init_params or _initialize_params(), "init-1"))
    return frames


async def test_side_question_echo_mode(tmp_path: Path) -> None:
    """在 allow_echo 模式下，threads.side_question 正确返回 echo 回复。"""
    server = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    frames = await _capture_server(server)

    await server.dispatch(
        _request(
            "threads.side_question",
            {"thread_id": "thread-1", "question": "什么是 Python？"},
            "sq-1",
        )
    )

    response = next(frame for frame in frames if frame.get("id") == "sq-1")
    assert "error" not in response
    assert response["result"]["reply_text"] == "echo: 什么是 Python？"
    assert response["result"]["model_profile_id"] == "echo"


async def test_side_question_requires_threads_read_capability(tmp_path: Path) -> None:
    """未协商 threads.read 能力时，threads.side_question 返回 CAPABILITY_REQUIRED 错误。"""
    server = AgentHost(allow_echo=True, config_home=tmp_path / "home", workspace=tmp_path)
    frames = await _capture_server(server, init_params=_initialize_params(capabilities=["models.read"]))

    await server.dispatch(
        _request(
            "threads.side_question",
            {"thread_id": "thread-1", "question": "测试问题"},
            "sq-2",
        )
    )

    response = next(frame for frame in frames if frame.get("id") == "sq-2")
    assert response["error"]["data"]["code"] == "CAPABILITY_REQUIRED"


async def test_side_question_does_not_mutate_transcript(tmp_path: Path) -> None:
    """threads.side_question 执行后，Thread Transcript 历史消息条数完全不变。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    persistence = await ThreadPersistence.open(project=project, home=home)
    await accept_thread(persistence, "thread-test-1", "第一条用户消息")

    initial_opened = await persistence.open_thread("thread-test-1")
    initial_message_count = len(initial_opened.messages)

    server = AgentHost(
        allow_echo=True,
        config_home=home,
        workspace=project,
    )
    frames = await _capture_server(server)

    await server.dispatch(
        _request(
            "threads.side_question",
            {"thread_id": "thread-test-1", "question": "临时问题：上一句是什么？"},
            "sq-3",
        )
    )

    response = next(frame for frame in frames if frame.get("id") == "sq-3")
    assert response["result"]["reply_text"] == "echo: 临时问题：上一句是什么？"

    after_opened = await persistence.open_thread("thread-test-1")
    assert len(after_opened.messages) == initial_message_count
