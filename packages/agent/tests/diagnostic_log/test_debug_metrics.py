"""DEBUG 只投影 Pool/文件工具的有界计数，不含 profile 或路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.runtime.agent_engine import AgentEngine, AgentEnginePool
from harness_agent.runtime.agent_engine_profile import AgentEngineProfile
from harness_agent.tools.file_tool_metrics import FileToolMetrics


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []

    def child(self, _context):
        return self

    def debug(self, event, fields) -> None:
        self.records.append(("debug", event, dict(fields)))

    def info(self, event, fields) -> None:
        self.records.append(("info", event, dict(fields)))

    def warn(self, event, fields) -> None:
        self.records.append(("warn", event, dict(fields)))

    def error(self, event, fields) -> None:
        self.records.append(("error", event, dict(fields)))


def _profile() -> AgentEngineProfile:
    from harness_agent.runtime.agent_engine_profile import ModelRoleBinding, component_fingerprint

    def fingerprint(component: str) -> str:
        return component_fingerprint({"test": "diagnostic-pool", "component": component})

    return AgentEngineProfile(
        project_fingerprint=fingerprint("project"),
        topology_id="single-agent",
        topology_version=1,
        model_roles=(ModelRoleBinding(role="primary", model_config_fingerprint=fingerprint("model")),),
        tool_catalog_fingerprint=fingerprint("tools"),
        skill_catalog_fingerprint=fingerprint("skills"),
        mcp_config_fingerprint=fingerprint("mcp"),
        sandbox_config_fingerprint=fingerprint("sandbox"),
        policy_fingerprint=fingerprint("policy"),
        middleware_fingerprint=fingerprint("middleware"),
        prompt_template_fingerprint=fingerprint("prompt"),
    )


@pytest.mark.asyncio
async def test_pool_acquire_emits_bounded_debug_snapshot() -> None:
    log = _RecordingLog()

    async def build(profile: AgentEngineProfile) -> AgentEngine:
        return AgentEngine(profile=profile, graph=object())

    pool = AgentEnginePool(build, diagnostic_log=log)
    lease = await pool.acquire(_profile())
    snapshots = [fields for _level, event, fields in log.records if event == "runtime.pool_snapshot"]
    assert snapshots
    fields = snapshots[-1]
    assert set(fields) == {"active_count", "idle_count", "waiter_count", "eviction_count"}
    dumped = str(log.records)
    assert "prompt" not in dumped
    assert "/Users" not in dumped
    await lease.release()


def test_file_tool_metrics_payload_has_only_counts() -> None:
    metrics = FileToolMetrics()
    metrics.record_read("SNAP_" + "secret-path")
    payload = metrics.snapshot().payload()
    dumped = str(payload)
    assert "secret-path" not in dumped
    assert "SNAP_" not in dumped
    assert "read_calls" in payload
