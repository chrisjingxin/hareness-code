"""Compose Workspace Markdown 的固定路径契约。

配置解析和文档存储必须共用这一处规则：文档根只能是规范化的工作空间相对
目录，slug 只能落在该根的一个直接子目录，文件名由文档 kind 固定决定。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Sequence

from harness_agent.compose.models import ComposeDocumentKind

DEFAULT_COMPOSE_DOCS_DIR = "docs/compose"
"""未配置时 Compose Markdown 的唯一工作空间相对根目录。"""

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){0,31}\Z")
_DOCUMENT_FILENAMES = {
    ComposeDocumentKind.TASK: "task.md",
    ComposeDocumentKind.SPEC: "spec.md",
    ComposeDocumentKind.PLAN: "plan.md",
    ComposeDocumentKind.TODO: "todo.md",
    ComposeDocumentKind.REPORT: "report.md",
}

_MAX_SLUG_CHARS = 48
"""可读 slug 的最大长度；截断后仍需满足 _SLUG 的段数约束。"""


class ComposeDocumentPathError(ValueError):
    """路径或 slug 不符合固定 Workspace 文档契约。"""


def normalize_compose_docs_dir(value: object) -> str:
    """返回唯一规范化的文档根；拒绝绝对路径、穿越与虚拟只读根。"""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ComposeDocumentPathError("COMPOSE_DOCS_DIR_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", "..", "~"} for part in path.parts):
        raise ComposeDocumentPathError("COMPOSE_DOCS_DIR_INVALID")
    normalized = path.as_posix()
    if value != normalized or path.parts[0] == ".harness":
        raise ComposeDocumentPathError("COMPOSE_DOCS_DIR_INVALID")
    return normalized


def compose_document_relative_path(
    docs_dir: str,
    slug: str,
    kind: ComposeDocumentKind,
) -> str:
    """由已经规范化的根、稳定 slug 和 kind 构造唯一相对文件路径。"""
    if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
        raise ComposeDocumentPathError("COMPOSE_DOCUMENT_PATH_INVALID")
    if not isinstance(kind, ComposeDocumentKind):
        raise ComposeDocumentPathError("COMPOSE_DOCUMENT_PATH_INVALID")
    return f"{normalize_compose_docs_dir(docs_dir)}/{slug}/{_DOCUMENT_FILENAMES[kind]}"


def make_compose_slug(goal: str, *, max_length: int = _MAX_SLUG_CHARS) -> str:
    """从首个目标文本生成稳定 slug；非 ASCII 目标回退到内容 hash 前缀。

    slug 只由目标文本决定（不随 Thread/时间变化），Runtime 再用同 Thread
    已占用 slug 解决冲突；中文等无法生成可读段的目标使用 ``work-<sha256
    前缀>``，保证目录稳定且不会为空。max_length 截断发生在分词之后，
    结果始终满足 ``[a-z0-9]+(?:-[a-z0-9]+)*`` 形状。
    """
    if not isinstance(goal, str):
        raise ComposeDocumentPathError("COMPOSE_SLUG_INVALID")
    parts = re.findall(r"[a-z0-9]+", goal.strip().lower())
    candidate = "-".join(parts)[:max_length].rstrip("-")
    if len(candidate) < 3:
        digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()
        candidate = f"work-{digest[:10]}"
    if _SLUG.fullmatch(candidate) is None:
        raise ComposeDocumentPathError("COMPOSE_SLUG_INVALID")
    return candidate

