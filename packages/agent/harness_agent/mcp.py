"""MCP 服务器配置解析、连接管理和工具加载。

基于 langchain-mcp-adapters 将外部 MCP Server 的工具转换为 LangChain
BaseTool，通过 create_harness_agent(tools=[...]) 注入 Agent 图。
连接生命周期由 AgentHost 管理：initialize 后建立，Host 关闭时释放。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
"""匹配 ${VAR} 环境变量引用语法。"""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 30
"""MCP 服务器连接超时（秒）。"""

DEFAULT_TOOL_TIMEOUT_SECONDS = 60
"""MCP 工具调用超时（秒）。"""

TransportType = Literal["stdio", "http", "sse"]
"""支持的 MCP 传输类型。"""


class McpConfigError(ValueError):
    """MCP 配置在可信边界校验失败时抛出的安全错误。"""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        """保存稳定错误码与字段路径，不回显配置秘密。"""
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """一个 MCP 服务器的已校验配置。"""

    name: str
    transport: TransportType
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """冻结嵌套映射，避免快照建立后仍被调用方修改。"""
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    @classmethod
    def from_mapping(cls, entry: Mapping[str, object]) -> "McpServerConfig":
        """把协议或 TOML 映射规范化为唯一的 MCP 配置值对象。"""
        name = entry.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name.strip()):
            raise McpConfigError(
                "MCP_SERVER_NAME_INVALID",
                f"Invalid server name {name!r}: only [a-zA-Z0-9_-] allowed",
                field="mcp.servers.name",
            )
        name = name.strip()
        transport = entry.get("transport", "stdio")
        if transport not in ("stdio", "http", "sse"):
            raise McpConfigError(
                "MCP_TRANSPORT_INVALID",
                f"Unsupported MCP transport {transport!r}",
                field="mcp.servers.transport",
            )
        timeout = entry.get("timeout", DEFAULT_CONNECT_TIMEOUT_SECONDS)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise McpConfigError("MCP_TIMEOUT_INVALID", "MCP 连接超时无效", field="mcp.servers.timeout")

        if transport == "stdio":
            command = entry.get("command")
            if not isinstance(command, str) or not command.strip():
                raise McpConfigError(
                    "MCP_COMMAND_REQUIRED",
                    "stdio transport requires 'command'",
                    field="mcp.servers.command",
                )
            args = _string_sequence(entry.get("args", ()), "mcp.servers.args")
            env = _string_mapping(entry.get("env", {}), "mcp.servers.env")
            return cls(
                name=name,
                transport="stdio",
                command=command.strip(),
                args=args,
                env=env,
                timeout_seconds=float(timeout),
            )

        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            raise McpConfigError(
                "MCP_URL_REQUIRED",
                f"{transport} transport requires 'url'",
                field="mcp.servers.url",
            )
        headers = _string_mapping(entry.get("headers", {}), "mcp.servers.headers")
        return cls(
            name=name,
            transport=transport,  # type: ignore[arg-type]
            url=url.strip(),
            headers=headers,
            timeout_seconds=float(timeout),
        )

    def to_document(self) -> dict[str, object]:
        """转换为不含额外运行时状态的 TOML MCP 条目。"""
        result: dict[str, object] = {"name": self.name, "transport": self.transport}
        if self.transport == "stdio":
            result["command"] = self.command
            if self.args:
                result["args"] = list(self.args)
            if self.env:
                result["env"] = dict(self.env)
        else:
            result["url"] = self.url
            if self.headers:
                result["headers"] = dict(self.headers)
        if self.timeout_seconds != DEFAULT_CONNECT_TIMEOUT_SECONDS:
            result["timeout"] = self.timeout_seconds
        return result


def _string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    """校验字符串数组，禁止写服务隐式转换任意对象。"""
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise McpConfigError("MCP_FIELD_INVALID", "MCP 字段类型无效", field=field_name)
    return tuple(value)


def _string_mapping(value: object, field_name: str) -> dict[str, str]:
    """校验字符串映射，返回与输入隔离的副本。"""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise McpConfigError("MCP_FIELD_INVALID", "MCP 字段类型无效", field=field_name)
    return dict(value)


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
    try:
        return McpServerConfig.from_mapping(entry)
    except McpConfigError as exc:
        if exc.code == "MCP_TIMEOUT_INVALID":
            # 启动时保持历史兼容：损坏的超时回退默认值；交互式写入仍严格拒绝。
            normalized = dict(entry)
            normalized["timeout"] = DEFAULT_CONNECT_TIMEOUT_SECONDS
            try:
                return McpServerConfig.from_mapping(normalized)
            except McpConfigError:
                pass
        logger.warning("mcp.servers[%d] rejected: %s", idx, exc)
        return None


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

    包含所有影响运行行为的非秘密字段：name、transport、command（stdio）、
    url（http/sse）、args、env 变量名列表（仅 key）、header 引用名列表（仅 key）、
    timeout_seconds。不包含 env 变量值、header 值或任何 token/秘密。
    无配置时返回与 {"transport": "disabled"} 一致的固定值。
    """
    from harness_agent.runtime_profile import component_fingerprint

    if not configs:
        return component_fingerprint({"transport": "disabled"})
    entries = []
    for c in sorted(configs, key=lambda c: c.name):
        entries.append(_runtime_identity_for_server(c))
    return component_fingerprint({"servers": entries})


