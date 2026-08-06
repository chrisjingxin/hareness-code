"""AUTO 模式下危险 allow 规则识别与剥离的单元测试。

覆盖范围：
- DANGEROUS_ALLOW_PATTERNS 列表完整性（16 个模式）
- is_dangerous_allow_rule() 判定逻辑
- strip_dangerous_rules() 分离行为
- on_mode_entered() 集成：进入 AUTO 剥离、退出 AUTO 恢复
- 边界情况：deny/ask 规则不受影响、非 PermissionRule 对象透传
"""

from __future__ import annotations

import pytest

from harness_agent.policy.dangerous_rules import (
    DANGEROUS_ALLOW_PATTERNS,
    is_dangerous_allow_rule,
    strip_dangerous_rules,
)
from harness_agent.policy.permission_rules import PermissionRule


# ── DANGEROUS_ALLOW_PATTERNS 列表完整性 ──────────────────────────


class TestDangerousAllowPatterns:
    """验证危险模式列表包含预期的全部 16 个条目。"""

    EXPECTED_PATTERNS = [
        "*",
        "python",
        "node",
        "bash",
        "sh",
        "sudo",
        "ssh",
        "curl",
        "wget",
        "npm",
        "npx",
        "yarn",
        "pip",
        "pip3",
        "eval",
        "chmod",
        "chown",
    ]

    def test_pattern_count(self) -> None:
        """危险模式列表应包含 17 个条目（含全通配 *）。"""
        assert len(DANGEROUS_ALLOW_PATTERNS) == 17

    @pytest.mark.parametrize("pattern", EXPECTED_PATTERNS)
    def test_each_pattern_present(self, pattern: str) -> None:
        """每个预期模式都应存在于列表中。"""
        assert pattern in DANGEROUS_ALLOW_PATTERNS


# ── is_dangerous_allow_rule() 单元测试 ───────────────────────────


class TestIsDangerousAllowRule:
    """验证单条规则的 dangerous 判定逻辑。"""

    # -- 全通配 resource --

    def test_execute_star_is_dangerous(self) -> None:
        """execute + * 是最宽泛的 allow，必须判定为危险。"""
        assert is_dangerous_allow_rule("execute", "*") is True

    # -- 精确匹配危险命令 --

    @pytest.mark.parametrize(
        "command",
        ["python", "node", "bash", "sh", "sudo", "ssh", "curl", "wget",
         "npm", "npx", "yarn", "pip", "pip3", "eval", "chmod", "chown"],
    )
    def test_execute_exact_dangerous_command(self, command: str) -> None:
        """execute + 精确危险命令名应判定为危险。"""
        assert is_dangerous_allow_rule("execute", command) is True

    # -- 带参数的危险命令（前缀匹配） --

    @pytest.mark.parametrize(
        "resource",
        [
            "python *",
            "python script.py",
            "node server.js",
            "bash -c 'rm -rf /'",
            "sh deploy.sh",
            "sudo apt install",
            "ssh user@host",
            "curl https://example.com",
            "wget https://example.com/file",
            "npm install",
            "npx create-app",
            "yarn add lodash",
            "pip install requests",
            "pip3 install flask",
            "eval 'echo hello'",
            "chmod 777 file",
            "chown root:root file",
        ],
    )
    def test_execute_dangerous_command_with_args(self, resource: str) -> None:
        """execute + 危险命令带参数（前缀匹配）应判定为危险。"""
        assert is_dangerous_allow_rule("execute", resource) is True

    # -- 非 execute 工具不受影响 --

    def test_non_execute_tool_not_dangerous(self) -> None:
        """非 execute 工具即使 resource 为 * 也不判定为危险。"""
        assert is_dangerous_allow_rule("read_file", "*") is False

    def test_non_execute_tool_python(self) -> None:
        """非 execute 工具 + python 不判定为危险。"""
        assert is_dangerous_allow_rule("write_file", "python") is False

    # -- 安全的 execute 规则 --

    def test_execute_safe_command(self) -> None:
        """execute + 非危险命令不判定为危险。"""
        assert is_dangerous_allow_rule("execute", "git status") is False
        assert is_dangerous_allow_rule("execute", "ls -la") is False
        assert is_dangerous_allow_rule("execute", "cat README.md") is False

    def test_execute_safe_command_prefix_not_matching(self) -> None:
        """resource 以安全命令开头但恰好包含危险子串不应误判。

        例如 'pythonic' 不是 'python' 前缀匹配（需要空格分隔或精确匹配）。
        """
        # 'pythonic' 不等于 'python'，也不以 'python ' 开头
        assert is_dangerous_allow_rule("execute", "pythonic") is False

    def test_execute_pip_not_matching_pip3(self) -> None:
        """'pip3' 不应被 'pip' 模式误判（需要空格或精确匹配）。"""
        # pip3 本身是独立的危险模式，但这里验证 pip 不会误匹配 pip3xxx
        # pip3 作为精确匹配在 DANGEROUS_ALLOW_PATTERNS 中独立存在
        assert is_dangerous_allow_rule("execute", "pip3x") is False

    def test_execute_sh_not_matching_ssh(self) -> None:
        """'sh' 模式不应误匹配 'ssh'。"""
        # ssh 本身也是危险模式，但 sh 不应匹配 ssh
        # 由于 ssh 是独立的危险模式所以 ssh 仍为 True，
        # 但这里验证 sh 前缀不会匹配到 ssh 开头的命令
        assert is_dangerous_allow_rule("execute", "shred") is False


