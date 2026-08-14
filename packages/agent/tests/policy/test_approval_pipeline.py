"""多级审批流水线 evaluate_permission 的单元测试。"""

from __future__ import annotations

import pytest

from harness_agent.policy.approval_policy import evaluate_permission
from harness_agent.policy.permission_rules import PermissionRule, extract_tool_resource


class TestDenyRuleOverridesMode:
    """deny 规则在任何模式下都不可被覆盖。"""

    def test_deny_rule_overrides_yolo_mode(self) -> None:
        rules = [PermissionRule(tool="execute", resource="rm *", effect="deny")]
        result = evaluate_permission(
            "execute", {"command": "rm -rf /"}, "yolo", rules=rules
        )
        assert result == "deny"


class TestReadOnlyTools:
    """只读工具在所有模式下免审批。"""

    def test_read_only_tools_always_allowed(self) -> None:
        for tool in ("ls", "read_file", "glob", "grep", "ask_user", "write_todos"):
            result = evaluate_permission(tool, {}, "default")
            assert result == "allow", f"{tool} should be allowed in default mode"


class TestSensitivePathSafety:
    """敏感路径写操作在非 yolo 模式下强制 ask，yolo 模式免疫。"""

    def test_sensitive_path_forces_ask_in_non_yolo_modes(self) -> None:
        for mode in ("default", "auto-edit", "auto"):
            result = evaluate_permission(
                "write_file", {"file_path": "/home/user/.git/config"}, mode
            )
            assert result == "ask", f"sensitive path should force ask in {mode} mode"

    def test_sensitive_path_immune_in_yolo(self) -> None:
        result = evaluate_permission(
            "write_file", {"file_path": "/home/user/.git/config"}, "yolo"
        )
        assert result == "allow"


class TestAllowRuleSkipsApproval:
    """allow 规则跳过审批直接执行。"""

    def test_allow_rule_skips_approval(self) -> None:
        rules = [PermissionRule(tool="execute", resource="npm test", effect="allow")]
        result = evaluate_permission(
            "execute", {"command": "npm test"}, "default", rules=rules
        )
        assert result == "allow"


class TestModePermissionTable:
    """审批模式查表行为验证。"""

    def test_mode_permission_table_default(self) -> None:
        # EDIT 工具在 default 模式下为 ask
        result = evaluate_permission("write_file", {"file_path": "/tmp/a.txt"}, "default")
        assert result == "ask"

    def test_mode_permission_table_auto_edit(self) -> None:
        # EDIT 工具在 auto-edit 模式下为 allow
        result = evaluate_permission(
            "edit_file", {"file_path": "/tmp/a.txt"}, "auto-edit"
        )
        assert result == "allow"
        # DELETE 工具在 auto-edit 模式下对普通文件为 allow
        result = evaluate_permission(
            "delete_file", {"file_path": "/tmp/a.txt"}, "auto-edit"
        )
        assert result == "allow"
        # DELETE 工具在 auto-edit 模式下对敏感路径仍为 ask
        result = evaluate_permission(
            "delete_file", {"file_path": "/home/user/.git/config"}, "auto-edit"
        )
        assert result == "ask"
        # EXECUTE 工具若是安全只读命令在 auto-edit 模式下自动放行（L3.1）
        result = evaluate_permission("execute", {"command": "ls"}, "auto-edit")
        assert result == "allow"
        result = evaluate_permission("execute", {"command": "git status"}, "auto-edit")
        assert result == "allow"
        # 非安全只读命令在 auto-edit 模式下仍为 ask
        result = evaluate_permission("execute", {"command": "npm install"}, "auto-edit")
        assert result == "ask"

    def test_safe_shell_commands_allowed_in_auto_mode(self) -> None:
        # EXECUTE 工具若是安全只读命令在 auto 模式下通过 L3.1 自动放行
        result = evaluate_permission("execute", {"command": "git status"}, "auto")
        assert result == "allow"
        result = evaluate_permission("execute", {"command": "ls"}, "auto")
        assert result == "allow"

    def test_mode_permission_table_yolo(self) -> None:
        # EXECUTE 工具在 yolo 模式下为 allow
        result = evaluate_permission("execute", {"command": "ls"}, "yolo")
        assert result == "allow"


class TestUnknownToolFailClosed:
    """未知工具 fail-closed 归为 EXECUTE 类别。"""

    def test_unknown_tool_fail_closed(self) -> None:
        # 未知工具在 default 模式下为 ask（EXECUTE 的 default 行为）
        result = evaluate_permission("some_unknown_tool", {}, "default")
        assert result == "ask"


class TestExtractResource:
    """资源提取辅助函数验证。"""

    def test_extract_resource_execute(self) -> None:
        assert extract_tool_resource("execute", {"command": "npm run build"}) == "npm run build"
        assert extract_tool_resource("monitor", {"command": "top"}) == "top"

    def test_extract_resource_file(self) -> None:
        assert extract_tool_resource("write_file", {"file_path": "/src/main.py"}) == "/src/main.py"
        assert extract_tool_resource("edit_file", {"file_path": "a.txt"}) == "a.txt"
        assert extract_tool_resource("delete_file", {}) == "*"

    def test_extract_resource_normalizes_backslash(self) -> None:
        assert extract_tool_resource("write_file", {"file_path": "src\\main.py"}) == "src/main.py"

    def test_extract_resource_web_fetch(self) -> None:
        assert (
            extract_tool_resource("web_fetch", {"url": "https://api.github.com/repos"})
            == "https://api.github.com/repos"
        )


class TestSafeCommandSegments:
    """L3.1 安全命令白名单对链式命令逐段判定。"""

    def test_all_safe_segments_auto_allowed(self) -> None:
        result = evaluate_permission(
            "execute", {"command": "git status && git diff"}, "default"
        )
        assert result == "allow"

    def test_dangerous_segment_blocks_fast_path(self) -> None:
        # 首段安全但次段危险时，不得借白名单整体放行
        result = evaluate_permission(
            "execute", {"command": "git status && rm -rf /"}, "default"
        )
        assert result == "ask"
