"""敏感路径保护模块。

本模块定义了用户主目录中常见的敏感配置文件和目录名称，并在写操作工具
执行前判断目标路径是否命中敏感路径。命中时应触发额外的安全审批流程，
防止 Agent 意外修改 ``.git/config``、``.bashrc`` 等关键文件。
"""

from __future__ import annotations

from pathlib import PurePosixPath

SENSITIVE_FILES: frozenset[str] = frozenset({
    ".gitconfig",
    ".bashrc",
    ".zshrc",
    ".bash_profile",
    ".zprofile",
    ".profile",
    ".mcp.json",
    ".ripgreprc",
})

SENSITIVE_DIRECTORIES: frozenset[str] = frozenset({
    ".git",
    ".vscode",
    ".idea",
    ".harness",
})

_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "delete_file",
    "apply_patch",
})


def is_sensitive_path(file_path: str) -> bool:
    """判断给定路径是否命中敏感文件或敏感目录。

    将路径中的反斜杠统一为正斜杠后按 POSIX 语义拆分，检查文件名是否
    属于 ``SENSITIVE_FILES``，以及任意目录组件是否属于
    ``SENSITIVE_DIRECTORIES``。

    Args:
        file_path: 待检测的文件路径，支持 ``/`` 和 ``\\`` 分隔符。

    Returns:
        路径命中敏感文件或敏感目录时返回 ``True``，否则返回 ``False``。
    """
    normalized = file_path.replace("\\", "/")
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

    仅对写操作工具（``write_file``、``edit_file``、``delete_file``、
    ``apply_patch``）检查其 ``file_path`` 参数；只读工具一律放行。

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
