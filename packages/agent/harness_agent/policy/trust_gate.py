"""受信目录门禁模块。

本模块负责管理受信目录列表，防止恶意仓库提权：未受信目录强制
default 审批模式、隐藏 Always-allow 选项。受信列表持久化到
``~/.harness/settings.json`` 的 ``trusted_directories`` 字段。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TRUSTED_DIRECTORIES_KEY = "trusted_directories"
"""JSON 文件中存储受信目录列表的键名。"""

_UNTRUSTED_DIRECTORIES_KEY = "untrusted_directories"
"""JSON 文件中存储显式取消受信的目录列表的键名。

与 ``trusted_directories`` 互补：用户通过 ``untrust_directory`` 显式移除
某目录的受信状态后，该目录被记录到此列表中，以便 ``get_directory_trust_status``
区分 "untrusted"（曾经被主动移除）与 "unknown"（从未标记过）。
"""


def get_trusted_directories_path() -> Path:
    """返回受信列表持久化文件路径。

    Returns:
        ``~/.harness/settings.json`` 的绝对路径。
    """
    return Path.home() / ".harness" / "settings.json"


def _read_settings() -> dict:
    """读取 settings.json 并返回解析后的字典。

    文件不存在或格式错误时返回空字典。
    """
    path = get_trusted_directories_path()
    try:
        if not path.is_file():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取受信目录配置文件失败: %s", exc)
        return {}


def _write_settings(data: dict) -> None:
    """将字典写入 settings.json，自动创建父目录。"""
    path = get_trusted_directories_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_trusted_directories() -> list[str]:
    """从 ``~/.harness/settings.json`` 的 ``trusted_directories`` 字段
    读取受信目录列表。

    Returns:
        受信目录路径字符串列表。文件不存在或格式错误时返回空列表。
    """
    settings = _read_settings()
    trusted = settings.get(TRUSTED_DIRECTORIES_KEY, [])
    if isinstance(trusted, list):
        return [str(item) for item in trusted if isinstance(item, str)]
    return []


def save_trusted_directories(directories: list[str]) -> None:
    """将受信目录列表持久化到 ``~/.harness/settings.json``。

    保留文件中已有的其他字段，只更新 ``trusted_directories``。

    Args:
        directories: 受信目录路径字符串列表。
    """
    settings = _read_settings()
    settings[TRUSTED_DIRECTORIES_KEY] = directories
    _write_settings(settings)


def is_trusted_directory(path: str | Path) -> bool:
    """判断给定路径是否在受信列表中。

    路径比较使用 ``Path.resolve()`` 后进行字符串比较。

    Args:
        path: 待检查的目录路径。

    Returns:
        路径在受信列表中时返回 ``True``，否则返回 ``False``。
    """
    resolved = str(Path(path).resolve())
    trusted = load_trusted_directories()
    return resolved in trusted


def trust_directory(path: str | Path) -> None:
    """将目录添加到受信列表并持久化。

    已在列表中则跳过。如果该目录此前被显式标记为取消受信，同时将其从
    取消受信列表中移除。

    Args:
        path: 要添加的目录路径。
    """
    resolved = str(Path(path).resolve())
    trusted = load_trusted_directories()
    if resolved in trusted:
        return
    trusted.append(resolved)
    save_trusted_directories(trusted)
    _remove_from_untrusted(resolved)


def untrust_directory(path: str | Path) -> None:
    """从受信列表移除目录并持久化。

    同时将该目录记录到显式取消受信列表，以便区分"主动取消受信"与
    "从未标记过"两种状态。

    Args:
        path: 要移除的目录路径。
    """
    resolved = str(Path(path).resolve())
    trusted = load_trusted_directories()
    if resolved not in trusted:
        return
    trusted.remove(resolved)
    save_trusted_directories(trusted)
    _add_to_untrusted(resolved)


def get_directory_trust_status(path: str | Path) -> str:
    """返回目录受信状态。

    三种状态的含义：
    * ``"trusted"`` — 目录在受信列表中。
    * ``"untrusted"`` — 目录曾被用户通过 ``untrust_directory`` 显式
      移出受信列表。
    * ``"unknown"`` — 首次进入的新目录，从未被标记过。

    Args:
        path: 待查询的目录路径。

    Returns:
        ``"trusted"``、``"untrusted"`` 或 ``"unknown"``。
    """
    resolved = str(Path(path).resolve())
    if is_trusted_directory(path):
        return "trusted"
    if resolved in _load_untrusted_directories():
        return "untrusted"
    return "unknown"


def is_restricted_mode_for_untrusted(
    mode: str, path: str | Path
) -> tuple[bool, str | None]:
    """未受信目录的权限模式限制。

    如果目录未受信且审批模式是 ``"yolo"`` 或 ``"auto"``，应将其锁定为
    ``default`` 模式，防止在未受信仓库中自动执行高风险操作。

    Args:
        mode: 当前审批模式。
        path: 待检查的目录路径。

    Returns:
        ``(True, 原因)`` 需要限制模式时返回原因字符串；
        ``(False, None)`` 无需限制。
    """
    if is_trusted_directory(path):
        return False, None
    if mode in ("yolo", "auto"):
        return True, "未受信目录，权限模式锁定为 default"
    return False, None


def should_hide_always_allow(path: str | Path) -> bool:
    """判断是否应在未受信目录中隐藏 Always-allow 选项。

    未受信目录中不应展示"始终允许"选项，防止用户误操作提升恶意仓库的权限。

    Args:
        path: 待检查的目录路径。

    Returns:
        目录不受信时返回 ``True``。
    """
    return not is_trusted_directory(path)


# ---------------------------------------------------------------------------
# 内部辅助：显式取消受信列表的加载、持久化与增删
# ---------------------------------------------------------------------------


def _load_untrusted_directories() -> list[str]:
    """从 settings.json 读取显式取消受信的目录列表。"""
    settings = _read_settings()
    untrusted = settings.get(_UNTRUSTED_DIRECTORIES_KEY, [])
    if isinstance(untrusted, list):
        return [str(item) for item in untrusted if isinstance(item, str)]
    return []


def _save_untrusted_directories(directories: list[str]) -> None:
    """持久化显式取消受信的目录列表。"""
    settings = _read_settings()
    settings[_UNTRUSTED_DIRECTORIES_KEY] = directories
    _write_settings(settings)


def _add_to_untrusted(resolved: str) -> None:
    """将已解析的目录路径添加到显式取消受信列表。"""
    untrusted = _load_untrusted_directories()
    if resolved not in untrusted:
        untrusted.append(resolved)
        _save_untrusted_directories(untrusted)


def _remove_from_untrusted(resolved: str) -> None:
    """从显式取消受信列表移除已解析的目录路径。"""
    untrusted = _load_untrusted_directories()
    if resolved in untrusted:
        untrusted.remove(resolved)
        _save_untrusted_directories(untrusted)
