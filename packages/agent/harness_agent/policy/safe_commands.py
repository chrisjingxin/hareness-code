"""安全命令白名单：在 default 模式下自动放行只读安全的 shell 命令。

本模块定义了只读命令、Git 子命令和搜索命令的三级白名单，
以及危险参数检测和综合安全判断逻辑。白名单判定基于命令根和
参数模式，不依赖运行时沙箱或用户审批。
"""

from __future__ import annotations

import logging
import re
import shlex

from harness_agent.policy.bash_parser import get_command_root

logger = logging.getLogger(__name__)

# ===================================================================
# 1. 只读文件查看命令白名单
# ===================================================================

ALWAYS_SAFE_COMMANDS: frozenset[str] = frozenset({
    "ls", "cat", "pwd", "whoami", "head", "tail", "wc", "sort", "uniq",
    "tr", "cut", "echo", "date", "uname", "df", "du", "free", "uptime",
    "which", "whereis", "file", "stat", "id", "groups", "hostname",
    "printenv", "basename", "dirname", "realpath", "readlink",
})

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
    # find 执行外部命令
    re.compile(r"^-exec$"),
    re.compile(r"^--exec$"),
    # rg / ripgrep 执行外部预处理器
    re.compile(r"^--pre$"),
    re.compile(r"^--preview-script$"),
    # 某些工具的 run 模式（deno / git hooks 等）
    re.compile(r"^--allow-run$"),
    # find 交互式执行（-ok / -okdir 会逐文件询问后执行）
    re.compile(r"^-ok$"),
    re.compile(r"^-okdir$"),
    # --output 后跟重定向路径
    re.compile(r"^--output$"),
    # xargs 替换模式（可执行任意命令）
    re.compile(r"^--replace$"),
    # -I 作为 xargs 替换模式标志
    re.compile(r"^-I$"),
]


def _tokenize_segment(segment: str) -> list[str]:
    """将命令段拆分为 token 列表。

    使用 shlex 进行 POSIX 兼容的 shell 分词，失败时回退到空白分割。
    """
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _extract_git_subcommand(segment: str) -> str:
    """从 git 命令段中提取子命令。

    跳过 ``git`` 本身和前导 flag（如 ``-C``），返回第一个非 flag 参数作为子命令。
    对于 ``git --no-pager log`` 等场景，跳过已知的 git 全局选项。
    """
    tokens = _tokenize_segment(segment)
    if len(tokens) < 2:
        return ""

    # git 全局选项（不消耗子命令位置）
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
        # 跳过全局 flag
        if token in _GIT_GLOBAL_FLAGS:
            i += 1
            # -c 和 --config 需要额外跳过下一个 token（值）
            if token in ("-c", "--config") and i < len(tokens):
                i += 1
            continue
        # 跳过 name=value 形式
        if "=" in token:
            i += 1
            continue
        # 第一个非 flag、非赋值的 token 就是子命令
        return token

    return ""


def _has_token_in_args(segment: str, *targets: str) -> bool:
    """检查命令段参数中是否包含指定 token。

    匹配时忽略前导短横线前缀差异（如 ``--drop`` 中的 ``drop``），
    仅对非 flag 参数进行精确匹配。
    """
    tokens = _tokenize_segment(segment)
    # 跳过命令名本身
    for token in tokens[1:]:
        # 移除前导短横线，检查是否为危险动作名
        stripped = token.lstrip("-")
        if stripped in targets:
            return True
    return False


