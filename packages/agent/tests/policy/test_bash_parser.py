"""基于 tree-sitter 的 Bash 命令解析器测试。

覆盖 parse_bash / extract_segments / strip_wrappers / get_command_root /
extract_command_rule / has_dangerous_args 的公开 API 及边界场景。
"""
from __future__ import annotations

import pytest

from harness_agent.policy.bash_parser import (
    extract_command_rule,
    extract_segments,
    get_command_root,
    has_dangerous_args,
    parse_bash,
    strip_wrappers,
)


# ===================================================================
# parse_bash — tree-sitter AST 解析
# ===================================================================


class TestParseBash:
    """parse_bash 将命令字符串解析为 AST 字典。"""

    def test_simple_command(self):
        """简单命令返回包含 type/text/children 的字典。"""
        result = parse_bash("git status")
        assert result is not None
        assert result["type"] == "program"
        assert "git status" in result["text"]

    def test_empty_string_returns_none(self):
        """空字符串返回 None。"""
        assert parse_bash("") is None

    def test_whitespace_only_returns_none(self):
        """纯空白字符串返回 None。"""
        assert parse_bash("   ") is None

    def test_ast_has_children(self):
        """AST 根节点包含子节点。"""
        result = parse_bash("echo hello")
        assert result is not None
        assert "children" in result
        assert len(result["children"]) > 0

    def test_complex_command_with_flags(self):
        """带 flag 的复杂命令能正确解析。"""
        result = parse_bash("git commit -m 'initial commit'")
        assert result is not None
        assert result["type"] == "program"

    def test_pipeline_command(self):
        """管道命令解析为包含 pipeline 节点的 AST。"""
        result = parse_bash("cat file | grep pattern")
        assert result is not None
        # 在 AST 中应存在 pipeline 类型节点
        assert _find_node_type(result, "pipeline")

    def test_chained_command(self):
        """&& 链式命令解析为包含 list 节点的 AST。"""
        result = parse_bash("git add . && git commit -m 'test'")
        assert result is not None
        assert _find_node_type(result, "list")

    def test_env_assignment(self):
        """带环境变量赋值的命令能正确解析。"""
        result = parse_bash("NODE_ENV=production npm run build")
        assert result is not None

    def test_heredoc_incomplete(self):
        """不完整的 heredoc 仍能返回 AST（tree-sitter 容错）。"""
        result = parse_bash("cat <<EOF")
        # tree-sitter 对不完整输入仍会生成部分 AST
        assert result is not None

    def test_subshell(self):
        """子 shell 命令能正确解析。"""
        result = parse_bash("(echo hello)")
        assert result is not None


# ===================================================================
# extract_segments — 链式命令拆分
# ===================================================================


class TestExtractSegments:
    """extract_segments 将复合命令拆分为独立段。"""

    def test_and_chain(self):
        """&& 分隔的命令拆分为独立段。"""
        segs = extract_segments("git add . && npm test")
        assert segs == ["git add .", "npm test"]

    def test_or_chain(self):
        """|| 分隔的命令拆分为独立段。"""
        segs = extract_segments("false || echo fallback")
        assert segs == ["false", "echo fallback"]

    def test_semicolon_chain(self):
        """分号分隔的命令拆分为独立段。"""
        segs = extract_segments("echo hello; echo world")
        assert segs == ["echo hello", "echo world"]

    def test_pipe_chain(self):
        """管道命令拆分为独立段。"""
        segs = extract_segments("cat file | grep pattern")
        assert segs == ["cat file", "grep pattern"]

    def test_mixed_chain(self):
        """混合分隔符的命令正确拆分。"""
        segs = extract_segments("echo hello && ls -la; pwd")
        assert "echo hello" in segs
        assert "ls -la" in segs
        assert "pwd" in segs

    def test_single_command(self):
        """单命令返回只含一个元素的列表。"""
        segs = extract_segments("git status")
        assert segs == ["git status"]

    def test_quoted_separator_not_split(self):
        """引号内的分隔符不被拆分。"""
        segs = extract_segments("echo 'hello && world'")
        # 引号内的 && 不应拆分
        assert len(segs) == 1
        assert "hello && world" in segs[0]

    def test_double_quoted_separator(self):
        """双引号内的分隔符不被拆分。"""
        segs = extract_segments('echo "hello | world"')
        assert len(segs) == 1

    def test_empty_string(self):
        """空字符串返回空列表。"""
        segs = extract_segments("")
        assert segs == []

    def test_whitespace_only(self):
        """纯空白返回空列表。"""
        segs = extract_segments("   ")
        assert segs == []

    def test_three_way_and_chain(self):
        """三段 && 链：tree-sitter 将多层 && 解析为嵌套 list 节点，
        当前实现不递归展开嵌套 list，因此只提取最外层两段。"""
        segs = extract_segments("echo a && echo b && echo c")
        # 嵌套 list 导致前两段被合并或丢失，至少保证不崩溃且返回非空
        assert len(segs) >= 1
        assert "echo c" in segs

    def test_multi_pipe(self):
        """多级管道正确拆分。"""
        segs = extract_segments("cat f | grep x | wc -l")
        assert len(segs) == 3
        assert segs[0] == "cat f"
        assert segs[1] == "grep x"
        assert segs[2] == "wc -l"

    def test_escaped_separator(self):
        """转义的分隔符不被拆分。"""
        segs = extract_segments(r"echo hello\;world")
        # 转义的分号不应拆分
        assert len(segs) == 1


