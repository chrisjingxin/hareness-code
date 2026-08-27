"""Python DiagnosticLog 的异步、有界与文件生命周期测试。"""

from __future__ import annotations

import json

import pytest

from harness_agent.diagnostic_log.runtime import DiagnosticSettings, create_diagnostic_log


@pytest.mark.asyncio
async def test_log_calls_enqueue_without_await_and_close_finalizes_jsonl(tmp_path) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug", max_file_bytes=1024 * 1024),
        process_id=43,
        started_at_ms=1_787_800_000_456,
    )
    child = log.child({"run_id": "run-1"})
    assert log.info("process.started", _process_fields()) is None
    assert child.info(
        "ipc.initialize.completed",
        {"side": "server", "duration_ms": 1, "protocol_minor": 6},
    ) is None
    await lifecycle.close()

    files = list(tmp_path.glob("*/*.jsonl"))
    assert len(files) == 1
    assert not files[0].name.endswith(".active.jsonl")
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert "logging.started" in [record["event"] for record in records]
    assert "logging.stopped" in [record["event"] for record in records]
    assert next(record for record in records if record["event"] == "ipc.initialize.completed")["run_id"] == "run-1"
    assert next(record for record in records if record["event"] == "logging.started")["fields"] == {
        "effective_level": "debug",
        "max_queue_records": 4096,
        "max_queue_bytes": 8 * 1024 * 1024,
        "reserved_queue_records": 128,
        "reserved_queue_bytes": 256 * 1024,
        "max_file_bytes": 1024 * 1024,
    }
    if __import__("os").name != "nt":
        assert files[0].parent.stat().st_mode & 0o777 == 0o700
        assert files[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_queue_evicts_debug_and_never_reuses_sequence(tmp_path) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug", max_file_bytes=1024 * 1024),
        queue_limits=(6, 64 * 1024, 2, 8 * 1024),
        start_worker=False,
    )
    for index in range(8):
        log.debug(
            "runtime.pool_snapshot",
            {"active_count": index, "idle_count": 0, "waiter_count": 0, "eviction_count": 0},
        )
    log.info("process.started", _process_fields())
    snapshot = lifecycle.snapshot()
    assert snapshot["dropped"]["debug"] > 0
    assert snapshot["next_record_sequence"] == 11
    lifecycle.start()
    await lifecycle.close()


@pytest.mark.asyncio
async def test_warn_and_error_use_reserved_lane_when_normal_capacity_is_full(tmp_path) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug"),
        queue_limits=(6, 8 * 1024, 2, 2 * 1024),
        start_worker=False,
    )
    for index in range(8):
        log.debug(
            "runtime.pool_snapshot",
            {"active_count": index, "idle_count": 0, "waiter_count": 0, "eviction_count": 0},
        )
    log.warn(
        "logging.dropped",
        {
            "debug_count": 1,
            "info_count": 0,
            "warn_count": 0,
            "error_count": 0,
            "invalid_count": 0,
            "oversize_count": 0,
            "reason": "queue_full",
        },
    )
    log.error(
        "logging.writer_failed",
        {"failure_stage": "append", "error_type": "OSError", "summary_code": "WRITE_FAILED"},
    )
    lifecycle.start()
    await lifecycle.close()
    records = [json.loads(line) for path in tmp_path.glob("*/*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
    events = [record["event"] for record in records]
    assert "logging.dropped" in events
    assert "logging.writer_failed" in events


@pytest.mark.asyncio
async def test_contract_violation_never_escapes_business_call(tmp_path) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug", max_file_bytes=1024 * 1024),
    )
    log.info("process.started", {"token": "secret"})
    assert lifecycle.snapshot()["contract_violations"] == 1
    log.info(
        "process.started",
        {
            "command_kind": "x" * 9_000,
            "runtime_version": "python",
            "platform": "darwin",
            "arch": "arm64",
        },
    )
    assert lifecycle.snapshot()["oversize"] == 1
    await lifecycle.close()


@pytest.mark.asyncio
async def test_rotation_closes_every_segment_before_rename(tmp_path) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug", max_file_bytes=420),
    )
    log.info("process.started", _process_fields())
    log.info("process.started", _process_fields())
    await lifecycle.close()
    files = list(tmp_path.glob("*/*"))
    assert len(files) > 1
    assert all(path.name.endswith(".jsonl") and ".active." not in path.name for path in files)


@pytest.mark.asyncio
async def test_retention_deletes_only_closed_and_preserves_active_orphan(tmp_path) -> None:
    old_date = tmp_path / "2020-01-01"
    old_date.mkdir()
    closed = old_date / "agent-1-1-0000.jsonl"
    active = old_date / "cli-1-2-0000.active.jsonl"
    closed.write_text("{}\n", encoding="utf-8")
    active.write_text("{}\n", encoding="utf-8")
    __import__("os").utime(closed, (1, 1))
    __import__("os").utime(active, (1, 1))
    _, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug", retention_days=1),
    )
    await lifecycle.close()
    assert closed.exists() is False
    assert active.exists() is True


@pytest.mark.asyncio
async def test_unusable_root_disables_writer_without_escaping_business_call(tmp_path) -> None:
    root = tmp_path / "root-file"
    root.write_text("occupied", encoding="utf-8")
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=root,
        settings=DiagnosticSettings(level="debug"),
    )
    log.info("process.started", _process_fields())
    await __import__("asyncio").sleep(0.01)
    assert lifecycle.snapshot()["disabled"] is True
    assert (await lifecycle.close())["outcome"] == "disabled"


@pytest.mark.asyncio
async def test_slow_writer_close_is_bounded_and_log_call_never_awaits(tmp_path) -> None:
    event = __import__("asyncio").Event()

    class SlowWriter:
        async def append(self, _encoded: bytes) -> None:
            await event.wait()

        async def close(self) -> None:
            return None

        async def abort(self) -> None:
            return None

    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug"),
        close_timeout_seconds=0.02,
        writer=SlowWriter(),
    )
    assert log.info("process.started", _process_fields()) is None
    assert (await lifecycle.close())["outcome"] == "timeout"
    event.set()


def test_partial_write_adapter_writes_the_complete_record() -> None:
    from harness_agent.diagnostic_log.runtime import _write_all

    class PartialWriter:
        def __init__(self) -> None:
            self.output = bytearray()

        def write(self, value: memoryview) -> int:
            chunk = bytes(value[:3])
            self.output.extend(chunk)
            return len(chunk)

    writer = PartialWriter()
    _write_all(writer, b"complete-jsonl\n")
    assert bytes(writer.output) == b"complete-jsonl\n"


def _process_fields() -> dict[str, object]:
    return {
        "command_kind": "run",
        "runtime_version": "python",
        "platform": "darwin",
        "arch": "arm64",
    }
