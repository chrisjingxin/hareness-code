"""文件工具名称、路径参数、风险类别与稳定 schema 的唯一目录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FileAccess = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class FileToolSpec:
    """描述一个文件相关工具在构图、Policy 和执行 seam 中的共同事实。"""

    name: str
    parameters: tuple[tuple[str, str], ...]
    path_argument: str
    access: FileAccess
    deepagents_builtin: bool = False
    handled_by_contract: bool = False
    resident: bool = False
    path_optional: bool = False

    def schema_shape(self) -> dict[str, object]:
        """返回参与 AgentEngine fingerprint 的确定性 schema 形状。"""
        return {"name": self.name, "parameters": dict(self.parameters)}


FILE_TOOL_SPECS = (
    FileToolSpec("ls", (("path", "string"),), "path", "read", True, True, True),
    FileToolSpec(
        "read_file",
        (("file_path", "string"), ("offset", "integer"), ("limit", "integer")),
        "file_path",
        "read",
        True,
        True,
        True,
    ),
    FileToolSpec(
        "write_file",
        (("file_path", "string"), ("content", "string")),
        "file_path",
        "write",
        True,
        True,
        True,
    ),
    FileToolSpec(
        "edit_file",
        (
            ("file_path", "string"),
            ("snapshot_id", "string"),
            ("old_string", "string"),
            ("new_string", "string"),
        ),
        "file_path",
        "write",
        True,
        True,
        True,
    ),
    FileToolSpec(
        "glob",
        (("pattern", "string"), ("path", "string")),
        "path",
        "read",
        True,
        True,
        True,
        path_optional=True,
    ),
    FileToolSpec(
        "grep",
        (("pattern", "string"), ("path", "string"), ("glob", "string")),
        "path",
        "read",
        True,
        True,
        True,
        path_optional=True,
    ),
    FileToolSpec(
        "delete_file",
        (("file_path", "string"), ("snapshot_id", "string")),
        "file_path",
        "write",
        handled_by_contract=True,
        resident=True,
    ),
    FileToolSpec(
        "lsp",
        (
            ("action", "string"),
            ("file_path", "string"),
            ("line", "integer"),
            ("column", "integer"),
        ),
        "file_path",
        "read",
    ),
)
"""所有文件相关工具的 canonical 元数据；不得在 Policy/Runtime 重复手写名称。"""

FILE_TOOL_SPECS_BY_NAME = {spec.name: spec for spec in FILE_TOOL_SPECS}
FILE_TOOL_NAMES = frozenset(FILE_TOOL_SPECS_BY_NAME)
FILESYSTEM_READ_TOOL_NAMES = frozenset(
    spec.name for spec in FILE_TOOL_SPECS if spec.access == "read"
)
FILESYSTEM_WRITE_TOOL_NAMES = frozenset(
    spec.name for spec in FILE_TOOL_SPECS if spec.access == "write"
)
FILE_TOOL_PATH_ARGUMENTS = {
    spec.name: spec.path_argument for spec in FILE_TOOL_SPECS
}
DIRECT_FILE_TOOL_PATH_ARGUMENTS = {
    spec.name: spec.path_argument for spec in FILE_TOOL_SPECS if not spec.path_optional
}
BUILTIN_FILE_TOOL_NAMES = frozenset(
    spec.name for spec in FILE_TOOL_SPECS if spec.deepagents_builtin
)
HARNESS_FILE_TOOL_NAMES = frozenset(
    spec.name for spec in FILE_TOOL_SPECS if spec.handled_by_contract
)
RESIDENT_FILE_TOOL_NAMES = frozenset(
    spec.name for spec in FILE_TOOL_SPECS if spec.resident
)
FILE_TOOL_SCHEMA_SHAPES = tuple(spec.schema_shape() for spec in FILE_TOOL_SPECS)


__all__ = [
    "BUILTIN_FILE_TOOL_NAMES",
    "FILESYSTEM_READ_TOOL_NAMES",
    "FILESYSTEM_WRITE_TOOL_NAMES",
    "DIRECT_FILE_TOOL_PATH_ARGUMENTS",
    "FILE_TOOL_NAMES",
    "FILE_TOOL_PATH_ARGUMENTS",
    "FILE_TOOL_SCHEMA_SHAPES",
    "FILE_TOOL_SPECS",
    "FILE_TOOL_SPECS_BY_NAME",
    "FileToolSpec",
    "HARNESS_FILE_TOOL_NAMES",
    "RESIDENT_FILE_TOOL_NAMES",
]
