"""Bash 命令规则匹配器与安全门评估引擎的回归测试。

覆盖四个公开函数：
- matches_command_prefix：词边界安全的命令前缀匹配
- matches_command_glob：自由 glob 模式匹配
- evaluate_bash_segment：单段命令权限评估
- evaluate_bash：完整链式命令安全门评估（含 CWE-178 防护）
"""
from __future__ import annotations

import pytest

from harness_agent.policy.bash_matcher import (
    evaluate_bash,
    evaluate_bash_segment,
    matches_command_glob,
    matches_command_prefix,
)
from harness_agent.policy.permission_rules import PermissionRule


# ---------------------------------------------------------------------------
# matches_command_prefix：词边界安全的命令前缀匹配
# ---------------------------------------------------------------------------


class TestMatchesCommandPrefix:
    """tests for matches_command_prefix"""

    # -- 基本匹配 --

    def test_exact_single_command(self):
        """单命令完全匹配。"""
        assert matches_command_prefix("git", "git") is True

    def test_prefix_matches_longer_command(self):
        """前缀匹配更长的命令（后跟空格，词边界安全）。"""
        assert matches_command_prefix("git", "git status") is True

    def test_prefix_does_not_match_different_command(self):
        """前缀不匹配完全不同的命令。"""
        assert matches_command_prefix("git", "npm install") is False

    # -- 词边界安全：核心安全属性 --

    def test_git_does_not_match_gitleaks(self):
        """git 不应匹配 gitleaks（词边界安全，token 级别匹配）。"""
        assert matches_command_prefix("git", "gitleaks detect") is False

    def test_rm_does_not_match_rmdir(self):
        """rm 不应匹配 rmdir。"""
        assert matches_command_prefix("rm", "rmdir /tmp/foo") is False

    def test_cat_does_not_match_concat(self):
        """cat 不应匹配 concat。"""
        assert matches_command_prefix("cat", "concat file1 file2") is False

    def test_npm_does_not_match_npx(self):
        """npm 不应匹配 npx。"""
        assert matches_command_prefix("npm", "npx create-app") is False

    # -- 多 token 前缀匹配 --

    def test_multi_token_prefix(self):
        """多 token 前缀匹配：git commit 匹配 git commit -m x。"""
        assert matches_command_prefix("git commit", "git commit -m x") is True

    def test_multi_token_prefix_exact(self):
        """多 token 前缀完全匹配。"""
        assert matches_command_prefix("git commit", "git commit") is True

    def test_multi_token_prefix_too_long(self):
        """前缀 token 数多于命令 token 数时不匹配。"""
        assert matches_command_prefix("git commit -m", "git commit") is False

    def test_three_token_prefix(self):
        """三 token 前缀匹配。"""
        assert matches_command_prefix(
            "docker compose up", "docker compose up -d"
        ) is True

    # -- 空白处理 --

    def test_leading_trailing_whitespace_in_pattern(self):
        """pattern 首尾空白被 strip 处理。"""
        assert matches_command_prefix("  git  ", "git status") is True

    def test_leading_trailing_whitespace_in_command(self):
        """command 首尾空白被 strip 处理。"""
        assert matches_command_prefix("git", "  git status  ") is True

    def test_multiple_spaces_between_tokens(self):
        """token 间多个空格不影响匹配（split 语义）。"""
        assert matches_command_prefix("git  commit", "git   commit  -m  x") is True

    # -- 空输入 --

    def test_empty_pattern(self):
        """空 pattern 不匹配任何命令。"""
        assert matches_command_prefix("", "git") is False

    def test_whitespace_only_pattern(self):
        """纯空白 pattern 不匹配。"""
        assert matches_command_prefix("   ", "git") is False

    def test_empty_command(self):
        """空 command 不匹配非空 pattern。"""
        assert matches_command_prefix("git", "") is False

    def test_both_empty(self):
        """pattern 和 command 都为空时不匹配。"""
        assert matches_command_prefix("", "") is False


# ---------------------------------------------------------------------------
# matches_command_glob：自由 glob 模式匹配
# ---------------------------------------------------------------------------


