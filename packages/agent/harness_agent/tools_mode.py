"""模式与任务管理工具：提供计划模式切换和后台任务管理能力。"""

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


class BackgroundTaskManager:
    """后台任务管理器，跟踪正在执行的后台命令。"""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def register(self, command: str) -> str:
        """注册一个后台任务，返回 task_id。"""
        self._counter += 1
        task_id = f"task-{self._counter}"
        self._tasks[task_id] = {
            "command": command,
            "status": "running",
            "output": "",
        }
        return task_id

    def get_output(self, task_id: str) -> dict[str, Any]:
        """获取后台任务的当前输出。"""
        task = self._tasks.get(task_id)
        if task is None:
            return {"success": False, "error": f"任务 {task_id} 不存在"}
        return {"success": True, "task_id": task_id, "status": task["status"], "output": task["output"]}

    def stop(self, task_id: str) -> dict[str, Any]:
        """终止后台任务。"""
        task = self._tasks.get(task_id)
        if task is None:
            return {"success": False, "error": f"任务 {task_id} 不存在"}
        task["status"] = "stopped"
        return {"success": True, "task_id": task_id, "message": "任务已终止"}

    def list_tasks(self) -> list[dict[str, Any]]:
        """列出所有后台任务。"""
        return [{"task_id": tid, **info} for tid, info in self._tasks.items()]
