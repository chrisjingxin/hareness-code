"""ThreadSnapshotStore 的作用域、范围合并和有界生命周期测试。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


_WAIT_TIMEOUT_SECONDS = 2.0


def _assert_store_invariants(store: object) -> None:
    """断言记录、identity index 与字节预算没有部分更新。"""
    records = dict(getattr(store, "_records"))
    identity_index = dict(getattr(store, "_identity_index"))
    total_bytes = getattr(store, "_total_bytes")

    assert len({record.key for record in records.values()}) == len(records)
    assert total_bytes == sum(record.byte_length for record in records.values())
    assert total_bytes >= 0
    assert len(identity_index) == len(records)
    for snapshot_id, record in records.items():
        assert record.snapshot_id == snapshot_id
        assert identity_index.get(record.key) == snapshot_id
    for identity, snapshot_id in identity_index.items():
        assert snapshot_id in records
        assert records[snapshot_id].key == identity

    if getattr(store, "_closed"):
        assert records == {}
        assert identity_index == {}
        assert total_bytes == 0


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


@pytest.mark.parametrize("_iteration", range(20))
def test_concurrent_same_identity_reuses_one_record_and_merges_ranges(
    monkeypatch: pytest.MonkeyPatch,
    _iteration: int,
) -> None:
    """同 identity 同时越过候选 ID 生成点也只能提交一份记录。"""
    from harness_agent.threads import snapshots as snapshot_module
    from harness_agent.threads.snapshots import ThreadSnapshotStore

    candidate_barrier = threading.Barrier(2)
    candidate_lock = threading.Lock()
    candidate_number = 0

    def synchronized_token(_length: int) -> str:
        nonlocal candidate_number
        candidate_barrier.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        with candidate_lock:
            candidate_number += 1
            return f"candidate-{candidate_number}"

    monkeypatch.setattr(snapshot_module.secrets, "token_urlsafe", synchronized_token)
    store = ThreadSnapshotStore()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(store.record_read, "thread-a", "/a.txt", "local", "a\nb\n", 0, 1),
            pool.submit(store.record_read, "thread-a", "/a.txt", "local", "a\nb\n", 1, 1),
        )
        records = [future.result(timeout=_WAIT_TIMEOUT_SECONDS) for future in futures]

    assert records[0] is not None and records[1] is not None
    assert records[0].snapshot_id == records[1].snapshot_id
    resolved = store.resolve(records[0].snapshot_id, "thread-a", "/a.txt", "local")
    assert resolved.seen_lines == ((0, 2),)
    assert store.size == 1
    assert store.total_bytes == 4
    _assert_store_invariants(store)


def test_concurrent_different_scopes_never_merge_identity_or_seen_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread、path 或 backend 不同的并发读取各自保留独立证明。"""
    from harness_agent.threads import snapshots as snapshot_module
    from harness_agent.threads.snapshots import ThreadSnapshotStore

    scopes = (
        ("thread-a", "/a.txt", "local-a"),
        ("thread-b", "/a.txt", "local-a"),
        ("thread-a", "/b.txt", "local-a"),
        ("thread-a", "/a.txt", "remote-a"),
    )
    candidate_barrier = threading.Barrier(len(scopes))
    candidate_lock = threading.Lock()
    candidate_number = 0

    def synchronized_token(_length: int) -> str:
        nonlocal candidate_number
        candidate_barrier.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        with candidate_lock:
            candidate_number += 1
            return f"scope-{candidate_number}"

    monkeypatch.setattr(snapshot_module.secrets, "token_urlsafe", synchronized_token)
    store = ThreadSnapshotStore()
    with ThreadPoolExecutor(max_workers=len(scopes)) as pool:
        futures = [
            pool.submit(store.record_read, thread_id, path, backend_id, "same\n", 0, 1)
            for thread_id, path, backend_id in scopes
        ]
        records = [future.result(timeout=_WAIT_TIMEOUT_SECONDS) for future in futures]

    assert all(record is not None for record in records)
    assert len({record.snapshot_id for record in records if record is not None}) == len(scopes)
    for record, (thread_id, path, backend_id) in zip(records, scopes, strict=True):
        assert record is not None
        assert store.resolve(record.snapshot_id, thread_id, path, backend_id).seen_lines == ((0, 1),)
    _assert_store_invariants(store)