class TestMatchesCommandGlob:
    """tests for matches_command_glob"""

    # -- 基本 glob 匹配 --

    def test_star_matches_any_suffix(self):
        """* 匹配任意后缀。"""
        assert matches_command_glob("git *", "git status") is True

    def test_star_matches_long_command(self):
        """* 可跨单词边界匹配长命令。"""
        assert matches_command_glob("git *", "git commit -m x --amend") is True

    def test_star_does_not_match_bare_prefix(self):
        """command strip 后 "git " 变为 "git"，正则 "git .*?" 无法 fullmatch。"""
        # "git *" → regex "git .*?"，但 command.strip() 将 "git " 变为 "git"
        # fullmatch("git .*?", "git") 失败，因为空格后无内容可匹配
        assert matches_command_glob("git *", "git ") is False

    def test_exact_match_without_glob(self):
        """不含通配符时退化为精确匹配。"""
        assert matches_command_glob("git status", "git status") is True
        assert matches_command_glob("git status", "git log") is False

    # -- 多星号模式 --

    def test_multiple_stars(self):
        """多个 * 的模式。"""
        assert matches_command_glob("git * -m *", "git commit -m hello") is True

    def test_star_at_beginning(self):
        """* 在开头匹配任意前缀。"""
        assert matches_command_glob("* status", "git status") is True

    def test_star_only(self):
        """纯 * 匹配任何命令。"""
        assert matches_command_glob("*", "anything at all") is True

    # -- 特殊字符转义 --

    def test_dot_is_escaped(self):
        """glob 中的 . 被正确转义（不是正则的通配符）。"""
        assert matches_command_glob("echo *.py", "echo main.py") is True
        assert matches_command_glob("echo *.py", "echo mainXpy") is False

    def test_special_regex_chars_escaped(self):
        """正则特殊字符被正确转义。"""
        assert matches_command_glob("echo [test]", "echo [test]") is True
        assert matches_command_glob("echo [test]", "echo t") is False

    # -- 空白处理 --

    def test_leading_trailing_whitespace(self):
        """首尾空白被 strip 处理。"""
        assert matches_command_glob("  git *  ", "  git status  ") is True

    # -- 空输入 --

    def test_empty_pattern(self):
        """空 pattern 不匹配。"""
        assert matches_command_glob("", "git") is False

    def test_whitespace_only_pattern(self):
        """纯空白 pattern 不匹配。"""
        assert matches_command_glob("   ", "git") is False

    def test_empty_command(self):
        """空 command 对非空 pattern 不匹配。"""
        assert matches_command_glob("git *", "") is False

    # -- fullmatch 语义 --

    def test_partial_match_rejected(self):
        """部分匹配不被接受（必须 fullmatch）。"""
        assert matches_command_glob("git", "git status") is False

    def test_glob_requires_full_coverage(self):
        """glob 模式必须完整覆盖命令字符串。"""
        assert matches_command_glob("git *", "git") is False


# ---------------------------------------------------------------------------
# evaluate_bash_segment：单段命令权限评估
# ---------------------------------------------------------------------------


