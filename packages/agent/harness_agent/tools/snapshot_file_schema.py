"""Snapshot 文件工具的模型 schema、参数校验与注册 placeholder。"""

from __future__ import annotations

from typing import Any

from deepagents.backends.utils import validate_path
from deepagents.middleware.filesystem import GlobSchema, GrepSchema, LsSchema
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from harness_agent.tools.file_tools import FileToolContractError

DEFAULT_READ_LIMIT = 100
"""与既有 read_file 相同的默认读取行数。"""

MAX_READ_LIMIT = 200
"""单次模型上下文允许的最大源行数。"""


class CanonicalReadFileSchema(BaseModel):
    """Snapshot read_file 的唯一模型参数。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="以 / 开头、相对于工作区根目录的文件路径。")
    offset: int = Field(default=0, ge=0, description="从 0 开始的源行偏移；返回行号从 1 开始。")
    limit: int = Field(
        default=DEFAULT_READ_LIMIT,
        ge=1,
        le=MAX_READ_LIMIT,
        description=f"读取的最大源行数，最多 {MAX_READ_LIMIT} 行。",
    )


class CanonicalWriteFileSchema(BaseModel):
    """只创建新文件的 write_file 参数。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="以 / 开头、相对于工作区根目录的新文件路径。")
    content: str = Field(description="要创建的 UTF-8 文本内容；已有文件不会被覆盖。")


class CanonicalEditFileSchema(BaseModel):
    """exact-string + prior-read edit_file 参数。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="以 / 开头、相对于工作区根目录的已有文件路径。")
    snapshot_id: str = Field(description="由同一 Thread 的 read_file 返回的 Snapshot ID。")
    old_string: str = Field(description="从已读内容复制的唯一原文本；不支持 replace_all。")
    new_string: str = Field(description="替换 old_string 的新文本；可为空以删除该文本。")


class CanonicalDeleteFileSchema(BaseModel):
    """必须绑定完整已读 Snapshot 的 delete_file 参数。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="以 / 开头、相对于工作区根目录的已有文件路径。")
    snapshot_id: str = Field(description="由同一 Thread 完整 read_file 返回的 Snapshot ID。")


_CANONICAL_SCHEMAS = {
    "read_file": CanonicalReadFileSchema,
    "write_file": CanonicalWriteFileSchema,
    "edit_file": CanonicalEditFileSchema,
    "delete_file": CanonicalDeleteFileSchema,
}
_CANONICAL_ARGUMENT_KEYS = {
    name: frozenset(schema.model_fields) for name, schema in _CANONICAL_SCHEMAS.items()
}


def create_file_tool_definitions() -> tuple[BaseTool, ...]:
    """创建主、子 Agent 共用的一套 canonical 文件工具 definition。"""
    definitions = (
        ("ls", "列出一个以 / 开头的目录路径。", LsSchema),
        (
            "read_file",
            "读取文件局部内容；可安全编辑的 UTF-8 文本会返回当前 Thread Snapshot。",
            CanonicalReadFileSchema,
        ),
        ("write_file", "只创建新的 UTF-8 文本文件；目标已存在时失败。", CanonicalWriteFileSchema),
        (
            "edit_file",
            "先读取，再用同一 Thread Snapshot 将唯一 old_string 替换为 new_string。",
            CanonicalEditFileSchema,
        ),
        ("glob", "查找匹配 glob 的文件。", GlobSchema),
        ("grep", "搜索文件中的字面文本。", GrepSchema),
        (
            "delete_file",
            "仅删除同一 Thread 已完整读取且 Snapshot 仍为当前版本的文件。",
            CanonicalDeleteFileSchema,
        ),
    )
    return tuple(
        _placeholder_tool(name=name, description=description, args_schema=args_schema)
        for name, description, args_schema in definitions
    )


def _placeholder_tool(*, name: str, description: str, args_schema: type[BaseModel]) -> BaseTool:
    """创建只作 schema/ToolNode registration 的 fail-closed placeholder。"""

    def fail_closed(**_kwargs: Any) -> str:
        """interposition 被移除时绝不让 placeholder 写入文件。"""
        raise FileToolContractError("FILE_TOOL_INTERPOSITION_REQUIRED")

    return StructuredTool.from_function(
        func=fail_closed,
        name=name,
        description=description,
        infer_schema=False,
        args_schema=args_schema,
    )


def path_argument(args: dict[str, Any], field: str, name: str) -> str:
    """验证 DeepAgents virtual path，并保护 /.harness 只读命名空间。"""
    value = args.get(field)
    if not isinstance(value, str) or not value:
        raise FileToolContractError("FILE_TOOL_PATH_INVALID")
    try:
        path = validate_path(value)
    except ValueError as exc:
        raise FileToolContractError("FILE_TOOL_PATH_INVALID") from exc
    if is_harness_virtual_path(path) and name != "read_file":
        raise FileToolContractError("VIRTUAL_READONLY")
    return path


def optional_path_argument(args: dict[str, Any], field: str, name: str) -> str | None:
    """验证 glob/grep 的可选虚拟搜索根。"""
    return None if args.get(field) is None else path_argument(args, field, name)


def string_argument(args: dict[str, Any], name: str) -> str:
    """读取必需字符串参数。"""
    value = args.get(name)
    if not isinstance(value, str):
        raise FileToolContractError("FILE_TOOL_ARGUMENT_INVALID")
    return value


def require_canonical_argument_keys(name: str, args: dict[str, Any]) -> None:
    """拒绝 canonical 文件 mutation/read 调用中的任何未声明字段。"""
    allowed = _CANONICAL_ARGUMENT_KEYS.get(name)
    if allowed is not None and not set(args).issubset(allowed):
        raise FileToolContractError("FILE_TOOL_SCHEMA_INVALID")


def snapshot_id_argument(args: dict[str, Any]) -> str:
    """读取 edit/delete 强制要求的非空 Snapshot ID。"""
    value = args.get("snapshot_id")
    if not isinstance(value, str) or not value:
        raise FileToolContractError("SNAPSHOT_REQUIRED")
    return value


def non_negative_int_argument(value: object, name: str) -> int:
    """读取非负整数参数，拒绝 bool。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FileToolContractError(f"FILE_TOOL_{name.upper()}_INVALID")
    return value


def positive_int_argument(value: object, name: str) -> int:
    """读取正整数参数，拒绝 bool。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FileToolContractError(f"FILE_TOOL_{name.upper()}_INVALID")
    return value


def is_harness_virtual_path(path: str) -> bool:
    """判断只读 /.harness 虚拟路由。"""
    return path == "/.harness" or path.startswith("/.harness/")


__all__ = [
    "CanonicalDeleteFileSchema",
    "CanonicalEditFileSchema",
    "CanonicalReadFileSchema",
    "CanonicalWriteFileSchema",
    "DEFAULT_READ_LIMIT",
    "MAX_READ_LIMIT",
    "create_file_tool_definitions",
    "is_harness_virtual_path",
    "non_negative_int_argument",
    "optional_path_argument",
    "path_argument",
    "positive_int_argument",
    "require_canonical_argument_keys",
    "snapshot_id_argument",
    "string_argument",
]
