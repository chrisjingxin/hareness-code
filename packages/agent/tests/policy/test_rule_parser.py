"""DSL 规则解析/序列化器的全面回归测试。

覆盖 ``parse_rule``、``serialize_rule``、``parse_rule_list`` 和 ``load_rules_from_dsl``
四个公开函数，以及转义括号、旧工具名别名、三种限定符模式等边界场景。
"""
from __future__ import annotations

import logging

import pytest

from harness_agent.policy.permission_rules import PermissionRule
from harness_agent.policy.rule_parser import (
    load_rules_from_dsl,
    parse_rule,
    parse_rule_list,
    serialize_rule,
)


# ---------------------------------------------------------------------------
# parse_rule — 基本解析
# ---------------------------------------------------------------------------


class TestParseRuleBasic:
    """parse_rule 基本行为：将 ToolName(RuleContent) DSL 解析为 PermissionRule。"""

    def test_parse_rule_tool_with_resource(self):
        """带资源限定的工具规则正确解析为内部工具名和资源。"""
        rule = parse_rule("Bash(git clone *)")
        assert rule == PermissionRule(
            tool="execute", resource="git clone *", effect="allow", scope="session"
        )

    def test_parse_rule_tool_only_defaults_to_wildcard(self):
        """仅写工具名时资源默认为通配符 *。"""
        rule = parse_rule("Read")
        assert rule == PermissionRule(
            tool="read_file", resource="*", effect="allow", scope="session"
        )

    def test_parse_rule_with_explicit_effect(self):
        """显式传入 effect 参数时正确设置。"""
        rule = parse_rule("Bash(rm *)", effect="deny")
        assert rule.effect == "deny"

    def test_parse_rule_with_explicit_scope(self):
        """显式传入 scope 参数时正确设置。"""
        rule = parse_rule("Edit", scope="project")
        assert rule.scope == "project"

    def test_parse_rule_empty_resource_in_parens_raises(self):
        """空括号无法匹配正则（.+ 要求至少一个字符），抛出 ValueError。"""
        with pytest.raises(ValueError, match="无法解析规则字符串"):
            parse_rule("Bash()")

    def test_parse_rule_resource_with_special_chars(self):
        """资源中包含特殊字符（星号、斜杠、冒号等）时原样保留。"""
        rule = parse_rule("Read(src/**/*.py)")
        assert rule.resource == "src/**/*.py"

    def test_parse_rule_strips_whitespace(self):
        """前后空白被自动去除。"""
        rule = parse_rule("  Bash(git *)  ")
        assert rule.tool == "execute"
        assert rule.resource == "git *"


# ---------------------------------------------------------------------------
# parse_rule — 全部 DSL 工具名映射
# ---------------------------------------------------------------------------


class TestParseRuleToolMapping:
    """验证所有 DSL 工具名到内部工具名的映射。"""

    @pytest.mark.parametrize(
        "dsl_name, internal_name",
        [
            ("Bash", "execute"),
            ("Read", "read_file"),
            ("Edit", "edit_file"),
            ("Write", "write_file"),
            ("Delete", "delete_file"),
            ("WebFetch", "web_fetch"),
            ("WebSearch", "web_search"),
            ("Grep", "grep"),
            ("Glob", "glob"),
            ("LS", "ls"),
            ("Agent", "task"),
            ("MCP", "mcp_tool"),
            ("NotebookRead", "read_file"),
            ("NotebookEdit", "edit_file"),
            ("ToolSearch", "tool_search"),
        ],
    )
    def test_parse_rule_dsl_to_internal_name(self, dsl_name, internal_name):
        """每个 DSL 工具名正确映射到内部工具名。"""
        rule = parse_rule(dsl_name)
        assert rule.tool == internal_name


# ---------------------------------------------------------------------------
# parse_rule — 旧工具名别名兼容
# ---------------------------------------------------------------------------


