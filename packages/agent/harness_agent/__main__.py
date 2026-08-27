"""``python -m harness_agent`` 入口：启动 Project-scoped v3 Agent Host。"""
import asyncio
import os
import time
from pathlib import Path

from harness_agent.config.config import ConfigError, DiagnosticsSettings, load_config
from harness_agent.diagnostic_log.runtime import (
    DiagnosticSettings,
    create_diagnostic_log,
    default_process_fields,
)
from harness_agent.host.agent_host import AgentHost
from harness_agent.threads.thread_persistence import workspace_fingerprint


def main() -> None:
    """创建服务端并在 asyncio 事件循环中持续处理 CLI 请求。"""
    asyncio.run(_run())


async def _run() -> None:
    """在运行中的 event loop 内创建异步 writer，并保证退出时有界关闭。"""
    workspace = Path.cwd().resolve()
    config_path = os.environ.get("HARNESS_AGENT_CONFIG_PATH")
    try:
        effective = load_config(workspace=workspace, config_path=config_path).diagnostics
    except ConfigError:
        level = os.environ.get("HARNESS_LOG_LEVEL", "info")
        effective = DiagnosticsSettings(
            level=level if level in {"debug", "info", "warn", "error"} else "info"
        )
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint=workspace_fingerprint(workspace),
        settings=DiagnosticSettings(
            level=effective.level,
            retention_days=effective.retention_days,
            max_total_bytes=effective.max_total_mib * 1024 * 1024,
            max_file_bytes=effective.max_file_mib * 1024 * 1024,
        ),
    )
    command_kind = os.environ.get("HARNESS_COMMAND_KIND", "agent")
    log.info("process.started", default_process_fields(command_kind))
    server = AgentHost(
        workspace=workspace,
        config_path=config_path,
        diagnostic_log=log,
    )
    started_at = time.monotonic()
    outcome = "completed"
    exit_code = 0
    try:
        await server.run()
    except BaseException:
        outcome = "failed"
        exit_code = 1
        raise
    finally:
        level = log.error if outcome == "failed" else log.info
        level(
            "process.stopped",
            {
                "outcome": outcome,
                "exit_code": exit_code,
                "duration_ms": max(0, round((time.monotonic() - started_at) * 1000)),
            },
        )
        await lifecycle.close()


if __name__ == "__main__":
    main()
