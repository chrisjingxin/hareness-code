"""敏感路径保护模块。

本模块定义了用户主目录中常见的敏感配置文件和目录名称，并在写操作工具
执行前判断目标路径是否命中敏感路径。命中时应触发额外的安全审批流程，
防止 Agent 意外修改 ``.git/config``、``.bashrc`` 等关键文件。

扩展功能：
- 符号链接安全解析（``resolve_symlink_safe``）：解析符号链接并返回真实路径。
- 工作区越界检测（``is_outside_workspace``）：判断路径是否经符号链接指向工作区外。
- 绝对路径深度检查（``is_shallow_absolute_delete``）：检测 Delete 操作是否指向层级过浅
  的绝对路径（如 ``/``、``/home``、``C:\\``）。
- 受保护编辑路径判断（``is_protected_edit_path``）：综合敏感路径和 ``.github/workflows/``
  前缀检测，即使有 allow 规则也强制进入 ask 审批流程。
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

# 敏感文件清单与规格对齐：Shell/Git 个人配置与构建入口（Makefile）。
# package.json、.gitignore 等日常频繁编辑的项目文件不在此列，避免 default
# 模式下正常编辑也被强制弹窗。
SENSITIVE_FILES: frozenset[str] = frozenset({
    ".gitconfig",
    ".bashrc",
    ".zshrc",
    ".bash_profile",
    ".zprofile",
    ".profile",
    ".mcp.json",
    ".ripgreprc",
    "Makefile",
})

SENSITIVE_DIRECTORIES: frozenset[str] = frozenset({
    ".git",
    ".vscode",
    ".idea",
    ".harness",
    ".husky",
})

_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "delete_file",
})


def is_sensitive_path(file_path: str) -> bool:
    """判断给定路径是否命中敏感文件或敏感目录。

    将路径中的反斜杠统一为正斜杠后按 POSIX 语义拆分，检查文件名是否
    属于 ``SENSITIVE_FILES``，以及任意目录组件是否属于
    ``SENSITIVE_DIRECTORIES``。还会检测 ``.github/workflows/`` 前缀。

    Args:
        file_path: 待检测的文件路径，支持 ``/`` 和 ``\\`` 分隔符。

    Returns:
        路径命中敏感文件或敏感目录时返回 ``True``，否则返回 ``False``。
    """
    normalized = file_path.replace("\\", "/")

    # 检测 .github/workflows/ 前缀
    if (normalized.startswith(".github/workflows/")
            or "/.github/workflows/" in normalized):
        return True

    parts = PurePosixPath(normalized).parts

    # 文件名（最后一个组件）命中敏感文件列表
    if parts and parts[-1] in SENSITIVE_FILES:
        return True

    # 任意目录组件命中敏感目录列表
    for part in parts:
        if part in SENSITIVE_DIRECTORIES:
            return True

    return False


def requires_safety_check(tool_name: str, tool_args: dict) -> bool:
    """判断指定工具调用是否需要触发敏感路径安全检查。

    仅对写操作工具（``write_file``、``edit_file``、``delete_file``）检查其
    ``file_path`` 参数；只读工具一律放行。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典，预期包含 ``file_path`` 键。

    Returns:
        写操作工具的目标路径为敏感路径时返回 ``True``，否则返回 ``False``。
    """
    if tool_name not in _WRITE_TOOLS:
        return False

    file_path = tool_args.get("file_path")
    if not file_path or not isinstance(file_path, str):
        return False

    return is_sensitive_path(file_path)


def resolve_symlink_safe(path: str | Path, workspace_root: str | Path) -> Path | None:
    """安全解析符号链接并返回真实路径。

    使用 ``os.path.realpath()`` 解析路径中所有符号链接组件，
    不抛出异常。解析失败（例如文件不存在）时返回 ``None``。

    Args:
        path: 待解析的路径，可以是字符串或 ``Path`` 对象。
        workspace_root: 工作区根路径（未使用，保留用于接口一致性）。

    Returns:
        解析成功时返回解析后的 ``Path``，失败时返回 ``None``。
    """
    try:
        return Path(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return None


def is_outside_workspace(target_path: str | Path, workspace_root: str | Path) -> bool:
    """判断目标路径是否在工作区之外。

    先对 ``target_path`` 和 ``workspace_root`` 做 ``realpath`` 解析，
    然后使用 ``Path.is_relative_to`` 判断解析后的目标路径是否在工作区
    子树内。如果 ``realpath`` 解析失败（文件尚不存在），退回到检查
    相对路径是否以 ``..`` 开头。

    Args:
        target_path: 待检测的目标路径。
        workspace_root: 工作区根路径。

    Returns:
        目标路径经符号链接解析后越出工作区边界时返回 ``True``，
        否则返回 ``False``。
    """
    try:
        resolved_target = Path(os.path.realpath(str(target_path)))
        resolved_root = Path(os.path.realpath(str(workspace_root)))
    except (OSError, ValueError):
        # realpath 失败时退回到相对路径检查
        return _is_relative_outside(str(target_path), str(workspace_root))

    try:
        return not resolved_target.is_relative_to(resolved_root)
    except (OSError, ValueError):
        return _is_relative_outside(str(target_path), str(workspace_root))


def _is_relative_outside(target_path: str, workspace_root: str) -> bool:
    """退回检查：相对路径是否以 ``..`` 开头。"""
    try:
        rel = Path(target_path).resolve().relative_to(
            Path(workspace_root).resolve()
        )
        return str(rel).startswith("..")
    except (OSError, ValueError):
        # 无法确定时保守返回 True
        return True


def is_shallow_absolute_delete(file_path: str | Path) -> bool:
    """判断 Delete 操作的目标路径是否是层级过浅的绝对路径。

    绝对路径深度 ≤ 2（如 ``/``、``/home``、``C:\\``、``C:\\Users``）
    视为过浅。Windows 盘符根路径 ``C:\\`` 深度为 1。
    相对路径不检查，直接返回 ``False``。

    Args:
        file_path: 待检测的文件路径。

    Returns:
        绝对路径且深度 ≤ 2 时返回 ``True``，否则返回 ``False``。
    """
    fp = Path(str(file_path))
    # 相对路径不检查
    if not fp.is_absolute():
        return False
    # 顶层根目录（Linux / 或 Windows C:\）深度为 1，其直接子目录深度为 2
    return len(fp.parts) <= 2


def is_protected_edit_path(file_path: str | Path) -> bool:
    """判断编辑目标是否是受保护路径。

    受保护路径包括：
    - ``SENSITIVE_FILES`` / ``SENSITIVE_DIRECTORIES`` 命中的路径
    - ``.github/workflows/`` 前缀的路径

    命中时即使有 allow 规则也应强制进入 ask 审批流程。

    Args:
        file_path: 待检测的文件路径。

    Returns:
        路径属于受保护路径时返回 ``True``，否则返回 ``False``。
    """
    fp = str(file_path)
    return is_sensitive_path(fp)
