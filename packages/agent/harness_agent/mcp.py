"""MCP 服务器配置解析、连接管理和工具加载。

基于 langchain-mcp-adapters 将外部 MCP Server 的工具转换为 LangChain
BaseTool，通过 create_harness_agent(tools=[...]) 注入 Agent 图。
连接生命周期由 JsonRpcServer 管理：initialize 后建立，shutdown 时释放。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""匹配 ${VAR} 环境变量引用语法。"""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 30
"""MCP 服务器连接超时（秒）。"""

DEFAULT_TOOL_TIMEOUT_SECONDS = 60
"""MCP 工具调用超时（秒）。"""

TransportType = Literal["stdio", "http", "sse"]
"""支持的 MCP 传输类型。"""


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """一个 MCP 服务器的已校验配置。"""

    name: str
    transport: TransportType
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS


def parse_mcp_config(mcp_section: dict[str, Any] | None) -> list[McpServerConfig]:
    """从 TOML [mcp] 区段解析服务器配置列表。

    无效配置记录警告并跳过，不阻止 sidecar 启动。
    环境变量 ${VAR} 在此阶段不展开，延迟到连接时解析。
    """
    if not mcp_section or not isinstance(mcp_section, dict):
        return []
    servers_raw = mcp_section.get("servers")
    if not isinstance(servers_raw, list):
        if servers_raw is not None:
            logger.warning("mcp.servers must be an array of tables; ignoring")
        return []

    configs: list[McpServerConfig] = []
    for idx, entry in enumerate(servers_raw):
        if not isinstance(entry, dict):
            logger.warning("mcp.servers[%d] is not a table; skipping", idx)
            continue
        config = _parse_single_server(entry, idx)
        if config is not None:
            configs.append(config)
    return configs


