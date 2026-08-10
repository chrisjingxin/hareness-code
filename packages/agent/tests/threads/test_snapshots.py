"""ThreadSnapshotStore 的作用域、范围合并和有界生命周期测试。"""

from __future__ import annotations

import pytest


def test_same_content_reuses_snapshot_and_merges_seen_ranges() -> None:
    """同一 Thread/path/backend 的同内容读取复用 ID，并合并半开行区间。"""
    from harness_agent.threads.snapshots import ThreadSnapshotStore

    store = ThreadSnapshotStore()
    first = store.record_read("thread-a", "/src/a.py", "local-a", "a\nb\nc\n", 0, 1)
    second = store.record_read("thread-a", "/src/a.py", "local-a", "a\nb\nc\n", 2, 1)

    assert first is not None and second is not None
    assert second.snapshot_id == first.snapshot_id
    assert second.seen_lines == ((0, 1), (2, 3))
    assert store.has_seen(second, 0, 1)
    assert not store.has_seen(second, 1, 3)


def test_changed_content_gets_new_id_and_scope_mismatch_fails_closed() -> None:
    """内容变化、Thread、path 或 backend 变化都不能复用旧句柄。"""
    from harness_agent.threads.snapshots import (
        SnapshotExpiredError,
        SnapshotScopeMismatchError,
        ThreadSnapshotStore,
    )

    store = ThreadSnapshotStore()
    old = store.record_read("thread-a", "/a.txt", "local-a", "old\n")
    new = store.record_read("thread-a", "/a.txt", "local-a", "new\n")
    assert old is not None and new is not None
    assert old.snapshot_id != new.snapshot_id

    with pytest.raises(SnapshotScopeMismatchError) as mismatch:
        store.resolve(old.snapshot_id, "thread-b", "/a.txt", "local-a")
    assert mismatch.value.code == "SNAPSHOT_SCOPE_MISMATCH"
    with pytest.raises(SnapshotScopeMismatchError):
        store.resolve(old.snapshot_id, "thread-a", "/other.txt", "local-a")
    with pytest.raises(SnapshotScopeMismatchError):
        store.resolve(old.snapshot_id, "thread-a", "/a.txt", "remote-a")

    store.close_thread("thread-a")
    with pytest.raises(SnapshotExpiredError) as expired:
        store.resolve(new.snapshot_id, "thread-a", "/a.txt", "local-a")
    assert expired.value.code == "SNAPSHOT_EXPIRED"


def test_ttl_and_version_limits_evict_deterministically() -> None:
    """TTL、path version 和总字节预算都保持有界，淘汰后返回 expired。"""
    from harness_agent.threads.snapshots import SnapshotExpiredError, ThreadSnapshotStore

    now = [0.0]
    store = ThreadSnapshotStore(
        max_versions_per_path=2,
        max_total_bytes=8,
        ttl_seconds=5,
        clock=lambda: now[0],
    )
    first = store.record_read("thread-a", "/a.txt", "local", "one")
    now[0] = 1
    second = store.record_read("thread-a", "/a.txt", "local", "two")
    now[0] = 2
    third = store.record_read("thread-a", "/a.txt", "local", "three")
    assert first is not None and second is not None and third is not None
    assert store.size <= 2
    with pytest.raises(SnapshotExpiredError):
        store.resolve(first.snapshot_id, "thread-a", "/a.txt", "local")

    now[0] = 8
    with pytest.raises(SnapshotExpiredError):
        store.resolve(third.snapshot_id, "thread-a", "/a.txt", "local")


def test_virtual_harness_route_never_creates_writable_snapshot() -> None:
    """/.harness 只读内容被读取时不产生可用于 edit/delete 的记录。"""
    from harness_agent.threads.snapshots import ThreadSnapshotStore

    store = ThreadSnapshotStore()
    assert store.record_read("thread-a", "/.harness/skills/x/SKILL.md", "virtual", "body\n") is None
    assert store.size == 0
