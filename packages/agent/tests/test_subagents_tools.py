"""工具作用域过滤的单元测试。"""
import pytest
from harness_agent.subagents import (
    ALL_TOOL_NAMES,
    SUBAGENT_EXCLUDED_TOOLS,
    filter_tools_for_agent,
)


def test_full_inheritance():
    """tools=None 时继承全部工具，但排除 SUBAGENT_EXCLUDED_TOOLS。"""
    result = filter_tools_for_agent()
    assert "task" not in result
    assert "ask_user" not in result
    assert "enter_plan_mode" not in result
    assert "exit_plan_mode" not in result
    assert "read_file" in result
    assert "execute" in result
    assert "write_file" in result


def test_whitelist():
    """tools 指定时仅保留列出的工具。"""
    result = filter_tools_for_agent(tools=["read_file", "grep", "glob"])
    assert result == ["glob", "grep", "read_file"]


def test_whitelist_still_excludes_task():
    """即使白名单中包含 task，也会被强制排除。"""
    result = filter_tools_for_agent(tools=["read_file", "task", "grep"])
    assert "task" not in result
    assert "read_file" in result
    assert "grep" in result


def test_blacklist():
    """disallowed_tools 从全量中移除指定工具。"""
    result = filter_tools_for_agent(disallowed_tools=["execute", "write_file", "delete_file"])
    assert "execute" not in result
    assert "write_file" not in result
    assert "delete_file" not in result
    assert "read_file" in result


def test_whitelist_and_blacklist():
    """白名单和黑名单同时使用时，先白后黑。"""
    result = filter_tools_for_agent(
        tools=["read_file", "grep", "execute", "write_file"],
        disallowed_tools=["execute"],
    )
    assert result == ["grep", "read_file", "write_file"]


def test_custom_tool_set():
    """支持自定义工具全集。"""
    custom = {"alpha", "beta", "gamma", "task"}
    result = filter_tools_for_agent(custom)
    assert result == ["alpha", "beta", "gamma"]
    assert "task" not in result


def test_result_sorted():
    """结果始终按字母序排列。"""
    result = filter_tools_for_agent(tools=["grep", "ls", "read_file", "glob"])
    assert result == sorted(result)