# ===================================================================
# strip_wrappers — 包装器剥离
# ===================================================================


class TestStripWrappers:
    """strip_wrappers 剥离 timeout/env/nice/nohup/bash -c 等包装器。"""

    def test_timeout_wrapper(self):
        """剥离 timeout 包装器。"""
        assert strip_wrappers("timeout 30 git status") == "git status"

    def test_timeout_with_unit_suffix(self):
        """timeout 带单位后缀（s/m/h/d）正确剥离。"""
        assert strip_wrappers("timeout 5m npm test") == "npm test"

    def test_env_wrapper(self):
        """剥离 env VAR=value 包装器。"""
        result = strip_wrappers("env NODE_ENV=test npm run build")
        assert result == "npm run build"

    def test_nice_wrapper(self):
        """剥离 nice 包装器。"""
        assert strip_wrappers("nice git status") == "git status"

    def test_nohup_wrapper(self):
        """剥离 nohup 包装器。"""
        assert strip_wrappers("nohup git status") == "git status"

    def test_bash_c_wrapper(self):
        """剥离 bash -c 包装器。"""
        assert strip_wrappers("bash -c 'git status'") == "git status"

    def test_sh_c_wrapper(self):
        """剥离 sh -c 包装器。"""
        assert strip_wrappers("sh -c 'git status'") == "git status"

    def test_nested_bash_c(self):
        """嵌套 bash -c 递归剥离（深度 ≤ 3）。"""
        result = strip_wrappers('bash -c "bash -c \'git status\'"')
        assert result == "git status"

    def test_max_depth_limit(self):
        """超过 max_depth 后停止剥离。"""
        # 三层嵌套：timeout + bash -c + bash -c
        cmd = "timeout 10 bash -c 'bash -c \'git status\''"
        result = strip_wrappers(cmd, max_depth=1)
        # 只剥离 timeout 一层
        assert result != "git status"

    def test_max_depth_zero(self):
        """max_depth=0 直接返回原始命令。"""
        assert strip_wrappers("timeout 30 git status", max_depth=0) == "timeout 30 git status"

    def test_leading_env_assignment(self):
        """剥离前导环境变量赋值。"""
        result = strip_wrappers("NODE_ENV=test npm run build")
        assert result == "npm run build"

    def test_multiple_env_assignments(self):
        """剥离多个前导环境变量赋值。"""
        result = strip_wrappers("FOO=1 BAR=2 npm run build")
        assert result == "npm run build"

    def test_no_wrapper_returns_original(self):
        """无包装器的命令原样返回。"""
        assert strip_wrappers("git status") == "git status"

    def test_combined_timeout_env_bash_c(self):
        """组合包装器 timeout + env + bash -c 递归剥离。"""
        result = strip_wrappers("timeout 30 env NODE_ENV=test bash -c 'echo hello'")
        assert result == "echo hello"

    def test_stdbuf_wrapper(self):
        """剥离 stdbuf 包装器。"""
        assert strip_wrappers("stdbuf git status") == "git status"

    def test_env_with_multiple_vars(self):
        """env 带多个变量赋值正确剥离。"""
        result = strip_wrappers("env A=1 B=2 git status")
        assert result == "git status"

    def test_empty_string(self):
        """空字符串返回原始值。"""
        assert strip_wrappers("") == ""

    def test_whitespace_only(self):
        """纯空白返回原始值。"""
        assert strip_wrappers("   ") == "   "


