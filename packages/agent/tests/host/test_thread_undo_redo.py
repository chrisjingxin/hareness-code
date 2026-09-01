"""threads.list_turns、threads.undo、threads.redo RPC 与状态机测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from harness_agent.threads.thread_persistence import TranscriptAppend, _user_record_id
from tests.support.thread_fixtures import accept_thread


def _request(method: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}


def _initialize_params() -> dict[str, Any]:
    return {
        "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
        "client": {"name": "test", "version": "0.1.0", "kind": "test"},
        "capabilities": {"requests": ["threads.read", "context.manage"], "handles": []},
    }


def _git_init(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    (path / "file1.txt").write_text("initial v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


async def _capture_host(tmp_path: Path, workspace: Path) -> tuple[Any, list[dict[str, Any]]]:
    from harness_agent.host.agent_host import AgentHost

    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server = AgentHost(
        allow_echo=False,
        config_home=tmp_path / "home",
        workspace=workspace,
    )
    server.send = capture
    await server.dispatch(_request("initialize", _initialize_params(), "init"))
    return server, frames


def _result(frames: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    frame = next(item for item in frames if item.get("id") == request_id)
    assert "error" not in frame, frame
    return frame["result"]


async def _seed_two_turns(server: Any, workspace: Path, thread_id: str) -> tuple[str, str]:
    store = await server._ensure_thread_persistence()
    await accept_thread(store, thread_id, "Turn 1: 创建初始文件", run_id="run-1")
    tree_1 = server._git_checkpoints.create_tree_snapshot(workspace)
    turn_1 = _user_record_id("run-1")
    await store.record_git_checkpoint(thread_id, turn_1, "run-1", tree_1)
    await store.append_transcript(
        TranscriptAppend(
            thread_id=thread_id,
            record_id="run:run-1:assistant:1",
            kind="assistant",
            content="已创建初始文件",
            run_id="run-1",
            execution_id="root-run-1",
        )
    )

    (workspace / "file1.txt").write_text("modified in turn 2\n", encoding="utf-8")
    (workspace / "file2.txt").write_text("added in turn 2\n", encoding="utf-8")
    await accept_thread(store, thread_id, "Turn 2: 修改文件", run_id="run-2")
    tree_2 = server._git_checkpoints.create_tree_snapshot(workspace)
    turn_2 = _user_record_id("run-2")
    await store.record_git_checkpoint(thread_id, turn_2, "run-2", tree_2)
    await store.append_transcript(
        TranscriptAppend(
            thread_id=thread_id,
            record_id="run:run-2:assistant:1",
            kind="assistant",
            content="已修改文件",
            run_id="run-2",
            execution_id="root-run-2",
        )
    )
    return turn_1, turn_2


async def test_list_turns_uses_user_record_ids_and_omits_null_optional_fields(
    tmp_path: Path,
) -> None:
    """list_turns 必须用 User record_id 作为 turn_id，且不得把可选字段写成 null。"""
    workspace = _git_init(tmp_path / "git_project")
    server, frames = await _capture_host(tmp_path, workspace)
    thread_id = "test-undo-thread"
    turn_1, turn_2 = await _seed_two_turns(server, workspace, thread_id)

    await server.dispatch(_request("threads.list_turns", {"thread_id": thread_id}, "list_turns"))
    result = _result(frames, "list_turns")
    turns = result["turns"]
    assert [item["turn_id"] for item in turns] == [turn_1, turn_2]
    assert result["active_turn_id"] == turn_2
    assert "reverted_turn_id" not in result
    assert all("diff_stats" in item for item in turns)
    assert all(item["has_git_checkpoint"] is True for item in turns)
    assert turns[0]["user_prompt"] == "Turn 1: 创建初始文件"
    await server.close()


async def test_undo_both_restores_files_and_redo_restores_pre_undo_workspace(
    tmp_path: Path,
) -> None:
    """both 模式还原代码并进入暂存态；redo 恢复 undo 前的工作区。"""
    workspace = _git_init(tmp_path / "git_project")
    server, frames = await _capture_host(tmp_path, workspace)
    thread_id = "test-undo-thread"
    turn_1, _turn_2 = await _seed_two_turns(server, workspace, thread_id)

    await server.dispatch(
        _request(
            "threads.undo",
            {"thread_id": thread_id, "target_turn_id": turn_1, "mode": "both"},
            "undo",
        )
    )
    undo_res = _result(frames, "undo")
    assert undo_res["success"] is True
    assert undo_res["reverted_turn_id"] == turn_1
    assert (workspace / "file1.txt").read_text(encoding="utf-8") == "initial v1\n"
    assert not (workspace / "file2.txt").exists()

    await server.dispatch(_request("threads.list_turns", {"thread_id": thread_id}, "list_after_undo"))
    assert _result(frames, "list_after_undo")["reverted_turn_id"] == turn_1

    await server.dispatch(_request("threads.redo", {"thread_id": thread_id}, "redo"))
    redo_res = _result(frames, "redo")
    assert redo_res["success"] is True
    assert (workspace / "file1.txt").read_text(encoding="utf-8") == "modified in turn 2\n"
    assert (workspace / "file2.txt").exists()

    store = await server._ensure_thread_persistence()
    assert await store.get_thread_reverted_turn(thread_id) is None
    await server.close()


async def test_undo_conversation_keeps_files_and_marks_reverted(tmp_path: Path) -> None:
    """仅回退对话时不改工作区，但标记暂存态供后续 cleanup。"""
    workspace = _git_init(tmp_path / "git_project")
    server, frames = await _capture_host(tmp_path, workspace)
    thread_id = "test-undo-thread"
    turn_1, _turn_2 = await _seed_two_turns(server, workspace, thread_id)

    await server.dispatch(
        _request(
            "threads.undo",
            {"thread_id": thread_id, "target_turn_id": turn_1, "mode": "conversation"},
            "undo",
        )
    )
    _result(frames, "undo")
    assert (workspace / "file1.txt").read_text(encoding="utf-8") == "modified in turn 2\n"
    assert (workspace / "file2.txt").exists()

    store = await server._ensure_thread_persistence()
    assert await store.get_thread_reverted_turn(thread_id) == turn_1
    records = await store.load_transcript(thread_id)
    assert [rec.record_id for rec in records if rec.kind == "user"] == [
        _user_record_id("run-1"),
        _user_record_id("run-2"),
    ]
    await server.close()


async def test_undo_code_restores_files_but_keeps_transcript(tmp_path: Path) -> None:
    """仅还原代码时改工作区，对话记录仍保留。"""
    workspace = _git_init(tmp_path / "git_project")
    server, frames = await _capture_host(tmp_path, workspace)
    thread_id = "test-undo-thread"
    turn_1, _turn_2 = await _seed_two_turns(server, workspace, thread_id)

    await server.dispatch(
        _request(
            "threads.undo",
            {"thread_id": thread_id, "target_turn_id": turn_1, "mode": "code"},
            "undo",
        )
    )
    _result(frames, "undo")
    assert (workspace / "file1.txt").read_text(encoding="utf-8") == "initial v1\n"
    assert not (workspace / "file2.txt").exists()

    store = await server._ensure_thread_persistence()
    records = await store.load_transcript(thread_id)
    assert [rec.record_id for rec in records if rec.kind == "user"] == [
        _user_record_id("run-1"),
        _user_record_id("run-2"),
    ]
    await server.close()


async def test_new_run_after_conversation_undo_physically_truncates(tmp_path: Path) -> None:
    """Reverted 态下发新 Prompt 会物理删除目标 Turn 之后的 Transcript 与 checkpoint。"""
    workspace = _git_init(tmp_path / "git_project")
    server, frames = await _capture_host(tmp_path, workspace)
    thread_id = "test-undo-thread"
    turn_1, turn_2 = await _seed_two_turns(server, workspace, thread_id)

    await server.dispatch(
        _request(
            "threads.undo",
            {"thread_id": thread_id, "target_turn_id": turn_1, "mode": "conversation"},
            "undo",
        )
    )
    _result(frames, "undo")

    await server.dispatch(
        _request(
            "run.start",
            {"mode": "build", "message": "Turn 3: 新分支", "thread_id": thread_id, "run_id": "run-3"},
            "run-3",
        )
    )

    store = await server._ensure_thread_persistence()
    records = await store.load_transcript(thread_id)
    record_ids = [rec.record_id for rec in records]
    assert turn_1 in record_ids
    assert "run:run-1:assistant:1" in record_ids
    assert turn_2 not in record_ids
    assert "run:run-2:assistant:1" not in record_ids
    assert await store.get_thread_reverted_turn(thread_id) is None
    checkpoints = await store.get_git_checkpoints(thread_id)
    assert turn_2 not in checkpoints
    await server.close()


async def test_run_start_records_checkpoint_under_user_turn_id(tmp_path: Path) -> None:
    """run.start 必须把 Git 快照记在 User Turn record_id 上，而不是裸 run_id。"""
    workspace = _git_init(tmp_path / "git_project")
    server, frames = await _capture_host(tmp_path, workspace)
    thread_id = "test-checkpoint-thread"

    await server.dispatch(
        _request(
            "run.start",
            {"mode": "build", "message": "hello checkpoint", "thread_id": thread_id, "run_id": "run-a"},
            "run-a",
        )
    )

    store = await server._ensure_thread_persistence()
    checkpoints = await store.get_git_checkpoints(thread_id)
    assert _user_record_id("run-a") in checkpoints
    assert "run-a" not in checkpoints
    await server.close()
