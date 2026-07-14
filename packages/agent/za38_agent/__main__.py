"""Entry point: python -m za38_agent"""
import asyncio

from za38_agent.server import JsonRpcServer


def main():
    server = JsonRpcServer()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
