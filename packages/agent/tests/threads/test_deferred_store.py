"""ThreadDeferredToolStore：按 Thread 隔离的 deferred 工具存储单元测试。"""

from __future__ import annotations

import pytest

from harness_agent.threads.deferred_store import ThreadDeferredToolStore


def test_deferred_store_isolation_between_threads():
    """不同 Thread 的 reveal 状态相互独立，绝不跨 Thread 泄漏。"""
    store = ThreadDeferredToolStore()

    store.reveal("thread-1", ["web_search", "web_fetch"])
    store.reveal("thread-2", ["lsp"])

    assert store.get_revealed("thread-1") == frozenset({"web_search", "web_fetch"})
    assert store.get_revealed("thread-2") == frozenset({"lsp"})
    assert store.get_revealed("thread-3") == frozenset()


def test_deferred_store_reveal_incremental_and_filters_empty():
    """同一 Thread 的 reveal 追加生效，空名与重复项安全过滤。"""
    store = ThreadDeferredToolStore()

    store.reveal("t1", ["tool_a", "", "tool_a"])
    assert store.get_revealed("t1") == frozenset({"tool_a"})

    store.reveal("t1", ["tool_b"])
    assert store.get_revealed("t1") == frozenset({"tool_a", "tool_b"})


def test_deferred_store_clear_thread():
    """clear 仅清除指定 Thread 的 reveal 状态，不影响其他 Thread。"""
    store = ThreadDeferredToolStore()

    store.reveal("t1", ["tool_a"])
    store.reveal("t2", ["tool_b"])

    store.clear("t1")
    assert store.get_revealed("t1") == frozenset()
    assert store.get_revealed("t2") == frozenset({"tool_b"})


def test_deferred_store_ttl_expiration():
    """超期未访问的 Thread 记录在下次操作时自动过期回收。"""
    current_time = 1000.0

    def mock_clock() -> float:
        return current_time

    store = ThreadDeferredToolStore(ttl_seconds=60.0, clock=mock_clock)
    store.reveal("t1", ["tool_a"])

    assert store.get_revealed("t1") == frozenset({"tool_a"})

    # 跃进 70 秒（超过 60s TTL）
    current_time += 70.0
    assert store.get_revealed("t1") == frozenset()
    assert store.size == 0


def test_deferred_store_lru_capacity_eviction():
    """超出最大容量时按 LRU 淘汰最早访问的 Thread。"""
    current_time = 100.0

    def mock_clock() -> float:
        nonlocal current_time
        current_time += 1.0
        return current_time

    store = ThreadDeferredToolStore(max_threads=2, clock=mock_clock)
    store.reveal("t1", ["tool_1"])
    store.reveal("t2", ["tool_2"])
    # 访问 t1，使 t2 成为最久未访问
    store.get_revealed("t1")

    # 插入 t3，应淘汰 t2
    store.reveal("t3", ["tool_3"])
    assert store.get_revealed("t1") == frozenset({"tool_1"})
    assert store.get_revealed("t3") == frozenset({"tool_3"})
    assert store.get_revealed("t2") == frozenset()
