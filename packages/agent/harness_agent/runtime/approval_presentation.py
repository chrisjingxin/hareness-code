"""Run 内文件审批展示数据的短生命周期、有界缓存。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

MAX_APPROVAL_PRESENTATIONS = 64
"""单个 Run 最多保留的不同审批展示条目。"""

MAX_APPROVAL_PRESENTATION_BYTES = 32 * 1024
"""单条展示 JSON 的上限；正常 file diff 仍受更严格的 16 KiB 文本限制。"""

APPROVAL_PRESENTATION_TTL_SECONDS = 10 * 60
"""覆盖默认五分钟 Interaction 超时，同时避免异常 Run 长期保留预览。"""


@dataclass(frozen=True, slots=True)
class _PresentationEntry:
    """缓存中的不可变展示副本及其过期时刻。"""

    value: dict[str, object]
    expires_at: float


class ApprovalPresentationStore:
    """按工具名和原始参数指纹关联当前 Run 的只读审批展示。"""

    def __init__(
        self,
        *,
        max_entries: int = MAX_APPROVAL_PRESENTATIONS,
        ttl_seconds: float = APPROVAL_PRESENTATION_TTL_SECONDS,
    ) -> None:
        """创建内存缓存；源码预览不会跨 Run 或写入持久化。"""
        if max_entries < 1 or ttl_seconds <= 0:
            raise ValueError("APPROVAL_PRESENTATION_LIMIT_INVALID")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, _PresentationEntry] = OrderedDict()
        self._lock = threading.RLock()

    def remember(
        self,
        tool_name: str,
        tool_args: Mapping[str, object],
        presentation: Mapping[str, object],
    ) -> bool:
        """保存 JSON-safe 有界副本；非法或过大的展示直接拒绝登记。"""
        key = _presentation_key(tool_name, tool_args)
        if key is None:
            return False
        try:
            encoded = json.dumps(
                dict(presentation),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            value = json.loads(encoded)
        except (TypeError, ValueError):
            return False
        if not isinstance(value, dict) or len(encoded) > MAX_APPROVAL_PRESENTATION_BYTES:
            return False
        with self._lock:
            self._purge_expired(time.monotonic())
            self._entries.pop(key, None)
            self._entries[key] = _PresentationEntry(
                value=value,
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return True

    def lookup(
        self,
        tool_name: str,
        tool_args: Mapping[str, object],
    ) -> dict[str, object] | None:
        """返回独立副本；缺失、过期或参数不可稳定编码时安全降级。"""
        key = _presentation_key(tool_name, tool_args)
        if key is None:
            return None
        with self._lock:
            self._purge_expired(time.monotonic())
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return dict(entry.value)

    def clear(self) -> None:
        """在 Run 收敛时立即释放所有有界源码预览。"""
        with self._lock:
            self._entries.clear()

    def _purge_expired(self, now: float) -> None:
        """删除所有过期项；条目数量很小，完整扫描保持语义简单。"""
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


def _presentation_key(tool_name: str, tool_args: Mapping[str, object]) -> str | None:
    """生成不保留原始参数的稳定关联 key。"""
    if not tool_name:
        return None
    try:
        encoded = json.dumps(
            {"name": tool_name, "args": dict(tool_args)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "APPROVAL_PRESENTATION_TTL_SECONDS",
    "ApprovalPresentationStore",
    "MAX_APPROVAL_PRESENTATIONS",
]
