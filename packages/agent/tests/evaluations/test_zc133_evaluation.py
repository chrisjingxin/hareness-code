"""ZC-133 fixture、纯 simulator 和 mock replay 的安全门槛测试。"""

from __future__ import annotations

import json

import pytest

from evaluations.zc133.cli import main
from evaluations.zc133.fixtures import fixture_catalog
from evaluations.zc133.runner import (
    CANDIDATE_SPECS,
    build_replay_calls,
    run_mock_evaluation,
    write_report,
)
from evaluations.zc133.simulator import InMemoryEditSimulator


def test_fixture_catalog_covers_required_zc133_cases() -> None:
    """固定 fixture 至少覆盖 24 个安全、编码、范围和恢复场景。"""

    fixtures = fixture_catalog()
    assert len(fixtures) >= 24
    categories = {fixture.category for fixture in fixtures}
    assert {
        "replace",
        "insert",
        "delete",
        "multi-edit",
        "duplicate",
        "long-file",
        "encoding",
        "stale",
        "concurrency",
        "scope",
        "seen-range",
        "overlap",
        "write",
        "recovery",
    } <= categories
    assert len({fixture.fixture_id for fixture in fixtures}) == len(fixtures)


def test_simulator_fail_closed_scenarios_do_not_write() -> None:
    """stale、跨 Thread、未读范围和 overlap 均返回稳定错误且零写入。"""

    stale = next(item for item in fixture_catalog() if item.fixture_id == "stale-after-read")
    simulator = InMemoryEditSimulator(stale)
    assert simulator.read(
        thread_id=stale.thread_id,
        path=stale.path,
        start_line=stale.read_start,
        end_line=stale.read_end,
    ) is not None
    simulator.external_change(stale.external_after_read or "")
    stale_outcome = simulator.execute(
        "snapshot-single-explicit",
        build_replay_calls(stale, CANDIDATE_SPECS[1])[0],
        thread_id=stale.call_thread_id,
    )
    assert stale_outcome.code == "STALE_FILE"
    assert stale_outcome.writes == 0

    cross_thread = next(item for item in fixture_catalog() if item.fixture_id == "cross-thread")
    simulator = InMemoryEditSimulator(cross_thread)
    assert simulator.read(
        thread_id=cross_thread.thread_id,
        path=cross_thread.path,
        start_line=cross_thread.read_start,
        end_line=cross_thread.read_end,
    ) is not None
    cross_outcome = simulator.execute(
        "snapshot-single-explicit",
        build_replay_calls(cross_thread, CANDIDATE_SPECS[1])[0],
        thread_id=cross_thread.call_thread_id,
    )
    assert cross_outcome.code == "SNAPSHOT_SCOPE_MISMATCH"
    assert cross_outcome.writes == 0

    overlap = next(item for item in fixture_catalog() if item.fixture_id == "overlapping-edits")
    report = run_mock_evaluation(repetitions=1)
    overlap_attempt = next(
        attempt for attempt in report.attempts
        if attempt.fixture_id == overlap.fixture_id and attempt.candidate == "snapshot-edits-explicit"
    )
    assert overlap_attempt.error_code == "OVERLAPPING_EDITS"
    assert overlap_attempt.partial_write is False


def test_exact_string_replay_initializes_only_the_empty_document() -> None:
    """HC-141 空 fixture 可完成，非空文件的空匹配仍稳定拒绝。"""
    empty = next(item for item in fixture_catalog() if item.fixture_id == "empty-file-insert")
    simulator = InMemoryEditSimulator(empty)
    assert simulator.read(
        thread_id=empty.thread_id,
        path=empty.path,
        start_line=empty.read_start,
        end_line=empty.read_end,
    ) is not None
    replay_call = build_replay_calls(empty, CANDIDATE_SPECS[0])[0]
    assert replay_call["args"]["snapshot_id"] == f"snap-{empty.fixture_id}"
    initialized = simulator.execute(
        "exact-string",
        replay_call,
        thread_id=empty.call_thread_id,
    )
    assert initialized.ok is True
    assert initialized.content == "created\n"

    nonempty = next(item for item in fixture_catalog() if item.fixture_id == "replace-single-line")
    simulator = InMemoryEditSimulator(nonempty)
    assert simulator.read(
        thread_id=nonempty.thread_id,
        path=nonempty.path,
        start_line=nonempty.read_start,
        end_line=nonempty.read_end,
    ) is not None
    rejected = simulator.execute(
        "exact-string",
        {
            "name": "edit_file",
            "args": {
                "file_path": nonempty.path,
                "snapshot_id": f"snap-{nonempty.fixture_id}",
                "old_string": "",
                "new_string": "prefix\n",
            },
        },
        thread_id=nonempty.call_thread_id,
    )
    assert rejected.code == "INVALID_EDIT"
    assert rejected.writes == 0
    assert rejected.content == nonempty.source


