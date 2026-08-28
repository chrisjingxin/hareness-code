"""Plugin Hook/LSP/Monitor 的专用 stdout 协议不得被 Diagnostic Log 拦截。"""

from __future__ import annotations

import inspect

from harness_agent.plugins import runtime as plugin_runtime


def test_hook_runner_does_not_redirect_stdout_to_diagnostic_log() -> None:
    """Hook 可写结构化生命周期，但协议 stdout 始终只由 PIPE 读取。"""
    source = inspect.getsource(plugin_runtime.HookRunner)
    assert "create_diagnostic_log" not in source
    assert 'diagnostic_log.info("hook.started"' in source
    assert "stdin=asyncio.subprocess.PIPE" in source
    assert "stdout=asyncio.subprocess.PIPE" in source
    assert '"stdout": stdout_text' not in source


def test_monitor_manager_does_not_redirect_stdout_to_diagnostic_log() -> None:
    source = inspect.getsource(plugin_runtime.MonitorManager)
    assert "DiagnosticLog" not in source
    assert "create_diagnostic_log" not in source