class TestParseRuleLegacyAliases:
    """过渡期旧工具名别名：Task→task、Write→write_file、KillShell→task_stop。"""

    def test_legacy_task_maps_to_task(self):
        """旧别名 Task 映射到内部 task（与 Agent 相同）。"""
        rule = parse_rule("Task(sub_agent_prompt)")
        assert rule.tool == "task"
        assert rule.resource == "sub_agent_prompt"

    def test_legacy_killshell_maps_to_task_stop(self):
        """旧别名 KillShell 映射到内部 task_stop。"""
        rule = parse_rule("KillShell")
        assert rule.tool == "task_stop"
        assert rule.resource == "*"

    def test_legacy_write_maps_to_write_file(self):
        """Write 在映射表中已规范化为 write_file（非旧别名，但确认映射正确）。"""
        rule = parse_rule("Write(/tmp/output.txt)")
        assert rule.tool == "write_file"
        assert rule.resource == "/tmp/output.txt"


# ---------------------------------------------------------------------------
# parse_rule — 转义括号
# ---------------------------------------------------------------------------


class TestParseRuleEscapedParens:
    """资源内容中转义括号 \\( 和 \\) 被还原为字面括号。"""

    def test_escaped_opening_paren(self):
        """\\( 还原为 (；正则贪婪匹配最后一个 ) 作为分隔符。"""
        # 输入: Bash(echo \(hello)
        # 正则捕获组: echo \(hello（最后一个 ) 作为分隔符）
        # 反转义后: echo (hello
        rule = parse_rule(r"Bash(echo \(hello)")
        assert rule.resource == "echo (hello"

    def test_escaped_closing_paren(self):
        """\\) 还原为 )。"""
        rule = parse_rule(r"Bash(echo hello\))")
        assert rule.resource == "echo hello)"

    def test_both_escaped_parens(self):
        """同时包含 \\( 和 \\) 时均被还原。"""
        rule = parse_rule(r"Edit(func\(\))")
        assert rule.resource == "func()"

    def test_no_escape_regular_parens_as_delimiters(self):
        """未转义的外层括号作为 DSL 分隔符，不出现在资源内容中。"""
        rule = parse_rule("Bash(git status)")
        assert rule.resource == "git status"
        assert "(" not in rule.resource
        assert ")" not in rule.resource


# ---------------------------------------------------------------------------
# parse_rule — 三种限定符模式
# ---------------------------------------------------------------------------


class TestParseRuleSpecifierModes:
    """三种典型限定符模式：命令前缀（Bash）、路径通配（Edit/Read/Write）、域名（WebFetch）。"""

    def test_command_prefix_mode(self):
        """Bash 使用命令前缀匹配，资源为 shell 命令模式。"""
        rule = parse_rule("Bash(git clone *)")
        assert rule.tool == "execute"
        assert rule.resource == "git clone *"

    def test_path_glob_mode_edit(self):
        """Edit 使用路径通配模式。"""
        rule = parse_rule("Edit(src/**/*.py)")
        assert rule.tool == "edit_file"
        assert rule.resource == "src/**/*.py"

    def test_path_glob_mode_read(self):
        """Read 使用路径通配模式。"""
        rule = parse_rule("Read(/etc/*.conf)")
        assert rule.tool == "read_file"
        assert rule.resource == "/etc/*.conf"

    def test_path_glob_mode_write(self):
        """Write 使用路径通配模式。"""
        rule = parse_rule("Write(/tmp/output_*.txt)")
        assert rule.tool == "write_file"
        assert rule.resource == "/tmp/output_*.txt"

    def test_domain_specifier_mode(self):
        """WebFetch 使用 domain: 限定符。"""
        rule = parse_rule("WebFetch(domain:github.com)")
        assert rule.tool == "web_fetch"
        assert rule.resource == "domain:github.com"

    def test_domain_specifier_with_subdomain(self):
        """WebFetch 域名限定支持子域名。"""
        rule = parse_rule("WebFetch(domain:api.example.com)")
        assert rule.resource == "domain:api.example.com"


# ---------------------------------------------------------------------------
# parse_rule — 错误处理
# ---------------------------------------------------------------------------


