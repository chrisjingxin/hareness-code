"""Shell allow-list middleware — validates shell commands without HITL interrupts.

照搬自 dcode agent.py:275-386，提取为独立模块。
"""
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
"""Sentinel: allow all shell commands without restriction."""

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


def is_shell_command_allowed(command: str, allow_list: list[str]) -> bool:
    """Check if a shell command's first token is in the allow-list."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    first_cmd = tokens[0]
    # Handle path prefixes (e.g. /usr/bin/python → python)
    if "/" in first_cmd:
        first_cmd = first_cmd.rsplit("/", 1)[-1]
    return first_cmd in allow_list


class ShellAllowListMiddleware(AgentMiddleware):
    """Validate shell commands against an allow-list without HITL interrupts.

    When the agent invokes the `execute` shell tool, this middleware checks
    the command against the configured allow-list before execution.
    Rejected commands are returned as error ToolMessage objects — the graph
    never pauses, so traces stay as a single continuous run.
    """

    def __init__(self, allow_list: list[str]) -> None:
        super().__init__()
        if not allow_list:
            msg = "allow_list must not be empty; disable shell access instead"
            raise ValueError(msg)
        if isinstance(allow_list, type(SHELL_ALLOW_ALL)):
            msg = "SHELL_ALLOW_ALL should not be used with ShellAllowListMiddleware"
            raise TypeError(msg)
        self._allow_list = list(allow_list)

    def _validate_tool_call(self, request: ToolCallRequest) -> Any | None:
        """Return an error tool message when a shell command is not allowed."""
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
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        if (rejection := self._validate_tool_call(request)) is not None:
            return rejection
        return await handler(request)
