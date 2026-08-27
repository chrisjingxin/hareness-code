"""Diagnostic Log v1 的 canonical Python validator。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from harness_agent.diagnostic_log.generated import MAX_DIAGNOSTIC_RECORD_BYTES


_ROOT = Path(__file__).parent
_SCHEMA_BYTES = (_ROOT / "diagnostic_log_v1.json").read_bytes()
_EXPECTED_DIGEST = (_ROOT / "diagnostic_log_v1.sha256").read_text(encoding="ascii").strip()
if hashlib.sha256(_SCHEMA_BYTES).hexdigest() != _EXPECTED_DIGEST:
    raise RuntimeError("Diagnostic Log v1 schema digest mismatch; regenerate protocol")
_SCHEMA: dict[str, Any] = json.loads(_SCHEMA_BYTES.decode("utf-8"))
_METADATA: dict[str, Any] = _SCHEMA["x-harness-diagnostic"]
_RECORD_VALIDATOR = Draft202012Validator({"$ref": "#/$defs/record", "$defs": _SCHEMA["$defs"]})
_SENSITIVE_KEY = re.compile(r"api[_-]?key|authorization|cookie|credential|password|secret|token", re.I)
_ALLOWED_TOKEN_COUNT_KEYS = {
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "estimated_tokens",
    "before_estimated_tokens",
    "after_estimated_tokens",
}


def validate_record(value: object) -> None:
    """验证 envelope、event/level/fields 对应关系和落盘安全边界。"""
    _RECORD_VALIDATOR.validate(value)
    assert isinstance(value, dict)
    event = _METADATA["events"].get(value.get("event"))
    if event is None:
        raise ValueError(f"unknown Diagnostic Event: {value.get('event')}")
    if value.get("level") not in event["levels"]:
        raise ValueError("Diagnostic Event level mismatch")
    Draft202012Validator(
        {"$ref": event["fields"], "$defs": _SCHEMA["$defs"]}
    ).validate(value.get("fields"))
    _validate_safe_value(value.get("fields"))
    encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_DIAGNOSTIC_RECORD_BYTES:
        raise ValueError("Diagnostic record exceeds 8 KiB")


def _validate_safe_value(value: object, key: str | None = None) -> None:
    if key is not None and key not in _ALLOWED_TOKEN_COUNT_KEYS and _SENSITIVE_KEY.search(key):
        raise ValueError("Diagnostic fields contain sensitive key")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Diagnostic fields contain non-finite number")
    if isinstance(value, str) and any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Diagnostic fields contain control characters")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _validate_safe_value(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            _validate_safe_value(child)
