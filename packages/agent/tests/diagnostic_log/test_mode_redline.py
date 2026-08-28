"""停点 3 红线：Compose/child/MCP/stdlib/故障路径的 canary 不得进入 JSONL。"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness_agent.diagnostic_log.runtime import DiagnosticSettings, create_diagnostic_log
from harness_agent.diagnostic_log.stdlib import install_harness_stdlib_handler
from harness_agent.extensions.mcp import McpConnectionManager, McpServerConfig, build_mcp_snapshot
from harness_agent.runtime.child_stream import child_context_for
from harness_agent.runtime.execution_binding import ExecutionRef
from harness_agent.runtime.run_context import RunContext
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot


CANARY = "CANARY_HC163_MODE_SECRET"


def _scan_jsonl(root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.jsonl"))


def _context(log, execution_id: str = "root") -> RunContext:
    return RunContext(
        thread_id="thread-1",
        run_id="run-1",
        approval_mode="yolo",
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id="thread-1",
            system_prompt=CANARY,
            workspace="/tmp",
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        execution_id=execution_id,
        agent_id="main",
        diagnostic_log=log,
    )


@pytest.mark.parametrize("level", ["info", "debug"])
@pytest.mark.asyncio
async def test_compose_child_mcp_stdlib_fault_canary_zero_hit(tmp_path: Path, level: str) -> None:
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path / "logs",
        settings=DiagnosticSettings(level=level),
        queue_limits=(8, 16 * 1024, 2, 4 * 1024),
    )
    install_harness_stdlib_handler(log)
    logging.getLogger("harness_agent.runtime.agent").warning("prompt=%s", CANARY)

    parent = _context(log)
    child = child_context_for(
        parent,
        child_ref=ExecutionRef(
            thread_id="thread-1",
            run_id="run-1",
            execution_id="child-redline",
            parent_execution_id="root",
        ),
        agent_id="general-purpose",
    )
    child.diagnostic_log.info(
        "tool.started",
        {"tool_name": "read_file", "tool_kind": "read", "model_round": 1},
    )

    config = McpServerConfig(
        name="fs",
        transport="stdio",
        command=CANARY,
        args=(CANARY,),
        env={"TOKEN": CANARY},
        url=None,
    )
    mgr = McpConnectionManager(build_mcp_snapshot([config], revision="test"), diagnostic_log=log)
    client = MagicMock()
    client.get_tools = AsyncMock(side_effect=RuntimeError(CANARY))
    with patch("langchain_mcp_adapters.client.MultiServerMCPClient", return_value=client):
        with patch.object(mgr, "_build_connections", return_value={"fs": {"transport": "stdio", "command": CANARY}}):
            await mgr.connect_all()

    for index in range(12):
        log.debug(
            "runtime.pool_snapshot",
            {"active_count": index, "idle_count": 0, "waiter_count": 0, "eviction_count": 0},
        )
    await lifecycle.close()
    dumped = _scan_jsonl(tmp_path / "logs")
    assert dumped
    assert CANARY not in dumped