def test_resolve_and_invalidate_cannot_resurrect_a_removed_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve touch 与路径失效必须按完整线性顺序提交，不能写回半条状态。"""
    from harness_agent.threads import snapshots as snapshot_module
    from harness_agent.threads.snapshots import SnapshotExpiredError, ThreadSnapshotStore

    store = ThreadSnapshotStore()
    record = store.record_read("thread-a", "/a.txt", "local", "a\n")
    assert record is not None

    original_replace = snapshot_module.replace
    resolve_paused = threading.Event()
    allow_resolve = threading.Event()
    invalidate_started = threading.Event()
    invalidate_finished = threading.Event()

    def pausing_replace(value: object, **changes: object) -> object:
        if threading.current_thread().name.startswith("snapshot") and "last_used_at" in changes:
            resolve_paused.set()
            if not allow_resolve.wait(_WAIT_TIMEOUT_SECONDS):
                raise AssertionError("resolve timing barrier timed out")
        return original_replace(value, **changes)

    def invalidate() -> int:
        invalidate_started.set()
        try:
            return store.invalidate_path("thread-a", "/a.txt", "local")
        finally:
            invalidate_finished.set()

    monkeypatch.setattr(snapshot_module, "replace", pausing_replace)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="snapshot") as pool:
        resolve_future = pool.submit(
            store.resolve,
            record.snapshot_id,
            "thread-a",
            "/a.txt",
            "local",
        )
        assert resolve_paused.wait(_WAIT_TIMEOUT_SECONDS)
        invalidate_future = pool.submit(invalidate)
        assert invalidate_started.wait(_WAIT_TIMEOUT_SECONDS)
        # 未加锁实现会在 resolve 暂停时完成失效；加锁实现会等 resolve 退出短临界区。
        invalidate_finished.wait(0.2)
        allow_resolve.set()
        assert resolve_future.result(timeout=_WAIT_TIMEOUT_SECONDS).snapshot_id == record.snapshot_id
        assert invalidate_future.result(timeout=_WAIT_TIMEOUT_SECONDS) == 1

    assert store.size == 0
    assert store.total_bytes == 0
    with pytest.raises(SnapshotExpiredError):
        store.resolve(record.snapshot_id, "thread-a", "/a.txt", "local")
    _assert_store_invariants(store)


def test_record_that_overlaps_host_close_cannot_reopen_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host close 先线性化后，在途 record 必须失败且不能重新插入状态。"""
    from harness_agent.threads import snapshots as snapshot_module
    from harness_agent.threads.snapshots import SnapshotStoreError, ThreadSnapshotStore

    token_started = threading.Event()
    allow_token = threading.Event()

    def pausing_token(_length: int) -> str:
        token_started.set()
        if not allow_token.wait(_WAIT_TIMEOUT_SECONDS):
            raise AssertionError("record timing barrier timed out")
        return "after-close"

    monkeypatch.setattr(snapshot_module.secrets, "token_urlsafe", pausing_token)
    store = ThreadSnapshotStore()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.record_read, "thread-a", "/a.txt", "local", "a\n")
        assert token_started.wait(_WAIT_TIMEOUT_SECONDS)
        store.close()
        allow_token.set()
        with pytest.raises(SnapshotStoreError) as closed:
            future.result(timeout=_WAIT_TIMEOUT_SECONDS)

    assert closed.value.code == "SNAPSHOT_STORE_CLOSED"
    assert store.closed
    assert store.size == 0
    assert store.total_bytes == 0
    _assert_store_invariants(store)


