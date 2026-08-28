"""DiagnosticToolMiddleware 只记录安全字段，且不把审批等待计入 duration。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from harness_agent.diagnostic_log.middleware import DiagnosticToolMiddleware
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
from harness_agent.threads.context_pressure import ModelCallLifecycle
from harness_agent.runtime.run_context import RunContext


CANARY = "CANARY_HC163_TOOL_ARGS_AND_RESULT"


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []
        self.context: dict[str, object] = {}

    def child(self, context):
        child = _RecordingLog()
        child.records = self.records
        child.context = {**self.context, **context}
        return child

    def info(self, event, fields) -> None:
        self.records.append(("info", event, dict(fields)))

    def warn(self, event, fields) -> None:
        self.records.append(("warn", event, dict(fields)))

    def error(self, event, fields) -> None:
        self.records.append(("error", event, dict(fields)))

    def debug(self, event, fields) -> None:
        self.records.append(("debug", event, dict(fields)))


def _context(log: _RecordingLog, model_round: int = 1) -> RunContext:
    snapshot = prepare_embedded_context_snapshot(
        thread_id="thread",
        system_prompt="prompt",
        workspace="/tmp",
        sandboxed=False,
        provider=None,
        approval_mode="default",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )
    lifecycle = ModelCallLifecycle()
    lifecycle.model_round = model_round
    return RunContext(
        thread_id="thread",
        run_id="run-1",
        approval_mode="default",
        context_snapshot=snapshot,
        model_call_lifecycle=lifecycle,
        diagnostic_log=log,
    )


@pytest.mark.asyncio
async def test_tool_middleware_logs_handler_duration_without_args_or_result() -> None:
    """handler 边界只记 name/kind/duration/bytes，不复制 args 或 result。"""
    log = _RecordingLog()
    request = SimpleNamespace(
        tool_call={
            "name": "read_file",
            "id": "call-1",
            "args": {"path": CANARY, "command": CANARY},
        },
        runtime=SimpleNamespace(context=_context(log)),
    )

    async def handler(_request):
        return ToolMessage(
            content=CANARY,
            name="read_file",
            tool_call_id="call-1",
        )

    result = await DiagnosticToolMiddleware().awrap_tool_call(request, handler)

    assert result.content == CANARY
    events = [event for _, event, _ in log.records]
    assert events == ["tool.started", "tool.completed"]
    started = log.records[0][2]
    completed = log.records[1][2]
    assert started == {
        "tool_name": "read_file",
        "tool_kind": "read",
        "model_round": 1,
    }
    assert completed["outcome"] == "completed"
    assert completed["result_bytes"] == len(CANARY.encode("utf-8"))
    assert CANARY not in repr(log.records)


@pytest.mark.asyncio
async def test_tool_middleware_failure_does_not_include_exception_message() -> None:
    """handler 抛错时记 tool.failed，不含异常原文。"""
    log = _RecordingLog()
    request = SimpleNamespace(
        tool_call={"name": "execute", "id": "call-err", "args": {"command": CANARY}},
        runtime=SimpleNamespace(context=_context(log)),
    )

    async def handler(_request):
        raise RuntimeError(CANARY)

    with pytest.raises(RuntimeError, match=CANARY):
        await DiagnosticToolMiddleware().awrap_tool_call(request, handler)

    assert [event for _, event, _ in log.records] == ["tool.started", "tool.failed"]
    failed = log.records[1][2]
    assert failed["tool_name"] == "execute"
    assert failed["summary_code"] == "tool_handler_failed"
    assert CANARY not in repr(log.records)
