"""tools_mode 与 tools_memory 模块的单元测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harness_agent.tools.tools_mode import (
    enter_plan_mode,
    exit_plan_mode,
    format_plan_decision,
    plan_decision_tool_message,
    request_plan_entry,
)
from harness_agent.runtime.run_context import RunPlanConstraint
from harness_agent.tools.tools_memory import _sanitize_key, memory_save, memory_search


# ---------- plan mode tools ----------


def test_enter_plan_mode_directs_user_to_slash_command():
    result = enter_plan_mode()
    assert result["success"] is False
    assert "/plan" in result["error"]
    assert "Shift+Tab" in result["error"]


def test_exit_plan_mode_directs_user_to_slash_command():
    result = exit_plan_mode()
    assert result["success"] is False
    assert "当前不在计划模式" in result["error"]


def test_format_plan_decision_terminal_and_revise():
    assert format_plan_decision("approved") == {"message": "已批准", "terminal": True}
    assert format_plan_decision("abandoned") == {"message": "已放弃", "terminal": True}
    assert format_plan_decision("revise", "")["message"] == "用户要求继续打磨计划"
    assert format_plan_decision("revise", "")["terminal"] is False
    assert format_plan_decision("revise", "这段不要动")["message"] == "这段不要动"
    expired = format_plan_decision("abandoned", expired=True)
    assert expired["terminal"] is False
    assert expired.get("error") is True


def test_plan_decision_tool_message_status_is_success_or_error():
    """打回/批准/放弃必须写出合法 ToolMessage status，不能是 None。"""
    revise = plan_decision_tool_message("exit_plan_mode", "call-1", format_plan_decision("revise", "使用ts实现"))
    assert revise.status == "success"
    assert revise.content == "使用ts实现"
    approved = plan_decision_tool_message("exit_plan_mode", "call-2", format_plan_decision("approved"))
    assert approved.status == "success"
    expired = plan_decision_tool_message("exit_plan_mode", "call-3", format_plan_decision("abandoned", expired=True))
    assert expired.status == "error"


def test_request_plan_entry_approves_and_activates_same_run(monkeypatch):
    """用户本次允许后先种子计划文件，再打开同一 Run 的计划约束。"""
    constraint = RunPlanConstraint()
    context = SimpleNamespace(
        thread_id="thread-plan-entry",
        approval_mode="default",
        plan_constraint=constraint,
    )
    request = SimpleNamespace(
        tool_call={"name": "enter_plan_mode", "id": "call-enter", "args": {}},
        runtime=SimpleNamespace(context=context),
    )
    seeded: list[str] = []
    monkeypatch.setattr(
        "harness_agent.tools.tools_mode.require_run_context", lambda _runtime: context
    )
    monkeypatch.setattr(
        "harness_agent.tools.tools_mode.ensure_plan_file", lambda thread_id: seeded.append(thread_id)
    )
    monkeypatch.setattr(
        "langgraph.types.interrupt",
        lambda _payload: {"decisions": [{"type": "approve"}]},
    )

    result = request_plan_entry(request)

    assert result.status == "success"
    assert constraint.active is True
    assert seeded == ["thread-plan-entry"]
    assert "/.harness/plan.md" in str(result.content)


def test_request_plan_entry_rejection_keeps_run_unconstrained(monkeypatch):
    """用户拒绝时不种子文件、不改变当前 Run 权限。"""
    constraint = RunPlanConstraint()
    context = SimpleNamespace(
        thread_id="thread-plan-reject",
        approval_mode="default",
        plan_constraint=constraint,
    )
    request = SimpleNamespace(
        tool_call={"name": "enter_plan_mode", "id": "call-enter", "args": {}},
        runtime=SimpleNamespace(context=context),
    )
    seeded: list[str] = []
    monkeypatch.setattr(
        "harness_agent.tools.tools_mode.require_run_context", lambda _runtime: context
    )
    monkeypatch.setattr(
        "harness_agent.tools.tools_mode.ensure_plan_file", lambda thread_id: seeded.append(thread_id)
    )
    monkeypatch.setattr(
        "langgraph.types.interrupt",
        lambda _payload: {"decisions": [{"type": "reject"}]},
    )

    result = request_plan_entry(request)

    assert result.status == "error"
    assert constraint.active is False
    assert seeded == []


def test_request_plan_entry_without_run_context_fails_closed():
    """无交互 Run Context 时不能靠模型调用静默进入计划约束。"""
    request = SimpleNamespace(
        tool_call={"name": "enter_plan_mode", "id": "call-enter", "args": {}},
        runtime=None,
    )

    result = request_plan_entry(request)

    assert result.status == "error"
    assert "不能请求进入计划模式" in str(result.content)


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
