"""Python Pydantic 模型消费与 TypeScript 相同的 v2 契约 fixture。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harness_agent.protocol_generated import (
    EventEnvelope,
    ContextCompactParams,
    InitializeParams,
    InteractionRequestEnvelope,
    RunStartParams,
    ThreadsListParams,
    ThreadsOpenParams,
)


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "protocol" / "fixtures" / "v2-contract.json"


def test_python_accepts_all_shared_valid_fixtures() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for fixture in fixtures["valid"]:
        _validate(fixture)


def test_python_rejects_all_shared_invalid_fixtures() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for fixture in fixtures["invalid"]:
        with pytest.raises(ValidationError):
            _validate(fixture)


def test_python_validates_v2_4_manual_compaction_params() -> None:
    """手动上下文压缩只接受服务端已知的内部 thread ID 字段。"""
    assert ContextCompactParams.model_validate({"thread_id": "thread-1"}).thread_id == "thread-1"
    with pytest.raises(ValidationError):
        ContextCompactParams.model_validate({"thread_id": "", "unknown": True})


def test_python_validates_v2_7_thread_model_selection() -> None:
    """新 minor 的每次 Run 选择必须只携带非空 primary Profile。"""
    parsed = RunStartParams.model_validate(
        {
            "message": "使用 pro",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "model_selection": {"primary_profile": "pro"},
        }
    )
    assert parsed.model_selection is not None
    assert parsed.model_selection.primary_profile == "pro"
    with pytest.raises(ValidationError):
        RunStartParams.model_validate(
            {"message": "x", "model_selection": {"primary_profile": "", "unknown": True}}
        )


def _validate(fixture: dict[str, Any]) -> None:
    model = {
        "initialize": InitializeParams,
        "event": EventEnvelope,
        "request": InteractionRequestEnvelope,
        "threads.list": ThreadsListParams,
        "threads.open": ThreadsOpenParams,
    }[fixture["kind"]]
    model.model_validate(fixture["value"])
