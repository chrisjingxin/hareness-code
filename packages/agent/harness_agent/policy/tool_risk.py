"""工具风险分级：将工具名映射到风险类别，并定义各审批模式下的默认行为。"""

from __future__ import annotations

from enum import Enum


class ToolKind(Enum):
    """工具风险类别枚举。"""

    READ = "read"
    EDIT = "edit"
    DELETE = "delete"
    EXECUTE = "execute"
    AGENT = "agent"
    INTERACT = "interact"
    PLAN = "plan"
    FETCH = "fetch"


TOOL_KIND_MAP: dict[str, ToolKind] = {
    # READ
    "ls": ToolKind.READ,
    "read_file": ToolKind.READ,
    "glob": ToolKind.READ,
    "grep": ToolKind.READ,
    "web_search": ToolKind.READ,
    "lsp": ToolKind.READ,
    "tool_search": ToolKind.READ,
    "memory_search": ToolKind.READ,
    "task_output": ToolKind.READ,
    # EDIT
    "write_file": ToolKind.EDIT,
    "edit_file": ToolKind.EDIT,
    # DELETE
    "delete_file": ToolKind.DELETE,
    # EXECUTE
    "execute": ToolKind.EXECUTE,
    "monitor": ToolKind.EXECUTE,
    "task_stop": ToolKind.EXECUTE,
    # AGENT
    "task": ToolKind.AGENT,
    # INTERACT
    "ask_user": ToolKind.INTERACT,
    "write_todos": ToolKind.INTERACT,
    "memory_save": ToolKind.INTERACT,
    # PLAN
    "enter_plan_mode": ToolKind.PLAN,
    "exit_plan_mode": ToolKind.PLAN,
    # FETCH
    "web_fetch": ToolKind.FETCH,
}

# 各 ToolKind 在审批模式 plan/default/auto-edit/auto/yolo 下的默认行为
# auto 模式下标记为 "filter" 的类别将进入 AUTO 四层过滤器管线
KIND_MODE_PERMISSION: dict[ToolKind, dict[str, str]] = {
    ToolKind.READ: {
        "plan": "allow",
        "default": "allow",
        "auto-edit": "allow",
        "auto": "allow",
        "yolo": "allow",
    },
    ToolKind.EDIT: {
        "plan": "deny",
        "default": "ask",
        "auto-edit": "allow",
        "auto": "filter",
        "yolo": "allow",
    },
    ToolKind.DELETE: {
        "plan": "deny",
        "default": "ask",
        "auto-edit": "ask",
        "auto": "filter",
        "yolo": "allow",
    },
    ToolKind.EXECUTE: {
        "plan": "deny",
        "default": "ask",
        "auto-edit": "ask",
        "auto": "filter",
        "yolo": "allow",
    },
    ToolKind.AGENT: {
        "plan": "deny",
        "default": "ask",
        "auto-edit": "ask",
        "auto": "filter",
        "yolo": "allow",
    },
    ToolKind.INTERACT: {
        "plan": "allow",
        "default": "allow",
        "auto-edit": "allow",
        "auto": "allow",
        "yolo": "allow",
    },
    ToolKind.PLAN: {
        "plan": "allow",
        "default": "allow",
        "auto-edit": "allow",
        "auto": "allow",
        "yolo": "allow",
    },
    ToolKind.FETCH: {
        "plan": "allow",
        "default": "ask",
        "auto-edit": "ask",
        "auto": "filter",
        "yolo": "allow",
    },
}


def get_tool_kind(tool_name: str) -> ToolKind:
    """返回工具对应的风险类别；未知工具 fail-closed 归为 EXECUTE。"""
    return TOOL_KIND_MAP.get(tool_name, ToolKind.EXECUTE)


def is_read_only(tool_name: str) -> bool:
    """判断工具是否为只读（READ/INTERACT/PLAN 类别视为只读）。"""
    kind = get_tool_kind(tool_name)
    return kind in (ToolKind.READ, ToolKind.INTERACT, ToolKind.PLAN)


def get_mode_permission(kind: ToolKind, mode: str) -> str:
    """查询指定风险类别在给定审批模式下的默认行为。

    Args:
        kind: 工具风险类别。
        mode: 审批模式名称（plan/default/auto-edit/yolo）。

    Returns:
        "allow"、"ask" 或 "deny"。
    """
    return KIND_MODE_PERMISSION[kind][mode]
