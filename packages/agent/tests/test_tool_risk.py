"""工具风险分级模块：验证工具分类、只读判定和审批模式权限矩阵。"""

from __future__ import annotations

import pytest

from harness_agent.tool_risk import (
    KIND_MODE_PERMISSION,
    TOOL_KIND_MAP,
    ToolKind,
    get_mode_permission,
    get_tool_kind,
    is_read_only,
)


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("ls", ToolKind.READ),
        ("read_file", ToolKind.READ),
        ("glob", ToolKind.READ),
        ("grep", ToolKind.READ),
        ("web_search", ToolKind.READ),
        ("lsp", ToolKind.READ),
        ("tool_search", ToolKind.READ),
        ("memory_search", ToolKind.READ),
        ("task_output", ToolKind.READ),
        ("write_file", ToolKind.EDIT),
        ("edit_file", ToolKind.EDIT),
        ("apply_patch", ToolKind.EDIT),
        ("delete_file", ToolKind.DELETE),
        ("execute", ToolKind.EXECUTE),
        ("monitor", ToolKind.EXECUTE),
        ("task_stop", ToolKind.EXECUTE),
        ("task", ToolKind.AGENT),
        ("ask_user", ToolKind.INTERACT),
        ("write_todos", ToolKind.INTERACT),
        ("memory_save", ToolKind.INTERACT),
        ("enter_plan_mode", ToolKind.PLAN),
        ("exit_plan_mode", ToolKind.PLAN),
        ("web_fetch", ToolKind.FETCH),
    ],
)
def test_known_tools_return_correct_kind(tool_name: str, expected: ToolKind):
    """所有已注册工具名应映射到预期的风险类别。"""
    assert get_tool_kind(tool_name) is expected


@pytest.mark.parametrize("tool_name", ["unknown_tool", "rm_rf", "custom_mcp", ""])
def test_unknown_tool_falls_back_to_execute(tool_name: str):
    """未知工具 fail-closed 归为 EXECUTE，确保不会意外放行。"""
    assert get_tool_kind(tool_name) is ToolKind.EXECUTE


@pytest.mark.parametrize(
    "tool_name",
    ["ls", "read_file", "glob", "grep", "ask_user", "write_todos", "enter_plan_mode"],
)
def test_read_only_tools_return_true(tool_name: str):
    """READ/INTERACT/PLAN 类别的工具应被判定为只读。"""
    assert is_read_only(tool_name) is True


@pytest.mark.parametrize(
    "tool_name",
    ["write_file", "edit_file", "delete_file", "execute", "task", "web_fetch"],
)
def test_non_read_only_tools_return_false(tool_name: str):
    """EDIT/DELETE/EXECUTE/AGENT/FETCH 类别的工具不是只读。"""
    assert is_read_only(tool_name) is False


@pytest.mark.parametrize(
    ("kind", "mode", "expected"),
    [
        # READ: 所有模式均 allow
        (ToolKind.READ, "plan", "allow"),
        (ToolKind.READ, "default", "allow"),
        (ToolKind.READ, "auto-edit", "allow"),
        (ToolKind.READ, "yolo", "allow"),
        # EDIT
        (ToolKind.EDIT, "plan", "deny"),
        (ToolKind.EDIT, "default", "ask"),
        (ToolKind.EDIT, "auto-edit", "allow"),
        (ToolKind.EDIT, "yolo", "allow"),
        # DELETE
        (ToolKind.DELETE, "plan", "deny"),
        (ToolKind.DELETE, "default", "ask"),
        (ToolKind.DELETE, "auto-edit", "ask"),
        (ToolKind.DELETE, "yolo", "allow"),
        # EXECUTE
        (ToolKind.EXECUTE, "plan", "deny"),
        (ToolKind.EXECUTE, "default", "ask"),
        (ToolKind.EXECUTE, "auto-edit", "ask"),
        (ToolKind.EXECUTE, "yolo", "allow"),
        # AGENT
        (ToolKind.AGENT, "plan", "deny"),
        (ToolKind.AGENT, "default", "ask"),
        (ToolKind.AGENT, "auto-edit", "ask"),
        (ToolKind.AGENT, "yolo", "allow"),
        # INTERACT: 所有模式均 allow
        (ToolKind.INTERACT, "plan", "allow"),
        (ToolKind.INTERACT, "default", "allow"),
        (ToolKind.INTERACT, "auto-edit", "allow"),
        (ToolKind.INTERACT, "yolo", "allow"),
        # PLAN: 所有模式均 allow
        (ToolKind.PLAN, "plan", "allow"),
        (ToolKind.PLAN, "default", "allow"),
        (ToolKind.PLAN, "auto-edit", "allow"),
        (ToolKind.PLAN, "yolo", "allow"),
        # FETCH
        (ToolKind.FETCH, "plan", "allow"),
        (ToolKind.FETCH, "default", "ask"),
        (ToolKind.FETCH, "auto-edit", "ask"),
        (ToolKind.FETCH, "yolo", "allow"),
    ],
)
def test_mode_permission_matrix(kind: ToolKind, mode: str, expected: str):
    """权限矩阵各组合应返回预期行为。"""
    assert get_mode_permission(kind, mode) == expected


def test_permission_matrix_covers_all_kinds_and_modes():
    """权限矩阵应覆盖全部 ToolKind 和四种审批模式。"""
    modes = {"plan", "default", "auto-edit", "yolo"}
    assert set(KIND_MODE_PERMISSION.keys()) == set(ToolKind)
    for kind, mode_map in KIND_MODE_PERMISSION.items():
        assert set(mode_map.keys()) == modes, f"{kind} 缺少模式覆盖"
        for mode, action in mode_map.items():
            assert action in {"allow", "ask", "deny"}, f"{kind}/{mode} 行为非法"


def test_tool_kind_map_covers_all_expected_tools():
    """TOOL_KIND_MAP 应包含全部 23 个已定义工具。"""
    assert len(TOOL_KIND_MAP) == 23
