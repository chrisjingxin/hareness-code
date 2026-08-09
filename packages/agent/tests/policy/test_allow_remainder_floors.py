"""ZC-117 约束 B：allow 规则命中后按剩余部分复核安全底线。"""

from __future__ import annotations

from harness_agent.policy.approval_policy import evaluate_permission
from harness_agent.policy.bash_matcher import (
    allow_remainder_triggers_floor,
    remainder_after_command_prefix,
)
from harness_agent.policy.permission_rules import PermissionRule


def test_remainder_after_prefix() -> None:
    assert remainder_after_command_prefix("git commit --amend", "git commit") == "--amend"
    assert remainder_after_command_prefix("echo evil > ~/.bashrc", "echo") == "evil > ~/.bashrc"
    assert remainder_after_command_prefix("git status", "git status") == ""
    assert remainder_after_command_prefix("git status", "npm install") is None


def test_git_commit_amend_does_not_reask() -> None:
    """git commit 规则命中后，--amend 不触发底线，应放行。"""
    rules = [PermissionRule(tool="execute", resource="git commit", effect="allow")]
    assert allow_remainder_triggers_floor("git commit --amend", rules) is False
    assert (
        evaluate_permission("execute", {"command": "git commit --amend"}, "default", rules)
        == "allow"
    )


def test_echo_redirect_remainder_asks() -> None:
    """echo 规则命中后，剩余部分含重定向应强制 ask。"""
    rules = [PermissionRule(tool="execute", resource="echo", effect="allow")]
    assert allow_remainder_triggers_floor("echo evil > ~/.bashrc", rules) is True
    assert (
        evaluate_permission(
            "execute", {"command": "echo evil > ~/.bashrc"}, "default", rules
        )
        == "ask"
    )


def test_glob_allow_rule_checks_whole_command() -> None:
    """含 * 的手写宽规则对整段命令跑底线。"""
    rules = [PermissionRule(tool="execute", resource="echo *", effect="allow")]
    assert allow_remainder_triggers_floor("echo evil > ~/.bashrc", rules) is True
