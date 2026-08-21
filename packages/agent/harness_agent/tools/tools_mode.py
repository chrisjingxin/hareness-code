"""模式管理工具：提供计划模式状态管理能力。"""

from __future__ import annotations

from typing import Any


class PlanModeState:
    """计划模式状态管理器，跟踪当前是否处于计划模式及进入前的模式。"""

    def __init__(self, initial_mode: str = "default") -> None:
        self._active = False
        self._previous_mode = initial_mode

    @property
    def active(self) -> bool:
        return self._active

    @property
    def previous_mode(self) -> str:
        return self._previous_mode

    def enter(self, current_mode: str) -> dict[str, Any]:
        """进入计划模式，记录之前的模式以便退出时恢复。"""
        if self._active:
            return {"success": False, "error": "已处于计划模式中"}
        self._previous_mode = current_mode
        self._active = True
        return {"success": True, "message": "已进入计划模式，仅允许只读工具"}

    def exit(self) -> dict[str, Any]:
        """退出计划模式，恢复到进入前的审批模式。"""
        if not self._active:
            return {"success": False, "error": "当前不在计划模式中"}
        self._active = False
        return {"success": True, "restored_mode": self._previous_mode}