class TestParseRuleErrors:
    """parse_rule 对非法输入的异常处理。"""

    def test_empty_string_raises(self):
        """空字符串抛出 ValueError。"""
        with pytest.raises(ValueError, match="无效的规则字符串"):
            parse_rule("")

    def test_whitespace_only_raises(self):
        """纯空白字符串抛出 ValueError。"""
        with pytest.raises(ValueError, match="无效的规则字符串"):
            parse_rule("   ")

    def test_non_string_raises(self):
        """非字符串类型抛出 ValueError。"""
        with pytest.raises(ValueError, match="无效的规则字符串"):
            parse_rule(123)  # type: ignore[arg-type]

    def test_none_raises(self):
        """None 输入抛出 ValueError。"""
        with pytest.raises(ValueError, match="无效的规则字符串"):
            parse_rule(None)  # type: ignore[arg-type]

    def test_unknown_tool_name_passes_through(self):
        """未知 DSL 工具名（含 MCP 工具）原样保留，保证规则写入后可读回。"""
        rule = parse_rule("amap-maps_maps_weather(*)")
        assert rule.tool == "amap-maps_maps_weather"
        assert rule.resource == "*"

        rule = parse_rule("UnknownTool(something)")
        assert rule.tool == "UnknownTool"
        assert rule.resource == "something"

    def test_invalid_effect_raises(self):
        """非法 effect 值抛出 ValueError。"""
        with pytest.raises(ValueError, match="无效的 effect"):
            parse_rule("Bash(git *)", effect="block")

    def test_starts_with_digit_raises(self):
        """以数字开头的字符串不匹配正则，抛出 ValueError。"""
        with pytest.raises(ValueError, match="无法解析规则字符串"):
            parse_rule("123Tool(something)")


# ---------------------------------------------------------------------------
# serialize_rule — 基本序列化
# ---------------------------------------------------------------------------


class TestSerializeRuleBasic:
    """serialize_rule 将 PermissionRule 反向序列化为 DSL 字符串。"""

    def test_serialize_tool_with_resource(self):
        """带资源限定的规则序列化为 ToolName(resource) 格式。"""
        rule = PermissionRule(tool="execute", resource="git clone *", effect="allow")
        assert serialize_rule(rule) == "Bash(git clone *)"

    def test_serialize_wildcard_resource_omits_parens(self):
        """资源为 * 时仅输出工具名，不带括号。"""
        rule = PermissionRule(tool="write_file", resource="*", effect="deny")
        assert serialize_rule(rule) == "Write"

    def test_serialize_read_wildcard(self):
        """Read 通配规则序列化。"""
        rule = PermissionRule(tool="read_file", resource="*", effect="allow")
        assert serialize_rule(rule) == "Read"

    def test_serialize_edit_with_path(self):
        """Edit 带路径的序列化。"""
        rule = PermissionRule(tool="edit_file", resource="src/**/*.ts", effect="allow")
        assert serialize_rule(rule) == "Edit(src/**/*.ts)"

    def test_serialize_web_fetch_domain(self):
        """WebFetch 域名限定的序列化。"""
        rule = PermissionRule(tool="web_fetch", resource="domain:github.com", effect="allow")
        assert serialize_rule(rule) == "WebFetch(domain:github.com)"


# ---------------------------------------------------------------------------
# serialize_rule — 反向映射
# ---------------------------------------------------------------------------