def _parse_single_server(entry: dict[str, Any], idx: int) -> McpServerConfig | None:
    """校验并解析单个 MCP 服务器配置条目。"""
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("mcp.servers[%d] missing or empty 'name'; skipping", idx)
        return None
    name = name.strip()

    transport = entry.get("transport", "stdio")
    if transport not in ("stdio", "http", "sse"):
        logger.warning(
            "mcp.servers[%d] (%s): unsupported transport %r; skipping", idx, name, transport
        )
        return None

    timeout = entry.get("timeout", DEFAULT_CONNECT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS

    if transport == "stdio":
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            logger.warning(
                "mcp.servers[%d] (%s): stdio transport requires 'command'; skipping", idx, name
            )
            return None
        args_raw = entry.get("args", [])
        if not isinstance(args_raw, list):
            logger.warning(
                "mcp.servers[%d] (%s): 'args' must be an array; skipping", idx, name
            )
            return None
        args = tuple(str(a) for a in args_raw)
        env_raw = entry.get("env", {})
        env = {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {}
        return McpServerConfig(
            name=name, transport="stdio", command=command.strip(),
            args=args, env=env, timeout_seconds=float(timeout),
        )

    # http / sse
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        logger.warning(
            "mcp.servers[%d] (%s): %s transport requires 'url'; skipping", idx, name, transport
        )
        return None
    headers_raw = entry.get("headers", {})
    headers = {str(k): str(v) for k, v in headers_raw.items()} if isinstance(headers_raw, dict) else {}
    return McpServerConfig(
        name=name, transport=transport,  # type: ignore[arg-type]
        url=url.strip(), headers=headers, timeout_seconds=float(timeout),
    )


def expand_env_vars(value: str) -> str | None:
    """展开字符串中的 ${VAR} 引用；任一变量缺失时返回 None。"""
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            missing.append(var_name)
            return ""
        return env_value

    result = _ENV_VAR_RE.sub(_replace, value)
    if missing:
        logger.warning(
            "MCP environment variable(s) not set: %s", ", ".join(sorted(set(missing)))
        )
        return None
    return result


def mcp_config_fingerprint(configs: list[McpServerConfig]) -> str:
    """计算 MCP 配置的脱敏 SHA-256 指纹。

    仅包含服务器名称和传输类型，不包含命令路径、URL、凭据。
    无配置时返回与 {"transport": "disabled"} 一致的固定值。
    """
    from harness_agent.runtime_profile import component_fingerprint

    if not configs:
        return component_fingerprint({"transport": "disabled"})
    entries = [{"name": c.name, "transport": c.transport} for c in sorted(configs, key=lambda c: c.name)]
    return component_fingerprint({"servers": entries})


class McpConnectionManager:
    """管理所有 MCP 服务器的连接生命周期和工具加载。

    使用 langchain-mcp-adapters 的 MultiServerMCPClient 建立连接，
    将 MCP 工具转换为 LangChain BaseTool 供 Agent 使用。
    """

    def __init__(self, configs: list[McpServerConfig]) -> None:
        """保存已校验配置；实际连接在 connect_all() 中建立。"""
        self._configs = configs
        self._tools: list[Any] = []
        self._client: Any | None = None
        self._connected = False
        self._server_statuses: dict[str, dict[str, object]] = {}

    @property
    def connected(self) -> bool:
        """是否已成功建立至少一次连接。"""
        return self._connected

    def get_tools(self) -> list[Any]:
        """返回已加载的 MCP 工具列表（LangChain BaseTool）。"""
        return list(self._tools)

    def get_tool_names(self) -> list[str]:
        """返回所有 MCP 工具的名称，用于审批注册。"""
        return [tool.name for tool in self._tools]

    def get_server_statuses(self) -> list[dict[str, object]]:
        """返回每个 MCP 服务器的连接状态和工具列表。

        工具名通过 ``{server_name}_`` 前缀从已加载工具中匹配归属。
        """
        result: list[dict[str, object]] = []
        for config in self._configs:
            entry = dict(self._server_statuses.get(config.name, {
                "name": config.name,
                "transport": config.transport,
                "status": "failed",
                "error": "not connected",
            }))
            # 按服务器名称前缀匹配归属工具
            prefix = f"{config.name}_"
            entry["tool_names"] = [
                t.name for t in self._tools
                if hasattr(t, "name") and t.name.startswith(prefix)
            ]
            result.append(entry)
        return result

    async def add_server(self, config: McpServerConfig) -> dict[str, object]:
        """运行时添加并连接单个 MCP 服务器，合并其工具到已有列表。

        连接失败不影响已有工具，仅记录状态。
        """
        self._configs.append(config)
        connection = self._build_single_connection(config)
        if connection is None:
            # 环境变量缺失导致跳过，记录 skipped 状态
            self._server_statuses[config.name] = {
                "name": config.name,
                "transport": config.transport,
                "status": "skipped",
                "error": "environment variable(s) not set",
            }
            return dict(self._server_statuses[config.name])

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient({config.name: connection})
            new_tools = await asyncio.wait_for(
                client.get_tools(),
                timeout=config.timeout_seconds,
            )
            self._tools.extend(new_tools)
            self._server_statuses[config.name] = {
                "name": config.name,
                "transport": config.transport,
                "status": "connected",
                "tool_names": [t.name for t in new_tools if hasattr(t, "name")],
            }
            logger.info("MCP server '%s' hot-connected: %d tool(s)", config.name, len(new_tools))
        except Exception as exc:
            logger.warning("MCP server '%s' hot-connect failed: %s", config.name, exc)
            self._server_statuses[config.name] = {
                "name": config.name,
                "transport": config.transport,
                "status": "failed",
                "error": str(exc),
            }

        return dict(self._server_statuses[config.name])

    def remove_server(self, name: str) -> bool:
        """运行时移除指定 MCP 服务器的工具并更新状态。

        Returns:
            是否找到并移除了该服务器。
        """
        # 从配置列表中移除
        self._configs = [c for c in self._configs if c.name != name]

        # 按前缀移除工具
        prefix = f"{name}_"
        before = len(self._tools)
        self._tools = [t for t in self._tools if not (hasattr(t, "name") and t.name.startswith(prefix))]
        removed_count = before - len(self._tools)

        # 更新状态
        if name in self._server_statuses:
            self._server_statuses[name] = {
                "name": name,
                "transport": self._server_statuses[name].get("transport", "stdio"),
                "status": "removed",
                "tool_names": [],
            }
            logger.info("MCP server '%s' removed: %d tool(s) unloaded", name, removed_count)
            return True

        return False

    async def connect_all(self) -> None:
        """建立所有已配置 MCP 服务器的连接并加载工具。

        连接失败的服务器记录警告并跳过，不阻止 Agent 启动。
        """
        if not self._configs:
            self._connected = True
            return

        from langchain_mcp_adapters.client import MultiServerMCPClient

        connections = self._build_connections()
        if not connections:
            logger.warning("No valid MCP connections after environment expansion")
            self._connected = True
            return

        try:
            self._client = MultiServerMCPClient(connections)
            self._tools = await asyncio.wait_for(
                self._client.get_tools(),
                timeout=max(c.timeout_seconds for c in self._configs),
            )
            self._connected = True
            # 标记所有成功连接的服务器
            for name in connections:
                self._server_statuses[name] = {
                    "name": name,
                    "transport": next(c.transport for c in self._configs if c.name == name),
                    "status": "connected",
                }
            logger.info(
                "MCP connected: %d server(s), %d tool(s) loaded",
                len(connections), len(self._tools),
            )
        except asyncio.TimeoutError:
            logger.warning("MCP connection timed out; continuing without MCP tools")
            self._tools = []
            self._connected = True
            for name in connections:
                self._server_statuses[name] = {
                    "name": name,
                    "transport": next(c.transport for c in self._configs if c.name == name),
                    "status": "failed",
                    "error": "connection timed out",
                }
        except Exception as exc:
            logger.warning("MCP connection failed: %s; continuing without MCP tools", exc)
            self._tools = []
            self._connected = True
            for name in connections:
                self._server_statuses[name] = {
                    "name": name,
                    "transport": next(c.transport for c in self._configs if c.name == name),
                    "status": "failed",
                    "error": str(exc),
                }

    async def close_all(self) -> None:
        """关闭所有 MCP 连接，释放子进程和网络资源。"""
        if self._client is None:
            return
        try:
            # MultiServerMCPClient 在 0.2.x 没有显式 close 方法；
            # 工具持有的 session 在无状态模式下每次调用后自动关闭。
            # 清理引用即可让 GC 回收底层传输。
            self._client = None
            self._tools = []
            logger.info("MCP connections closed")
        except Exception as exc:
            logger.warning("Error closing MCP connections: %s", exc)

    def _build_connections(self) -> dict[str, Any]:
        """将 McpServerConfig 列表转换为 MultiServerMCPClient 的 connections 字典。

        环境变量在此时展开；缺失变量的服务器被跳过。
        """
        connections: dict[str, Any] = {}
        for config in self._configs:
            conn = self._build_single_connection(config)
            if conn is not None:
                connections[config.name] = conn
            else:
                # 环境变量缺失导致跳过，记录 skipped 状态
                self._server_statuses[config.name] = {
                    "name": config.name,
                    "transport": config.transport,
                    "status": "skipped",
                    "error": "environment variable(s) not set",
                }
        return connections

    def _build_single_connection(self, config: McpServerConfig) -> dict[str, Any] | None:
        """构建单个服务器的连接参数；环境变量缺失时返回 None。"""
        if config.transport == "stdio":
            command = expand_env_vars(config.command or "")
            if command is None:
                logger.warning("MCP server %r: missing env vars in command; skipping", config.name)
                return None
            args = []
            for arg in config.args:
                expanded = expand_env_vars(arg)
                if expanded is None:
                    logger.warning("MCP server %r: missing env vars in args; skipping", config.name)
                    return None
                args.append(expanded)
            env = {}
            for key, value in config.env.items():
                expanded = expand_env_vars(value)
                if expanded is None:
                    logger.warning(
                        "MCP server %r: missing env var for %s; skipping", config.name, key
                    )
                    return None
                env[key] = expanded
            return {
                "transport": "stdio",
                "command": command,
                "args": args,
                "env": env or None,
            }

        # http / sse
        url = expand_env_vars(config.url or "")
        if url is None:
            logger.warning("MCP server %r: missing env vars in url; skipping", config.name)
            return None
        headers = {}
        for key, value in config.headers.items():
            expanded = expand_env_vars(value)
            if expanded is None:
                logger.warning(
                    "MCP server %r: missing env var in header %s; skipping", config.name, key
                )
                return None
            headers[key] = expanded

        # langchain-mcp-adapters 0.2.x 使用 "streamable_http" 作为 HTTP 传输键
        transport_key = "streamable_http" if config.transport == "http" else "sse"
        conn: dict[str, Any] = {
            "transport": transport_key,
            "url": url,
        }
        if headers:
            conn["headers"] = headers
        return conn
