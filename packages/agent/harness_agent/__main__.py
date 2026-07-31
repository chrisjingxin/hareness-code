"""``python -m harness_agent`` 入口：启动 Project-scoped v3 Agent Host。"""
import asyncio
import os
from pathlib import Path

from harness_agent.host.agent_host import AgentHost


def main() -> None:
    """创建服务端并在 asyncio 事件循环中持续处理 CLI 请求。"""
    server = AgentHost(
        workspace=Path.cwd(),
        config_path=os.environ.get("HARNESS_AGENT_CONFIG_PATH"),
    )
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