class TestSerializeRuleReverseMapping:
    """验证内部工具名到 DSL 名的反向映射。"""

    @pytest.mark.parametrize(
        "internal_name, dsl_name",
        [
            ("execute", "Bash"),
            ("read_file", "Read"),
            ("edit_file", "Edit"),
            ("write_file", "Write"),
            ("delete_file", "Delete"),
            ("web_fetch", "WebFetch"),
            ("web_search", "WebSearch"),
            ("grep", "Grep"),
            ("glob", "Glob"),
            ("ls", "LS"),
            ("task", "Agent"),
            ("task_stop", "KillShell"),
            ("mcp_tool", "MCP"),
            ("tool_search", "ToolSearch"),
        ],
    )
    def test_serialize_internal_to_dsl_name(self, internal_name, dsl_name):
        """内部工具名正确映射回 DSL 名（资源为 * 时不带括号）。"""
        rule = PermissionRule(tool=internal_name, resource="*", effect="allow")
        assert serialize_rule(rule) == dsl_name

    def test_unknown_tool_name_passes_through(self):
        """未在映射表中的工具名直接透传。"""
        rule = PermissionRule(tool="custom_tool", resource="*", effect="allow")
        assert serialize_rule(rule) == "custom_tool"


# ---------------------------------------------------------------------------
# 往返一致性：parse_rule ↔ serialize_rule
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """解析和序列化的往返一致性。"""

    @pytest.mark.parametrize(
        "dsl",
        [
            "Bash(git clone *)",
            "Read",
            "Edit(src/**/*.py)",
            "Write",
            "WebFetch(domain:github.com)",
            "Delete",
            "Grep(pattern)",
            "Agent",
        ],
    )
    def test_parse_then_serialize_roundtrip(self, dsl):
        """parse_rule → serialize_rule 往返后 DSL 字符串一致。"""
        rule = parse_rule(dsl)
        assert serialize_rule(rule) == dsl

    def test_legacy_alias_serializes_to_canonical_name(self):
        """旧别名解析后序列化使用规范 DSL 名（Task → Agent）。"""
        rule = parse_rule("Task(prompt)")
        # Task 映射到内部 task，反向映射取规范名 Agent
        assert serialize_rule(rule) == "Agent(prompt)"


# ---------------------------------------------------------------------------
# parse_rule_list — JSON 和 DSL 混合输入
# ---------------------------------------------------------------------------


class TestParseRuleList:
    """parse_rule_list 兼容解析 JSON 字典和 DSL 字符串混合格式。"""

    def test_dsl_string_entries(self):
        """纯 DSL 字符串列表正确解析。"""
        rules = parse_rule_list(["Bash(git *)", "Read"])
        assert len(rules) == 2
        assert rules[0].tool == "execute"
        assert rules[0].resource == "git *"
        assert rules[1].tool == "read_file"
        assert rules[1].resource == "*"

    def test_json_dict_entries(self):
        """JSON 字典格式条目正确解析。"""
        rules = parse_rule_list([
            {"tool": "execute", "resource": "git *", "effect": "allow"},
            {"tool": "read_file", "resource": "*.py", "effect": "deny"},
        ])
        assert len(rules) == 2
        assert rules[0] == PermissionRule(
            tool="execute", resource="git *", effect="allow", scope="session"
        )
        assert rules[1] == PermissionRule(
            tool="read_file", resource="*.py", effect="deny", scope="session"
        )

    def test_mixed_dsl_and_json(self):
        """DSL 字符串和 JSON 字典混合列表正确解析。"""
        rules = parse_rule_list([
            "Bash(git *)",
            {"tool": "read_file", "resource": "*.py", "effect": "deny"},
        ])
        assert len(rules) == 2
        assert rules[0].tool == "execute"
        assert rules[1].tool == "read_file"
        assert rules[1].effect == "deny"

    def test_default_effect_applied_to_dsl_entries(self):
        """默认 effect 参数传递给 DSL 字符串条目。"""
        rules = parse_rule_list(["Bash(git *)"], effect="deny")
        assert rules[0].effect == "deny"

    def test_default_scope_applied_to_dsl_entries(self):
        """默认 scope 参数传递给 DSL 字符串条目。"""
        rules = parse_rule_list(["Bash(git *)"], scope="project")
        assert rules[0].scope == "project"

    def test_json_entry_overrides_default_effect(self):
        """JSON 字典条目中自带的 effect 覆盖默认值。"""
        rules = parse_rule_list(
            [{"tool": "execute", "resource": "*", "effect": "ask"}],
            effect="deny",
        )
        assert rules[0].effect == "ask"

    def test_json_entry_overrides_default_scope(self):
        """JSON 字典条目中自带的 scope 覆盖默认值。"""
        rules = parse_rule_list(
            [{"tool": "execute", "resource": "*", "effect": "allow", "scope": "user"}],
            scope="session",
        )
        assert rules[0].scope == "user"

    def test_empty_list_returns_empty(self):
        """空列表返回空结果。"""
        assert parse_rule_list([]) == []

    def test_non_list_input_returns_empty(self):
        """非列表输入返回空列表。"""
        assert parse_rule_list("not a list") == []  # type: ignore[arg-type]
        assert parse_rule_list(None) == []  # type: ignore[arg-type]
        assert parse_rule_list(42) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_rule_list — 无效条目跳过
