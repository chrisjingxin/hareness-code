"""Harness v3 JSON Schema 运行时：协议边界只执行 canonical Schema。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCHEMA_PATH = Path(__file__).with_name("protocol_v3.json")
# 摘要必须按原始字节计算：生成器对 UTF-8 字节取 sha256，若用 read_text
# 读取，Windows 检出的 CRLF 会被通用换行规则归一化成 LF，导致摘要漂移。
SCHEMA_BYTES = SCHEMA_PATH.read_bytes()
SCHEMA_TEXT = SCHEMA_BYTES.decode("utf-8")
EXPECTED_DIGEST = Path(__file__).with_name("protocol_v3.sha256").read_text(encoding="ascii").strip()
ACTUAL_DIGEST = hashlib.sha256(SCHEMA_BYTES).hexdigest()
if ACTUAL_DIGEST != EXPECTED_DIGEST:
    raise RuntimeError("protocol_v3.json digest mismatch; regenerate the protocol package")
SCHEMA: dict[str, Any] = json.loads(SCHEMA_TEXT)
VALIDATOR = Draft202012Validator(SCHEMA)
METADATA: dict[str, Any] = SCHEMA["x-harness"]


class SchemaValue(dict[str, Any]):
    """提供 Pydantic 风格属性读取，便于领域 handler 与协议校验解耦。"""

    def __getattr__(self, name: str) -> Any:
        return self.get(name)

    def model_dump(self) -> dict[str, Any]:
        """返回普通 JSON 对象。"""
        return _plain(self)


class _SchemaModel:
    schema_ref = ""

    @classmethod
    def model_validate(cls, value: object) -> SchemaValue:
        """按类绑定的 Schema ref 校验并应用声明的默认值。"""
        validated = copy.deepcopy(value)
        definition = _resolve(cls.schema_ref)
        _apply_defaults(definition, validated)
        Draft202012Validator({"$ref": cls.schema_ref, "$defs": SCHEMA["$defs"]}).validate(validated)
        return _wrap(validated)


def schema_model(schema_ref: str, *, name: str) -> type[_SchemaModel]:
    """创建绑定一个 `$defs` 的轻量模型类。"""
    return type(name, (_SchemaModel,), {"schema_ref": schema_ref})


def event_model() -> type[_SchemaModel]:
    """创建同时校验信封和 type/payload 对应关系的事件模型。"""

    class EventModel(_SchemaModel):
        @classmethod
        def model_validate(cls, value: object) -> SchemaValue:
            Draft202012Validator(
                {"$ref": "#/$defs/eventBase", "$defs": SCHEMA["$defs"]}
            ).validate(value)
            assert isinstance(value, dict)
            event = METADATA["events"].get(value.get("type"))
            if event is None:
                raise ValueError(f"unknown event type: {value.get('type')}")
            validate_ref(event["payload"], value.get("payload"))
            return _wrap(copy.deepcopy(value))

    return EventModel


def validate_ref(schema_ref: str, value: object) -> None:
    """校验一个 canonical `$defs` 引用。"""
    Draft202012Validator({"$ref": schema_ref, "$defs": SCHEMA["$defs"]}).validate(value)


def validate_operation_params(method: str, value: object) -> None:
    """校验 client operation 参数。"""
    validate_ref(_entry("operations", method)["params"], value)


def validate_operation_result(method: str, value: object) -> None:
    """校验 client operation 结果。"""
    validate_ref(_entry("operations", method)["result"], value)


def validate_interaction_result(method: str, value: object) -> None:
    """校验反向 Interaction 结果。"""
    validate_ref(_entry("interactions", method)["result"], value)


def validate_interaction_params(method: str, value: object) -> None:
    """校验反向 Interaction 参数。"""
    validate_ref(_entry("interactions", method)["params"], value)


def validate_protocol_error_data(value: object) -> None:
    """校验客户端可分支处理的稳定业务错误。"""
    validate_ref("#/$defs/protocolErrorData", value)


def _entry(group: str, name: str) -> dict[str, Any]:
    entry = METADATA[group].get(name)
    if entry is None:
        raise ValueError(f"unknown protocol {group}: {name}")
    return entry


def _resolve(schema_ref: str) -> dict[str, Any]:
    prefix = "#/$defs/"
    if not schema_ref.startswith(prefix):
        raise ValueError(f"unsupported schema ref: {schema_ref}")
    return SCHEMA["$defs"][schema_ref[len(prefix):]]


def _apply_defaults(schema: dict[str, Any], value: object) -> None:
    if "$ref" in schema:
        _apply_defaults(_resolve(schema["$ref"]), value)
        return
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key not in value and "default" in child:
                value[key] = copy.deepcopy(child["default"])
            if key in value:
                _apply_defaults(child, value[key])
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for item in value:
            _apply_defaults(schema["items"], item)


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return SchemaValue({key: _wrap(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value