class TestEvaluateBashSegment:
    """tests for evaluate_bash_segment"""

    # -- 前缀匹配模式（resource 不含 *） --

    def test_prefix_allow(self):
        """前缀匹配 allow 规则时返回 allow。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        assert evaluate_bash_segment("git status", rules) == "allow"

    def test_prefix_deny(self):
        """前缀匹配 deny 规则时返回 deny。"""
        rules = [PermissionRule(tool="execute", resource="rm", effect="deny")]
        assert evaluate_bash_segment("rm -rf /", rules) == "deny"

    def test_prefix_no_match_returns_none(self):
        """无规则匹配时返回 None。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        assert evaluate_bash_segment("npm install", rules) is None

    # -- glob 匹配模式（resource 含 *） --

    def test_glob_allow(self):
        """glob 模式匹配 allow 规则时返回 allow。"""
        rules = [
            PermissionRule(tool="execute", resource="git commit *", effect="allow")
        ]
        assert evaluate_bash_segment("git commit -m fix", rules) == "allow"

    def test_glob_deny(self):
        """glob 模式匹配 deny 规则时返回 deny。"""
        rules = [
            PermissionRule(tool="execute", resource="rm -rf *", effect="deny")
        ]
        assert evaluate_bash_segment("rm -rf /tmp", rules) == "deny"

    # -- deny 优先级 --

    def test_deny_overrides_allow(self):
        """deny 优先于 allow，与规则顺序无关。"""
        rules = [
            PermissionRule(tool="execute", resource="git", effect="allow"),
            PermissionRule(tool="execute", resource="git push", effect="deny"),
        ]
        # "git push" 同时匹配两条规则，deny 优先
        assert evaluate_bash_segment("git push", rules) == "deny"

    def test_deny_overrides_allow_reversed_order(self):
        """deny 优先于 allow，即使 deny 在 allow 之前。"""
        rules = [
            PermissionRule(tool="execute", resource="git push", effect="deny"),
            PermissionRule(tool="execute", resource="git", effect="allow"),
        ]
        assert evaluate_bash_segment("git push origin main", rules) == "deny"

    # -- allow 优先于 ask --

    def test_allow_overrides_ask(self):
        """allow 优先于 ask。"""
        rules = [
            PermissionRule(tool="execute", resource="git", effect="ask"),
            PermissionRule(tool="execute", resource="git status", effect="allow"),
        ]
        assert evaluate_bash_segment("git status", rules) == "allow"

    def test_ask_when_no_allow(self):
        """仅有 ask 匹配时返回 ask。"""
        rules = [
            PermissionRule(tool="execute", resource="npm", effect="ask"),
        ]
        assert evaluate_bash_segment("npm install", rules) == "ask"

    # -- tool 过滤 --

    def test_non_execute_tool_ignored(self):
        """tool 不是 execute 或 * 的规则被忽略。"""
        rules = [
            PermissionRule(tool="read", resource="git", effect="deny"),
        ]
        assert evaluate_bash_segment("git status", rules) is None

    def test_wildcard_tool_matches(self):
        """tool 为 * 的规则参与匹配。"""
        rules = [
            PermissionRule(tool="*", resource="rm", effect="deny"),
        ]
        assert evaluate_bash_segment("rm -rf /", rules) == "deny"

    # -- 空输入 --

    def test_empty_segment(self):
        """空段返回 None。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        assert evaluate_bash_segment("", rules) is None

    def test_whitespace_only_segment(self):
        """纯空白段返回 None。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        assert evaluate_bash_segment("   ", rules) is None

    def test_empty_rules(self):
        """空规则列表返回 None。"""
        assert evaluate_bash_segment("git status", []) is None

    # -- 词边界安全 --

    def test_prefix_word_boundary_in_segment_eval(self):
        """段评估中前缀匹配保持词边界安全。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        # gitleaks 不应被 git 前缀匹配
        assert evaluate_bash_segment("gitleaks detect", rules) is None


# ---------------------------------------------------------------------------
# evaluate_bash：完整链式命令安全门评估
# ---------------------------------------------------------------------------


class TestEvaluateBash:
    """tests for evaluate_bash"""

    # -- 单段命令 --

    def test_single_command_allow(self):
        """单段命令匹配 allow 规则时整体 allow。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        result = evaluate_bash("git status", rules)
        assert result["decision"] == "allow"
        assert len(result["segments"]) == 1
        assert result["segments"][0]["decision"] == "allow"

    def test_single_command_deny(self):
        """单段命令匹配 deny 规则时整体 deny。"""
        rules = [PermissionRule(tool="execute", resource="rm", effect="deny")]
        result = evaluate_bash("rm -rf /", rules)
        assert result["decision"] == "deny"

    def test_single_command_no_match_falls_to_ask(self):
        """单段命令无规则匹配时整体 fallback 到 ask。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        result = evaluate_bash("npm test", rules)
        assert result["decision"] == "ask"

    # -- 链式命令（合取式评估） --

    def test_chain_all_allow(self):
        """所有段都 allow 时整体 allow。"""
        rules = [
            PermissionRule(tool="execute", resource="git", effect="allow"),
            PermissionRule(tool="execute", resource="echo", effect="allow"),
        ]
        result = evaluate_bash("git status && echo done", rules)
        assert result["decision"] == "allow"
        assert len(result["segments"]) == 2

    def test_chain_one_deny_overrides_all_allow(self):
        """任何段 deny 时整体 deny。"""
        rules = [
            PermissionRule(tool="execute", resource="git", effect="allow"),
            PermissionRule(tool="execute", resource="rm", effect="deny"),
        ]
        result = evaluate_bash("git status && rm -rf /", rules)
        assert result["decision"] == "deny"

    def test_chain_one_ask_downgrades_to_ask(self):
        """存在未匹配段（ask/None）时整体降级为 ask。"""
        rules = [
            PermissionRule(tool="execute", resource="git", effect="allow"),
        ]
        result = evaluate_bash("git status && npm test", rules)
        # git status → allow, npm test → None → 整体 ask
        assert result["decision"] == "ask"

    # -- CWE-178 防护：前导/尾随空白 trim --

    def test_leading_whitespace_trimmed(self):
        """前导空白被 trim（CWE-178 防护）。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        result = evaluate_bash("   git status", rules)
        assert result["decision"] == "allow"

    def test_trailing_whitespace_trimmed(self):
        """尾随空白被 trim。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        result = evaluate_bash("git status   ", rules)
        assert result["decision"] == "allow"

    def test_leading_and_trailing_whitespace_trimmed(self):
        """首尾空白同时存在时被 trim。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        result = evaluate_bash("  \t git status \n ", rules)
        assert result["decision"] == "allow"

    # -- 空命令 --

    def test_empty_command_returns_deny(self):
        """空命令返回 deny（安全默认）。"""
        result = evaluate_bash("", [])
        assert result["decision"] == "deny"
        assert result["segments"] == []

    def test_whitespace_only_command_returns_deny(self):
        """纯空白命令 trim 后为空，返回 deny。"""
        result = evaluate_bash("   ", [])
        assert result["decision"] == "deny"
        assert result["segments"] == []

    # -- 包装器剥离 --

    def test_env_wrapper_stripped(self):
        """环境变量赋值包装器被剥离后正确评估。"""
        rules = [PermissionRule(tool="execute", resource="npm", effect="allow")]
        result = evaluate_bash("NODE_ENV=production npm test", rules)
        assert result["decision"] == "allow"
        # processed 应剥离了环境变量前缀
        assert result["segments"][0]["processed"] != result["segments"][0]["raw"]

    def test_timeout_wrapper_stripped(self):
        """timeout 包装器被剥离后正确评估。"""
        rules = [PermissionRule(tool="execute", resource="npm", effect="allow")]
        result = evaluate_bash("timeout 30 npm test", rules)
        assert result["decision"] == "allow"

    # -- 段结果结构 --

    def test_segment_result_structure(self):
        """每段结果包含 raw、processed、decision 三个键。"""
        rules = [PermissionRule(tool="execute", resource="git", effect="allow")]
        result = evaluate_bash("git status", rules)
        seg = result["segments"][0]
        assert "raw" in seg
        assert "processed" in seg
        assert "decision" in seg

    def test_segment_raw_preserves_original(self):
        """段的 raw 字段保留原始命令文本。"""
        rules = [
            PermissionRule(tool="execute", resource="git", effect="allow"),
            PermissionRule(tool="execute", resource="echo", effect="allow"),
        ]
        result = evaluate_bash("git status && echo done", rules)
        assert result["segments"][0]["raw"] == "git status"
        assert result["segments"][1]["raw"] == "echo done"

    # -- 管道命令 --

    def test_pipeline_segments_eval(self):
        """管道命令各段独立评估。"""
        rules = [
            PermissionRule(tool="execute", resource="cat", effect="allow"),
            PermissionRule(tool="execute", resource="grep", effect="allow"),
        ]
        result = evaluate_bash("cat file.txt | grep pattern", rules)
        assert result["decision"] == "allow"
        assert len(result["segments"]) == 2

    # -- 分号分隔命令 --

    def test_semicolon_chain(self):
        """分号分隔的命令各段独立评估。"""
        rules = [
            PermissionRule(tool="execute", resource="git", effect="allow"),
            PermissionRule(tool="execute", resource="npm", effect="allow"),
        ]
        result = evaluate_bash("git status; npm test", rules)
        assert result["decision"] == "allow"
        assert len(result["segments"]) == 2

    # -- 空规则列表 --

    def test_empty_rules_all_ask(self):
        """空规则列表时所有段 fallback 到 ask。"""
        result = evaluate_bash("git status", [])
        assert result["decision"] == "ask"

    # -- deny 在链式命令中的传播 --

    def test_deny_in_second_segment(self):
        """第二段 deny 导致整体 deny。"""
        rules = [
            PermissionRule(tool="execute", resource="echo", effect="allow"),
            PermissionRule(tool="execute", resource="rm", effect="deny"),
        ]
        result = evaluate_bash("echo hello && rm -rf /", rules)
        assert result["decision"] == "deny"
        assert result["segments"][0]["decision"] == "allow"
        assert result["segments"][1]["decision"] == "deny"

    # -- glob 规则在完整评估中 --

    def test_glob_rule_in_full_eval(self):
        """glob 规则在完整评估中正确工作。"""
        rules = [
            PermissionRule(tool="execute", resource="git *", effect="allow"),
        ]
        result = evaluate_bash("git commit -m fix", rules)
        assert result["decision"] == "allow"

    def test_glob_rule_deny_in_chain(self):
        """glob deny 规则在链式命令中正确生效。"""
        rules = [
            PermissionRule(tool="execute", resource="git *", effect="allow"),
            PermissionRule(tool="execute", resource="rm -rf *", effect="deny"),
        ]
        result = evaluate_bash("git status && rm -rf /tmp", rules)
        assert result["decision"] == "deny"
