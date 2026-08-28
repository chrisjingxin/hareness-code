"""MCP 连接生命周期走 mcp.connection.*，工具调用仍走通用 tool.*。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import ToolMessage

from harness_agent.diagnostic_log.middleware import DiagnosticToolMiddleware
from harness_agent.extensions.mcp import (
    McpConnectionManager,
    McpServerConfig,
    build_mcp_snapshot,
    mcp_server_fingerprint,
)
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
from harness_agent.threads.context_pressure import ModelCallLifecycle
from harness_agent.runtime.run_context import RunContext


CANARY = "CANARY_HC163_MCP_SECRET_VALUE"


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def child(self, _context):
        nested = _RecordingLog()
        nested.records = self.records
        return nested

    def debug(self, event, fields) -> None:
        self.records.append(("debug", event, dict(fields)))

    def info(self, event, fields) -> None:
        self.records.append(("info", event, dict(fields)))

    def warn(self, event, fields) -> None:
        self.records.append(("warn", event, dict(fields)))

    def error(self, event, fields) -> None:
        self.records.append(("error", event, dict(fields)))


def _manager(configs, log=None) -> McpConnectionManager:
    return McpConnectionManager(
        build_mcp_snapshot(configs, revision="test"),
        diagnostic_log=log,
    )


def test_mcp_server_fingerprint_excludes_url_command_and_secrets() -> None:
    """同一 canonical id 指纹相同；URL/command/args/header/env 不得进入指纹输入。"""
    left = McpServerConfig(
        name="fs",
        transport="stdio",
        command=CANARY,
        args=(CANARY,),
        env={"API_KEY": CANARY},
    )
    right = McpServerConfig(
        name="fs",
        transport="stdio",
        command="other-bin",
        args=("safe",),
        env={"API_KEY": "other"},
    )
    assert mcp_server_fingerprint(left) == mcp_server_fingerprint(right)
    http = McpServerConfig(
        name="gh",
        transport="http",
        url=f"https://example.test/{CANARY}",
        headers={"Authorization": CANARY},
    )
    assert CANARY not in mcp_server_fingerprint(http)
    assert len(mcp_server_fingerprint(left)) == 64


@pytest.mark.asyncio
async def test_connect_success_emits_mcp_connection_completed() -> None:
    log = _RecordingLog()
    config = McpServerConfig(name="fs", transport="stdio", command="npx")
    mgr = _manager([config], log)
    tool = MagicMock()
    tool.name = "fs_list"
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[tool])
    with patch("langchain_mcp_adapters.client.MultiServerMCPClient", return_value=client):
        with patch.object(mgr, "_build_connections", return_value={"fs": {"transport": "stdio"}}):
            await mgr.connect_all()
    events = [record for record in log.records if record[1] == "mcp.connection.completed"]
    assert len(events) == 1
    fields = events[0][2]
    assert fields["server_name"] == "fs"
    assert fields["server_fingerprint"] == mcp_server_fingerprint(config)
    assert fields["transport"] == "stdio"
    assert fields["tool_count"] == 1
    assert isinstance(fields["duration_ms"], int)
    dumped = str(log.records)
    assert "npx" not in dumped
    assert CANARY not in dumped


@pytest.mark.asyncio
async def test_connect_failure_emits_mcp_connection_failed_without_canary() -> None:
    log = _RecordingLog()
    config = McpServerConfig(
        name="fs",
        transport="stdio",
        command=CANARY,
        args=(CANARY,),
        env={"TOKEN": CANARY},
    )
    mgr = _manager([config], log)
    client = MagicMock()
    client.get_tools = AsyncMock(side_effect=RuntimeError(CANARY))
    with patch("langchain_mcp_adapters.client.MultiServerMCPClient", return_value=client):
        with patch.object(mgr, "_build_connections", return_value={"fs": {"transport": "stdio", "command": CANARY}}):
            await mgr.connect_all()
    events = [record for record in log.records if record[1] == "mcp.connection.failed"]
    assert len(events) == 1
    fields = events[0][2]
    assert fields["server_name"] == "fs"
    assert fields["server_fingerprint"] == mcp_server_fingerprint(config)
    assert fields["transport"] == "stdio"
    assert fields["error_type"] == "RuntimeError"
    assert fields["summary_code"] == "MCP_CONNECT_FAILED"
    dumped = str(log.records)
    assert CANARY not in dumped


@pytest.mark.asyncio
async def test_close_emits_mcp_connection_closed() -> None:
    log = _RecordingLog()
    config = McpServerConfig(name="fs", transport="stdio", command="npx")
    mgr = _manager([config], log)
    tool = MagicMock()
    tool.name = "fs_list"
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[tool])
    client.aclose = AsyncMock()
    with patch("langchain_mcp_adapters.client.MultiServerMCPClient", return_value=client):
        with patch.object(mgr, "_build_connections", return_value={"fs": {"transport": "stdio"}}):
            await mgr.connect_all()
            await mgr.close_all()
    closed = [record for record in log.records if record[1] == "mcp.connection.closed"]
    assert closed
    assert closed[0][2]["server_name"] == "fs"
    assert closed[0][2]["server_fingerprint"] == mcp_server_fingerprint(config)
    assert closed[0][2]["outcome"] == "closed"


@pytest.mark.asyncio
async def test_mcp_tool_uses_generic_tool_events_with_mcp_kind() -> None:
    """MCP 工具不能另起 mcp.tool.*；tool_kind 必须是 mcp。"""
    log = _RecordingLog()
    tool = MagicMock()
    tool.name = "fs_list"
    tool._harness_tool_kind = "mcp"
    tool._harness_mcp_server_name = "fs"
    request = SimpleNamespace(
        tool_call={"name": "fs_list", "id": "call-1", "args": {"path": CANARY}},
        tool=tool,
        runtime=SimpleNamespace(
            context=RunContext(
                thread_id="thread",
                run_id="run-1",
                approval_mode="default",
                context_snapshot=prepare_embedded_context_snapshot(
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
                ),
                model_call_lifecycle=ModelCallLifecycle(),
                diagnostic_log=log,
            )
        ),
    )

    async def handler(_request):
        return ToolMessage(content=CANARY, name="fs_list", tool_call_id="call-1")

    await DiagnosticToolMiddleware().awrap_tool_call(request, handler)
    started = next(fields for _level, event, fields in log.records if event == "tool.started")
    completed = next(fields for _level, event, fields in log.records if event == "tool.completed")
    assert started["tool_kind"] == "mcp"
    assert started["server_name"] == "fs"
    assert completed["tool_kind"] == "mcp"
    assert completed["server_name"] == "fs"
    assert all(event != "mcp.tool.started" for _level, event, _fields in log.records)
    dumped = str(log.records)
    assert CANARY not in dumped
