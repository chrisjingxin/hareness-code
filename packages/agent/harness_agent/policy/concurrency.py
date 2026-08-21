"""工具并发安全分类与异步读写锁。

ToolNode 的 _afunc 使用 asyncio.gather 无差别地并行执行所有 tool_calls，
生产路径通过 ConcurrencyGuardMiddleware 使用本模块的安全分类和读写锁，
在每次工具调用实际执行前协调读写操作。
"""

from __future__ import annotations

import asyncio
import re
import shlex

# ---------------------------------------------------------------------------
# 工具分类常量
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS = frozenset({
    "ls", "read_file", "glob", "grep",
    "web_search", "lsp", "tool_search", "memory_search",
})
"""始终可并行的只读工具。"""

_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})
"""始终不可并行的写工具。"""

# ---------------------------------------------------------------------------
# Shell 只读判定
# ---------------------------------------------------------------------------

_SHELL_READ_ONLY_COMMANDS = frozenset({
    "cat", "cd", "df", "dirname", "du", "echo", "find", "git", "grep",
    "head", "less", "ls", "more", "pwd", "rg", "sort", "stat", "tail",
    "tree", "uniq", "wc", "which", "where", "whoami", "basename",
    "column", "cut", "printenv", "printf", "ps", "awk", "sed",
})
"""白名单：根命令本身不产生副作用。"""

_GIT_READ_ONLY_SUBCOMMANDS = frozenset({
    "blame", "branch", "cat-file", "diff", "grep", "log", "ls-files",
    "remote", "rev-parse", "show", "status", "describe",
})
"""git 只读子命令白名单。"""

_GIT_WRITE_SUBCOMMANDS = frozenset({
    "push", "commit", "merge", "rebase", "reset", "checkout", "add",
    "rm", "mv", "stash", "cherry-pick", "revert", "apply", "am",
    "clean", "tag",
})
"""git 写子命令黑名单。"""

# 使命令变为非只读的参数模式
_BLOCK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfind\b.*\s-delete\b"),
    re.compile(r"\bsed\b.*\s-i\b"),
    re.compile(r"\bgit\s+branch\s+-[dD]\b"),
]

# 输出重定向 / 管道到写命令
_REDIRECT_PATTERN = re.compile(r"(>>?|&>)")
_PIPE_WRITE_COMMANDS = frozenset({
    "tee", "dd", "install", "cp", "mv", "rm", "mkdir", "touch",
    "chmod", "chown", "ln", "truncate",
})

# 复合命令分隔符
_COMPOUND_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;)\s*")


def is_shell_command_read_only(command: str) -> bool:
    """判断 shell 命令是否为只读（可安全并行执行）。

    采用白名单策略：仅当命令的根程序在只读白名单中、且不包含任何
    写操作模式（重定向、管道到写命令、破坏性参数）时返回 True。
    复合命令（&&、||、;）要求所有子命令均为只读。
    解析失败时保守返回 False（fail-closed）。
    """
    if not command or not command.strip():
        return False

    command = command.strip()

    # 复合命令：拆分子命令后逐一判定
    if _COMPOUND_SEPARATORS.search(command):
        sub_commands = _COMPOUND_SEPARATORS.split(command)
        return all(
            is_shell_command_read_only(sub)
            for sub in sub_commands
            if sub.strip()
        )

    # 输出重定向一律视为写操作
    if _REDIRECT_PATTERN.search(command):
        return False

    # 管道：检查管道目标是否为写命令
    if "|" in command:
        segments = command.split("|")
        # 管道中每一段都必须只读
        return all(
            _is_single_command_read_only(seg.strip())
            for seg in segments
            if seg.strip()
        )

    return _is_single_command_read_only(command)


