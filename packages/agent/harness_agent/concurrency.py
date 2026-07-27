"""并发安全分区：将工具调用拆分为可并行批次与必须串行的批次。

ToolNode 的 _afunc 使用 asyncio.gather 无差别地并行执行所有 tool_calls，
本模块在 gather 之前提供预分区层，确保写操作不会与其他操作并发执行，
从而避免文件竞争和状态不一致。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# 工具分类常量
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
"""始终可并行的只读工具。"""

_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
"""始终不可并行的写工具。"""

_SUBAGENT_TOOLS = frozenset({"task"})
"""子代理工具：拥有隔离上下文，不共享可变状态，可并行。"""

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
    - 子代理工具（task）：隔离上下文，始终安全
    - 未知工具：保守返回 False（fail-closed）
    """
    if tool_name in _READ_ONLY_TOOLS:
        return True

    if tool_name in _WRITE_TOOLS:
        return False

    if tool_name in _SUBAGENT_TOOLS:
        return True

    if tool_name == "execute":
        command = args.get("command", "")
        if not isinstance(command, str):
            return False
        return is_shell_command_read_only(command)

    # 未知工具一律视为不安全
    return False


# ---------------------------------------------------------------------------
# 分区算法
# ---------------------------------------------------------------------------


@dataclass
class ToolBatch:
    """工具执行批次：标记该批次是否可并行以及包含的工具调用。"""

    concurrent: bool
    """该批次内的调用是否可以并行执行。"""

    calls: list[dict] = field(default_factory=list)
    """本批次包含的工具调用字典列表。"""


def partition_tool_calls(tool_calls: list[dict]) -> list[ToolBatch]:
    """将工具调用列表分区为可并行批次和必须串行的批次。

    算法：连续的并发安全工具合并为一个并行批次；每个不安全工具
    独立形成一个串行批次。仅包含单个调用的批次标记为串行（无并行收益）。
    保持原始顺序不变。

    示例：[read, read, edit, read] →
      [ToolBatch(concurrent=True, calls=[read, read]),
       ToolBatch(concurrent=False, calls=[edit]),
       ToolBatch(concurrent=False, calls=[read])]
    """
    batches: list[ToolBatch] = []

    for call in tool_calls:
        name = call.get("name", "")
        args = call.get("args") or {}
        safe = is_concurrency_safe(name, args)

        if safe:
            # 尝试合并到上一个并行批次
            if batches and batches[-1].concurrent:
                batches[-1].calls.append(call)
            else:
                batches.append(ToolBatch(concurrent=True, calls=[call]))
        else:
            # 每个不安全调用独立成批
            batches.append(ToolBatch(concurrent=False, calls=[call]))

    # 单个调用的并行批次无并行收益，降级为串行
    for batch in batches:
        if batch.concurrent and len(batch.calls) <= 1:
            batch.concurrent = False

    return batches


# ---------------------------------------------------------------------------
# 批次执行协调器
# ---------------------------------------------------------------------------

_DEFAULT_MAX_CONCURRENCY = 10
_ENV_MAX_CONCURRENCY = "HARNESS_MAX_TOOL_CONCURRENCY"


def get_max_concurrency() -> int:
    """读取最大并发数配置。

    从环境变量 HARNESS_MAX_TOOL_CONCURRENCY 解析正整数，
    无效值（非数字、零、负数）回退到默认值 10。
    """
    raw = os.environ.get(_ENV_MAX_CONCURRENCY, "")
    if not raw.strip():
        return _DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw.strip())
    except (ValueError, TypeError):
        return _DEFAULT_MAX_CONCURRENCY
    if value <= 0:
        return _DEFAULT_MAX_CONCURRENCY
    return value


async def execute_batches(
    batches: list[ToolBatch],
    executor: Callable[[dict], Any],
    max_concurrency: int | None = None,
) -> list:
    """按批次顺序执行工具调用，并行批次内部使用信号量限流。

    参数：
        batches: partition_tool_calls 产出的批次列表。
        executor: 异步可调用对象，接收单个 tool_call 字典并返回结果。
        max_concurrency: 并行批次内的最大并发数；为 None 时从环境变量读取。

    返回：
        按原始 tool_call 顺序排列的结果列表。
    """
    if max_concurrency is None:
        max_concurrency = get_max_concurrency()

    semaphore = asyncio.Semaphore(max_concurrency)
    results: list[Any] = []

    for batch in batches:
        if batch.concurrent and len(batch.calls) > 1:
            # 并行批次：信号量限流 + gather 保持顺序
            batch_results = await _execute_parallel(batch.calls, executor, semaphore)
            results.extend(batch_results)
        else:
            # 串行批次（或仅含单个调用的批次）：逐一执行
            for call in batch.calls:
                result = await executor(call)
                results.append(result)

    return results


async def _execute_parallel(
    calls: list[dict],
    executor: Callable[[dict], Any],
    semaphore: asyncio.Semaphore,
) -> list[Any]:
    """在信号量保护下并行执行一组工具调用，gather 保证结果顺序。"""

    async def _limited(call: dict) -> Any:
        async with semaphore:
            return await executor(call)

    return list(await asyncio.gather(*(_limited(call) for call in calls)))


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
