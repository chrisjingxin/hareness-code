"""提供提示词、工具 schema 与上下文压缩共用的确定性纯函数。

Run 的 AGENTS 来源读取和 system context 组装统一由
``context_lifecycle.ContextLifecycle`` 负责；本模块不再保留旧 PromptEpoch
或第二套 AGENTS 读取入口。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping


def sha256_text(value: str) -> str:
    """返回 UTF-8 文本的完整 SHA-256，用于本地可观测性而非身份认证。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: object) -> str:
    """以确定性 JSON 序列化可提示词化的结构。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


HISTORY_REWRITE_VERSION = sha256_text("context-v1:structured-summary:tool-preview")
"""历史重写算法的内容指纹；算法变动会成为新的可观测变化原因。"""


def estimate_tokens(value: str | bytes) -> int:
    """按 UTF-8 字节保守估算 token，统一用于预算而不伪装成厂商 tokenization。"""
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return max(1, math.ceil(len(data) / 4))


def output_reserve_tokens(context_window_tokens: int) -> int:
    """计算窗口的响应预留，始终落在 4K 到 16K 的稳定区间。"""
    return max(4_096, min(16_384, math.ceil(context_window_tokens * 0.10)))


def input_cap_tokens(context_window_tokens: int) -> int:
    """返回模型输入预算，配置解析层保证窗口最小值已经合法。"""
    return context_window_tokens - output_reserve_tokens(context_window_tokens)


def normalized_tool_schemas(tools: Iterable[object]) -> tuple[dict[str, object], ...]:
    """按名称、描述和参数 JSON 排序工具 schema，消除注册顺序带来的前缀抖动。"""
    schemas: list[dict[str, object]] = []
    for tool in tools:
        if isinstance(tool, Mapping):
            name = str(tool.get("name", ""))
            description = str(tool.get("description", ""))
            parameters = tool.get("parameters", tool.get("input_schema", {}))
        else:
            name = str(getattr(tool, "name", ""))
            description = str(getattr(tool, "description", ""))
            args_schema = getattr(tool, "args_schema", None)
            try:
                parameters = args_schema.model_json_schema() if args_schema is not None else {}
            except (AttributeError, TypeError, ValueError):
                parameters = {}
        schemas.append(
            {
                "name": name,
                "description": description,
                "parameters": json.loads(canonical_json(parameters)),
            }
        )
    return tuple(
        sorted(
            schemas,
            key=lambda schema: (
                str(schema["name"]),
                str(schema["description"]),
                canonical_json(schema["parameters"]),
            ),
        )
    )


def tool_schema_fingerprint(tools: Iterable[object]) -> str:
    """对规范化后的工具参数形状取指纹，不记录厂商专用缓存字段。"""
    return sha256_text(canonical_json(normalized_tool_schemas(tools)))