def _is_single_command_read_only(command: str) -> bool:
    """判定单一（无管道、无复合）命令是否只读。"""
    if not command:
        return False

    # 全局阻断模式检查
    for pattern in _BLOCK_PATTERNS:
        if pattern.search(command):
            return False

    # 解析根命令
    try:
        tokens = shlex.split(command)
    except ValueError:
        # 引号不闭合等解析错误，保守拒绝
        return False

    if not tokens:
        return False

    root = tokens[0]
    # 去除路径前缀，取 basename
    root = root.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    if root not in _SHELL_READ_ONLY_COMMANDS:
        return False

    # git 子命令特殊处理
    if root == "git":
        return _is_git_read_only(tokens)

    # sed 需确认无 -i（全局模式已覆盖，此处双重保险）
    if root == "sed":
        return "-i" not in tokens and "--in-place" not in tokens

    # find 需确认无 -delete（全局模式已覆盖）
    if root == "find":
        return "-delete" not in tokens

    return True


def _is_git_read_only(tokens: list[str]) -> bool:
    """判定 git 命令是否为只读子命令。"""
    # 跳过 git 本身及全局选项（如 -C、--git-dir 等）
    idx = 1
    while idx < len(tokens) and tokens[idx].startswith("-"):
        idx += 1

    if idx >= len(tokens):
        # 无子命令，如裸 `git`，视为只读
        return True

    subcommand = tokens[idx]

    if subcommand in _GIT_WRITE_SUBCOMMANDS:
        return False

    if subcommand not in _GIT_READ_ONLY_SUBCOMMANDS:
        # 未知子命令保守拒绝
        return False

    # git branch 特殊处理：-d / -D 为删除操作
    if subcommand == "branch":
        remaining = tokens[idx + 1:]
        if any(arg in ("-d", "-D", "--delete") for arg in remaining):
            return False

    return True


# ---------------------------------------------------------------------------
# 工具并发安全性判定
# ---------------------------------------------------------------------------


def is_concurrency_safe(tool_name: str, args: dict) -> bool:
    """判断单次工具调用是否可安全地与其他调用并发执行。

    规则：
    - 只读工具（ls、read_file、glob、grep）：始终安全
    - 写工具（write_file、edit_file）：始终不安全
    - Shell 工具（execute）：动态判定，仅当命令只读时安全
    - 子代理工具（task）：当前子图可能写同一工作区，必须独占
    - 未知工具：保守返回 False（fail-closed）
    """
    if tool_name in _READ_ONLY_TOOLS:
        return True

    if tool_name in _WRITE_TOOLS:
        return False

    if tool_name == "task":
        # 默认 general-purpose 子图不继承主图的并发 guard，且可使用写工具；
        # 父 task 持有独占锁直到该同步子调用返回。
        return False

    if tool_name == "execute":
        command = args.get("command", "")
        if not isinstance(command, str):
            return False
        return is_shell_command_read_only(command)

    # 未知工具一律视为不安全
    return False


# ---------------------------------------------------------------------------
# 异步读写锁
# ---------------------------------------------------------------------------


class AsyncRWLock:
    """异步读写锁：允许多读者共享，写者独占。

    用于并发守卫中间件：只读工具获取读锁（多个读者可同时持有），
    写工具获取写锁（独占，阻塞所有其他读者和写者），从而在不替换
    ToolNode 的前提下实现与分区执行等价的安全保证。
    """

    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        self._lock = asyncio.Lock()
        self._readers_ok = asyncio.Condition(self._lock)
        self._writer_ok = asyncio.Condition(self._lock)

    async def acquire_read(self) -> None:
        """获取读锁：当有写者持锁时等待，否则立即共享进入。"""
        async with self._lock:
            while self._writer:
                await self._readers_ok.wait()
            self._readers += 1

    async def release_read(self) -> None:
        """释放读锁：最后一个读者离开时唤醒等待的写者。"""
        async with self._lock:
            self._readers -= 1
            if self._readers == 0:
                self._writer_ok.notify_all()

    async def acquire_write(self) -> None:
        """获取写锁：等待所有读者和写者释放后独占进入。"""
        async with self._lock:
            while self._writer or self._readers > 0:
                await self._writer_ok.wait()
            self._writer = True

    async def release_write(self) -> None:
        """释放写锁：唤醒所有等待的读者和写者。"""
        async with self._lock:
            self._writer = False
            self._readers_ok.notify_all()
            self._writer_ok.notify_all()
