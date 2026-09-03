"""Diagnostic Log v1 的 Python 共享契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from harness_agent.diagnostic_log.contract import validate_record


FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "protocol"
    / "diagnostic-log"
    / "fixtures"
    / "v1-contract.json"
)


def test_python_and_typescript_share_v1_contract_fixtures() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for fixture in fixtures:
        if fixture["valid"]:
            validate_record(fixture["value"])
        else:
            with pytest.raises((ValidationError, ValueError)):
                validate_record(fixture["value"])


def test_python_rejects_non_finite_sensitive_and_oversized_records() -> None:
    record = _minimal_record()
    with pytest.raises((ValidationError, ValueError)):
        validate_record({**record, "fields": {"duration_ms": float("nan")}})
    with pytest.raises((ValidationError, ValueError)):
        validate_record({**record, "fields": {"token": "secret"}})
    with pytest.raises((ValidationError, ValueError)):
        validate_record(
            {
                **record,
                "fields": {
                    "command_kind": "x" * 9_000,
                    "runtime_version": "python",
                    "platform": "darwin",
                    "arch": "arm64",
                },
            }
        )


@pytest.mark.parametrize(
    ("level", "event", "fields"),
    [
        (
            "info",
            "goal.proposal.model.completed",
            {
                "response_mode": "forced_tool_schema",
                "readiness": "needs_clarification",
                "assumption_count": 0,
                "criterion_count": 0,
                "question_count": 1,
                "repository_calls": 2,
                "repository_result_chars": 318,
                "repository_limited_results": 1,
                "last_repository_tool": "glob",
                "last_repository_result_chars": 51_827,
            },
        ),
        (
            "warn",
            "goal.proposal.model.invalid",
            {
                "response_mode": "forced_tool_schema",
                "error_code": "GOAL_OBJECTIVE_INVALID",
                "repository_calls": 1,
                "repository_result_chars": 16,
                "repository_limited_results": 0,
                "last_repository_tool": "read_file",
                "last_repository_result_chars": 16,
            },
        ),
    ],
)
def test_goal_proposal_events_accept_only_safe_repository_statistics(
    level: str,
    event: str,
    fields: dict[str, object],
) -> None:
    record = _minimal_record()

    validate_record({**record, "level": level, "event": event, "fields": fields})


def _minimal_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "timestamp_ms": 1,
        "level": "info",
        "event": "process.started",
        "component": "agent",
        "process": {"pid": 1, "started_at_ms": 1, "record_sequence": 1},
        "project_fingerprint": "a" * 64,
        "fields": {
            "command_kind": "run",
            "runtime_version": "python",
            "platform": "darwin",
            "arch": "arm64",
        },
    }
