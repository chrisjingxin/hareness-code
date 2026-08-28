"""Build 诊断红线：INFO/DEBUG JSONL 不得出现注入 canary。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from harness_agent.diagnostic_log.middleware import DiagnosticToolMiddleware
from harness_agent.diagnostic_log.runtime import DiagnosticSettings, create_diagnostic_log
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
from harness_agent.threads.context_pressure import ModelCallLifecycle
from harness_agent.runtime.run_context import RunContext
from harness_agent.threads.thread_persistence import TranscriptAppend, ThreadPersistence
from tests.support.thread_fixtures import accept_thread

CANARY = "CANARY_HC163_UNIQUE_SECRET_VALUE"


def _scan_jsonl(root: Path) -> str:
    parts: list[str] = []
    for path in root.rglob("*.jsonl"):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.mark.parametrize("level", ["info", "debug"])
@pytest.mark.asyncio
async def test_build_owners_never_write_canary_to_jsonl(tmp_path: Path, level: str) -> None:
    """Tool/Persistence/Context 路径把同一 canary 注入正文后，落盘记录必须为零命中。"""
    fingerprint = "a" * 64
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint=fingerprint,
        root=tmp_path / "logs",
        settings=DiagnosticSettings(level=level),
        start_worker=True,
    )
    snapshot = prepare_embedded_context_snapshot(
        thread_id="thread",
        system_prompt=CANARY,
        workspace=str(tmp_path),
        sandboxed=False,
        provider=None,
        approval_mode="default",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )
    lifecycle_state = ModelCallLifecycle()
    lifecycle_state.model_round = 1
    context = RunContext(
        thread_id="thread",
        run_id="run-1",
        approval_mode="default",
        context_snapshot=snapshot,
        model_call_lifecycle=lifecycle_state,
        diagnostic_log=log,
    )
    request = SimpleNamespace(
        tool_call={"name": "execute", "id": "call-1", "args": {"command": CANARY}},
        runtime=SimpleNamespace(context=context),
    )

    async def handler(_request):
        return ToolMessage(content=CANARY, name="execute", tool_call_id="call-1")

    await DiagnosticToolMiddleware().awrap_tool_call(request, handler)

    store = await ThreadPersistence.open(project=tmp_path / "project", home=tmp_path / "home")
    store.bind_diagnostic_log(log)
    await accept_thread(store, "thread", CANARY, run_id="run-1")
    await store.append_transcript(
        TranscriptAppend(
            thread_id="thread",
            record_id="assistant-1",
            kind="assistant",
            content=CANARY,
            run_id="run-1",
        )
    )
    from harness_agent.threads.context_projection import ContextProjector

    await ContextProjector(store).project("thread")
    await store.close()
    await lifecycle.close()

    dumped = _scan_jsonl(tmp_path / "logs")
    assert dumped
    assert CANARY not in dumped