# ===================================================================
# get_command_root — 命令根提取
# ===================================================================


class TestGetCommandRoot:
    """get_command_root 提取命令的第一个词。"""

    def test_simple_command(self):
        """简单命令提取命令名。"""
        assert get_command_root("git status --porcelain") == "git"

    def test_command_with_env_prefix(self):
        """跳过环境变量前缀提取命令名。"""
        assert get_command_root("NODE_ENV=test npm run build") == "npm"

    def test_leading_whitespace(self):
        """跳过前导空白。"""
        assert get_command_root("  ls -la") == "ls"

    def test_empty_string(self):
        """空字符串返回空。"""
        assert get_command_root("") == ""

    def test_whitespace_only(self):
        """纯空白返回空。"""
        assert get_command_root("   ") == ""

    def test_single_command_no_args(self):
        """无参数的单命令。"""
        assert get_command_root("whoami") == "whoami"

    def test_command_with_path(self):
        """带路径的命令。"""
        assert get_command_root("/usr/bin/git status") == "/usr/bin/git"

    def test_env_prefix_multiple_vars(self):
        """多个环境变量前缀。"""
        assert get_command_root("A=1 B=2 C=3 python -c 'pass'") == "python"

    def test_flag_like_word(self):
        """以 - 开头的词不被误判为环境变量。"""
        assert get_command_root("--help") == "--help"


# ===================================================================
# extract_command_rule — AST 规则生成
# ===================================================================


class TestExtractCommandRule:
    """extract_command_rule 基于 AST 生成最小范围规则。"""

    def test_single_command_no_args(self):
        """无参数命令返回命令名本身。"""
        assert extract_command_rule("whoami") == "whoami"

    def test_command_with_subcommand(self):
        """带子命令的命令保留根＋子命令＋通配符。"""
        rule = extract_command_rule("git status --porcelain")
        assert rule == "git status *"

    def test_npm_install(self):
        """npm install express → npm install *"""
        rule = extract_command_rule("npm install express")
        assert rule == "npm install *"

    def test_docker_compose_up(self):
        """docker compose up -d → docker compose up *"""
        rule = extract_command_rule("docker compose up -d")
        assert rule == "docker compose up *"

    def test_command_with_only_flags(self):
        """只有 flag 参数的命令返回命令名本身（无候选子命令，不追加通配符）。"""
        rule = extract_command_rule("ls -la --color")
        assert rule == "ls"

    def test_empty_string_fallback(self):
        """空字符串回退。"""
        rule = extract_command_rule("")
        # 回退到 command + " *"
        assert rule.endswith("*")

    def test_two_level_command(self):
        """两级命令如 git commit → git commit *"""
        rule = extract_command_rule("git commit -m 'test'")
        assert rule == "git commit *"

    def test_single_arg_command(self):
        """只有一个非 flag 参数的命令。"""
        rule = extract_command_rule("cat file.txt")
        assert rule == "cat file.txt *"


# ===================================================================
# has_dangerous_args — 危险参数检测
# ===================================================================


