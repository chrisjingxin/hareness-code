"""Build、Compose 与 Plugin adapter 必须只经 ManagedAgentExecutor 进入 graph。"""

from __future__ import annotations

import inspect


def test_compose_stage_port_has_no_direct_graph_execution() -> None:
    """Compose stage adapter 只准备 request/observer，不能自行访问 graph。"""
    from harness_agent.compose.stage_agents import ManagedStageAgentPort

    source = inspect.getsource(ManagedStageAgentPort)

    assert "ManagedAgentExecutor" in source
    assert "engine.graph" not in source
    assert ".ainvoke(" not in source
    assert ".astream(" not in source
