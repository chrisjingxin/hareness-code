"""在真实 Tool handler 边界记录 Diagnostic Event，不投影业务正文。"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from harness_agent.diagnostic_log.runtime import ensure_log, safe_context_value
from harness_agent.policy.tool_risk import get_tool_kind
from harness_agent.runtime.run_context import RunContext


class DiagnosticToolMiddleware(AgentMiddleware):
    """包裹实际 tool handler：审批等待结束后才开始计时。"""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """同步工具调用记录 started/completed/failed，失败不得改变 handler 结果。"""
        log, tool_name, tool_kind, server_name, model_round = _tool_log_context(request)
        started = time.monotonic()
        fields: dict[str, object] = {
            "tool_name": tool_name,
            "tool_kind": tool_kind,
            "model_round": model_round,
        }
        if server_name is not None:
            fields["server_name"] = server_name
        log.info(
            "tool.started",
            fields,
        )
        try:
            result = handler(request)
        except Exception as exc:
            _log_tool_failure(log, tool_name, tool_kind, server_name, started, exc)
            raise
        _log_tool_result(log, tool_name, tool_kind, server_name, started, result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """异步工具调用沿用同一事件和脱敏边界。"""
        log, tool_name, tool_kind, server_name, model_round = _tool_log_context(request)
        started = time.monotonic()
        fields: dict[str, object] = {
            "tool_name": tool_name,
            "tool_kind": tool_kind,
            "model_round": model_round,
        }
        if server_name is not None:
            fields["server_name"] = server_name
        log.info(
            "tool.started",
            fields,
        )
        try:
            result = await handler(request)
        except Exception as exc:
            _log_tool_failure(log, tool_name, tool_kind, server_name, started, exc)
            raise
        _log_tool_result(log, tool_name, tool_kind, server_name, started, result)
        return result


def _tool_log_context(request: ToolCallRequest) -> tuple[Any, str, str, str | None, int]:
    """从 RunContext 读取 logger 与 model_round；身份不合法时省略。"""
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None)
    raw_log = getattr(context, "diagnostic_log", None) if isinstance(context, RunContext) else None
    log = ensure_log(raw_log)
    tool_call = request.tool_call if isinstance(request.tool_call, dict) else {}
    raw_name = str(tool_call.get("name") or "unknown_tool")
    tool_name = safe_context_value(raw_name) or "unknown_tool"
    tool_kind = _diagnostic_tool_kind(request, tool_name)
    server_name = _diagnostic_mcp_server_name(request) if tool_kind == "mcp" else None
    lifecycle = getattr(context, "model_call_lifecycle", None) if context is not None else None
    model_round = int(getattr(lifecycle, "model_round", 0) or 0) or 1
    tool_call_id = safe_context_value(str(tool_call.get("id") or ""))
    if tool_call_id is not None:
        log = log.child({"tool_call_id": tool_call_id})
    return log, tool_name, tool_kind, server_name, model_round


def _log_tool_result(
    log: Any,
    tool_name: str,
    tool_kind: str,
    server_name: str | None,
    started: float,
    result: object,
) -> None:
    status = getattr(result, "status", None)
    outcome = "error" if status == "error" else "completed"
    fields: dict[str, object] = {
        "tool_name": tool_name,
        "tool_kind": tool_kind,
        "outcome": outcome,
        "duration_ms": _duration_ms(started),
        "result_bytes": _result_bytes(result),
        "truncated": bool(getattr(result, "truncated", False)),
    }
    if server_name is not None:
        fields["server_name"] = server_name
    log.info("tool.completed", fields)


def _log_tool_failure(
    log: Any,
    tool_name: str,
    tool_kind: str,
    server_name: str | None,
    started: float,
    error: BaseException,
) -> None:
    fields: dict[str, object] = {
        "tool_name": tool_name,
        "tool_kind": tool_kind,
        "duration_ms": _duration_ms(started),
        "failure_stage": "tool_handler",
        "error_code": type(error).__name__,
        "error_type": type(error).__name__,
        "retryable": False,
        "summary_code": "tool_handler_failed",
    }
    if server_name is not None:
        fields["server_name"] = server_name
    log.error("tool.failed", fields)


def _diagnostic_tool_kind(request: ToolCallRequest, tool_name: str) -> str:
    """MCP 工具记 tool_kind=mcp；其余沿用审批风险类别。"""
    tool = getattr(request, "tool", None)
    if getattr(tool, "_harness_tool_kind", None) == "mcp":
        return "mcp"
    extra = getattr(tool, "metadata", None)
    if isinstance(extra, dict) and extra.get("harness_tool_kind") == "mcp":
        return "mcp"
    return get_tool_kind(tool_name).value


def _diagnostic_mcp_server_name(request: ToolCallRequest) -> str | None:
    """只读取 MCP owner 写入的已校验配置名，不从工具名反推。"""
    tool = getattr(request, "tool", None)
    name = safe_context_value(getattr(tool, "_harness_mcp_server_name", None))
    if name is not None:
        return name
    metadata = getattr(tool, "metadata", None)
    if isinstance(metadata, dict):
        return safe_context_value(metadata.get("harness_mcp_server_name"))
    return None


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _result_bytes(result: object) -> int:
    """只统计结果大小，不把内容写入 Diagnostic Log。"""
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, bytes):
        return len(content)
    if content is None:
        return 0
    try:
        return len(json.dumps(content, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0
