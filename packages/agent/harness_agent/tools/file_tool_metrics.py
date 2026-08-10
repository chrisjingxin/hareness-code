"""文件工具的进程内聚合观测。

指标只用于确认 Snapshot 文件工具是否频繁要求重读或遭遇安全拒绝；它不保存源码、
模型参数、完整路径或 Snapshot ID，也不写入 Thread 持久化。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass

_MAX_ERROR_CODES = 32
_MAX_SEEN_SNAPSHOTS = 1_024


@dataclass(frozen=True, slots=True)
class FileToolMetricsSnapshot:
    """可安全输出给本机配置诊断的聚合指标快照。"""

    read_calls: int
    reread_calls: int
    edit_attempts: int
    edit_successes: int
    result_bytes_total: int
    error_codes: dict[str, int]
    snapshot_expired: int
    stale_file: int
    unread_range: int
    diagnostics_calls: int
    diagnostics_unavailable: int
    diagnostics_timeouts: int
    diagnostics_latency_ms_total: float

    def payload(self) -> dict[str, object]:
        """返回不含任何输入内容或资源标识的稳定 JSON 形状。"""
        return {
            "read_calls": self.read_calls,
            "reread_calls": self.reread_calls,
            "edit_attempts": self.edit_attempts,
            "edit_successes": self.edit_successes,
            "result_bytes_total": self.result_bytes_total,
            "error_codes": dict(self.error_codes),
            "snapshot_expired": self.snapshot_expired,
            "stale_file": self.stale_file,
            "unread_range": self.unread_range,
            "diagnostics": {
                "calls": self.diagnostics_calls,
                "unavailable": self.diagnostics_unavailable,
                "timeouts": self.diagnostics_timeouts,
                "latency_ms_total": self.diagnostics_latency_ms_total,
            },
        }


class FileToolMetrics:
    """为整个 Host 聚合文件工具结果，内部句柄只用于重读去重。"""

    def __init__(self) -> None:
        """创建线程安全、容量受限的计数器。"""
        self._lock = threading.Lock()
        self._read_calls = 0
        self._reread_calls = 0
        self._edit_attempts = 0
        self._edit_successes = 0
        self._result_bytes_total = 0
        self._error_codes: OrderedDict[str, int] = OrderedDict()
        self._seen_snapshot_digests: OrderedDict[bytes, None] = OrderedDict()
        self._diagnostics_calls = 0
        self._diagnostics_unavailable = 0
        self._diagnostics_timeouts = 0
        self._diagnostics_latency_ms_total = 0.0

    def record_read(self, snapshot_id: str | None) -> None:
        """记录 read 与同一 Snapshot 的重复读取，不对外暴露句柄。"""
        with self._lock:
            self._read_calls += 1
            if snapshot_id is None:
                return
            # 重读指标只需要相等判断，绝不保留可重放的完整 Snapshot ID。
            digest = hashlib.sha256(snapshot_id.encode("utf-8")).digest()[:16]
            if digest in self._seen_snapshot_digests:
                self._reread_calls += 1
                self._seen_snapshot_digests.move_to_end(digest)
                return
            self._seen_snapshot_digests[digest] = None
            while len(self._seen_snapshot_digests) > _MAX_SEEN_SNAPSHOTS:
                self._seen_snapshot_digests.popitem(last=False)

    def record_result(self, name: str, *, ok: bool, result_bytes: int, error_code: str | None) -> None:
        """记录单次工具终态的大小、编辑成功率和稳定错误码。"""
        with self._lock:
            self._result_bytes_total += max(result_bytes, 0)
            if name == "edit_file":
                self._edit_attempts += 1
                if ok:
                    self._edit_successes += 1
            if error_code:
                self._record_error(error_code)

    def record_diagnostics(self, status: str, latency_ms: float | None) -> None:
        """记录 diagnostics 可用性和耗时；状态以受控枚举传入。"""
        with self._lock:
            if status != "unavailable" or latency_ms is not None:
                self._diagnostics_calls += 1
            if status == "unavailable":
                self._diagnostics_unavailable += 1
            elif status == "timeout":
                self._diagnostics_timeouts += 1
            if latency_ms is not None:
                self._diagnostics_latency_ms_total += max(latency_ms, 0.0)

    def snapshot(self) -> FileToolMetricsSnapshot:
        """读取脱敏聚合快照，不返回内部重读去重集合。"""
        with self._lock:
            return FileToolMetricsSnapshot(
                read_calls=self._read_calls,
                reread_calls=self._reread_calls,
                edit_attempts=self._edit_attempts,
                edit_successes=self._edit_successes,
                result_bytes_total=self._result_bytes_total,
                error_codes=dict(self._error_codes),
                snapshot_expired=self._error_codes.get("SNAPSHOT_EXPIRED", 0),
                stale_file=self._error_codes.get("STALE_FILE", 0),
                unread_range=self._error_codes.get("UNREAD_RANGE", 0),
                diagnostics_calls=self._diagnostics_calls,
                diagnostics_unavailable=self._diagnostics_unavailable,
                diagnostics_timeouts=self._diagnostics_timeouts,
                diagnostics_latency_ms_total=self._diagnostics_latency_ms_total,
            )

    def _record_error(self, code: str) -> None:
        """限制异常 code 的维度数，避免不可信输入形成无界指标标签。"""
        current = self._error_codes.pop(code, 0)
        self._error_codes[code] = current + 1
        while len(self._error_codes) > _MAX_ERROR_CODES:
            self._error_codes.popitem(last=False)


__all__ = ["FileToolMetrics", "FileToolMetricsSnapshot"]
