"""审批模式策略矩阵：确保配置语义不会分散在 Agent、TUI 或测试中。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from harness_agent.policy.approval_policy import (
    AutoDestructiveGuardMiddleware,
    DenyRulesMiddleware,
    PlanModeMiddleware,
    approval_mode_prompt,
    interrupt_on_for_approval_mode,
)
from harness_agent.policy.permission_rules import PermissionRule

_ALL_HITL_TOOLS = {
    "execute",
    "write_file",
    "edit_file",
    "delete",
    "delete_file",
    "task",
    "web_fetch",
    "apply_patch",
    "monitor",
    "task_stop",
}


def test_hitl_mapping_keeps_compaction_outside_all_approval_modes():
    """默认和自动编辑只拦截真实外部副作用，压缩始终由内核自动维护。"""
    default = interrupt_on_for_approval_mode("default")
    auto_edit = interrupt_on_for_approval_mode("auto-edit")
    auto = interrupt_on_for_approval_mode("auto")

    assert default is not None
    assert set(default) == _ALL_HITL_TOOLS
    assert auto_edit is not None
    # auto-edit 需要拦截编辑类工具：敏感路径编辑由预检弹窗确认，
    # 工作区内非敏感编辑由预检自动放行，不会真正产生审批。
    assert set(auto_edit) == _ALL_HITL_TOOLS
    assert auto is not None
    # auto 模式集合与 default 相同：编辑类工具需要经过四层过滤器判断。
    assert set(auto) == _ALL_HITL_TOOLS
    assert interrupt_on_for_approval_mode("plan") is None
    assert interrupt_on_for_approval_mode("yolo") is None
    assert "compact_conversation" not in default
    assert "compact_conversation" not in auto_edit


@pytest.mark.parametrize(
    "tool_name",
    ["ls", "read_file", "glob", "grep", "ask_user", "write_todos", "web_fetch", "task_output"],
)
def test_plan_mode_allows_only_explicit_read_and_thread_tools(tool_name: str):
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
    ["write_file", "edit_file", "execute", "delete", "task", "mcp_future_tool"],
)
async def test_plan_mode_rejects_mutation_and_unknown_future_tools(tool_name: str):
    """计划模式必须在执行前短路写入、shell、子 Agent 和未来 MCP。"""
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


def test_extra_interrupt_tools_merged_in_default_and_auto_edit():
    """MCP 等外部工具在 default 和 auto-edit 下纳入审批，plan/yolo 忽略。"""
    mcp_tools = frozenset({"mcp_github__create_issue", "mcp_gitee__search"})

    default = interrupt_on_for_approval_mode("default", extra_interrupt_tools=mcp_tools)
    assert default is not None
    assert set(default) == _ALL_HITL_TOOLS | mcp_tools

    auto_edit = interrupt_on_for_approval_mode("auto-edit", extra_interrupt_tools=mcp_tools)
    assert auto_edit is not None
    assert set(auto_edit) == _ALL_HITL_TOOLS | mcp_tools

    auto = interrupt_on_for_approval_mode("auto", extra_interrupt_tools=mcp_tools)
    assert auto is not None
    assert set(auto) == _ALL_HITL_TOOLS | mcp_tools

    # plan 和 yolo 即使传入额外工具也不产生拦截配置
    assert interrupt_on_for_approval_mode("plan", extra_interrupt_tools=mcp_tools) is None
    assert interrupt_on_for_approval_mode("yolo", extra_interrupt_tools=mcp_tools) is None


def test_extra_interrupt_tools_none_keeps_original_set():
    """不传 extra_interrupt_tools 时行为与原有完全一致。"""
    default = interrupt_on_for_approval_mode("default", extra_interrupt_tools=None)
    assert default is not None
    assert set(default) == _ALL_HITL_TOOLS


def test_approval_mode_prompts_state_the_actual_enforced_policy():
    """提示词只解释已由中间件执行的事实，不能成为唯一安全机制。"""
    assert "严格计划模式" in approval_mode_prompt("plan")
    assert "自动执行" in approval_mode_prompt("auto-edit")
    assert "不会为工具调用请求人工审批" in approval_mode_prompt("yolo")
    assert "需要用户确认" in approval_mode_prompt("default")


# ---------------------------------------------------------------------------
# HITL 组合预检行为
# ---------------------------------------------------------------------------


def _make_request(tool_name: str, args: dict[str, Any]) -> SimpleNamespace:
    """构造与 HITL 上下文一致的伪 ToolCallRequest。"""
    return SimpleNamespace(tool_call={"name": tool_name, "id": f"call-{tool_name}", "args": args})


def _make_preflight(
    workspace: Path,
    mode: str,
    rules: list[PermissionRule] | None = None,
    original: Callable[[Any], bool] | None = None,
) -> Callable[[Any], bool]:
    """构造指定审批模式和规则集合下的组合预检。"""
    from harness_agent.runtime.agent import _make_approval_preflight

    preflight = _make_approval_preflight(mode, original, lambda: rules or [], str(workspace))
    assert preflight is not None
    return preflight


class TestApprovalPreflight:
    """组合预检按设计顺序裁决弹窗与否（True 弹窗、False 自动执行）。"""

    def test_default_deny_rule_skips_dialog(self, tmp_path: Path):
        """default 模式 + deny 规则命中：不弹窗，由执行层硬拒绝。"""
        preflight = _make_preflight(
            tmp_path,
            "default",
            rules=[PermissionRule(tool="execute", resource="rm *", effect="deny")],
        )
        request = _make_request("execute", {"command": "rm -rf /"})
        assert preflight(request) is False

    def test_default_allow_rule_skips_dialog_for_non_sensitive(self, tmp_path: Path):
        """default 模式 + allow 规则命中非敏感操作：跳过审批。"""
        preflight = _make_preflight(
            tmp_path,
            "default",
            rules=[PermissionRule(tool="execute", resource="npm test", effect="allow")],
        )
        request = _make_request("execute", {"command": "npm test"})
        assert preflight(request) is False

    def test_default_allow_rule_still_asks_for_sensitive_path(self, tmp_path: Path):
        """default 模式 + allow 规则命中敏感路径：仍必须弹窗确认。"""
        preflight = _make_preflight(
            tmp_path,
            "default",
            rules=[PermissionRule(tool="edit_file", resource="*", effect="allow")],
        )
        request = _make_request(
            "edit_file", {"file_path": str(tmp_path / ".git" / "config")}
        )
        assert preflight(request) is True

    def test_default_unmatched_execute_asks(self, tmp_path: Path):
        """default 模式 + 无规则命中 execute：进入 HITL 集合默认弹窗。"""
        preflight = _make_preflight(tmp_path, "default")
        request = _make_request("execute", {"command": "ls"})
        assert preflight(request) is True

    def test_default_ask_rule_forces_dialog(self, tmp_path: Path):
        """default 模式 + ask 规则命中：弹窗。"""
        preflight = _make_preflight(
            tmp_path,
            "default",
            rules=[PermissionRule(tool="execute", resource="npm *", effect="ask")],
        )
        request = _make_request("execute", {"command": "npm install"})
        assert preflight(request) is True

    def test_auto_edit_in_workspace_edit_runs_without_dialog(self, tmp_path: Path):
        """auto-edit 模式 + 无规则 + 工作区内非敏感编辑：自动执行。"""
        preflight = _make_preflight(tmp_path, "auto-edit")
        request = _make_request(
            "edit_file", {"file_path": str(tmp_path / "src" / "main.py")}
        )
        assert preflight(request) is False

    def test_auto_edit_sensitive_edit_still_asks(self, tmp_path: Path):
        """auto-edit 模式 + 敏感路径编辑：弹窗确认。"""
        preflight = _make_preflight(tmp_path, "auto-edit")
        request = _make_request(
            "edit_file", {"file_path": str(tmp_path / ".git" / "config")}
        )
        assert preflight(request) is True

    def test_auto_in_workspace_edit_allowed_by_f1(self, tmp_path: Path):
        """auto 模式 + 工作区内非敏感编辑：F1 快速通道自动放行。"""
        preflight = _make_preflight(tmp_path, "auto")
        request = _make_request(
            "edit_file", {"file_path": str(tmp_path / "src" / "main.py")}
        )
        assert preflight(request) is False

    def test_auto_ordinary_command_falls_back_to_dialog(self, tmp_path: Path):
        """auto 模式 + execute 普通命令：F4 回退弹窗人工审批。"""
        preflight = _make_preflight(tmp_path, "auto")
        request = _make_request("execute", {"command": "ls"})
        assert preflight(request) is True

    def test_auto_destructive_command_skips_dialog(self, tmp_path: Path):
        """auto 模式 + 破坏性命令：不弹窗，由执行层守卫硬拒绝。"""
        preflight = _make_preflight(tmp_path, "auto")
        request = _make_request("execute", {"command": "rm -rf /"})
        assert preflight(request) is False

    def test_boundary_rejection_skips_dialog(self, tmp_path: Path):
        """边界预检拒绝（越界）时不产生假审批。"""
        preflight = _make_preflight(tmp_path, "default", original=lambda _request: False)
        request = _make_request("write_file", {"file_path": "/outside.md"})
        assert preflight(request) is False


# ---------------------------------------------------------------------------
# AutoDestructiveGuardMiddleware
# ---------------------------------------------------------------------------


class TestAutoDestructiveGuardMiddleware:
    """AUTO 模式 F3 破坏性命令在执行层兜底硬拒绝。"""

    def test_destructive_command_rejected_without_handler(self):
        """无规则时破坏性命令返回错误消息，且不调用底层 handler。"""
        middleware = AutoDestructiveGuardMiddleware(None, None)
        request = _make_request("execute", {"command": "rm -rf /"})
        called = False

        def handler(_request: object) -> object:
            nonlocal called
            called = True
            return object()

        result = middleware.wrap_tool_call(request, handler)

        assert called is False
        assert result.status == "error"
        assert "AUTO 模式拒绝 execute" in str(result.content)

    def test_allow_rule_beats_auto_filter(self):
        """allow 规则命中时优先于 AUTO 过滤器，直接放行到 handler。"""
        rules = [PermissionRule(tool="execute", resource="rm *", effect="allow")]
        middleware = AutoDestructiveGuardMiddleware(lambda: rules, None)
        request = _make_request("execute", {"command": "rm -rf /"})
        called = False

        def handler(_request: object) -> object:
            nonlocal called
            called = True
            return object()

        result = middleware.wrap_tool_call(request, handler)

        assert called is True
        assert result is not None

    def test_ordinary_command_passes(self):
        """普通命令经四层过滤器回退 ask 时不在执行层硬拒绝。"""
        middleware = AutoDestructiveGuardMiddleware(None, None)
        request = _make_request("execute", {"command": "ls"})
        called = False

        def handler(_request: object) -> object:
            nonlocal called
            called = True
            return object()

        middleware.wrap_tool_call(request, handler)

        assert called is True

    async def test_async_destructive_command_rejected(self):
        """异步路径同样硬拒绝破坏性命令且不调用 handler。"""
        middleware = AutoDestructiveGuardMiddleware(None, None)
        request = _make_request("execute", {"command": "rm -rf /"})
        called = False

        async def handler(_request: object) -> object:
            nonlocal called
            called = True
            return object()

        result = await middleware.awrap_tool_call(request, handler)

        assert called is False
        assert result.status == "error"
        assert "AUTO 模式拒绝 execute" in str(result.content)


# ---------------------------------------------------------------------------
# deny 审计日志
# ---------------------------------------------------------------------------


class TestDenyAuditLog:
    """静默硬拒绝必须留痕：deny 规则和 AUTO F3 拒绝都记录审计日志。"""

    def test_deny_rule_rejection_writes_audit_log(self, caplog: pytest.LogCaptureFixture):
        """deny 规则命中时硬拒绝并记录 source=rule 审计日志。"""
        rules = [PermissionRule(tool="execute", resource="rm *", effect="deny")]
        middleware = DenyRulesMiddleware(lambda: rules)
        request = _make_request("execute", {"command": "rm -rf /"})
        called = False

        def handler(_request: object) -> object:
            nonlocal called
            called = True
            return object()

        with caplog.at_level("INFO", logger="harness_agent.policy.approval_policy"):
            result = middleware.wrap_tool_call(request, handler)

        assert called is False
        assert result.status == "error"
        assert any(
            "approval_deny" in record.message and "source=rule" in record.message
            for record in caplog.records
        )

    def test_deny_rule_miss_writes_no_audit_log(self, caplog: pytest.LogCaptureFixture):
        """未命中 deny 规则时放行且不产生审计日志。"""
        rules = [PermissionRule(tool="execute", resource="npm *", effect="deny")]
        middleware = DenyRulesMiddleware(lambda: rules)
        request = _make_request("execute", {"command": "git status"})
        called = False

        def handler(_request: object) -> object:
            nonlocal called
            called = True
            return object()

        with caplog.at_level("INFO", logger="harness_agent.policy.approval_policy"):
            middleware.wrap_tool_call(request, handler)

        assert called is True
        assert not any("approval_deny" in record.message for record in caplog.records)

    def test_auto_guard_rejection_writes_audit_log(self, caplog: pytest.LogCaptureFixture):
        """AUTO F3 静默拒绝破坏性命令时记录 source=auto_destructive_guard 审计日志。"""
        middleware = AutoDestructiveGuardMiddleware(None, None)
        request = _make_request("execute", {"command": "git push --force"})

        def handler(_request: object) -> object:
            return object()

        with caplog.at_level("INFO", logger="harness_agent.policy.approval_policy"):
            result = middleware.wrap_tool_call(request, handler)

        assert result.status == "error"
        assert any(
            "approval_deny" in record.message
            and "source=auto_destructive_guard" in record.message
            for record in caplog.records
        )
