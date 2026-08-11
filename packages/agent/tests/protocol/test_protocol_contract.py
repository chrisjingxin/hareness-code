"""Python 与 TypeScript 消费同一份 v3 contract fixture。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from harness_agent.protocol.generated import ContextCompactParams, EventEnvelope, RunStartParams
from harness_agent.protocol.runtime import (
    validate_interaction_params,
    validate_interaction_result,
    validate_operation_params,
    validate_operation_result,
    validate_protocol_error_data,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "protocol" / "fixtures" / "v3-contract.json"
)


def test_python_accepts_all_shared_valid_fixtures() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for fixture in fixtures["valid"]:
        _validate(fixture)


def test_python_rejects_all_shared_invalid_fixtures() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for fixture in fixtures["invalid"]:
        with pytest.raises((ValidationError, ValueError)):
            _validate(fixture)


def test_python_validates_manual_compaction_params() -> None:
    assert ContextCompactParams.model_validate({"thread_id": "thread-1"}).thread_id == "thread-1"
    with pytest.raises(ValidationError):
        ContextCompactParams.model_validate({"thread_id": "", "unknown": True})


def test_python_validates_thread_model_selection() -> None:
    parsed = RunStartParams.model_validate(
        {
            "mode": "build",
            "message": "使用 pro",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "model_selection": {"primary_profile": "pro"},
        }
    )
    assert parsed.model_selection.primary_profile == "pro"
    with pytest.raises(ValidationError):
        RunStartParams.model_validate(
            {
                "mode": "build",
                "message": "x",
                "thread_id": "thread-1",
                "run_id": "run-1",
                "model_selection": {"primary_profile": "", "unknown": True},
            }
        )


def test_python_accepts_execution_identity_on_event_envelope() -> None:
    """事件可以携带 AgentExecution 归属，旧字段仍保持不变。"""
    EventEnvelope.model_validate(
        {
            "event_id": "event-1",
            "type": "content.delta",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "execution_id": "root-run-1",
            "agent_id": "main",
            "sequence": 1,
            "timestamp_ms": 1,
            "payload": {"text": "ok"},
        }
    )


def test_python_accepts_run_progress_event() -> None:
    """Chat Completions 运行反馈使用独立的事实进度 payload。"""
    EventEnvelope.model_validate(
        {
            "event_id": "run-progress-event",
            "type": "run.progress",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "sequence": 1,
            "timestamp_ms": 1,
            "payload": {"phase": "preparing", "elapsed_ms": 12},
        }
    )


def test_python_requires_run_start_work_mode() -> None:
    """run.start 必填 build|compose 工作模式，未知模式被拒绝。"""
    base = {"message": "检查", "thread_id": "thread-1", "run_id": "run-1"}
    with pytest.raises(ValidationError):
        RunStartParams.model_validate(base)
    with pytest.raises(ValidationError):
        RunStartParams.model_validate({**base, "mode": "yolo"})
    assert RunStartParams.model_validate({**base, "mode": "build"}).mode == "build"
    assert RunStartParams.model_validate({**base, "mode": "compose"}).mode == "compose"


def test_python_requires_run_started_work_mode() -> None:
    """run.started 必须回传实际工作模式。"""
    envelope = {
        "event_id": "event-started",
        "type": "run.started",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "sequence": 1,
        "timestamp_ms": 1,
        "payload": {"resumed": False},
    }
    with pytest.raises((ValidationError, ValueError)):
        EventEnvelope.model_validate(envelope)
    parsed = EventEnvelope.model_validate(
        {**envelope, "payload": {"resumed": False, "mode": "compose"}}
    )
    assert parsed.payload["mode"] == "compose"


def test_python_validates_compose_state_projection() -> None:
    """compose.state 是带 revision 的完整有界 projection，未知字段和枚举被拒绝。"""
    payload = {
        "revision": 3,
        "stage": "build",
        "status": "running",
        "stages": [{"id": "understand", "status": "passed", "attempts": 1}],
        "tasks": [{"id": "task-1", "title": "实现搜索", "status": "running"}],
        "evidence": [{"label": "pytest -q tests/foo", "status": "passed"}],
    }
    envelope = {
        "event_id": "compose-state-event",
        "type": "compose.state",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "sequence": 1,
        "timestamp_ms": 1,
        "payload": payload,
    }
    parsed = EventEnvelope.model_validate(envelope)
    assert parsed.payload["revision"] == 3
    with pytest.raises((ValidationError, ValueError)):
        EventEnvelope.model_validate({**envelope, "payload": {**payload, "extra": True}})
    with pytest.raises((ValidationError, ValueError)):
        EventEnvelope.model_validate({**envelope, "payload": {**payload, "stage": "deploy"}})
    with pytest.raises((ValidationError, ValueError)):
        EventEnvelope.model_validate({**envelope, "payload": {**payload, "revision": -1}})
    with pytest.raises((ValidationError, ValueError)):
        EventEnvelope.model_validate(
            {
                **envelope,
                "payload": {
                    **payload,
                    "stages": [
                        {"id": "understand", "status": "passed", "attempts": 1, "extra": True}
                    ],
                },
            }
        )


def _validate(fixture: dict[str, Any]) -> None:
    kind = fixture["kind"]
    if kind == "operation.params":
        validate_operation_params(fixture["name"], fixture["value"])
    elif kind == "operation.result":
        validate_operation_result(fixture["name"], fixture["value"])
    elif kind == "event":
        EventEnvelope.model_validate(fixture["value"])
    elif kind == "interaction.params":
        validate_interaction_params(fixture["name"], fixture["value"])
    elif kind == "interaction.result":
        validate_interaction_result(fixture["name"], fixture["value"])
    else:
        validate_protocol_error_data(fixture["value"])
