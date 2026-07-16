"""``python -m harness_agent`` 入口：启动 stdio JSON-RPC 服务端。"""
import asyncio

from harness_agent.server import JsonRpcServer


def main() -> None:
    """创建服务端并在 asyncio 事件循环中持续处理 CLI 请求。"""
    server = JsonRpcServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
