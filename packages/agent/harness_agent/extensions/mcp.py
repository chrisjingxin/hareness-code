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
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from harness_agent.diagnostic_log.runtime import ensure_log
from harness_agent.runtime.resource_lifecycle import (
    ResourceScope,
    ResourceState,
    SharedResourceHandle,
    SharedResourceLease,
    SharedResourceUnavailableError,
)

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
    cwd: str | None = None
    source: str = "config"
    source_fingerprint: str | None = None
    inherit_environment: bool = True
    plugin_root: str | None = None
    plugin_data: str | None = None

    def __post_init__(self) -> None:
        """冻结嵌套映射，避免快照建立后仍被调用方修改。"""
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.cwd is not None and not self.cwd.strip():
            raise McpConfigError("MCP_CWD_INVALID", "MCP cwd 不能为空", field="mcp.servers.cwd")
        if not self.source:
            raise McpConfigError("MCP_SOURCE_INVALID", "MCP source 不能为空")

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


def _server_status(
    config: McpServerConfig,
    status: str,
    *,
    error: str | None = None,
) -> dict[str, object]:
    """构造不含连接对象的单 server 状态记录。"""
    result: dict[str, object] = {
        "name": config.name,
        "transport": config.transport,
        "source": config.source,
        "status": status,
    }
    if error is not None:
        result["error"] = error
    return result


def _create_plugin_http_client(
    headers: dict[str, str] | None = None,
    timeout: Any | None = None,
    auth: Any | None = None,
) -> Any:
    """为 Plugin HTTP/SSE 禁用自动 redirect，避免认证 header 跨 origin 传播。"""
    import httpx

    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
    )


def mcp_server_fingerprint(config: McpServerConfig) -> str:
    """MCP server canonical id 的不可逆 hash，不含 URL、command、args、header 或 env。"""
    from harness_agent.runtime.agent_engine_profile import component_fingerprint

    return component_fingerprint(
        {
            "name": config.name,
            "transport": config.transport,
            "source": config.source,
        }
    )


def _mark_mcp_tool(tool: Any, server_name: str) -> None:
    """给 MCP 工具打上类型与配置名诊断标记；失败不影响工具本身。"""
    try:
        object.__setattr__(tool, "_harness_tool_kind", "mcp")
        object.__setattr__(tool, "_harness_mcp_server_name", server_name)
    except Exception:
        try:
            tool._harness_tool_kind = "mcp"
            tool._harness_mcp_server_name = server_name
        except Exception:
            metadata = getattr(tool, "metadata", None)
            if isinstance(metadata, dict):
                metadata["harness_tool_kind"] = "mcp"
                metadata["harness_mcp_server_name"] = server_name


def mcp_config_fingerprint(configs: list[McpServerConfig]) -> str:
    """计算 MCP 配置的脱敏 SHA-256 指纹。

    包含所有影响运行行为的非秘密字段：name、transport、command（stdio）、
    url（http/sse）、args、env 变量名列表（仅 key）、header 引用名列表（仅 key）、
    timeout_seconds、来源、包内容指纹、cwd 与环境继承策略。不包含 env
    变量值、header 值或任何 token/秘密。
    无配置时返回与 {"transport": "disabled"} 一致的固定值。
    """
    from harness_agent.runtime.agent_engine_profile import component_fingerprint

    if not configs:
        return component_fingerprint({"transport": "disabled"})
    entries = []
    for c in sorted(configs, key=lambda c: c.name):
        entries.append(_engine_identity_for_server(c))
    return component_fingerprint({"servers": entries})