def _runtime_identity_for_server(config: McpServerConfig) -> dict[str, object]:
    """返回包含运行字段但不包含凭据值的服务器身份。"""
    identity: dict[str, object] = {
        "name": config.name,
        "transport": config.transport,
        "args": tuple(config.args),
        "env_keys": tuple(sorted(config.env)),
        "header_keys": tuple(sorted(config.headers)),
        "timeout_seconds": config.timeout_seconds,
    }
    if config.transport == "stdio":
        identity["command"] = config.command
    else:
        identity["url"] = _safe_url_identity(config.url or "")
    return identity


def _safe_url_identity(url: str) -> Mapping[str, object]:
    """提取 URL 的连接形状，避免认证 query 值和用户信息进入身份。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return MappingProxyType({"invalid": True})
    try:
        port = parsed.port
    except ValueError:
        port = None
    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        safe_value = "<redacted>" if any(
            marker in lowered for marker in ("token", "auth", "key", "secret", "password", "credential")
        ) else value
        query.append((key, safe_value))
    return MappingProxyType(
        {
            "scheme": parsed.scheme,
            "hostname": parsed.hostname or "",
            "port": port,
            "path": parsed.path,
            "query": tuple(sorted(query)),
        }
    )


def _freeze(value: object) -> object:
    """递归冻结快照身份中的映射、列表和集合。"""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class McpConfigSnapshot:
    """Host 认可的不可变 MCP 配置快照。

    作为 McpConnectionManager 和 RuntimeProfile 的唯一配置真相来源，
    消除持久化文件、连接管理器和 Runtime 身份之间的多副本不一致。
    """

    servers: tuple[McpServerConfig, ...]
    digest: str
    revision: str
    runtime_identity: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结传入的服务器序列和身份摘要，防止绕过 builder 修改快照。"""
        object.__setattr__(self, "servers", tuple(self.servers))
        object.__setattr__(self, "runtime_identity", _freeze(self.runtime_identity))


def build_mcp_snapshot(servers: Sequence[McpServerConfig], revision: str) -> McpConfigSnapshot:
    """从已校验配置列表构建不可变 MCP 快照。

    servers 按 name 排序后冻结；digest 使用扩充后的 fingerprint；
    runtime_identity 包含脱敏的运行相关字段摘要。
    """
    from harness_agent.runtime_profile import component_fingerprint

    ordered = tuple(sorted(servers, key=lambda c: c.name))
    fingerprint = mcp_config_fingerprint(list(ordered))
    identity: dict[str, object] = {
        "server_count": len(ordered),
        "fingerprint": fingerprint,
        "servers": [_runtime_identity_for_server(c) for c in ordered],
    }
    return McpConfigSnapshot(
        servers=ordered,
        digest=fingerprint,
        revision=revision,
        runtime_identity=_freeze(identity),  # type: ignore[arg-type]
    )


class McpConnectionManager:
    """管理所有 MCP 服务器的连接生命周期和工具加载。

    使用 langchain-mcp-adapters 的 MultiServerMCPClient 建立连接，
    将 MCP 工具转换为 LangChain BaseTool 供 Agent 使用。
    """

    def __init__(self, configs: McpConfigSnapshot | Sequence[McpServerConfig]) -> None:
        """保存快照；序列输入仅为旧测试与嵌入调用保留兼容。"""
        if isinstance(configs, McpConfigSnapshot):
            self._snapshot = configs
            self._configs = list(configs.servers)
        else:
            self._configs = list(configs)
            self._snapshot = build_mcp_snapshot(self._configs, revision="legacy")
        self._tools: list[Any] = []
        self._client: Any | None = None
        self._connected = False
        self._server_statuses: dict[str, dict[str, object]] = {}

    @property
    def snapshot(self) -> McpConfigSnapshot:
        """返回当前连接管理器认可的配置快照。"""
        return self._snapshot

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
        self._snapshot = build_mcp_snapshot(self._configs, revision=self._snapshot.revision)
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

            client = MultiServerMCPClient({config.name: connection}, tool_name_prefix=True)
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
        self._snapshot = build_mcp_snapshot(self._configs, revision=self._snapshot.revision)

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

    async def apply_snapshot(self, snapshot: McpConfigSnapshot) -> list[dict[str, object]]:
        """替换配置快照并重建连接；失败只影响新快照，不回写旧 Runtime。"""
        await self.close_all()
        self._snapshot = snapshot
        self._configs = list(snapshot.servers)
        self._server_statuses = {}
        self._connected = False
        await self.connect_all()
        return self.get_server_statuses()

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
            self._client = MultiServerMCPClient(connections, tool_name_prefix=True)
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
            self._tools = []
            self._connected = False
            return
        try:
            # MultiServerMCPClient 在 0.2.x 没有显式 close 方法；
            # 工具持有的 session 在无状态模式下每次调用后自动关闭。
            # 清理引用即可让 GC 回收底层传输。
            self._client = None
            self._tools = []
            self._connected = False
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
