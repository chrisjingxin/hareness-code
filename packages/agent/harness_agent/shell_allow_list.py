"""Shell 白名单中间件：仅放行可可靠判定的单一命令。"""
from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from langchain.tools.tool_node import ToolCallRequest

logger = logging.getLogger(__name__)

SHELL_ALLOW_ALL = type("SHELL_ALLOW_ALL", (), {"__repr__": lambda self: "SHELL_ALLOW_ALL"})()
"""哨兵值：显式表示不限制 shell 命令，仅供调用方识别而非中间件使用。"""

_RECOMMENDED_SHELL_ALLOW_LIST = [
    "ls", "cat", "grep", "find", "head", "tail", "wc", "sort", "uniq",
    "echo", "pwd", "which", "whereis", "file", "stat", "du", "df",
    "git", "diff", "rg", "ag", "sed", "awk", "tr", "cut", "paste",
    "python", "python3", "pip", "pip3", "uv", "node", "npm", "npx",
    "bun", "yarn", "pnpm", "cargo", "go", "rustc", "gcc", "g++", "make",
    "cmake", "pytest", "ruff", "black", "mypy", "pyright", "tsc",
    "eslint", "prettier", "oxlint", "jq", "yq", "tree", "basename",
    "dirname", "realpath", "mkdir", "touch", "cp", "mv", "ln",
]

_SHELL_OPERATOR_CHARS = frozenset("();<>|&")
_COMMAND_SUBSTITUTION_MARKERS = ("$(", "`")


def _tokenize_single_command(command: object) -> list[str] | None:
    """解析单一 POSIX shell 命令；任何复合语法或歧义均安全拒绝。"""
    if not isinstance(command, str) or not command.strip():
        return None
    if any(character in command for character in ("\r", "\n", "\x00")):
        return None
    if any(marker in command for marker in _COMMAND_SUBSTITUTION_MARKERS):
        return None

    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars="".join(sorted(_SHELL_OPERATOR_CHARS)),
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except (TypeError, ValueError):
        return None

    if not tokens:
        return None
    if any(token and all(character in _SHELL_OPERATOR_CHARS for character in token) for token in tokens):
        return None
    return tokens


def is_shell_command_allowed(command: object, allow_list: list[str]) -> bool:
    """仅当输入是白名单中可执行文件启动的单一命令时返回真。

    绝对路径不会按 basename 降级匹配：调用方若要允许 ``/usr/bin/git``，
    必须把该完整路径显式加入白名单，避免工作区或临时目录中的同名程序冒充。
    参数作为同一命令的参数保留，但不能包含 shell 控制运算符、重定向、
    命令替换、换行或无法闭合的引号。
    """
    tokens = _tokenize_single_command(command)
    return bool(tokens and tokens[0] in allow_list)


class ShellAllowListMiddleware(AgentMiddleware):
    """在不触发 HITL 中断的情况下按白名单校验 shell 命令。

    When the agent invokes the `execute` shell tool, this middleware checks
    the command against the configured allow-list before execution.
    Rejected commands are returned as error ToolMessage objects — the graph
    never pauses, so traces stay as a single continuous run.
    """

    def __init__(self, allow_list: list[str]) -> None:
        """校验白名单配置并复制一份不可被调用方就地修改的列表。"""
        super().__init__()
        if not allow_list:
            msg = "allow_list must not be empty; disable shell access instead"
            raise ValueError(msg)
        if isinstance(allow_list, type(SHELL_ALLOW_ALL)):
            msg = "SHELL_ALLOW_ALL should not be used with ShellAllowListMiddleware"
            raise TypeError(msg)
        self._allow_list = list(allow_list)

    def _validate_tool_call(self, request: ToolCallRequest) -> Any | None:
        """当 execute 命令未获准时返回错误 ToolMessage，否则返回 None。"""
        from langchain_core.messages import ToolMessage as LCToolMessage

        if request.tool_call["name"] != "execute":
            return None

        args = request.tool_call.get("args") or {}
        command = args.get("command", "")
        if is_shell_command_allowed(command, self._allow_list):
            logger.debug("Shell command allowed: %r", command)
            return None

        logger.warning("Shell command rejected by allow-list: %r", command)
        allowed_str = ", ".join(self._allow_list)
        return LCToolMessage(
            content=(
                f"Shell command rejected: `{command}` is not in the allow-list. "
                f"Allowed commands: {allowed_str}. "
                f"Please use an allowed command or try another approach."
            ),
            name="execute",
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步工具调用入口：先执行白名单检查，再委托原处理器。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步工具调用入口：复用同一检查逻辑，避免同步/异步策略漂移。"""
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return await handler(request)