# ---------------------------------------------------------------------------


class TestParseRuleListSkipInvalid:
    """parse_rule_list 跳过无效条目并记录 warning。"""

    def test_skip_invalid_dsl_string(self, caplog: pytest.LogCaptureFixture):
        """语法非法的 DSL 字符串被跳过；未知工具名原样保留不视为非法。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = parse_rule_list(["Bash(git *)", "123Tool(foo)", "UnknownTool(bar)", "Read"])
        assert len(rules) == 3
        assert rules[0].tool == "execute"
        assert rules[1].tool == "UnknownTool"
        assert rules[2].tool == "read_file"
        assert "123Tool" in caplog.text

    def test_skip_json_missing_tool(self, caplog: pytest.LogCaptureFixture):
        """JSON 字典缺少 tool 字段时被跳过。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = parse_rule_list([{"resource": "*", "effect": "allow"}])
        assert len(rules) == 0

    def test_skip_json_missing_resource(self, caplog: pytest.LogCaptureFixture):
        """JSON 字典缺少 resource 字段时被跳过。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = parse_rule_list([{"tool": "execute", "effect": "allow"}])
        assert len(rules) == 0

    def test_skip_json_invalid_effect(self, caplog: pytest.LogCaptureFixture):
        """JSON 字典 effect 值非法时被跳过。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = parse_rule_list([{"tool": "execute", "resource": "*", "effect": "block"}])
        assert len(rules) == 0

    def test_skip_json_wrong_type_tool(self, caplog: pytest.LogCaptureFixture):
        """JSON 字典 tool 字段类型非字符串时被跳过。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = parse_rule_list([{"tool": 123, "resource": "*", "effect": "allow"}])
        assert len(rules) == 0

    def test_skip_unsupported_type(self, caplog: pytest.LogCaptureFixture):
        """不支持的类型（如整数、布尔值）被跳过。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = parse_rule_list([42, True, "Read"])
        assert len(rules) == 1
        assert rules[0].tool == "read_file"


# ---------------------------------------------------------------------------
# load_rules_from_dsl — effect 前缀解析
# ---------------------------------------------------------------------------


