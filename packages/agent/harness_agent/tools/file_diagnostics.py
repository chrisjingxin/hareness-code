"""文件 mutation 完成后的有界、去敏 diagnostics 摘要。"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

MAX_DIAGNOSTIC_ITEMS = 20
"""单次提交后返回给模型的 diagnostics 摘要条数上限。"""

MAX_DIAGNOSTIC_BYTES = 8 * 1024
"""单次 diagnostics 摘要的 UTF-8 字节上限。"""


def diagnostics_unavailable(latency_ms: float | None = None) -> dict[str, object]:
    """返回未配置或调用失败的中性 diagnostics 状态。"""
    payload: dict[str, object] = {"status": "unavailable"}
    if latency_ms is not None:
        payload["latency_ms"] = latency_ms
    return payload


def diagnostics_timeout(latency_ms: float) -> dict[str, object]:
    """返回不影响已提交 mutation 的 diagnostics timeout。"""
    return {"status": "timeout", "latency_ms": latency_ms}


def summarize_diagnostics(response: Mapping[str, object], latency_ms: float) -> dict[str, object]:
    """把 provider/LSP 响应压成数量、位置、severity 和短 code。"""
    error = response.get("error")
    if isinstance(error, str):
        if "TIMEOUT" in error.upper():
            return diagnostics_timeout(latency_ms)
        return diagnostics_unavailable(latency_ms)
    if "results" not in response:
        return diagnostics_unavailable(latency_ms)
    items = _diagnostic_items(response["results"])
    projected: list[dict[str, object]] = []
    for item in items:
        summary = _diagnostic_summary(item)
        if summary is None:
            continue
        candidate = [*projected, summary]
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(candidate) > MAX_DIAGNOSTIC_ITEMS or len(encoded) > MAX_DIAGNOSTIC_BYTES:
            break
        projected.append(summary)
    return {
        "status": "ok",
        "count": len(items),
        "items": projected,
        "truncated": len(projected) < len(items),
        "latency_ms": latency_ms,
    }


def elapsed_ms(started: float) -> float:
    """将单调时钟耗时归一到毫秒，避免输出过高精度环境信息。"""
    return round(max(time.monotonic() - started, 0.0) * 1_000, 3)


def _diagnostic_items(value: object) -> list[Mapping[str, object]]:
    """兼容 LSP diagnostic report 的 items 与旧 server 数组结果。"""
    if isinstance(value, Mapping):
        nested = value.get("items")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _diagnostic_summary(item: Mapping[str, object]) -> dict[str, object] | None:
    """把不可信 LSP item 收敛为固定小字段。"""
    raw_range = item.get("range")
    if not isinstance(raw_range, Mapping):
        return None
    start = raw_range.get("start")
    end = raw_range.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return None
    start_line = start.get("line")
    end_line = end.get("line")
    if (
        isinstance(start_line, bool)
        or isinstance(end_line, bool)
        or not isinstance(start_line, int)
        or not isinstance(end_line, int)
        or start_line < 0
        or end_line < 0
    ):
        return None
    summary: dict[str, object] = {
        "start_line": start_line + 1,
        "end_line": max(end_line + 1, start_line + 1),
        "severity": _diagnostic_severity(item.get("severity")),
    }
    code = _diagnostic_code(item.get("code"))
    if code is not None:
        summary["code"] = code
    return summary


def _diagnostic_severity(value: object) -> str:
    """把 LSP severity 归一为有限枚举。"""
    return {1: "error", 2: "warning", 3: "information", 4: "hint"}.get(value, "unknown")


def _diagnostic_code(value: object) -> str | None:
    """只保留短 ASCII code，不传递任意诊断正文。"""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    rendered = str(value).replace("\n", " ").replace("\r", " ").strip()
    if not rendered or not rendered.isascii() or len(rendered) > 80:
        return None
    return rendered if all(character.isalnum() or character in "._:-" for character in rendered) else None


__all__ = [
    "MAX_DIAGNOSTIC_BYTES",
    "MAX_DIAGNOSTIC_ITEMS",
    "diagnostics_timeout",
    "diagnostics_unavailable",
    "elapsed_ms",
    "summarize_diagnostics",
]