def is_safe_command_root(segment: str) -> bool:
    """判断命令根（第一个词）是否在白名单中。

    判定逻辑：
    1. 提取命令根（使用 ``get_command_root``）
    2. 命令根在 ``ALWAYS_SAFE_COMMANDS`` → ``True``
    3. 命令根是 ``git``：
       - 子命令在 ``SAFE_GIT_SUBCOMMANDS`` → ``True``
       - 子命令为 ``stash`` 且参数包含 ``drop``/``pop``/``clear`` → ``False``
       - 子命令为 ``branch`` 且参数包含 ``-D``/``-d`` → ``False``
       - 其他 git 子命令 → ``False``
    4. 命令根在 ``SAFE_SEARCH_COMMANDS`` → ``True``（需进一步参数检查）

    Args:
        segment: 命令字符串。

    Returns:
        命令根在白名单中返回 ``True``，否则返回 ``False``。

    Examples:
        ``"ls -la"`` → ``True``
        ``"git log --oneline"`` → ``True``
        ``"git stash drop"`` → ``False``
        ``"rm -rf /"`` → ``False``
    """
    if not segment or not segment.strip():
        return False

    root = get_command_root(segment)
    if not root:
        return False

    # 检查总是安全的命令
    if root in ALWAYS_SAFE_COMMANDS:
        logger.debug("命令根 %r 在 ALWAYS_SAFE_COMMANDS 中", root)
        return True

    # 检查 git 子命令
    if root == "git":
        sub = _extract_git_subcommand(segment)
        if not sub:
            return False

        # stash 仅 list/show 安全，其他操作（drop/pop/clear）危险
        if sub == "stash":
            if _has_token_in_args(segment, "drop", "pop", "clear"):
                logger.debug("git stash 携带危险动作: %r", segment)
                return False
            return True

        # branch -d/-D 删除分支，危险
        if sub == "branch":
            if _has_token_in_args(segment, "d", "D"):
                logger.debug("git branch 携带删除 flag: %r", segment)
                return False
            return True

        if sub in SAFE_GIT_SUBCOMMANDS:
            return True

        return False

    # 搜索命令需要进一步参数检查
    if root in SAFE_SEARCH_COMMANDS:
        logger.debug("搜索命令 %r 需要参数检查", root)
        return True

    return False


def has_dangerous_args(segment: str) -> bool:
    """检查安全白名单命令是否携带危险参数。

    使用正则模式匹配以下危险参数：
    - ``--exec`` / ``-exec``（find 执行外部命令）
    - ``--pre`` / ``--preview-script``（rg 执行预处理器）
    - ``--allow-run``（某些工具的 run 模式）
    - ``-ok`` / ``-okdir``（find 交互执行）
    - ``--output``（后跟可能的重定向路径）
    - ``--replace`` / ``-I``（xargs 替换模式）

    Args:
        segment: 命令字符串。

    Returns:
        如果携带危险参数返回 ``True``，否则返回 ``False``。

    Examples:
        ``"find . -exec rm {} \\;"`` → ``True``
        ``"rg --pre 'echo evil' pattern"`` → ``True``
        ``"find . -name '*.py'"`` → ``False``
    """
    if not segment or not segment.strip():
        return False

    tokens = _tokenize_segment(segment)

    for token in tokens:
        for pattern in _DANGEROUS_ARG_REGEX:
            if pattern.match(token):
                logger.debug("检测到危险参数 %r (模式 %s)", token, pattern.pattern)
                return True

    return False


def is_safe_command(segment: str) -> bool:
    """综合判断一条命令段是否安全可自动放行。

    判定流程：
    1. 调用 ``is_safe_command_root`` → 不在白名单 → ``False``
    2. 在白名单中 → 调用 ``has_dangerous_args`` → 有危险参数 → ``False``
    3. 全部通过 → ``True``

    Args:
        segment: 命令字符串。

    Returns:
        命令安全可自动放行返回 ``True``，否则返回 ``False``。

    Examples:
        ``"ls -la"`` → ``True``
        ``"git log --oneline"`` → ``True``
        ``"find . -exec rm {} \\;"`` → ``False``（携带 -exec）
        ``"rm -rf /"`` → ``False``（不在白名单）
    """
    if not is_safe_command_root(segment):
        return False

    if has_dangerous_args(segment):
        logger.info("白名单命令携带危险参数，拒绝放行: %r", segment)
        return False

    return True
