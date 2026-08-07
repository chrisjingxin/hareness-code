"""安全命令白名单：在 default 模式下自动放行只读安全的 shell 命令。

按宿主平台门控启用 Linux / macOS / Windows 各自的只读命令集合（ZC-117 决策 2）。
白名单判定基于命令根和参数模式，不依赖运行时沙箱或用户审批；命中后仍须经
安全底线检查。
"""

from __future__ import annotations

import logging
import re
import shlex
import sys

from harness_agent.policy.bash_parser import get_command_root

logger = logging.getLogger(__name__)

# ===================================================================
# 1. 按平台分组的只读命令白名单
# ===================================================================

# 三平台语义一致的通用只读命令（不含 Windows 上行为不同的 date/sort）
_COMMON_SAFE_COMMANDS: frozenset[str] = frozenset({
    "ls", "cat", "pwd", "whoami", "head", "tail", "wc", "uniq",
    "tr", "cut", "echo", "uname", "df", "du",
    "which", "whereis", "file", "stat", "id", "groups", "hostname",
    "printenv", "basename", "dirname", "realpath", "readlink",
    "cd",
})

_LINUX_SAFE_COMMANDS: frozenset[str] = frozenset({
    "free", "uptime", "lscpu", "lsblk", "lsb_release", "arch", "nproc", "vmstat",
    "sort", "date",
})

_MACOS_SAFE_COMMANDS: frozenset[str] = frozenset({
    "sw_vers", "vm_stat", "hostinfo", "machine", "arch", "md5", "system_profiler",
    "sort", "date",
})

# Windows cmd：不含 date（无参会交互改日期挂起）、不含 sort（/O 会写文件）
_WINDOWS_CMD_SAFE_COMMANDS: frozenset[str] = frozenset({
    "dir", "type", "cd", "where", "findstr", "ver", "vol", "tree",
    "systeminfo", "tasklist", "driverquery", "fc", "comp",
})

# PowerShell 只读 cmdlet（显式列举，禁止 Get-* 前缀启发式）
_WINDOWS_PS_SAFE_COMMANDS: frozenset[str] = frozenset({
    "Get-ChildItem", "Get-Content", "Get-Location", "Get-Item",
    "Resolve-Path", "Split-Path", "Join-Path", "Test-Path",
    "Select-String", "Measure-Object", "Select-Object", "Sort-Object",
    "Compare-Object", "Format-Table", "Format-List", "Out-String",
    "ConvertTo-Json", "ConvertFrom-Json", "Get-Date", "Get-Command",
    "Get-Member", "Get-Variable", "Get-Alias",
})

# 兼容旧导入：跨平台并集，仅作文档/枚举，运行时请用 safe_commands_for_platform
ALWAYS_SAFE_COMMANDS: frozenset[str] = (
    _COMMON_SAFE_COMMANDS
    | _LINUX_SAFE_COMMANDS
    | _MACOS_SAFE_COMMANDS
    | _WINDOWS_CMD_SAFE_COMMANDS
    | _WINDOWS_PS_SAFE_COMMANDS
)

# ===================================================================
# 2. Git 只读子命令白名单
# ===================================================================

SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "stash",
    "tag", "blame", "rev-parse", "rev-list", "ls-files", "ls-tree",
    "describe", "reflog", "config", "shortlog",
})

# ===================================================================
# 3. 搜索命令白名单
# ===================================================================

SAFE_SEARCH_COMMANDS: frozenset[str] = frozenset({"grep", "rg", "find"})

# ===================================================================
# 4. 危险参数正则模式
# ===================================================================

_DANGEROUS_ARG_REGEX: list[re.Pattern[str]] = [
    re.compile(r"^-exec$"),
    re.compile(r"^--exec$"),
    re.compile(r"^--pre$"),
    re.compile(r"^--preview-script$"),
    re.compile(r"^--allow-run$"),
    re.compile(r"^-ok$"),
    re.compile(r"^-okdir$"),
    re.compile(r"^--output$"),
    re.compile(r"^--replace$"),
    re.compile(r"^-I$"),
    # Windows sort 写文件
    re.compile(r"^/O$", re.IGNORECASE),
    re.compile(r"^/O:", re.IGNORECASE),
]


