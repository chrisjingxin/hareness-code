"""审批模式策略矩阵：确保配置语义不会分散在 Agent、TUI 或测试中。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harness_agent.approval_policy import (
    PlanModeMiddleware,
    approval_mode_prompt,
    interrupt_on_for_approval_mode,
)


def test_hitl_mapping_keeps_compaction_outside_all_approval_modes():
    """默认和自动编辑只拦截真实外部副作用，压缩始终由内核自动维护。"""
    default = interrupt_on_for_approval_mode("default")
    auto_edit = interrupt_on_for_approval_mode("auto-edit")

    assert default is not None
    assert set(default) == {"execute", "write_file", "edit_file", "delete", "task"}
    assert auto_edit is not None
    assert set(auto_edit) == {"execute", "delete", "task"}
    assert interrupt_on_for_approval_mode("plan") is None
    assert interrupt_on_for_approval_mode("yolo") is None
    assert "compact_conversation" not in default
    assert "compact_conversation" not in auto_edit


@pytest.mark.parametrize(
    "tool_name",
    ["ls", "read_file", "glob", "grep", "ask_user", "write_todos", "compact_conversation"],
)
def test_plan_mode_allows_only_explicit_read_and_session_tools(tool_name: str):
    """计划模式对白名单内工具放行，避免妨碍调查和上下文维护。"""
    middleware = PlanModeMiddleware()
    request = SimpleNamespace(tool_call={"name": tool_name, "id": f"call-{tool_name}", "args": {}})
    called = False

    def handler(_request: object) -> object:
        nonlocal called
        called = True
        return object()

    assert middleware.wrap_tool_call(request, handler) is not None
    assert called is True


@pytest.mark.parametrize(
    "tool_name",
    ["write_file", "edit_file", "execute", "delete", "task", "js_eval", "mcp_future_tool"],
)
async def test_plan_mode_rejects_mutation_and_unknown_future_tools(tool_name: str):
    """计划模式必须在执行前短路写入、shell、子 Agent、解释器和未来 MCP。"""
    middleware = PlanModeMiddleware()
    request = SimpleNamespace(tool_call={"name": tool_name, "id": f"call-{tool_name}", "args": {}})
    called = False

    async def handler(_request: object) -> object:
        nonlocal called
        called = True
        return object()

    result = await middleware.awrap_tool_call(request, handler)

    assert called is False
    assert result.status == "error"
    assert f"计划模式拒绝 {tool_name}" in str(result.content)


def test_approval_mode_prompts_state_the_actual_enforced_policy():
    """提示词只解释已由中间件执行的事实，不能成为唯一安全机制。"""
    assert "严格计划模式" in approval_mode_prompt("plan")
    assert "自动执行" in approval_mode_prompt("auto-edit")
    assert "不会为工具调用请求人工审批" in approval_mode_prompt("yolo")
    assert "需要用户确认" in approval_mode_prompt("default")