class TestLoadRulesFromDsl:
    """load_rules_from_dsl 解析带 effect 前缀的 DSL 列表。"""

    def test_allow_prefix(self):
        """allow: 前缀正确解析。"""
        rules = load_rules_from_dsl(["allow:Bash(git *)"])
        assert len(rules) == 1
        assert rules[0].effect == "allow"
        assert rules[0].tool == "execute"

    def test_deny_prefix(self):
        """deny: 前缀正确解析。"""
        rules = load_rules_from_dsl(["deny:Bash(rm *)"])
        assert len(rules) == 1
        assert rules[0].effect == "deny"

    def test_ask_prefix(self):
        """ask: 前缀正确解析。"""
        rules = load_rules_from_dsl(["ask:Edit(src/*.py)"])
        assert len(rules) == 1
        assert rules[0].effect == "ask"

    def test_no_prefix_defaults_to_allow(self):
        """无前缀时默认 effect 为 allow。"""
        rules = load_rules_from_dsl(["WebFetch(domain:github.com)"])
        assert len(rules) == 1
        assert rules[0].effect == "allow"

    def test_default_scope_is_user(self):
        """默认 scope 为 user。"""
        rules = load_rules_from_dsl(["Bash(git *)"])
        assert rules[0].scope == "user"

    def test_custom_scope(self):
        """自定义 scope 参数生效。"""
        rules = load_rules_from_dsl(["Bash(git *)"], scope="project")
        assert rules[0].scope == "project"

    def test_multiple_entries(self):
        """多条 DSL 规则全部正确解析。"""
        rules = load_rules_from_dsl([
            "allow:Bash(git *)",
            "deny:Bash(rm *)",
            "WebFetch(domain:github.com)",
        ])
        assert len(rules) == 3
        assert rules[0].effect == "allow"
        assert rules[1].effect == "deny"
        assert rules[2].effect == "allow"

    def test_empty_list_returns_empty(self):
        """空列表返回空结果。"""
        assert load_rules_from_dsl([]) == []

    def test_non_list_returns_empty(self):
        """非列表输入返回空列表。"""
        assert load_rules_from_dsl("not a list") == []  # type: ignore[arg-type]

    def test_skip_empty_entries(self):
        """空白条目被跳过。"""
        rules = load_rules_from_dsl(["", "  ", "Bash(git *)"])
        assert len(rules) == 1

    def test_skip_invalid_entries(self, caplog: pytest.LogCaptureFixture):
        """语法非法条目被跳过并记录 warning；未知工具名条目正常解析。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = load_rules_from_dsl(["allow:123Tool(x)", "allow:UnknownTool(x)", "Bash(git *)"])
        assert len(rules) == 2
        assert rules[0].tool == "UnknownTool"
        assert rules[1].tool == "execute"

    def test_prefix_only_raises_and_skips(self, caplog: pytest.LogCaptureFixture):
        """仅含前缀无规则体时被跳过。"""
        with caplog.at_level(logging.WARNING, logger="harness_agent.policy.rule_parser"):
            rules = load_rules_from_dsl(["allow:", "deny:Bash(rm *)"])
        assert len(rules) == 1
        assert rules[0].effect == "deny"


# ---------------------------------------------------------------------------
# 综合场景
# ---------------------------------------------------------------------------


class TestIntegrationScenarios:
    """端到端综合场景验证。"""

    def test_typical_project_permissions(self):
        """模拟典型项目权限配置：允许 git、禁止 rm、域名限定。"""
        raw = [
            "allow:Bash(git *)",
            "deny:Bash(rm -rf *)",
            "allow:Read",
            "allow:Edit(src/**)",
            "allow:WebFetch(domain:github.com)",
        ]
        rules = load_rules_from_dsl(raw, scope="project")
        assert len(rules) == 5

        # 验证各规则属性
        assert rules[0] == PermissionRule(
            tool="execute", resource="git *", effect="allow", scope="project"
        )
        assert rules[1] == PermissionRule(
            tool="execute", resource="rm -rf *", effect="deny", scope="project"
        )
        assert rules[2] == PermissionRule(
            tool="read_file", resource="*", effect="allow", scope="project"
        )
        assert rules[3] == PermissionRule(
            tool="edit_file", resource="src/**", effect="allow", scope="project"
        )
        assert rules[4] == PermissionRule(
            tool="web_fetch", resource="domain:github.com", effect="allow", scope="project"
        )

    def test_parse_rule_list_then_serialize(self):
        """parse_rule_list 解析后逐条 serialize 验证一致性。"""
        raw = ["Bash(git *)", "Read", "Edit(src/**/*.py)"]
        rules = parse_rule_list(raw)
        serialized = [serialize_rule(r) for r in rules]
        assert serialized == ["Bash(git *)", "Read", "Edit(src/**/*.py)"]

    def test_mcp_tool_with_server_prefix(self):
        """MCP 工具名解析，资源含 server:tool 格式。"""
        rule = parse_rule("MCP(github:create_issue)")
        assert rule.tool == "mcp_tool"
        assert rule.resource == "github:create_issue"
        assert serialize_rule(rule) == "MCP(github:create_issue)"