def test_ttl_expiry_and_resolve_race_finishes_in_a_complete_state() -> None:
    """TTL 与 resolve 同时触发时只允许完整成功或完整 expired。"""
    from harness_agent.threads.snapshots import SnapshotExpiredError, ThreadSnapshotStore

    for iteration in range(20):
        now = [0.0]
        store = ThreadSnapshotStore(max_snapshots=1, ttl_seconds=5, clock=lambda: now[0])
        old = store.record_read("thread-a", "/a.txt", "local", "old\n")
        assert old is not None
        now[0] = 10.0
        start = threading.Barrier(2)

        def resolve_expired() -> str:
            start.wait(timeout=_WAIT_TIMEOUT_SECONDS)
            try:
                store.resolve(old.snapshot_id, "thread-a", "/a.txt", "local")
            except SnapshotExpiredError:
                return "expired"
            return "resolved"

        def record_new() -> object:
            start.wait(timeout=_WAIT_TIMEOUT_SECONDS)
            return store.record_read(
                "thread-a",
                "/a.txt",
                "local",
                f"new-{iteration}\n",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            resolve_future = pool.submit(resolve_expired)
            record_future = pool.submit(record_new)
            assert resolve_future.result(timeout=_WAIT_TIMEOUT_SECONDS) == "expired"
            new = record_future.result(timeout=_WAIT_TIMEOUT_SECONDS)

        assert new is not None
        assert store.resolve(new.snapshot_id, "thread-a", "/a.txt", "local") == new
        assert store.size == 1
        _assert_store_invariants(store)


def test_lru_capacity_eviction_and_resolve_cannot_resurrect_the_old_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容量 LRU 淘汰与 resolve touch 竞争后不能复活旧记录。"""
    from harness_agent.threads import snapshots as snapshot_module
    from harness_agent.threads.snapshots import SnapshotExpiredError, ThreadSnapshotStore

    store = ThreadSnapshotStore(max_snapshots=1, ttl_seconds=60)
    old = store.record_read("thread-a", "/a.txt", "local", "old\n")
    assert old is not None

    original_replace = snapshot_module.replace
    resolve_paused = threading.Event()
    allow_resolve = threading.Event()
    record_started = threading.Event()
    record_finished = threading.Event()

    def pausing_replace(value: object, **changes: object) -> object:
        if threading.current_thread().name.startswith("lru-resolve") and "last_used_at" in changes:
            resolve_paused.set()
            if not allow_resolve.wait(_WAIT_TIMEOUT_SECONDS):
                raise AssertionError("resolve timing barrier timed out")
        return original_replace(value, **changes)

    def record_new() -> object:
        record_started.set()
        try:
            return store.record_read("thread-a", "/b.txt", "local", "new\n")
        finally:
            record_finished.set()

    monkeypatch.setattr(snapshot_module, "replace", pausing_replace)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lru-resolve") as pool:
        resolve_future = pool.submit(
            store.resolve,
            old.snapshot_id,
            "thread-a",
            "/a.txt",
            "local",
        )
        assert resolve_paused.wait(_WAIT_TIMEOUT_SECONDS)
        record_future = pool.submit(record_new)
        assert record_started.wait(_WAIT_TIMEOUT_SECONDS)
        # 修复后 record 等待 resolve 的短临界区；旧实现会先淘汰 old，随后被 resolve 写回。
        record_finished.wait(0.2)
        allow_resolve.set()
        assert resolve_future.result(timeout=_WAIT_TIMEOUT_SECONDS).snapshot_id == old.snapshot_id
        new = record_future.result(timeout=_WAIT_TIMEOUT_SECONDS)

    assert new is not None
    assert (
        store.resolve(new.snapshot_id, "thread-a", "/b.txt", "local").snapshot_id
        == new.snapshot_id
    )
    with pytest.raises(SnapshotExpiredError):
        store.resolve(old.snapshot_id, "thread-a", "/a.txt", "local")
    assert store.size == 1
    _assert_store_invariants(store)


def test_invalidate_and_close_thread_do_not_remove_other_scopes() -> None:
    """并行 path/Thread 清理不能删除另一 Thread 或 backend 的 Snapshot。"""
    from harness_agent.threads.snapshots import SnapshotExpiredError, ThreadSnapshotStore

    store = ThreadSnapshotStore()
    removed_by_path = store.record_read("thread-a", "/a.txt", "local", "a\n")
    removed_by_thread = store.record_read("thread-a", "/b.txt", "local", "b\n")
    retained_thread = store.record_read("thread-b", "/a.txt", "local", "a\n")
    retained_backend = store.record_read("thread-a", "/a.txt", "remote", "a\n")
    assert all(
        record is not None
        for record in (removed_by_path, removed_by_thread, retained_thread, retained_backend)
    )
    start = threading.Barrier(2)

    def invalidate_local_path() -> int:
        start.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        return store.invalidate_path("thread-a", "/a.txt", "local")

    def close_thread_a() -> int:
        start.wait(timeout=_WAIT_TIMEOUT_SECONDS)
        return store.close_thread("thread-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        invalidated = pool.submit(invalidate_local_path)
        closed = pool.submit(close_thread_a)
        assert invalidated.result(timeout=_WAIT_TIMEOUT_SECONDS) in {0, 1}
        assert closed.result(timeout=_WAIT_TIMEOUT_SECONDS) in {2, 3}

    assert removed_by_path is not None and removed_by_thread is not None
    with pytest.raises(SnapshotExpiredError):
        store.resolve(removed_by_path.snapshot_id, "thread-a", "/a.txt", "local")
    with pytest.raises(SnapshotExpiredError):
        store.resolve(removed_by_thread.snapshot_id, "thread-a", "/b.txt", "local")
    assert retained_thread is not None
    assert store.resolve(retained_thread.snapshot_id, "thread-b", "/a.txt", "local")
    assert retained_backend is not None
    # close_thread 清理同 Thread 的全部 backend；这里只验证没有跨 Thread 泄漏。
    with pytest.raises(SnapshotExpiredError):
        store.resolve(retained_backend.snapshot_id, "thread-a", "/a.txt", "remote")
    _assert_store_invariants(store)