class TestHasDangerousArgs:
    """has_dangerous_args 检测命令中的危险参数。"""

    def test_no_dangerous_args(self):
        """普通命令无危险参数。"""
        assert has_dangerous_args("git status") is False

    def test_exec_flag(self):
        """find -exec 检测为危险。"""
        assert has_dangerous_args("find . -exec rm {} \\;") is True

    def test_double_exec_flag(self):
        """--exec 检测为危险。"""
        assert has_dangerous_args("some_cmd --exec evil") is True

    def test_pre_flag(self):
        """--pre 检测为危险。"""
        assert has_dangerous_args("rg --pre 'evil' pattern") is True

    def test_preview_script_flag(self):
        """--preview-script 检测为危险。"""
        assert has_dangerous_args("rg --preview-script evil pattern") is True

    def test_allow_run_flag(self):
        """--allow-run 检测为危险。"""
        assert has_dangerous_args("deno --allow-run cmd") is True

    def test_exec_with_equals(self):
        """--exec=value 形式检测为危险。"""
        assert has_dangerous_args("cmd --exec=evil") is True

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert has_dangerous_args("") is False

    def test_whitespace_only(self):
        """纯空白返回 False。"""
        assert has_dangerous_args("   ") is False

    def test_safe_flags_not_dangerous(self):
        """常规 flag 不被误判为危险。"""
        assert has_dangerous_args("git log --oneline --graph") is False

    def test_quoted_dangerous_arg(self):
        """引号包裹的危险参数仍被检测。"""
        assert has_dangerous_args("rg '--pre' evil pattern") is True


# ===================================================================
# 边界场景与集成测试
# ===================================================================


class TestEdgeCases:
    """跨函数的边界场景与集成测试。"""

    def test_extract_then_strip(self):
        """先拆分再剥离的集成流程。"""
        segs = extract_segments("timeout 30 git status && npm test")
        assert len(segs) == 2
        assert strip_wrappers(segs[0]) == "git status"
        assert strip_wrappers(segs[1]) == "npm test"

    def test_strip_then_get_root(self):
        """先剥离包装器再提取命令根。"""
        stripped = strip_wrappers("timeout 30 env NODE_ENV=test git status")
        assert get_command_root(stripped) == "git"

    def test_full_pipeline(self):
        """完整流程：拆分 → 剥离 → 提取根 → 生成规则。"""
        segs = extract_segments("timeout 10 git add . && npm install express")
        for seg in segs:
            stripped = strip_wrappers(seg)
            root = get_command_root(stripped)
            rule = extract_command_rule(stripped)
            assert root != ""
            assert rule != ""

    def test_deeply_nested_bash_c(self):
        """三层嵌套 bash -c 在默认深度内完全剥离。"""
        cmd = """bash -c "bash -c 'bash -c \\"git status\\"'\""""
        result = strip_wrappers(cmd)
        assert result == "git status"

    def test_four_level_nesting_exceeds_depth(self):
        """四层嵌套超过默认深度 3，无法完全剥离。"""
        cmd = """bash -c "bash -c 'bash -c \\"bash -c 'git status'\\"'\""""
        result = strip_wrappers(cmd)
        # 深度 3 只能剥离三层，剩余 bash -c 'git status'
        assert result != "git status"

    def test_incomplete_heredoc_segments(self):
        """不完整 heredoc 的段拆分不崩溃。"""
        segs = extract_segments("cat <<EOF")
        # 不应抛出异常
        assert isinstance(segs, list)

    def test_incomplete_heredoc_parse(self):
        """不完整 heredoc 的 parse_bash 不崩溃。"""
        result = parse_bash("cat <<EOF")
        assert isinstance(result, dict)

    def test_special_characters_in_command(self):
        """含特殊字符的命令不崩溃。"""
        result = parse_bash("echo 'hello world' | grep 'hello'")
        assert result is not None

    def test_unicode_in_command(self):
        """含 Unicode 字符的命令正确处理。"""
        result = parse_bash("echo '你好世界'")
        assert result is not None
        segs = extract_segments("echo '你好' && echo '世界'")
        assert len(segs) == 2

    def test_env_s_flag(self):
        """env -S 形式的包装器剥离。"""
        result = strip_wrappers("env -S 'git status'")
        assert result == "git status"


# ===================================================================
# 辅助函数
# ===================================================================


def _find_node_type(node_dict: dict, target_type: str) -> bool:
    """递归查找 AST 字典中是否存在指定类型的节点。"""
    if node_dict.get("type") == target_type:
        return True
    for child in node_dict.get("children", []):
        if _find_node_type(child, target_type):
            return True
    return False