def test_exact_string_simulator_requires_the_production_snapshot_argument() -> None:
    """评测不能把生产 schema 会拒绝的无 Snapshot 调用计为成功。"""
    fixture = next(item for item in fixture_catalog() if item.fixture_id == "replace-single-line")
    simulator = InMemoryEditSimulator(fixture)
    assert simulator.read(
        thread_id=fixture.thread_id,
        path=fixture.path,
        start_line=fixture.read_start,
        end_line=fixture.read_end,
    ) is not None
    call = build_replay_calls(fixture, CANDIDATE_SPECS[0])[0]
    call["args"].pop("snapshot_id")

    rejected = simulator.execute("exact-string", call, thread_id=fixture.call_thread_id)

    assert rejected.code == "INVALID_TOOL_ARGS"
    assert rejected.schema_valid is False
    assert rejected.writes == 0


def test_mock_replay_counts_silent_corruption_as_veto() -> None:
    """模拟弱模型错选合法行号时，Snapshot 候选被一票否决而不进入生产。"""

    report = run_mock_evaluation(repetitions=1)
    assert report.fixture_count >= 24
    assert report.decision == "exact-string"
    assert all(summary.silent_corruption_count == 0 for summary in report.summaries if summary.candidate == "exact-string")
    assert any(summary.silent_corruption_count > 0 for summary in report.summaries if summary.family == "snapshot-single")
    assert any(summary.partial_write_count > 0 for summary in report.summaries if summary.family == "snapshot-single")
    assert "mock replay" in report.decision_reason


def test_encoding_and_atomic_candidates_replay_successfully() -> None:
    """BOM/CRLF/EOF 与 edits[] 正常路径可在 simulator 中完成。"""

    report = run_mock_evaluation(repetitions=1)
    for fixture_id in ("bom-utf8", "crlf", "no-final-newline"):
        attempt = next(
            attempt for attempt in report.attempts
            if attempt.fixture_id == fixture_id and attempt.candidate == "snapshot-edits-explicit"
        )
        assert attempt.completed is True
        assert attempt.silent_corruption is False
    multi = next(
        attempt for attempt in report.attempts
        if attempt.fixture_id == "multi-non-overlap" and attempt.candidate == "snapshot-edits-explicit"
    )
    assert multi.completed is True
    assert multi.partial_write is False


def test_report_is_deidentified_and_cli_requires_real_opt_in(tmp_path) -> None:
    """报告不含源码；真实模型必须同时声明 opt-in 和 Profile。"""

    report = run_mock_evaluation(repetitions=1)
    json_path, markdown_path = write_report(report, tmp_path)
    encoded = json_path.read_text(encoding="utf-8")
    assert "source" not in encoded
    assert "new_text" not in encoded
    assert "return \"hello\"" not in encoded
    assert "fixture_ids" in encoded
    assert markdown_path.exists()
    with pytest.raises(SystemExit):
        main(["--real-model", "--output-dir", str(tmp_path)])


def test_report_json_has_no_attempt_payloads() -> None:
    """可落盘报告只保留聚合结果，不把模型响应或工具参数带出评测进程。"""

    report = run_mock_evaluation(repetitions=1)
    encoded = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "args" not in encoded
    data = json.loads(encoded)
    assert all("fixture_id" not in summary for summary in data["candidates"])
