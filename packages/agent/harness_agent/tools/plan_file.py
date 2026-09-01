"""会话计划文件的虚拟路径与本机落盘。

模型只看见 ``/.harness/plan.md``；磁盘文件在用户 home 下按 thread 覆盖写入，
不进入工作区 Git。
"""

from __future__ import annotations

from pathlib import Path

PLAN_VIRTUAL_PATH = "/.harness/plan.md"
PLAN_VIRTUAL_NAME = "plan.md"


def is_plan_virtual_path(path: str) -> bool:
    """判断工具参数是否指向会话计划虚拟文件。"""
    if not isinstance(path, str) or not path:
        return False
    normalized = path.replace("\\", "/").rstrip("/")
    return (
        normalized == PLAN_VIRTUAL_PATH
        or normalized == PLAN_VIRTUAL_NAME
        or normalized.endswith("/.harness/plan.md")
    )


def plan_disk_path(thread_id: str, home: Path | None = None) -> Path:
    """返回本机计划文件路径：``{home}/.harness/plans/{thread_id}.md``。"""
    return (home or Path.home()) / ".harness" / "plans" / f"{thread_id}.md"


def plan_display_path(thread_id: str) -> str:
    """给 UI 的短路径，不把绝对 home 塞进模型上下文。"""
    return f"~/.harness/plans/{thread_id}.md"


def ensure_plan_file(thread_id: str, home: Path | None = None) -> Path:
    """确保会话计划文件存在；已有正文绝不截断。"""
    if not thread_id:
        raise ValueError("planning run requires a thread_id")
    path = plan_disk_path(thread_id, home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.touch(exist_ok=True)
    return path


def read_plan_markdown(thread_id: str, home: Path | None = None) -> tuple[str, bool]:
    """读取计划正文。文件不存在或 trim 后为空时 ``has_plan=False``。"""
    if not thread_id:
        return "", False
    path = plan_disk_path(thread_id, home)
    if not path.is_file():
        return "", False
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return "", False
    return text, True


def write_plan_markdown(thread_id: str, content: str, home: Path | None = None) -> Path:
    """覆盖写入计划文件；目录权限 ``0700``。没有 thread 时拒绝。"""
    if not thread_id:
        raise ValueError("planning run requires a thread_id")
    path = plan_disk_path(thread_id, home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_text(content, encoding="utf-8")
    return path
