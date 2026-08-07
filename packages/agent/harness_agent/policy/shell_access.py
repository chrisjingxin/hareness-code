"""Shell 命令对文件的读写触碰检测门。

本模块从 Shell 命令中提取输出重定向和写命令的目标路径，并对照工作区
路径规则进行安全检查。用于在 Shell 工具执行前判断是否会产生工作区外
的文件写入。
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# 输出重定向正则：匹配 >、>>、N>、&> 后接文件路径
# ---------------------------------------------------------------------------

_REDIRECT_PATTERN = re.compile(r"(?:^|\s)(?:>>?|\d>>?|&>>?)\s*(\S+)")

# ---------------------------------------------------------------------------
# tee 命令正则：匹配整段 tee 调用，后续参数中跳过 - 开头的选项
# ---------------------------------------------------------------------------

_TEE_PATTERN = re.compile(r"\btee\b([^|;&\n]+)")


def extract_write_paths(command: str) -> list[str]:
    """从 Shell 命令中提取输出重定向和写命令的目标路径。

    检测以下模式：

    - ``> filepath`` 或 ``>> filepath`` → 提取 ``filepath``
    - ``tee filepath`` → 提取 ``filepath``
    - ``dd of=filepath`` → 提取 ``filepath``

    使用简单字符串/正则匹配，不依赖 tree-sitter。

    Args:
        command: 待检测的 Shell 命令字符串。

    Returns:
        提取到的写目标路径列表。

    Examples:
        ``"echo data > /etc/config"`` → ``["/etc/config"]``
        ``"ls -la > /tmp/out.txt"`` → ``["/tmp/out.txt"]``
        ``"cat file | tee log.txt"`` → ``["log.txt"]``
    """
    paths: list[str] = []

    # 1. 输出重定向：> / >> / N> / &>
    for match in _REDIRECT_PATTERN.finditer(command):
        filepath = match.group(1)
        if _is_path_like(filepath):
            paths.append(filepath)

    # 2. tee 命令
    for match in _TEE_PATTERN.finditer(command):
        tee_args = match.group(1)
        for token in tee_args.split():
            # 跳过 - 开头的选项
            if token.startswith("-"):
                continue
            if _is_path_like(token):
                paths.append(token)

    # 3. dd of=filepath
    for token in _tokenize(command):
        if token.startswith("of=") and len(token) > 3:
            paths.append(token[3:])

    return paths


def evaluate_shell_file_access(
    command: str, workspace_root: str | None = None
) -> dict:
    """将提取的文件路径对照工作区路径规则进行安全检查。

    流程：
    1. 调用 :func:`extract_write_paths` 获取写目标路径列表
    2. 如果没有写目标 → 返回无文件访问
    3. 如果有 workspace_root，检查每个写目标是否在工作区外

    Args:
        command: 待检测的 Shell 命令字符串。
        workspace_root: 工作区根路径，为 ``None`` 时不检查工作区边界。

    Returns:
        包含以下字段的字典：

        - ``has_file_access``：是否存在写文件操作
        - ``outside_workspace``：是否有写目标在工作区外（无 workspace_root 时为 ``False``）
        - ``paths``：提取到的路径列表（仅当 ``has_file_access`` 为 ``True`` 时出现）
    """
    paths = extract_write_paths(command)

    if not paths:
        return {"has_file_access": False, "outside_workspace": False}

    if workspace_root is None:
        return {
            "has_file_access": True,
            "outside_workspace": False,
            "paths": paths,
        }

    try:
        root = Path(workspace_root).resolve()
    except (OSError, ValueError):
        # workspace_root 解析失败，保守认为在工作区外
        return {
            "has_file_access": True,
            "outside_workspace": True,
            "paths": paths,
        }

    for target in paths:
        try:
            resolved = Path(target)
            if not resolved.is_absolute():
                resolved = (root / target).resolve()
            else:
                resolved = resolved.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            # 任意一个目标不在工作区内
            return {
                "has_file_access": True,
                "outside_workspace": True,
                "paths": paths,
            }

    return {
        "has_file_access": True,
        "outside_workspace": False,
        "paths": paths,
    }


# ===================================================================
# 内部辅助函数
# ===================================================================


def _is_path_like(token: str) -> bool:
    """判断 token 是否看起来像文件路径（排除明显的非路径选项）。"""
    if not token:
        return False
    # 排除 shell 控制字符
    if token in ("|", ";", "&&", "||", "&", "2>&1", "1>&2"):
        return False
    # 排除纯数字文件描述符
    if token.isdigit():
        return False
    return True


def _tokenize(command: str) -> list[str]:
    """简单按空白和管道符/分隔符切分命令字符串。

    使用引号感知的状态机切分，确保引号内的空白不被拆分。
    """
    tokens: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if ch == "\\" and (in_double or not in_single):
            current.append(ch)
            i += 1
            if i < n:
                current.append(command[i])
            i += 1
            continue
        if not in_single and not in_double and ch in (" ", "\t", "|", ";", "&", "\n"):
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    if current:
        tokens.append("".join(current))
    return tokens
