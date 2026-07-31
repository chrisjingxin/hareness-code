"""tools_mode 与 tools_memory 模块的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.tools.tools_mode import BackgroundTaskManager, PlanModeState
from harness_agent.tools.tools_memory import _sanitize_key, memory_save, memory_search


# ---------- PlanModeState ----------


def test_plan_mode_enter_exit():
    state = PlanModeState(initial_mode="default")
    result = state.enter("auto_approve")
    assert result["success"] is True
    assert state.active is True
    assert state.previous_mode == "auto_approve"

    result = state.exit()
    assert result["success"] is True
    assert result["restored_mode"] == "auto_approve"
    assert state.active is False


def test_plan_mode_double_enter():
    state = PlanModeState()
    state.enter("default")
    result = state.enter("default")
    assert result["success"] is False
    assert "已处于计划模式中" in result["error"]


def test_plan_mode_exit_without_enter():
    state = PlanModeState()
    result = state.exit()
    assert result["success"] is False
    assert "当前不在计划模式中" in result["error"]


# ---------- BackgroundTaskManager ----------


def test_background_task_lifecycle():
    mgr = BackgroundTaskManager()
    task_id = mgr.register("npm run dev")
    assert task_id == "task-1"

    output = mgr.get_output(task_id)
    assert output["success"] is True
    assert output["status"] == "running"

    stop_result = mgr.stop(task_id)
    assert stop_result["success"] is True

    output = mgr.get_output(task_id)
    assert output["status"] == "stopped"

    tasks = mgr.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == task_id


def test_background_task_not_found():
    mgr = BackgroundTaskManager()
    result = mgr.get_output("task-999")
    assert result["success"] is False
    assert "不存在" in result["error"]

    result = mgr.stop("task-999")
    assert result["success"] is False
    assert "不存在" in result["error"]


# ---------- memory ----------


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """将 Path.home() 重定向到 tmp_path，隔离文件系统副作用。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_memory_save_and_search(fake_home: Path):
    result = memory_save("project-setup", "使用 Bun 作为包管理器")
    assert result["success"] is True
    assert result["key"] == "project-setup"

    # 验证文件已写入
    saved_file = fake_home / ".harness" / "memory" / "project-setup.json"
    assert saved_file.exists()

    # 搜索能命中
    search_result = memory_search("bun")
    assert len(search_result["results"]) == 1
    assert search_result["results"][0]["key"] == "project-setup"
    assert "Bun" in search_result["results"][0]["content"]


def test_memory_search_empty(fake_home: Path):
    result = memory_search("anything")
    assert result["results"] == []


def test_memory_key_sanitization(fake_home: Path):
    assert _sanitize_key("hello world!") == "hello_world_"
    assert _sanitize_key("a/b\\c:d") == "a_b_c_d"
    assert _sanitize_key("valid-key_123") == "valid-key_123"

    result = memory_save("my key@v2", "内容")
    assert result["success"] is True
    assert result["key"] == "my_key_v2"