def _engine_identity_for_server(config: McpServerConfig) -> dict[str, object]:
    """返回包含运行字段但不包含凭据值的服务器身份。"""
    identity: dict[str, object] = {
        "name": config.name,
        "transport": config.transport,
        "args": tuple(config.args),
        "env_keys": tuple(sorted(config.env)),
        "header_keys": tuple(sorted(config.headers)),
        "timeout_seconds": config.timeout_seconds,
        "cwd": config.cwd,
        "source": config.source,
        "source_fingerprint": config.source_fingerprint,
        "inherit_environment": config.inherit_environment,
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

    作为 McpConnectionManager 和 AgentEngineProfile 的唯一配置真相来源，
    消除持久化文件、连接管理器和 AgentEngine 身份之间的多副本不一致。
    """

    servers: tuple[McpServerConfig, ...]
    digest: str
    revision: str
    engine_identity: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结传入的服务器序列和身份摘要，防止绕过 builder 修改快照。"""
        object.__setattr__(self, "servers", tuple(self.servers))
        object.__setattr__(self, "engine_identity", _freeze(self.engine_identity))


def build_mcp_snapshot(servers: Sequence[McpServerConfig], revision: str) -> McpConfigSnapshot:
    """从已校验配置列表构建不可变 MCP 快照。

    servers 按 name 排序后冻结；digest 使用扩充后的 fingerprint；
    engine_identity 包含脱敏的运行相关字段摘要。
    """
    from harness_agent.runtime.agent_engine_profile import component_fingerprint

    ordered = tuple(sorted(servers, key=lambda c: c.name))
    fingerprint = mcp_config_fingerprint(list(ordered))
    identity: dict[str, object] = {
        "server_count": len(ordered),
        "fingerprint": fingerprint,
        "servers": [_engine_identity_for_server(c) for c in ordered],
    }
    return McpConfigSnapshot(
        servers=ordered,
        digest=fingerprint,
        revision=revision,
        engine_identity=_freeze(identity),  # type: ignore[arg-type]
    )


@dataclass(slots=True)
class _McpRuntime:
    """一个 immutable MCP snapshot 对应的 Host 级连接和工具集合。"""

    snapshot: McpConfigSnapshot
    tools: list[Any] = field(default_factory=list)
    client: Any | None = None
    connected: bool = False
    server_statuses: dict[str, dict[str, object]] = field(default_factory=dict)


class McpConnectionManager:
    """管理所有 MCP 服务器的连接生命周期和工具加载。

    使用 langchain-mcp-adapters 的 MultiServerMCPClient 建立连接，
    将 MCP 工具转换为 LangChain BaseTool 供 Agent 使用。
    """

    def __init__(
        self,
        snapshot: McpConfigSnapshot | Sequence[McpServerConfig],
        *,
        diagnostic_log: Any | None = None,
    ) -> None:
        """保存初始快照，并把每个连接快照置于 Host owner 之下。"""
        if not isinstance(snapshot, McpConfigSnapshot):
            snapshot = build_mcp_snapshot(snapshot, revision="legacy")
        self._resources: dict[str, list[SharedResourceHandle[_McpRuntime]]] = {}
        self._current_resource = self._create_resource(snapshot)
        self._resources[snapshot.digest] = [self._current_resource]
        self._diagnostic_log = ensure_log(diagnostic_log)
        self._connected_at: dict[str, float] = {}

    @property
    def snapshot(self) -> McpConfigSnapshot:
        """返回当前连接管理器认可的配置快照。"""
        return self._current_resource.value.snapshot

    @property
    def connected(self) -> bool:
        """是否已成功建立至少一次连接。"""
        return self._current_resource.value.connected

    def get_tools(self) -> list[Any]:
        """返回当前 snapshot 的工具副本（LangChain BaseTool）。"""
        return list(self._current_resource.value.tools)

    def get_tool_names(self) -> list[str]:
        """返回所有 MCP 工具的名称，用于审批注册。"""
        return [tool.name for tool in self._current_resource.value.tools]

    async def acquire(
        self,
        snapshot_or_digest: McpConfigSnapshot | str,
    ) -> SharedResourceLease[_McpRuntime]:
        """借用指定 MCP snapshot；AgentEngine 关闭时只释放租约。"""
        digest = (
            snapshot_or_digest.digest
            if isinstance(snapshot_or_digest, McpConfigSnapshot)
            else snapshot_or_digest
        )
        for resource in reversed(self._resources.get(digest, ())):
            if resource.state is not ResourceState.READY:
                continue
            try:
                return await resource.acquire()
            except SharedResourceUnavailableError:
                continue
        raise RuntimeError("MCP_RESOURCE_SNAPSHOT_UNAVAILABLE")

    def get_server_statuses(self) -> list[dict[str, object]]:
        """返回每个 MCP 服务器的连接状态和工具列表。

        工具名通过 ``{server_name}_`` 前缀从已加载工具中匹配归属。
        """
        runtime = self._current_resource.value
        result: list[dict[str, object]] = []
        for config in runtime.snapshot.servers:
            entry = dict(runtime.server_statuses.get(config.name, {
                "name": config.name,
                "transport": config.transport,
                "source": config.source,
                "status": "failed",
                "error": "not connected",
            }))
            entry.setdefault("source", config.source)
            # 按服务器名称前缀匹配归属工具
            prefix = f"{config.name}_"
            entry["tool_names"] = [
                t.name for t in runtime.tools
                if hasattr(t, "name") and t.name.startswith(prefix)
            ]
            result.append(entry)
        return result

    def _create_resource(
        self,
        snapshot: McpConfigSnapshot,
    ) -> SharedResourceHandle[_McpRuntime]:
        """创建一个由 Host owner 持有的 MCP snapshot resource。"""
        runtime = _McpRuntime(snapshot=snapshot)
        return SharedResourceHandle(
            name=f"mcp-snapshot:{snapshot.digest[:12]}",
            scope=ResourceScope.HOST,
            value=runtime,
            close=lambda: self._close_runtime(runtime),
        )

    async def _close_runtime(self, runtime: _McpRuntime) -> None:
        """关闭单个 snapshot 的 transport；不会影响其他 snapshot。"""
        client = runtime.client
        close = getattr(client, "aclose", None) if client is not None else None
        if close is None and client is not None:
            close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                await result
        runtime.client = None
        runtime.tools = []
        runtime.connected = False

    def _set_current_snapshot(self, snapshot: McpConfigSnapshot) -> _McpRuntime:
        """切换当前快照，但保留旧 snapshot resource 供旧引擎继续借用。"""
        resources = self._resources.setdefault(snapshot.digest, [])
        resource = next(
            (candidate for candidate in reversed(resources) if candidate.state is ResourceState.READY),
            None,
        )
        if resource is None:
            resource = self._create_resource(snapshot)
            resources.append(resource)
        self._current_resource = resource
        return resource.value

    async def _reap_closed_resources(self) -> None:
        """回收已排空且没有 AgentEngine 借用的旧 snapshot resource。"""
        for digest, resources in tuple(self._resources.items()):
            remaining: list[SharedResourceHandle[_McpRuntime]] = []
            for resource in resources:
                if resource is self._current_resource:
                    remaining.append(resource)
                    continue
                snapshot = await resource.snapshot()
                if snapshot.state is ResourceState.DRAINING and not snapshot.borrowers:
                    await resource.close()
                    continue
                remaining.append(resource)
            if remaining:
                self._resources[digest] = remaining
            else:
                self._resources.pop(digest, None)

    async def reap(self) -> None:
        """在旧 AgentEngine 释放租约后回收已排空的 MCP snapshot。"""
        await self._reap_closed_resources()

    async def add_server(self, config: McpServerConfig) -> dict[str, object]:
        """为新配置创建独立 Host snapshot；旧图继续使用旧工具对象。"""
        current = self._current_resource.value
        snapshot = build_mcp_snapshot(
            [*current.snapshot.servers, config],
            revision=current.snapshot.revision,
        )
        await self.apply_snapshot(snapshot)
        return next(
            (
                item
                for item in self.get_server_statuses()
                if item.get("name") == config.name
            ),
            {
                "name": config.name,
                "transport": config.transport,
                "source": config.source,
                "status": "failed",
                "error": "not connected",
                "tool_names": [],
            },
        )

    def remove_server(self, name: str) -> bool:
        """运行时移除指定 MCP 服务器的工具并更新状态。

        Returns:
            是否找到并移除了该服务器。
        """
        old_runtime = self._current_resource.value
        found = any(config.name == name for config in old_runtime.snapshot.servers) or name in old_runtime.server_statuses
        new_configs = [c for c in old_runtime.snapshot.servers if c.name != name]
        snapshot = build_mcp_snapshot(new_configs, revision=old_runtime.snapshot.revision)
        runtime = self._set_current_snapshot(snapshot)

        # 复制未删除服务器的工具到新 snapshot；旧 runtime 列表保持不变。
        prefix = f"{name}_"
        runtime.tools = [
            tool for tool in old_runtime.tools
            if not (hasattr(tool, "name") and tool.name.startswith(prefix))
        ]
        runtime.server_statuses = {
            server_name: dict(status)
            for server_name, status in old_runtime.server_statuses.items()
            if server_name != name
        }
        runtime.connected = old_runtime.connected
        if found:
            runtime.server_statuses[name] = {
                "name": name,
                "transport": old_runtime.server_statuses.get(name, {}).get("transport", "stdio"),
                "source": old_runtime.server_statuses.get(name, {}).get("source", "config"),
                "status": "removed",
                "tool_names": [],
            }
        logger.info("MCP server '%s' removed", name)
        return found

    async def apply_snapshot(self, snapshot: McpConfigSnapshot) -> list[dict[str, object]]:
        """切换当前快照；旧 connection resource 由 Host owner 保留至关闭。"""
        old_resource = self._current_resource
        runtime = self._set_current_snapshot(snapshot)
        if old_resource is not self._current_resource:
            await old_resource.begin_draining(reason="mcp_snapshot_changed")
        if runtime.connected:
            return self.get_server_statuses()
        runtime.server_statuses.clear()
        runtime.tools = []
        runtime.client = None
        runtime.connected = False
        await self.connect_all()
        return self.get_server_statuses()

    async def connect_all(self) -> None:
        """建立所有已配置 MCP 服务器的连接并加载工具。

        连接失败的服务器记录警告并跳过，不阻止 Agent 启动。
        """
        runtime = self._current_resource.value
        if runtime.connected:
            return
        configs = runtime.snapshot.servers
        if not configs:
            runtime.connected = True
            return

        from langchain_mcp_adapters.client import MultiServerMCPClient

        connections = self._build_connections()
        if not connections:
            logger.warning("No valid MCP connections after environment expansion")
            runtime.connected = True
            return

        config_by_name = {config.name: config for config in configs if config.name in connections}
        try:
            runtime.client = MultiServerMCPClient(connections, tool_name_prefix=True)
        except Exception as exc:
            logger.warning("MCP client construction failed: %s; continuing", exc)
            runtime.tools = []
            runtime.connected = True
            for name, config in config_by_name.items():
                runtime.server_statuses[name] = _server_status(config, "failed", error=str(exc))
                self._log_connection_failed(config, started=time.monotonic(), error=exc, summary_code="MCP_CLIENT_FAILED")
            return

        runtime.tools = []
        seen_tool_names: set[str] = set()
        for name, config in config_by_name.items():
            started = time.monotonic()
            try:
                server_tools = await asyncio.wait_for(
                    runtime.client.get_tools(server_name=name),
                    timeout=config.timeout_seconds,
                )
                loaded = 0
                for tool in server_tools:
                    tool_name = getattr(tool, "name", None)
                    if isinstance(tool_name, str) and tool_name in seen_tool_names:
                        continue
                    if isinstance(tool_name, str):
                        seen_tool_names.add(tool_name)
                    _mark_mcp_tool(tool, name)
                    runtime.tools.append(tool)
                    loaded += 1
                runtime.server_statuses[name] = _server_status(config, "connected")
                self._connected_at[name] = started
                self._log_connection_completed(config, started=started, tool_count=loaded)
            except asyncio.TimeoutError as exc:
                logger.warning("MCP server %r connection timed out; continuing", name)
                runtime.server_statuses[name] = _server_status(
                    config, "failed", error="connection timed out"
                )
                self._log_connection_failed(
                    config, started=started, error=exc, summary_code="MCP_CONNECT_TIMEOUT"
                )
            except Exception as exc:
                logger.warning("MCP server %r connection failed: %s; continuing", name, exc)
                runtime.server_statuses[name] = _server_status(config, "failed", error=str(exc))
                self._log_connection_failed(
                    config, started=started, error=exc, summary_code="MCP_CONNECT_FAILED"
                )
        runtime.connected = True
        logger.info(
            "MCP connection attempts complete: %d server(s), %d tool(s) loaded",
            len(connections), len(runtime.tools),
        )

    async def close_all(self) -> None:
        """由 Host owner 关闭所有 snapshot connection，不被单个引擎调用。"""
        resources = [
            resource
            for candidates in self._resources.values()
            for resource in candidates
        ]
        if resources:
            try:
                runtime = self._current_resource.value
            except Exception:
                runtime = None
            if runtime is not None:
                for config in runtime.snapshot.servers:
                    started = self._connected_at.pop(config.name, None)
                    duration_ms = (
                        max(0, round((time.monotonic() - started) * 1000))
                        if started is not None
                        else 0
                    )
                    self._diagnostic_log.info(
                        "mcp.connection.closed",
                        {
                            "server_name": config.name,
                            "server_fingerprint": mcp_server_fingerprint(config),
                            "outcome": "closed",
                            "duration_ms": duration_ms,
                        },
                    )
        self._resources = {}
        for resource in resources:
            await resource.begin_draining(reason="host_shutdown")
            try:
                await resource.close()
            except Exception:
                await resource.close(force=True)
        logger.info("MCP connections closed")

    def _log_connection_completed(
        self,
        config: McpServerConfig,
        *,
        started: float,
        tool_count: int,
    ) -> None:
        self._diagnostic_log.info(
            "mcp.connection.completed",
            {
                "server_name": config.name,
                "server_fingerprint": mcp_server_fingerprint(config),
                "transport": config.transport,
                "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
                "tool_count": tool_count,
            },
        )

    def _log_connection_failed(
        self,
        config: McpServerConfig,
        *,
        started: float,
        error: BaseException,
        summary_code: str,
    ) -> None:
        self._diagnostic_log.warn(
            "mcp.connection.failed",
            {
                "server_name": config.name,
                "server_fingerprint": mcp_server_fingerprint(config),
                "transport": config.transport,
                "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
                "failure_stage": "connect",
                "error_type": type(error).__name__,
                "summary_code": summary_code,
                "retryable": summary_code == "MCP_CONNECT_TIMEOUT",
            },
        )

    def _build_connections(self) -> dict[str, Any]:
        """将 McpServerConfig 列表转换为 MultiServerMCPClient 的 connections 字典。

        用户 MCP 在此时展开环境变量；portable Plugin MCP 已在 adapter 中完成
        保留 placeholder 替换，运行时保持其余字段字面值。
        """
        runtime = self._current_resource.value
        connections: dict[str, Any] = {}
        for config in runtime.snapshot.servers:
            conn = self._build_single_connection(config)
            if conn is not None:
                connections[config.name] = conn
            else:
                # 环境变量缺失导致跳过，记录 skipped 状态
                runtime.server_statuses[config.name] = {
                    "name": config.name,
                    "transport": config.transport,
                    "source": config.source,
                    "status": "skipped",
                    "error": "environment variable(s) not set",
                }
        return connections

    def _build_single_connection(self, config: McpServerConfig) -> dict[str, Any] | None:
        """构建单个服务器的连接参数；环境变量缺失时返回 None。"""
        is_portable_plugin = config.plugin_root is not None
        is_plugin_source = config.source.startswith("plugin:")
        if config.transport == "stdio":
            if is_portable_plugin:
                command = config.command or ""
                args = list(config.args)
                env = dict(config.env)
            else:
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
            if config.inherit_environment:
                process_env = env or None
            else:
                # Plugin MCP 不继承 API Key 等宿主环境；只保留命令查找和临时目录所需字段。
                process_env = {
                    key: value
                    for key in ("PATH", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC")
                    if (value := os.environ.get(key))
                }
                process_env.update(env)
            if is_portable_plugin and config.plugin_root and config.plugin_data:
                if process_env is None:
                    process_env = {}
                process_env["PLUGIN_ROOT"] = config.plugin_root
                process_env["PLUGIN_DATA"] = config.plugin_data
            connection: dict[str, Any] = {
                "transport": "stdio",
                "command": command,
                "args": args,
                "env": process_env or None,
            }
            if config.cwd is not None:
                if is_portable_plugin:
                    connection["cwd"] = config.cwd
                else:
                    cwd = expand_env_vars(config.cwd)
                    if cwd is None:
                        logger.warning("MCP server %r: missing env vars in cwd; skipping", config.name)
                        return None
                    connection["cwd"] = cwd
            return connection

        # http / sse
        if is_portable_plugin:
            url = config.url or ""
            headers = dict(config.headers)
        else:
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
        # Claude `.mcp.json` 与 portable `mcp.json` 保留各自的 placeholder、
        # environment 和 timeout 语义；但两者都是已安装 Plugin 的 header 来源，
        # 都不能让凭据随跨源 redirect 自动转发。
        if is_portable_plugin or is_plugin_source:
            conn["httpx_client_factory"] = _create_plugin_http_client
        return conn
