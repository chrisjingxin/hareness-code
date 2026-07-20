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


def _validate(fixture: dict[str, Any]) -> None:
    model = {
        "initialize": InitializeParams,
        "event": EventEnvelope,
        "request": InteractionRequestEnvelope,
        "threads.list": ThreadsListParams,
        "threads.open": ThreadsOpenParams,
    }[fixture["kind"]]
    model.model_validate(fixture["value"])
