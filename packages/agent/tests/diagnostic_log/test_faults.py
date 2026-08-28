"""Diagnostic Log 故障降级：主业务继续，丢弃合并且不递归。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from harness_agent.diagnostic_log.runtime import DiagnosticSettings, create_diagnostic_log


def _process_fields() -> dict[str, object]:
    return {
        "command_kind": "run",
        "runtime_version": "python",
        "platform": "darwin",
        "arch": "arm64",
    }


@pytest.mark.asyncio
async def test_queue_overflow_emits_merged_dropped_without_recursion(tmp_path: Path) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug"),
        queue_limits=(6, 8 * 1024, 2, 2 * 1024),
        start_worker=False,
    )
    for index in range(20):
        log.debug(
            "runtime.pool_snapshot",
            {"active_count": index, "idle_count": 0, "waiter_count": 0, "eviction_count": 0},
        )
    lifecycle.start()
    await lifecycle.close()
    records = [
        json.loads(line)
        for path in tmp_path.glob("*/*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    dropped = [record for record in records if record["event"] == "logging.dropped"]
    assert dropped
    assert dropped[0]["fields"]["debug_count"] > 0
    assert dropped[0]["fields"]["reason"] == "queue_full"
    nested = [
        record
        for record in dropped
        if record["fields"]["debug_count"] == 0 and record["fields"]["warn_count"] > 0
    ]
    assert nested == []


@pytest.mark.asyncio
async def test_writer_failure_emits_once_and_writes_fixed_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr)

    class FailingWriter:
        async def append(self, _encoded: bytes) -> None:
            raise OSError("disk full")

        async def close(self) -> None:
            return None

        async def abort(self) -> None:
            return None

    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug"),
        writer=FailingWriter(),
    )
    log.info("process.started", _process_fields())
    log.info("process.started", _process_fields())
    await __import__("asyncio").sleep(0.05)
    assert lifecycle.snapshot()["disabled"] is True
    result = await lifecycle.close()
    assert result["outcome"] == "disabled"
    output = stderr.getvalue()
    assert output.count("HARNESS_DIAGNOSTIC_WRITER_FAILED") == 1
    assert "disk full" not in output
    log.info("process.started", _process_fields())


@pytest.mark.asyncio
async def test_diagnostic_log_never_writes_agent_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path,
        settings=DiagnosticSettings(level="debug"),
    )
    log.info("process.started", _process_fields())
    await lifecycle.close()
    captured = capsys.readouterr()
    assert captured.out == ""
