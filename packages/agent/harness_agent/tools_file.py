"""文件操作工具：提供 delete_file 和 apply_patch 能力，扩展 Agent 的文件管理手段。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def delete_file(file_path: str, workspace_root: str) -> dict[str, Any]:
    """删除指定文件。

    Args:
        file_path: 要删除的文件路径（相对于工作区根目录，以 / 开头）。
        workspace_root: 工作区根目录的绝对路径。

    Returns:
        {"success": True, "deleted": str} 或 {"success": False, "error": str}
    """
    try:
        root = Path(workspace_root).resolve()
        target = (root / file_path.lstrip("/")).resolve()

        # 安全检查：防止路径穿越
        if not str(target).startswith(str(root)):
            return {"success": False, "error": "路径穿越：目标路径不在工作区内"}

        if not target.exists():
            return {"success": False, "error": f"文件不存在：{file_path}"}

        if target.is_dir():
            return {"success": False, "error": f"不允许删除目录：{file_path}"}

        target.unlink()
        return {"success": True, "deleted": file_path}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def apply_patch(patch: str, workspace_root: str) -> dict[str, Any]:
    """应用 unified diff 格式的补丁。

    Args:
        patch: unified diff 格式的补丁内容。
        workspace_root: 工作区根目录的绝对路径。

    Returns:
        {"success": True, "files_modified": list[str]} 或 {"success": False, "error": str}
    """
    try:
        file_diffs = _parse_unified_diff(patch)
        if not file_diffs:
            return {"success": False, "error": "无法解析补丁：未找到有效的 diff 内容"}

        root = Path(workspace_root).resolve()
        files_modified: list[str] = []

        # 先校验所有路径安全性，再执行修改（避免部分应用）
        for diff in file_diffs:
            for key in ("old_path", "new_path"):
                path_str = diff.get(key)
                if path_str and path_str != "/dev/null":
                    resolved = (root / path_str.lstrip("/")).resolve()
                    if not str(resolved).startswith(str(root)):
                        return {"success": False, "error": f"路径穿越：{path_str} 不在工作区内"}

        # 逐文件应用补丁
        for diff in file_diffs:
            old_path = diff.get("old_path", "/dev/null")
            new_path = diff.get("new_path", "/dev/null")

            if new_path == "/dev/null":
                # 删除文件
                target = (root / old_path.lstrip("/")).resolve()
                if target.exists():
                    target.unlink()
                    files_modified.append(old_path)
            elif old_path == "/dev/null":
                # 创建新文件
                target = (root / new_path.lstrip("/")).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                content = _build_new_file_content(diff["hunks"])
                target.write_text(content, encoding="utf-8")
                files_modified.append(new_path)
            else:
                # 修改现有文件
                target = (root / new_path.lstrip("/")).resolve()
                if not target.exists():
                    return {"success": False, "error": f"文件不存在：{new_path}"}
                original = target.read_text(encoding="utf-8")
                patched = _apply_hunks(original, diff["hunks"])
                target.write_text(patched, encoding="utf-8")
                files_modified.append(new_path)

        return {"success": True, "files_modified": files_modified}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _parse_unified_diff(patch: str) -> list[dict[str, Any]]:
    """解析 unified diff 为结构化数据。

    返回列表，每个元素代表一个文件的 diff，包含：
    - old_path: 原文件路径（/dev/null 表示新文件）
    - new_path: 新文件路径（/dev/null 表示删除）
    - hunks: hunk 列表，每个 hunk 包含 old_start、lines（带前缀的行列表）
    """
    files: list[dict[str, Any]] = []
    lines = patch.splitlines()
    i = 0

    while i < len(lines):
        # 寻找 --- 行
        if lines[i].startswith("--- "):
            old_path = _extract_path(lines[i][4:])
            new_path = None
            i += 1
            if i < len(lines) and lines[i].startswith("+++ "):
                new_path = _extract_path(lines[i][4:])
                i += 1
            else:
                i += 1
                continue

            hunks: list[dict[str, Any]] = []
            # 解析后续 hunk
            while i < len(lines) and lines[i].startswith("@@"):
                match = _HUNK_HEADER.match(lines[i])
                if not match:
                    i += 1
                    continue
                old_start = int(match.group(1))
                i += 1
                hunk_lines: list[str] = []
                while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("--- "):
                    hunk_lines.append(lines[i])
                    i += 1
                hunks.append({"old_start": old_start, "lines": hunk_lines})

            files.append({"old_path": old_path, "new_path": new_path, "hunks": hunks})
        else:
            i += 1

    return files


def _extract_path(raw: str) -> str:
    """从 diff 路径字段中提取文件路径，去除 a/ b/ 前缀。"""
    path = raw.strip()
    # 去除可能的制表符后的时间戳
    if "\t" in path:
        path = path.split("\t")[0]
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def _build_new_file_content(hunks: list[dict[str, Any]]) -> str:
    """从 hunk 中提取新文件内容（仅 + 行）。"""
    content_lines: list[str] = []
    for hunk in hunks:
        for line in hunk["lines"]:
            if line.startswith("+"):
                content_lines.append(line[1:])
    return "\n".join(content_lines) + "\n" if content_lines else ""


def _apply_hunks(original: str, hunks: list[dict[str, Any]]) -> str:
    """按 hunk 逐段应用替换到原始内容。

    从后向前应用，避免行号偏移影响后续 hunk。
    """
    lines = original.splitlines()
    # 从后向前应用各 hunk
    for hunk in reversed(hunks):
        old_start = hunk["old_start"] - 1  # 转为 0-based 索引
        old_count = 0
        new_lines: list[str] = []

        for line in hunk["lines"]:
            if line.startswith("-"):
                old_count += 1
            elif line.startswith("+"):
                new_lines.append(line[1:])
            elif line.startswith(" "):
                old_count += 1
                new_lines.append(line[1:])
            else:
                # 无前缀视为上下文行
                old_count += 1
                new_lines.append(line)

        lines[old_start:old_start + old_count] = new_lines

    return "\n".join(lines) + "\n" if lines else ""
