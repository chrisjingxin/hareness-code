"""Shell 白名单词法边界与中间件短路行为回归测试。"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harness_agent.shell_allow_list import ShellAllowListMiddleware, is_shell_command_allowed


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff -- README.md",
        "pytest -q packages/agent/tests",
        "echo '管道 | 只是参数'",
        r"echo escaped\|pipe",
    ],
)
def test_single_allowlisted_commands_remain_available(command: str):
    """白名单内的单命令与被引用或转义的普通参数保持可用。"""
    assert is_shell_command_allowed(command, ["git", "pytest", "echo"])


@pytest.mark.parametrize(
    "command",
    [
        "git status && forbidden",
        "git status || forbidden",
        "git status; forbidden",
        "git status | forbidden",
        "git status > output.txt",
        "git status 2>&1",
        "git status &",
        "echo $(forbidden)",
        'echo "$(forbidden)"',
        "echo `forbidden`",
        "git status\nforbidden",
        "git status\rforbidden",
        "git status\x00forbidden",
        "git status <(forbidden)",
        "git status (forbidden)",
    ],
)
def test_shell_compound_syntax_fails_closed(command: str):
    """复合命令、重定向和替换语法在执行前统一拒绝。"""
    assert not is_shell_command_allowed(command, ["git", "echo"])


@pytest.mark.parametrize(
    "command",
    [None, 42, "", "   ", "unknown --version", "git 'unterminated"],
)
def test_missing_unknown_or_unparseable_commands_fail_closed(command: object):
    """非字符串、未知命令和畸形引号不会退回不安全的空白切分。"""
    assert not is_shell_command_allowed(command, ["git"])


def test_absolute_executable_paths_require_an_exact_allow_list_entry():
    """同名绝对路径不得仅凭 basename 冒充已允许的系统命令。"""
    assert not is_shell_command_allowed("/tmp/git status", ["git"])
    assert is_shell_command_allowed("/usr/bin/git status", ["/usr/bin/git"])
    assert not is_shell_command_allowed(r"'C:\\Tools\\git.exe' status", ["git.exe"])
    assert is_shell_command_allowed(
        r"'C:\\Tools\\git.exe' status", [r"C:\\Tools\\git.exe"]
    )


def test_middleware_rejects_compound_command_without_calling_sync_handler():
    """同步中间件拒绝危险输入，且不会调用真实 execute handler。"""
    middleware = ShellAllowListMiddleware(["git"])
    request = SimpleNamespace(
        tool_call={
            "name": "execute",
            "id": "call-sync",
            "args": {"command": "git status && forbidden"},
        }
    )
    called = False

    def handler(_request: Any) -> str:
        nonlocal called
        called = True
        return "executed"

    result = middleware.wrap_tool_call(request, handler)  # type: ignore[arg-type]

    assert called is False
    assert result.status == "error"
    assert result.tool_call_id == "call-sync"


@pytest.mark.asyncio
async def test_middleware_allows_safe_command_and_blocks_unsafe_async_handler():
    """异步入口与同步入口共享同一词法决策，不执行被拒绝的命令。"""
    middleware = ShellAllowListMiddleware(["git"])
    calls: list[str] = []

    async def handler(request: Any) -> str:
        calls.append(request.tool_call["args"]["command"])
        return "executed"

    safe = SimpleNamespace(
        tool_call={"name": "execute", "id": "safe", "args": {"command": "git status"}}
    )
    unsafe = SimpleNamespace(
        tool_call={
            "name": "execute",
            "id": "unsafe",
            "args": {"command": "git status | forbidden"},
        }
    )

    assert await middleware.awrap_tool_call(safe, handler) == "executed"  # type: ignore[arg-type]
    rejection = await middleware.awrap_tool_call(unsafe, handler)  # type: ignore[arg-type]

    assert calls == ["git status"]
    assert rejection.status == "error"
