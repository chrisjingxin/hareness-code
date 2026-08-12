"""Build、Compose 与 Plugin adapter 必须只经 ManagedAgentExecutor 进入 graph。"""

from __future__ import annotations

import inspect


def test_build_adapter_has_no_direct_graph_execution() -> None:
    """Build adapter 只准备 managed request，不能自行进入 LangGraph。"""
    from harness_agent.host.run_execution import BuildRunAdapter

    source = inspect.getsource(BuildRunAdapter)

    assert "ManagedAgentExecutor" in source
    assert "engine.graph" not in source
    assert ".ainvoke(" not in source
    assert ".astream(" not in source


def test_compose_stage_port_has_no_direct_graph_execution() -> None:
    """Compose stage adapter 只准备 request/observer，不能自行访问 graph。"""
    from harness_agent.compose.stage_agents import ManagedStageAgentPort

    source = inspect.getsource(ManagedStageAgentPort)

    assert "ManagedAgentExecutor" in source
    assert "engine.graph" not in source
    assert ".ainvoke(" not in source
    assert ".astream(" not in source


def test_plugin_agent_adapter_has_no_direct_graph_execution() -> None:
    """Plugin Agent 显式 target 必须复用 managed executor，而非直接 ainvoke。"""
    from harness_agent.host.agent_host import AgentHost

    source = inspect.getsource(AgentHost._plugin_delegation_targets)

    assert "ManagedAgentExecutor" in source
    assert "engine.graph" not in source
    assert ".ainvoke(" not in source
    assert ".astream(" not in source


def test_delegation_module_has_no_second_managed_engine_runner() -> None:
    """Pool lease 只能由 ManagedAgentExecutor runtime seam 负责，不能保留旧 runner。"""
    from harness_agent.runtime import agent_delegation

    source = inspect.getsource(agent_delegation)

    assert "def managed_engine_runner" not in source


def test_compose_workflow_uses_role_registry_for_activity_scope() -> None:
    """Workflow 的 Activity scope 同样只能来自固定 RoleBindingRegistry。"""
    from harness_agent.compose.workflow import ComposeWorkflow

    source = inspect.getsource(ComposeWorkflow._run_stage)

    assert "compose_scope_stage(stage)" in source
    assert 'stage in {"requirement-reviewer", "code-reviewer"}' not in source
