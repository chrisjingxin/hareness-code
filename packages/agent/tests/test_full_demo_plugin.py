"""仓库内完整 Demo Plugin 的真实安装与运行组件回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness_agent.runtime.agent_catalog import AgentCatalog
from harness_agent.config.config import ModelCatalog, ModelProfile, ModelSettings
from harness_agent.extensions.mcp import McpConnectionManager, build_mcp_snapshot
from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.runtime import PluginRuntimeManager
from harness_agent.extensions.plugin_skills import SkillRegistry


DEMO_ROOT = (
    Path(__file__).resolve().parents[3]
    / "examples"
    / "plugins"
    / "harness-full-demo"
)


async def test_full_demo_plugin_installs_and_runs_all_supported_components(
    tmp_path: Path,
) -> None:
    """Demo 的 Skill/MCP/Agent/Team/Hook/LSP/Monitor 必须原样进入运行快照。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample = workspace / "experience.demo"
    sample.write_text(
        (DEMO_ROOT / "samples" / "example.demo").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    manager = PluginManager(home=tmp_path / "home")
    validation = manager.validate(DEMO_ROOT)["plugin"]
    assert isinstance(validation, dict)
    assert validation["format"] == "hybrid"
    assert validation["warnings"] == []
    components = {item["kind"]: item for item in validation["components"]}
    assert {
        "agents",
        "commands",
        "hooks",
        "lsp",
        "mcp",
        "monitors",
        "policies",
        "skills",
        "teams",
    } == set(components)
    assert all(item["count"] > 0 for item in components.values())

    installed = manager.install(DEMO_ROOT)["plugin"]
    assert isinstance(installed, dict)
    catalog = manager.catalog()

    skill_result = manager.skill_sources(catalog)
    skills = SkillRegistry(
        workspace,
        home=tmp_path / "home",
        plugin_sources=skill_result.sources,
        plugin_diagnostics=skill_result.diagnostics,
    )
    assert any(record.skill_id.endswith("/harness-full-demo/project-health") for record in skills.records)
    assert any(command["name"].endswith(":health") for command in skills.agent_commands())

    models = ModelCatalog(
        default_profile="demo",
        profiles={
            "demo": ModelProfile(
                "demo",
                ModelSettings("demo-model", "https://example.test"),
                "Demo",
            )
        },
        role_profiles={},
    )
    agent_result = manager.agent_sources(catalog)
    agents = AgentCatalog(model_catalog=models, sources=agent_result.sources)
    assert agent_result.diagnostics == ()
    assert agents.diagnostics == ()
    assert {item.agent_id for item in agents.agents} == {
        "demo-code-reviewer",
        "demo-review-lead",
        "demo-test-reviewer",
    }

    teams = manager.team_definitions(catalog)
    assert teams.diagnostics == ()
    assert [team.team_id for team in teams.teams] == ["demo-quality-team"]

    mcp_result = manager.mcp_servers(catalog, workspace=workspace)
    assert mcp_result.diagnostics == ()
    mcp = McpConnectionManager(
        build_mcp_snapshot(mcp_result.servers, "full-demo-test")
    )
    await mcp.connect_all()
    assert mcp.get_server_statuses()[0]["status"] == "connected"
    tools = mcp.get_tools()
    assert len(tools) == 1
    assert tools[0].name.endswith("_plugin_inventory")
    inventory = await tools[0].ainvoke({"path": "."})
    assert "harness-commands" in str(inventory)

    runtime = PluginRuntimeManager(
        manager.runtime_catalog(catalog, workspace=workspace)
    )
    await runtime.start()
    try:
        hook_results = await runtime.hooks.run(
            "PreToolUse",
            tool_name="execute",
            payload={
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo demo-forbidden"},
            },
        )
        assert hook_results[0].blocks_pre_tool == (
            True,
            "Harness Full Demo Hook blocked demo-forbidden",
        )

        for _ in range(100):
            if "harness-full-demo ready" in runtime.monitors.context():
                break
            await asyncio.sleep(0.01)
        assert "harness-full-demo ready" in runtime.monitors.context()

        hover = await runtime.lsp.query(
            "hover",
            sample.name,
            1,
            1,
            str(workspace),
        )
        assert hover["server"] == "demo-language"
        assert "Harness Full Demo LSP is active" in str(hover["results"])
    finally:
        await runtime.aclose()
        await mcp.close_all()