def safe_commands_for_platform(platform: str | None = None) -> frozenset[str]:
    """返回指定平台启用的只读命令根集合。"""
    host = platform if platform is not None else sys.platform
    if host.startswith("win"):
        return _COMMON_SAFE_COMMANDS | _WINDOWS_CMD_SAFE_COMMANDS | _WINDOWS_PS_SAFE_COMMANDS
    if host == "darwin":
        return _COMMON_SAFE_COMMANDS | _MACOS_SAFE_COMMANDS
    return _COMMON_SAFE_COMMANDS | _LINUX_SAFE_COMMANDS


def _tokenize_segment(segment: str, platform: str | None = None) -> list[str]:
    """将命令段拆分为 token 列表。

    Windows 使用非 POSIX shlex，避免反斜杠路径被吃掉。
    """
    host = platform if platform is not None else sys.platform
    posix = not host.startswith("win")
    try:
        return shlex.split(segment, posix=posix)
    except ValueError:
        return segment.split()


def _extract_git_subcommand(segment: str, platform: str | None = None) -> str:
    """从 git 命令段中提取子命令。"""
    tokens = _tokenize_segment(segment, platform)
    if len(tokens) < 2:
        return ""

    _GIT_GLOBAL_FLAGS = frozenset({
        "-C", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
        "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
        "-c", "--config", "--exec-path", "--html-path", "--man-path",
        "--info-path", "-p", "--paginate", "-P", "--no-pager",
        "--no-optional-locks", "--list-cmds", "--version", "--help",
    })

    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in _GIT_GLOBAL_FLAGS:
            i += 1
            if token in ("-c", "--config") and i < len(tokens):
                i += 1
            continue
        if "=" in token:
            i += 1
            continue
        return token
    return ""


def _has_token_in_args(segment: str, *targets: str, platform: str | None = None) -> bool:
    """检查命令段参数中是否包含指定 token。"""
    tokens = _tokenize_segment(segment, platform)
    for token in tokens[1:]:
        stripped = token.lstrip("-")
        if stripped in targets:
            return True
    return False


def _has_scriptblock(segment: str) -> bool:
    """检测 PowerShell ScriptBlock（含 ``{``），一律不放行。"""
    return "{" in segment


def is_safe_command_root(segment: str, *, platform: str | None = None) -> bool:
    """判断命令根是否在当前平台的白名单中。"""
    if not segment or not segment.strip():
        return False
    if _has_scriptblock(segment):
        return False

    root = get_command_root(segment)
    if not root:
        return False

    safe_roots = safe_commands_for_platform(platform)
    if root in safe_roots:
        return True
    # PowerShell cmdlet 大小写不敏感
    root_lower = root.lower()
    for candidate in safe_roots:
        if "-" in candidate and candidate.lower() == root_lower:
            return True

    if root == "git":
        sub = _extract_git_subcommand(segment, platform)
        if not sub:
            return False
        if sub == "stash":
            if _has_token_in_args(segment, "drop", "pop", "clear", platform=platform):
                return False
            return True
        if sub == "branch":
            if _has_token_in_args(segment, "d", "D", platform=platform):
                return False
            return True
        return sub in SAFE_GIT_SUBCOMMANDS

    if root in SAFE_SEARCH_COMMANDS:
        return True

    return False


def has_dangerous_args(segment: str, *, platform: str | None = None) -> bool:
    """检查安全白名单命令是否携带危险参数。"""
    if not segment or not segment.strip():
        return False

    tokens = _tokenize_segment(segment, platform)
    for token in tokens:
        for pattern in _DANGEROUS_ARG_REGEX:
            if pattern.match(token):
                logger.debug("检测到危险参数 %r (模式 %s)", token, pattern.pattern)
                return True
    return False


def is_safe_command(segment: str, *, platform: str | None = None) -> bool:
    """综合判断一条命令段是否安全可自动放行。

    判定流程：
    1. ScriptBlock（含 ``{``）→ ``False``
    2. 命令根不在平台白名单 → ``False``
    3. 携带危险参数 → ``False``
    4. 全部通过 → ``True``
    """
    if _has_scriptblock(segment):
        logger.info("命令含 ScriptBlock，拒绝放行: %r", segment)
        return False

    if not is_safe_command_root(segment, platform=platform):
        return False

    if has_dangerous_args(segment, platform=platform):
        logger.info("白名单命令携带危险参数，拒绝放行: %r", segment)
        return False

    return True
