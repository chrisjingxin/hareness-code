"""Thread-scoped 文件读取 Snapshot 内存存储。

Snapshot 只证明模型在某个 Thread 中看到过某个 backend 路径的某个内容版本。
它不写入 Transcript、SQLite 或工作区，也不承担后续任务尚未确认的编辑 schema。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Final

LineInterval = tuple[int, int]
"""半开区间 `[start, end)`；行号与 DeepAgents read_file 使用的 0-based offset 一致。"""

VIRTUAL_READ_ONLY_ROOT: Final = "/.harness"


class SnapshotStoreError(ValueError):
    """Snapshot 作用域、路径或存储生命周期不满足要求。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存稳定 code，便于工具层转换成可恢复的错误结果。"""
        self.code = code
        super().__init__(message or code)


class SnapshotExpiredError(SnapshotStoreError):
    """Snapshot 未知、已过期或已被有界淘汰。"""

    def __init__(self, snapshot_id: str) -> None:
        """不把完整 ID 写入异常文本，避免日志泄露可重放句柄。"""
        super().__init__("SNAPSHOT_EXPIRED", "Snapshot 已过期，请重新读取文件")
        self.snapshot_id = snapshot_id


class SnapshotScopeMismatchError(SnapshotStoreError):
    """Snapshot ID 存在，但 Thread、路径或 backend 身份不匹配。"""

    def __init__(self) -> None:
        """统一返回 fail-closed 作用域错误。"""
        super().__init__("SNAPSHOT_SCOPE_MISMATCH", "Snapshot 不属于当前 Thread、路径或 backend")


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """一次文件内容版本的只读证明和已读行范围。"""

    snapshot_id: str
    thread_id: str
    path: str
    backend_id: str
    content_hash: str
    byte_length: int
    line_count: int
    encoding: str
    has_bom: bool
    line_ending: str
    has_final_newline: bool
    seen_lines: tuple[LineInterval, ...]
    created_at: float
    last_used_at: float

    @property
    def strong_content_hash(self) -> str:
        """返回设计文档中的强内容 hash 名称。"""
        return self.content_hash

    @property
    def key(self) -> tuple[str, str, str, str]:
        """返回 Thread、路径、backend 和内容版本组成的内部 identity key。"""
        return (self.thread_id, self.path, self.backend_id, self.content_hash)


def canonical_snapshot_path(path: str) -> str:
    """规范化 backend 虚拟路径，并拒绝 `..`、`.` 和 `/.harness` 写快照。"""
    if not isinstance(path, str) or not path:
        raise SnapshotStoreError("SNAPSHOT_PATH_INVALID", "backend 路径必须是非空字符串")
    normalized = path.replace("\\", "/")
    if not normalized.startswith("/"):
        raise SnapshotStoreError("SNAPSHOT_PATH_INVALID", "backend 路径必须是绝对虚拟路径")
    parts = [part for part in normalized.split("/") if part]
    if "." in parts or ".." in parts:
        raise SnapshotStoreError("SNAPSHOT_PATH_INVALID", "backend 路径不能包含遍历段")
    canonical = "/" if not parts else "/" + "/".join(parts)
    if canonical == VIRTUAL_READ_ONLY_ROOT or canonical.startswith(f"{VIRTUAL_READ_ONLY_ROOT}/"):
        raise SnapshotStoreError("SNAPSHOT_VIRTUAL_READONLY", "/.harness 只读路径不能生成可写 Snapshot")
    return canonical


def merge_line_intervals(intervals: Iterable[LineInterval]) -> tuple[LineInterval, ...]:
    """排序并合并重叠或相邻的已读行区间。"""
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return ()
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        current = merged[-1]
        if start <= current[1]:
            current[1] = max(current[1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def _line_ending(content: str) -> str:
    """报告文本中的换行形态；混合换行由 backend adapter 负责拒绝。"""
    crlf = content.count("\r\n")
    without_crlf = content.replace("\r\n", "")
    lone_cr = without_crlf.count("\r")
    lone_lf = without_crlf.count("\n")
    kinds = sum(bool(value) for value in (crlf, lone_cr, lone_lf))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if lone_cr:
        return "cr"
    if lone_lf:
        return "lf"
    return "none"


class ThreadSnapshotStore:
    """按 Thread 隔离并有界淘汰的进程内 Snapshot store。

    `clock` 可注入单调时钟，便于稳定验证 TTL 和 LRU；生产实现不保存任何
    原文，`max_total_bytes` 约束的是 Snapshot 所证明的内容版本总量。
    """

    def __init__(
        self,
        *,
        max_snapshots: int = 256,
        max_versions_per_path: int = 8,
        max_total_bytes: int = 8 * 1024 * 1024,
        ttl_seconds: float = 30 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建只存在于 Host 生命周期内的有界存储。"""
        if max_snapshots < 1 or max_versions_per_path < 1 or max_total_bytes < 1:
            raise ValueError("SNAPSHOT_STORE_LIMIT_INVALID")
        if ttl_seconds <= 0:
            raise ValueError("SNAPSHOT_STORE_TTL_INVALID")
        self.max_snapshots = max_snapshots
        self.max_versions_per_path = max_versions_per_path
        self.max_total_bytes = max_total_bytes
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._records: dict[str, SnapshotRecord] = {}
        self._identity_index: dict[tuple[str, str, str, str], str] = {}
        self._total_bytes = 0
        self._closed = False

    @property
    def total_bytes(self) -> int:
        """返回当前 retained Snapshot 版本的字节预算占用。"""
        return self._total_bytes

    @property
    def size(self) -> int:
        """返回当前 retained Snapshot 数量。"""
        return len(self._records)

    @property
    def closed(self) -> bool:
        """返回 store 是否已经释放。"""
        return self._closed

    def record_read(
        self,
        thread_id: str,
        path: str,
        backend_id: str,
        content: str,
        offset: int = 0,
        limit: int | None = None,
        *,
        encoding: str = "utf-8",
        has_bom: bool = False,
        raw_bytes: bytes | None = None,
    ) -> SnapshotRecord | None:
        """记录一次 read，并复用同内容版本、合并本次看到的行区间。

        `/.harness` 是逻辑只读 route，调用方应把该次读取视为普通成功读取；
        本方法返回 `None` 而不是创建可用于后续写入的 Snapshot。
        """
        self._ensure_open()
        if not thread_id or not isinstance(thread_id, str):
            raise SnapshotStoreError("SNAPSHOT_THREAD_INVALID", "Thread ID 不能为空")
        if not backend_id or not isinstance(backend_id, str):
            raise SnapshotStoreError("SNAPSHOT_BACKEND_INVALID", "backend identity 不能为空")
        if isinstance(path, str):
            virtual_path = path.replace("\\", "/")
            if virtual_path == VIRTUAL_READ_ONLY_ROOT or virtual_path.startswith(
                f"{VIRTUAL_READ_ONLY_ROOT}/"
            ):
                return None
        canonical_path = canonical_snapshot_path(path)
        if not isinstance(content, str):
            raise SnapshotStoreError("SNAPSHOT_CONTENT_INVALID", "Snapshot 只接受文本内容")
        if offset < 0 or (limit is not None and limit < 0):
            raise SnapshotStoreError("SNAPSHOT_RANGE_INVALID", "已读行范围必须是非负数")
        now = self._clock()
        self._expire(now)
        bytes_value = raw_bytes if raw_bytes is not None else content.encode(encoding)
        content_hash = hashlib.sha256(bytes_value).hexdigest()
        line_count = len(content.splitlines(keepends=True))
        end = line_count if limit is None else min(line_count, offset + limit)
        seen = merge_line_intervals(((min(offset, line_count), end),))
        identity = (thread_id, canonical_path, backend_id, content_hash)
        existing_id = self._identity_index.get(identity)
        if existing_id is not None and existing_id in self._records:
            existing = self._records[existing_id]
            updated = replace(
                existing,
                seen_lines=merge_line_intervals((*existing.seen_lines, *seen)),
                last_used_at=now,
            )
            self._records[existing_id] = updated
            return updated

        record = SnapshotRecord(
            snapshot_id=f"snap_{secrets.token_urlsafe(18)}",
            thread_id=thread_id,
            path=canonical_path,
            backend_id=backend_id,
            content_hash=content_hash,
            byte_length=len(bytes_value),
            line_count=line_count,
            encoding=encoding,
            has_bom=has_bom,
            line_ending=_line_ending(content),
            has_final_newline=content.endswith(("\n", "\r")),
            seen_lines=seen,
            created_at=now,
            last_used_at=now,
        )
        self._records[record.snapshot_id] = record
        self._identity_index[identity] = record.snapshot_id
        self._total_bytes += record.byte_length
        self._evict(record.snapshot_id)
        return record

    def resolve(
        self,
        snapshot_id: str,
        thread_id: str,
        path: str,
        backend_id: str,
    ) -> SnapshotRecord:
        """按句柄及完整作用域解析 Snapshot，任何不匹配都 fail closed。"""
        self._ensure_open()
        now = self._clock()
        self._expire(now)
        record = self._records.get(snapshot_id)
        if record is None:
            raise SnapshotExpiredError(snapshot_id)
        canonical_path = canonical_snapshot_path(path)
        if (
            record.thread_id != thread_id
            or record.path != canonical_path
            or record.backend_id != backend_id
        ):
            raise SnapshotScopeMismatchError()
        updated = replace(record, last_used_at=now)
        self._records[snapshot_id] = updated
        return updated

    def has_seen(
        self,
        record: SnapshotRecord,
        start_line: int,
        end_line: int,
    ) -> bool:
        """判断一个半开源行区间是否完全落在 Snapshot 已读范围内。"""
        if start_line < 0 or end_line < start_line:
            return False
        return any(start_line >= start and end_line <= end for start, end in record.seen_lines)

    def invalidate_path(self, thread_id: str, path: str, backend_id: str | None = None) -> int:
        """使一个 Thread 的路径版本失效，返回被删除的 Snapshot 数量。"""
        self._ensure_open()
        canonical_path = canonical_snapshot_path(path)
        ids = [
            record.snapshot_id
            for record in self._records.values()
            if record.thread_id == thread_id
            and record.path == canonical_path
            and (backend_id is None or record.backend_id == backend_id)
        ]
        for snapshot_id in ids:
            self._remove(snapshot_id)
        return len(ids)

    def close_thread(self, thread_id: str) -> int:
        """释放一个 Thread 的所有内存 Snapshot，不触碰持久化 Thread 数据。"""
        self._ensure_open()
        ids = [record.snapshot_id for record in self._records.values() if record.thread_id == thread_id]
        for snapshot_id in ids:
            self._remove(snapshot_id)
        return len(ids)

    def close(self) -> None:
        """释放 Host 级 store；重复调用保持幂等。"""
        if self._closed:
            return
        self._records.clear()
        self._identity_index.clear()
        self._total_bytes = 0
        self._closed = True

    def _ensure_open(self) -> None:
        """拒绝在 Host close 后继续创建或解析句柄。"""
        if self._closed:
            raise SnapshotStoreError("SNAPSHOT_STORE_CLOSED")

    def _expire(self, now: float) -> None:
        """先按 TTL 移除旧版本，再让后续 LRU 规则保持确定。"""
        expired = [
            record.snapshot_id
            for record in self._records.values()
            if now - record.last_used_at >= self.ttl_seconds
        ]
        for snapshot_id in expired:
            self._remove(snapshot_id)

    def _evict(self, newest_id: str) -> None:
        """按 path/version、总字节和总数量顺序执行确定性 LRU 淘汰。"""
        newest = self._records.get(newest_id)
        if newest is None:
            return

        def path_group(record: SnapshotRecord) -> bool:
            return (
                record.thread_id == newest.thread_id
                and record.path == newest.path
                and record.backend_id == newest.backend_id
            )

        while sum(path_group(record) for record in self._records.values()) > self.max_versions_per_path:
            candidates = [
                record
                for record in self._records.values()
                if path_group(record) and record.snapshot_id != newest_id
            ]
            if not candidates:
                break
            self._remove(min(candidates, key=lambda record: (record.last_used_at, record.created_at, record.snapshot_id)).snapshot_id)

        while self._total_bytes > self.max_total_bytes and len(self._records) > 1:
            candidates = [record for record in self._records.values() if record.snapshot_id != newest_id]
            if not candidates:
                break
            self._remove(min(candidates, key=lambda record: (record.last_used_at, record.created_at, record.snapshot_id)).snapshot_id)

        while len(self._records) > self.max_snapshots:
            candidates = [record for record in self._records.values() if record.snapshot_id != newest_id]
            if not candidates:
                break
            self._remove(min(candidates, key=lambda record: (record.last_used_at, record.created_at, record.snapshot_id)).snapshot_id)

        # 单个版本超过全局字节预算时也不能留下超预算状态；返回的 record
        # 仍可用于本次调用的结果，但后续 resolve 会稳定返回 expired。
        if self._total_bytes > self.max_total_bytes and newest_id in self._records:
            self._remove(newest_id)

    def _remove(self, snapshot_id: str) -> None:
        """从记录、identity index 和字节计数中原子移除一个版本。"""
        record = self._records.pop(snapshot_id, None)
        if record is None:
            return
        self._total_bytes -= record.byte_length
        if self._identity_index.get(record.key) == snapshot_id:
            self._identity_index.pop(record.key, None)


__all__ = [
    "LineInterval",
    "SnapshotExpiredError",
    "SnapshotRecord",
    "SnapshotScopeMismatchError",
    "SnapshotStoreError",
    "ThreadSnapshotStore",
    "canonical_snapshot_path",
    "merge_line_intervals",
]