# ── strip_dangerous_rules() 单元测试 ─────────────────────────────


class TestStripDangerousRules:
    """验证规则列表的分离行为。"""

    def test_empty_rules(self) -> None:
        """空规则列表返回两个空列表。"""
        safe, stripped = strip_dangerous_rules([])
        assert safe == []
        assert stripped == []

    def test_all_safe_rules(self) -> None:
        """全部安全的规则不被剥离。"""
        rules = [
            PermissionRule(tool="execute", resource="git status", effect="allow"),
            PermissionRule(tool="read_file", resource="*", effect="allow"),
            PermissionRule(tool="execute", resource="ls -la", effect="allow"),
        ]
        safe, stripped = strip_dangerous_rules(rules)
        assert len(safe) == 3
        assert len(stripped) == 0

    def test_all_dangerous_rules(self) -> None:
        """全部危险的 allow 规则都被剥离。"""
        rules = [
            PermissionRule(tool="execute", resource="*", effect="allow"),
            PermissionRule(tool="execute", resource="python", effect="allow"),
            PermissionRule(tool="execute", resource="bash -c rm", effect="allow"),
        ]
        safe, stripped = strip_dangerous_rules(rules)
        assert len(safe) == 0
        assert len(stripped) == 3

    def test_mixed_rules(self) -> None:
        """混合规则中只剥离危险的 allow 规则。"""
        rules = [
            PermissionRule(tool="execute", resource="git status", effect="allow"),
            PermissionRule(tool="execute", resource="python", effect="allow"),
            PermissionRule(tool="execute", resource="ls", effect="allow"),
            PermissionRule(tool="execute", resource="sudo rm", effect="allow"),
        ]
        safe, stripped = strip_dangerous_rules(rules)
        assert len(safe) == 2
        assert safe[0].resource == "git status"
        assert safe[1].resource == "ls"
        assert len(stripped) == 2
        assert stripped[0].resource == "python"
        assert stripped[1].resource == "sudo rm"

    def test_deny_rules_not_stripped(self) -> None:
        """deny 规则即使 resource 匹配危险模式也不被剥离。"""
        rules = [
            PermissionRule(tool="execute", resource="python", effect="deny"),
            PermissionRule(tool="execute", resource="*", effect="deny"),
            PermissionRule(tool="execute", resource="bash", effect="deny"),
        ]
        safe, stripped = strip_dangerous_rules(rules)
        assert len(safe) == 3
        assert len(stripped) == 0

    def test_ask_rules_not_stripped(self) -> None:
        """ask 规则即使 resource 匹配危险模式也不被剥离。"""
        rules = [
            PermissionRule(tool="execute", resource="python", effect="ask"),
            PermissionRule(tool="execute", resource="*", effect="ask"),
            PermissionRule(tool="execute", resource="sudo", effect="ask"),
        ]
        safe, stripped = strip_dangerous_rules(rules)
        assert len(safe) == 3
        assert len(stripped) == 0

    def test_non_permission_rule_passthrough(self) -> None:
        """非 PermissionRule 对象直接归入安全列表，不做判定。"""
        rules = [
            "not_a_rule",
            42,
            None,
            PermissionRule(tool="execute", resource="python", effect="allow"),
        ]
        safe, stripped = strip_dangerous_rules(rules)
        # 非 PermissionRule 对象全部进入 safe
        assert len(safe) == 3
        assert safe[0] == "not_a_rule"
        assert safe[1] == 42
        assert safe[2] is None
        # 危险的 PermissionRule 被剥离
        assert len(stripped) == 1

    def test_preserves_rule_order(self) -> None:
        """安全规则保持原有顺序。"""
        rules = [
            PermissionRule(tool="execute", resource="git status", effect="allow"),
            PermissionRule(tool="execute", resource="python", effect="allow"),
            PermissionRule(tool="execute", resource="ls", effect="allow"),
            PermissionRule(tool="execute", resource="node", effect="allow"),
            PermissionRule(tool="execute", resource="cat file", effect="allow"),
        ]
        safe, stripped = strip_dangerous_rules(rules)
        assert [r.resource for r in safe] == ["git status", "ls", "cat file"]
        assert [r.resource for r in stripped] == ["python", "node"]

    def test_stripped_rules_preserve_full_object(self) -> None:
        """被剥离的规则保留完整的 PermissionRule 属性。"""
        rule = PermissionRule(
            tool="execute", resource="python *", effect="allow", scope="project"
        )
        _safe, stripped = strip_dangerous_rules([rule])
        assert len(stripped) == 1
        assert stripped[0].tool == "execute"
        assert stripped[0].resource == "python *"
        assert stripped[0].effect == "allow"
        assert stripped[0].scope == "project"


