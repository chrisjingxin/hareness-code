"""按 Thread 隔离的 deferred 工具 reveal 内存存储。

提供按 Thread 隔离的有界存储，避免多 Thread 共享 AgentEngine 时
已 reveal 的工具集合发生跨会话泄漏，同时保持同一 Thread 内多轮对话
工具状态的连贯性。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import threading
import time


class ThreadDeferredToolStore:
    """按 Thread 隔离并受容量与 TTL 保护的 deferred 工具 reveal 内存存储。"""

    def __init__(
        self,
        *,
        max_threads: int = 1024,
        ttl_seconds: float = 24 * 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建只存在于 Host 生命周期内的 Thread 隔离 deferred 工具存储。"""
        if max_threads < 1:
            raise ValueError("DEFERRED_STORE_MAX_THREADS_INVALID")
        if ttl_seconds <= 0:
            raise ValueError("DEFERRED_STORE_TTL_INVALID")
        self.max_threads = max_threads
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._threads: dict[str, set[str]] = {}
        self._access_times: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        """返回当前记录的 Thread 数量。"""
        with self._lock:
            return len(self._threads)

    def reveal(self, thread_id: str, names: Sequence[str]) -> frozenset[str]:
        """把命中的工具名加入指定 Thread 的 reveal 集合。"""
        if not thread_id:
            thread_id = ""
        valid_names = [name for name in names if name]
        with self._lock:
            self._prune_expired_locked()
            revealed = self._threads.setdefault(thread_id, set())
            if valid_names:
                revealed.update(valid_names)
            self._access_times[thread_id] = self._clock()
            self._enforce_capacity_locked()
            return frozenset(revealed)

    def get_revealed(self, thread_id: str) -> frozenset[str]:
        """返回指定 Thread 当前已 reveal 的工具名不可变快照。"""
        if not thread_id:
            thread_id = ""
        with self._lock:
            self._prune_expired_locked()
            revealed = self._threads.get(thread_id)
            if revealed is not None:
                self._access_times[thread_id] = self._clock()
                return frozenset(revealed)
            return frozenset()

    def clear(self, thread_id: str) -> None:
        """清除指定 Thread 的 reveal 状态。"""
        if not thread_id:
            thread_id = ""
        with self._lock:
            self._threads.pop(thread_id, None)
            self._access_times.pop(thread_id, None)

    def _prune_expired_locked(self) -> None:
        """修剪超期未访问的 Thread 记录。"""
        now = self._clock()
        expired = [
            tid
            for tid, last_access in self._access_times.items()
            if now - last_access > self.ttl_seconds
        ]
        for tid in expired:
            self._threads.pop(tid, None)
            self._access_times.pop(tid, None)

    def _enforce_capacity_locked(self) -> None:
        """超出最大 Thread 数时按 LRU 淘汰最早访问的 Thread。"""
        while len(self._threads) > self.max_threads:
            oldest_tid = min(self._access_times, key=lambda k: self._access_times[k])
            self._threads.pop(oldest_tid, None)
            self._access_times.pop(oldest_tid, None)
