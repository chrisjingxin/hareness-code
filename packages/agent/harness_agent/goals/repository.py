"""Goal proposal 使用的有界只读仓库工具。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langchain_core.tools import StructuredTool

from harness_agent.goals.models import GoalStoreError

GOAL_MAX_REPOSITORY_TOOL_CALLS = 25
GOAL_MAX_REPOSITORY_TOOL_RESULT_CHARS = 12_000
GOAL_MAX_REPOSITORY_CONTEXT_CHARS = 32_000
_GOAL_REPOSITORY_RESULT_TOO_LARGE = (
    "ERROR: repository result exceeds 12000 characters; "
    "narrow the path, pattern, or glob"
)


@dataclass(slots=True)
class GoalRepositoryBudget:
    """跨一次 proposal operation 统计只读调用和返回文本预算。"""

    calls: int = 0
    result_chars: int = 0
    violation: str | None = None
    limited_results: int = 0
    last_tool: str | None = None
    last_result_chars: int | None = None

    def consume(self, result: str) -> str:
        """记录一次工具结果；任何超限都会锁定稳定错误并 fail closed。"""
        self.calls += 1
        if self.calls > GOAL_MAX_REPOSITORY_TOOL_CALLS:
            self.violation = "GOAL_REPOSITORY_TOOL_LIMIT"
        elif len(result) > GOAL_MAX_REPOSITORY_TOOL_RESULT_CHARS:
            self.violation = "GOAL_REPOSITORY_RESULT_LIMIT"
        elif self.result_chars + len(result) > GOAL_MAX_REPOSITORY_CONTEXT_CHARS:
            self.violation = "GOAL_REPOSITORY_CONTEXT_LIMIT"
        if self.violation is not None:
            raise GoalStoreError(self.violation)
        self.result_chars += len(result)
        return result

    def forbid(self) -> None:
        """记录越过绑定 workspace 的读取企图。"""
        self.calls += 1
        self.violation = "GOAL_REPOSITORY_TOOL_FORBIDDEN"
        raise GoalStoreError(self.violation)

    def validate(self) -> None:
        """模型即使在工具错误后继续给出草案，也不得绕过既有越权/超限。"""
        if self.violation is not None:
            raise GoalStoreError(self.violation)

    def diagnostic_fields(self) -> dict[str, object]:
        """返回不含查询参数和正文的仓库工具诊断统计。"""
        fields: dict[str, object] = {
            "repository_calls": self.calls,
            "repository_result_chars": self.result_chars,
            "repository_limited_results": self.limited_results,
        }
        if self.last_tool is not None:
            fields["last_repository_tool"] = self.last_tool
        if self.last_result_chars is not None:
            fields["last_repository_result_chars"] = self.last_result_chars
        return fields


def create_goal_repository_tools(
    workspace: Path,
    budget: GoalRepositoryBudget,
) -> tuple[StructuredTool, ...]:
    """为固定 workspace 建立 ls/read_file/glob/grep 四个只读工具。"""
    root = workspace.expanduser().resolve()

    def resolve_path(value: str) -> Path:
        candidate = (root / value.lstrip("/")).resolve()
        if candidate != root and root not in candidate.parents:
            budget.forbid()
        return candidate

    def finish(tool_name: str, operation: Callable[[], str]) -> str:
        budget.last_tool = tool_name
        budget.last_result_chars = None
        try:
            result = operation()
            budget.last_result_chars = len(result)
            # 宽查询属于可恢复的模型操作失误：不给模型注入不完整正文，而是要求它
            # 缩小范围后重试。越界、调用次数和累计上下文仍由 budget fail closed。
            if len(result) > GOAL_MAX_REPOSITORY_TOOL_RESULT_CHARS:
                budget.limited_results += 1
                result = _GOAL_REPOSITORY_RESULT_TOO_LARGE
            return budget.consume(result)
        except GoalStoreError:
            raise
        except (OSError, UnicodeError, ValueError):
            return budget.consume("ERROR: repository path is unavailable")

    def ls(path: str = "/") -> str:
        """列出工作区内一个目录的直接子项。"""
        def operation() -> str:
            target = resolve_path(path)
            if not target.is_dir():
                return "ERROR: directory is unavailable"
            entries = [
                f"{item.name}{'/' if item.is_dir() else ''}"
                for item in sorted(target.iterdir(), key=lambda item: item.name)
            ]
            return "\n".join(entries)

        return finish("ls", operation)

    def read_file(file_path: str, offset: int = 0, limit: int = 2_000) -> str:
        """按行读取工作区内的文本文件。"""
        def operation() -> str:
            if offset < 0 or limit < 1 or limit > 2_000:
                return "ERROR: offset or limit is invalid"
            target = resolve_path(file_path)
            if not target.is_file():
                return "ERROR: file is unavailable"
            lines: list[str] = []
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    if index < offset:
                        continue
                    if index >= offset + limit:
                        break
                    lines.append(line)
            return "".join(lines)

        return finish("read_file", operation)

    def glob(pattern: str, path: str = "/") -> str:
        """在工作区目录内按 glob 模式列出相对路径。"""
        def operation() -> str:
            if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                budget.forbid()
            base = resolve_path(path)
            if not base.is_dir():
                return "ERROR: directory is unavailable"
            matches: list[str] = []
            for match in sorted(base.glob(pattern)):
                resolved = match.resolve()
                if resolved != root and root not in resolved.parents:
                    # 工作区扫描可能自然遇到依赖目录中的外部 symlink；跳过目标，
                    # 但不把模型发起的工作区内 glob 误判为主动越界。
                    continue
                matches.append("/" + resolved.relative_to(root).as_posix())
            return "\n".join(matches)

        return finish("glob", operation)

    def grep(pattern: str, path: str = "/", glob: str = "**/*") -> str:
        """在工作区文本文件中按正则搜索并返回相对路径与行号。"""
        def operation() -> str:
            expression = re.compile(pattern)
            base = resolve_path(path)
            candidates = [base] if base.is_file() else base.glob(glob)
            matches: list[str] = []
            for candidate in candidates:
                resolved = candidate.resolve()
                if resolved != root and root not in resolved.parents:
                    # 不跟随扫描得到的外部 symlink；显式 path 仍由 resolve_path 拒绝。
                    continue
                if not resolved.is_file():
                    continue
                try:
                    with resolved.open("r", encoding="utf-8", errors="replace") as handle:
                        for line_number, line in enumerate(handle, start=1):
                            if expression.search(line):
                                relative = resolved.relative_to(root).as_posix()
                                matches.append(f"/{relative}:{line_number}:{line.rstrip()}")
                except OSError:
                    continue
            return "\n".join(matches)

        return finish("grep", operation)

    return (
        StructuredTool.from_function(ls, name="ls"),
        StructuredTool.from_function(read_file, name="read_file"),
        StructuredTool.from_function(glob, name="glob"),
        StructuredTool.from_function(grep, name="grep"),
    )


__all__ = [
    "GOAL_MAX_REPOSITORY_CONTEXT_CHARS",
    "GOAL_MAX_REPOSITORY_TOOL_CALLS",
    "GOAL_MAX_REPOSITORY_TOOL_RESULT_CHARS",
    "GoalRepositoryBudget",
    "create_goal_repository_tools",
]