# ── on_mode_entered() 集成测试 ───────────────────────────────────


class TestOnModeEnteredIntegration:
    """验证 approval_mode.on_mode_entered() 与危险规则剥离的集成行为。"""

    def _make_rules(self) -> list[PermissionRule]:
        """构造一组测试用规则。"""
        return [
            PermissionRule(tool="execute", resource="git status", effect="allow"),
            PermissionRule(tool="execute", resource="python", effect="allow"),
            PermissionRule(tool="execute", resource="*", effect="allow"),
            PermissionRule(tool="execute", resource="ls", effect="allow"),
            PermissionRule(tool="execute", resource="sudo rm", effect="allow"),
        ]

    def test_enter_auto_strips_dangerous_rules(self) -> None:
        """进入 AUTO 模式时剥离危险 allow 规则。"""
        from harness_agent.policy.approval_mode import on_mode_entered, _dangerous_rules_stash

        # 先清空 stash 状态
        import harness_agent.policy.approval_mode as am
        am._dangerous_rules_stash.clear()

        rules = self._make_rules()
        result = on_mode_entered("auto", rules)

        # 只保留安全的规则
        assert len(result) == 2
        assert result[0].resource == "git status"
        assert result[1].resource == "ls"
        # stash 中暂存了被剥离的规则
        assert len(am._dangerous_rules_stash) == 3

        # 清理
        am._dangerous_rules_stash.clear()

    def test_exit_auto_restores_stripped_rules(self) -> None:
        """退出 AUTO 模式时恢复之前被剥离的规则。"""
        import harness_agent.policy.approval_mode as am

        am._dangerous_rules_stash.clear()

        rules = self._make_rules()

        # 先进入 AUTO 剥离
        safe_rules = am.on_mode_entered("auto", rules)
        assert len(safe_rules) == 2
        assert len(am._dangerous_rules_stash) == 3

        # 退出 AUTO（切换到 default），传入当前安全规则
        restored = am.on_mode_entered("default", safe_rules)
        # 恢复后应包含安全规则 + 之前被剥离的规则
        assert len(restored) == 5
        # stash 已清空
        assert len(am._dangerous_rules_stash) == 0

    def test_entering_auto_twice_replaces_stash(self) -> None:
        """连续两次进入 AUTO 模式，第二次替换 stash。"""
        import harness_agent.policy.approval_mode as am

        am._dangerous_rules_stash.clear()

        rules1 = [
            PermissionRule(tool="execute", resource="python", effect="allow"),
            PermissionRule(tool="execute", resource="git status", effect="allow"),
        ]
        am.on_mode_entered("auto", rules1)
        assert len(am._dangerous_rules_stash) == 1

        # 第二次进入 AUTO，使用不同的规则集
        rules2 = [
            PermissionRule(tool="execute", resource="bash", effect="allow"),
            PermissionRule(tool="execute", resource="node", effect="allow"),
            PermissionRule(tool="execute", resource="ls", effect="allow"),
        ]
        am.on_mode_entered("auto", rules2)
        # stash 被替换为第二次的剥离结果
        assert len(am._dangerous_rules_stash) == 2
        assert am._dangerous_rules_stash[0].resource == "bash"
        assert am._dangerous_rules_stash[1].resource == "node"

        am._dangerous_rules_stash.clear()

    def test_exit_auto_without_stash_is_noop(self) -> None:
        """退出 AUTO 但 stash 为空时，规则列表不变。"""
        import harness_agent.policy.approval_mode as am

        am._dangerous_rules_stash.clear()

        rules = [
            PermissionRule(tool="execute", resource="git status", effect="allow"),
        ]
        result = am.on_mode_entered("default", rules)
        assert result is rules  # 原样返回

    def test_non_auto_mode_without_stash_is_noop(self) -> None:
        """非 AUTO 模式且 stash 为空时，规则列表不变。"""
        import harness_agent.policy.approval_mode as am

        am._dangerous_rules_stash.clear()

        rules = [
            PermissionRule(tool="execute", resource="python", effect="allow"),
        ]
        result = am.on_mode_entered("plan", rules)
        assert result is rules

        result = am.on_mode_entered("yolo", rules)
        assert result is rules

    def test_deny_and_ask_rules_survive_auto_mode(self) -> None:
        """deny/ask 规则在 AUTO 模式切换中不受影响。"""
        import harness_agent.policy.approval_mode as am

        am._dangerous_rules_stash.clear()

        rules = [
            PermissionRule(tool="execute", resource="python", effect="deny"),
            PermissionRule(tool="execute", resource="bash", effect="ask"),
            PermissionRule(tool="execute", resource="sudo", effect="deny"),
        ]
        safe = am.on_mode_entered("auto", rules)
        # deny/ask 规则全部保留
        assert len(safe) == 3
        # stash 为空（没有 allow 规则被剥离）
        assert len(am._dangerous_rules_stash) == 0

        am._dangerous_rules_stash.clear()
