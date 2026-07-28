"""Harness v3 Agent Host：承载项目级运行资源与协议连接。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema.exceptions import ValidationError

from harness_agent import __version__
from harness_agent.agent_runtime import (
    AgentRuntime,
    AgentRuntimeLease,
    AgentRuntimeRunLease,
    RuntimeCloseAdapter,
    RuntimePool,
    RuntimePoolCapacityError,
    RuntimeResourceBundle,
)
from harness_agent.config import ConfigError, Za38Config, load_config
from harness_agent.config_change_service import (
    ConfigChange,
    ConfigChangeError,
    ConfigChangeService,
    ManagedConfigPolicy,
)
from harness_agent.model_router import ModelRouter, ThreadModelBindings
from harness_agent.protocol_generated import (
    MAX_FRAME_BYTES,
    MAX_TOOL_PAYLOAD_BYTES,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    CAPABILITY,
    EVENT_TYPE,
    METHOD,
    OPERATION_CAPABILITIES,
    SERVER_CAPABILITIES,
    ApprovalResponse,
    ContextCompactParams,
    ConfigCommitParams,
    ConfigDetailsParams,
    ConfigPreviewParams,
    HostAttachmentCreateParams,
    InitializeParams,
    ModelsListParams,
    McpAddParams,
    McpRemoveParams,
    QuestionResponse,
    RunCancelParams,
    RunStartParams,
    ThreadsListParams,
    ThreadsOpenParams,
)
from harness_agent.protocol_runtime import (
    validate_interaction_result,
    validate_operation_params,
    validate_operation_result,
    validate_protocol_error_data,
)
from harness_agent.skills import SkillError, SkillRegistry
from harness_agent.mcp import McpConnectionManager, mcp_config_fingerprint
from harness_agent.run_context import RunCancellationToken, RunContext
from harness_agent.runtime_profile import (
    RuntimeProfile,
    component_fingerprint,
    default_runtime_profile,
)
from harness_agent.thread_store import ThreadStore, ThreadStoreError
from harness_agent.providers.harness_gateway import ProviderClientPool

logger = logging.getLogger(__name__)
INTERACTION_TIMEOUT_MS = 300_000
STABLE_ERROR_CODES = {
    "PROTOCOL_VERSION_UNSUPPORTED",
    "CAPABILITY_REQUIRED",
    "THREAD_NOT_FOUND",
    "THREAD_BUSY",
    "RUN_NOT_FOUND",
    "RUN_NOT_OWNER",
    "RUN_ID_CONFLICT",
    "INTERACTION_EXPIRED",
    "CONFIG_REVISION_CONFLICT",
    "HOST_OWNER_REQUIRED",
    "ATTACHMENT_EXPIRED",
    "INTERNAL_ERROR",
}


class RpcError(Exception):
    """可安全返回给客户端的预期 JSON-RPC 错误。"""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        """保存错误码、文案和可选结构化详情。"""
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(slots=True)
class ActiveRun:
    """一次执行的隔离状态；sequence 只覆盖可广播事件。"""

    thread_id: str
    run_id: str
    message: str
    owner_connection_id: str = ""
    requested_skill: dict[str, str] | None = None
    task: asyncio.Task[None] | None = None
    sequence: int = 0
    status: str = "running"
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    tool_stream_ids: dict[str, str] = field(default_factory=dict)
    tool_result_ids: dict[str, str] = field(default_factory=dict)
    started_tool_ids: set[str] = field(default_factory=set)
    last_tool_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    context_summary: dict[str, object] = field(default_factory=dict)
    cancellation_token: RunCancellationToken = field(default_factory=RunCancellationToken)
    run_context: RunContext | None = None
    runtime_lease: AgentRuntimeLease | None = None
    runtime_run_lease: AgentRuntimeRunLease | None = None
    runtime_profile_key: str | None = None
    model_bindings: ThreadModelBindings | None = None
    requested_model_selection: dict[str, object] | None = None
    primary_model_binding: dict[str, object] | None = None
    runtime_profile_id: str | None = None
    resolved_runtime_profile: RuntimeProfile | None = None


@dataclass(frozen=True, slots=True)
class _RuntimeBuildSpec:
    """构建一个共享 Runtime 所需的稳定输入，不含 thread/run 私有状态。"""

    config: Za38Config
    workspace: Path
    skill_registry: SkillRegistry
    model_settings: Any


@dataclass(slots=True)
class _RuntimeArtifacts:
    """Runtime 图之外的共享 middleware 与执行上下文，由同一 Runtime 负责释放。"""

    execution_context: Any
    context_compactor: Any


@dataclass(slots=True)
class InteractionSpec:
    """从 LangGraph interrupt 规范化出的协议请求及恢复所需原始信息。"""

    request_id: str
    type: str
    payload: dict[str, Any]
    interrupt_id: str
    questions: list[Mapping[str, Any]] = field(default_factory=list)
    # 单次审批 interrupt 中挂起的工具调用数量；HITL 中间件要求 resume 的
    # decisions 列表长度必须与之相等，否则会抛出 decisions 不匹配错误。
    action_count: int = 1


@dataclass(slots=True)
class ProtocolConnection:
    """一个前端连接的协议状态，不拥有任何 Agent 运行资源。"""

    connection_id: str
    role: str
    sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    capability_ceiling: frozenset[str] = field(
        default_factory=lambda: frozenset(SERVER_CAPABILITIES)
    )
    initialized: bool = False
    protocol_minor: int = PROTOCOL_MINOR
    enabled_capabilities: set[str] = field(default_factory=set)
    interaction_handles: set[str] = field(default_factory=set)
    watched_threads: set[str] = field(default_factory=set)
    pending_requests: dict[str, asyncio.Future[object]] = field(default_factory=dict)
    interaction_specs: dict[str, InteractionSpec] = field(default_factory=dict)
    closed: bool = False


@dataclass(frozen=True, slots=True)
class _AttachmentGrant:
    """尚未消费的本机 Web attachment 凭证。"""

    origin: str
    expires_at_ms: int
    capability_ceiling: frozenset[str]


AgentFactory = Callable[[Za38Config, Path], Any | Awaitable[Any]]


class AgentHost:
    """管理 Project-scoped Agent 生命周期与 v3 协议控制面。"""

    def __init__(
        self,
        *,
        agent: Any | None = None,
        agent_factory: AgentFactory | None = None,
        allow_echo: bool | None = None,
        config_home: Path | None = None,
        config_change_policy: ManagedConfigPolicy | None = None,
        workspace: Path | None = None,
        config_path: str | None = None,
        connection_id: str | None = None,
        connection_role: str = "owner",
    ) -> None:
        """初始化运行表、反向请求表、发送锁和方法分发表。

        ``config_home`` 仅供嵌入式测试隔离用户目录；正式 CLI 始终使用
        操作系统解析出的真实 home，不能由 JSON-RPC 客户端传入。
        """
        self.agent = agent
        self._agent_factory = agent_factory
        self._uses_default_agent_factory = agent_factory is None and agent is None
        self._context_updates: dict[str, list[Any]] = {}
        self._allow_echo = (
            os.environ.get("HARNESS_ECHO_MODE") == "1" if allow_echo is None else allow_echo
        )
        self._running = True
        self._send_lock = asyncio.Lock()
        self._agent_build_lock = asyncio.Lock()
        self._runs: dict[str, ActiveRun] = {}
        self._workspace = (workspace or Path.cwd()).resolve()
        self._config_path = config_path or os.environ.get("HARNESS_AGENT_CONFIG_PATH")
        self._connection_role = connection_role
        self._config_home = config_home
        self._config: Za38Config | None = None
        self._config_change_policy = config_change_policy or ManagedConfigPolicy()
        self._config_change_service: ConfigChangeService | None = None
        self._startup_error: str | None = None
        self._skill_registry: SkillRegistry | None = None
        self._thread_store: ThreadStore | None = None
        self._runtime_pool: RuntimePool | None = None
        self._mcp_manager: McpConnectionManager | None = None
        self._runtime_build_specs: dict[str, _RuntimeBuildSpec] = {}
        self._runtime_artifacts: dict[str, _RuntimeArtifacts] = {}
        self._provider_client_pool = ProviderClientPool()
        self._owner_connection = ProtocolConnection(
            connection_id=connection_id or str(uuid.uuid4()),
            role=self._connection_role,
        )
        self._connections = {
            self._owner_connection.connection_id: self._owner_connection
        }
        self._connection_context: ContextVar[ProtocolConnection | None] = ContextVar(
            "harness_protocol_connection",
            default=None,
        )
        self._resource_init_lock = asyncio.Lock()
        self._resources_ready = False
        self._registry_lock = asyncio.Lock()
        self._starting_threads: set[str] = set()
        self._handlers = {
            METHOD["INITIALIZE"]: self._handle_initialize,
            METHOD["RUN_START"]: self._handle_run_start,
            METHOD["RUN_CANCEL"]: self._handle_run_cancel,
            METHOD["CONTEXT_COMPACT"]: self._handle_context_compact,
            METHOD["CONFIG_SHOW"]: self._handle_config_show,
            METHOD["CONFIG_PATH"]: self._handle_config_path,
            METHOD["CONFIG_DETAILS"]: self._handle_config_details,
            METHOD["CONFIG_PREVIEW"]: self._handle_config_preview,
            METHOD["CONFIG_COMMIT"]: self._handle_config_commit,
            METHOD["MODELS_LIST"]: self._handle_models_list,
            METHOD["THREADS_LIST"]: self._handle_threads_list,
            METHOD["THREADS_OPEN"]: self._handle_threads_open,
            METHOD["THREADS_WATCH"]: self._handle_threads_watch,
            METHOD["THREADS_UNWATCH"]: self._handle_threads_unwatch,
            METHOD["SKILLS_LIST"]: self._handle_skills_list,
            METHOD["SKILLS_INSPECT"]: self._handle_skills_inspect,
            METHOD["SKILLS_SET_ENABLED"]: self._handle_skills_set_enabled,
            METHOD["SKILLS_INSTALL"]: self._handle_skills_install,
            METHOD["SKILLS_UPDATE"]: self._handle_skills_update,
            METHOD["SKILLS_REMOVE"]: self._handle_skills_remove,
            METHOD["SKILLS_MARKET_LIST"]: self._handle_skills_market_list,
            METHOD["MCP_STATUS"]: self._handle_mcp_status,
            METHOD["MCP_ADD"]: self._handle_mcp_add,
            METHOD["MCP_REMOVE"]: self._handle_mcp_remove,
            METHOD["HOST_ATTACHMENT_CREATE"]: self._handle_host_attachment_create,
        }
        self._attachment_grants: dict[str, _AttachmentGrant] = {}
        self._attachment_lock = asyncio.Lock()
        self._websocket_server: Any | None = None

    async def run(self) -> None:
        """持续读取受限大小的 JSONL 帧，直到 EOF 或正常关闭。"""
        reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES + 1)
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            # Windows ProactorEventLoop 对重定向 stdin 句柄注册 IOCP 会抛 WinError 6，
            # 改用后台线程阻塞读取并喂入 StreamReader，保持分帧逻辑不变。
            def _feed_stdin() -> None:
                stdin = getattr(sys.stdin, "buffer", sys.stdin)
                try:
                    while True:
                        chunk = stdin.readline()
                        if not chunk:
                            break
                        loop.call_soon_threadsafe(reader.feed_data, chunk)
                except Exception:
                    pass
                try:
                    loop.call_soon_threadsafe(reader.feed_eof)
                except RuntimeError:
                    # 事件循环已关闭；stdin 线程退出即可。
                    pass

            threading.Thread(target=_feed_stdin, name="za38-stdin", daemon=True).start()
        else:
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            while self._running:
                try:
                    line = await reader.readline()
                except ValueError:
                    await self.send_error(None, -32600, "JSON-RPC frame exceeds size limit")
                    break
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await self.send_error(None, -32600, "JSON-RPC frame exceeds size limit")
                    continue
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self.send_error(None, -32700, "Parse error")
                    continue
                if not isinstance(message, dict):
                    await self.send_error(None, -32600, "Invalid Request")
                    continue
                await self.dispatch(message)
        finally:
            await self.close()

    async def close(self) -> None:
        """关闭 Host 及其持有的运行时资源；可重复调用。"""
        self._running = False
        await self._cancel_all_runs()
        for connection in list(self._connections.values()):
            connection.closed = True
            self._fail_connection_requests(
                connection,
                RpcError(-32004, "Peer connection closed"),
            )
        if self._websocket_server is not None:
            self._websocket_server.close()
            await self._websocket_server.wait_closed()
            self._websocket_server = None
        self._attachment_grants.clear()
        if self._mcp_manager is not None:
            await self._mcp_manager.close_all()
            self._mcp_manager = None
        await self._close_runtime_pool()
        await self._close_thread_store()

    async def close_connection(self, connection: ProtocolConnection) -> None:
        """释放 attached Connection，并取消仅由它拥有的 active Runs。"""
        if connection.closed:
            return
        connection.closed = True
        connection.watched_threads.clear()
        self._fail_connection_requests(connection, RpcError(-32004, "Peer connection closed"))
        owned = [
            run
            for run in self._runs.values()
            if run.owner_connection_id == connection.connection_id
            and run.status in {"running", "interrupted"}
        ]
        tasks: list[asyncio.Task[None]] = []
        for run in owned:
            run.cancellation_token.cancel()
            if run.task and not run.task.done():
                run.task.cancel()
                if run.task is not asyncio.current_task():
                    tasks.append(run.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connections.pop(connection.connection_id, None)

    def create_connection(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        role: str = "attached",
        capability_ceiling: Iterable[str] = SERVER_CAPABILITIES,
    ) -> ProtocolConnection:
        """建立轻量协议连接；Project 资源仍由当前 Host 唯一持有。"""
        connection = ProtocolConnection(
            connection_id=str(uuid.uuid4()),
            role=role,
            sender=sender,
            capability_ceiling=frozenset(capability_ceiling),
        )
        self._connections[connection.connection_id] = connection
        return connection

    async def dispatch(self, message: dict[str, Any]) -> None:
        """从 owner stdio Connection 分派一帧。"""
        await self.dispatch_connection(self._owner_connection, message)

    async def dispatch_connection(
        self,
        connection: ProtocolConnection,
        message: dict[str, Any],
    ) -> None:
        """在指定 Connection 上分派一帧，隔离协商与请求关联状态。"""
        if connection.closed:
            return
        token = self._connection_context.set(connection)
        try:
            await self._dispatch_current(message)
        finally:
            self._connection_context.reset(token)

    async def _dispatch_current(self, message: dict[str, Any]) -> None:
        """校验并发消息；response 负责恢复反向请求，request 则进入业务分发。"""
        connection = self._current_connection()
        if message.get("jsonrpc") != "2.0":
            await self.send_error(message.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")
            return
        method = message.get("method")
        if method is None:
            if set(message) - {"jsonrpc", "id", "result", "error"}:
                await self.send_error(message.get("id"), -32600, "Response contains unknown fields")
                return
            await self._handle_peer_response(message)
            return
        request_id = message.get("id")
        if not isinstance(request_id, str):
            await self.send_error(None, -32600, "Invalid Request: id must be a string")
            return
        if not isinstance(method, str):
            await self.send_error(request_id, -32600, "Invalid Request: method must be a string")
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            await self.send_error(request_id, -32602, "Invalid params: params must be an object")
            return
        if set(message) - {"jsonrpc", "method", "params", "id"}:
            await self.send_error(request_id, -32600, "Request contains unknown fields")
            return
        if method != METHOD["INITIALIZE"] and not self._connection_initialized(connection):
            await self.send_error(request_id, -32000, "initialize must be the first request")
            return
        handler = self._handlers.get(method)
        if handler is None:
            await self.send_error(request_id, -32601, f"Method not found: {method}")
            return
        try:
            if method == METHOD["INITIALIZE"]:
                protocol = params.get("protocol")
                if not isinstance(protocol, dict) or protocol.get("major") != PROTOCOL_MAJOR:
                    raise RpcError(
                        -32003,
                        "PROTOCOL_VERSION_UNSUPPORTED",
                        {
                            "code": "PROTOCOL_VERSION_UNSUPPORTED",
                            "retryable": False,
                            "details": {"supported_major": PROTOCOL_MAJOR},
                        },
                    )
            validate_operation_params(method, params)
            required_capability = OPERATION_CAPABILITIES.get(method)
            if required_capability and required_capability not in self._connection_capabilities(connection):
                raise RpcError(
                    -32002,
                    "CAPABILITY_REQUIRED",
                    {"code": "CAPABILITY_REQUIRED", "retryable": False, "capability": required_capability},
                )
            result = await handler(params, request_id)
        except ValidationError as exc:
            await self.send_error(
                request_id,
                -32602,
                "Invalid params",
                {
                    "code": "INVALID_PARAMS",
                    "retryable": False,
                    "details": {
                        "path": list(exc.absolute_path),
                        "message": exc.message,
                    },
                },
            )
        except SkillError as exc:
            await self.send_error(request_id, -32602, str(exc))
        except ThreadStoreError as exc:
            await self.send_error(
                request_id,
                -32020,
                "THREAD_STORE_UNAVAILABLE",
                {"code": str(exc)},
            )
        except RuntimePoolCapacityError as exc:
            await self.send_error(
                request_id,
                -32030,
                "RUNTIME_POOL_CAPACITY_EXHAUSTED",
                {"code": str(exc)},
            )
        except RpcError as exc:
            await self.send_error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - 最后的协议隔离层。
            logger.exception("Unhandled JSON-RPC handler error for %s", method)
            await self.send_error(request_id, -32603, f"{type(exc).__name__}: {exc}")
        else:
            if result is not None:
                validate_operation_result(method, result)
                await self.send_response(request_id, result)
        finally:
            if method == METHOD["RUN_START"]:
                thread_id = params.get("thread_id")
                if isinstance(thread_id, str):
                    async with self._registry_lock:
                        self._starting_threads.discard(thread_id)

    async def send(self, message: dict[str, Any]) -> None:
        """向 owner stdio 写出单帧；测试也通过替换此 seam 捕获输出。"""
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_FRAME_BYTES:
            raise RpcError(-32603, "Outbound JSON-RPC frame exceeds size limit")
        async with self._send_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def _current_connection(self) -> ProtocolConnection:
        return self._connection_context.get() or self._owner_connection

    def _connection_initialized(self, connection: ProtocolConnection) -> bool:
        return connection.initialized

    def _connection_capabilities(self, connection: ProtocolConnection | None = None) -> set[str]:
        return (connection or self._current_connection()).enabled_capabilities

    def _connection_handles(self, connection: ProtocolConnection | None = None) -> set[str]:
        return (connection or self._current_connection()).interaction_handles

    def _connection_watches(self, connection: ProtocolConnection | None = None) -> set[str]:
        return (connection or self._current_connection()).watched_threads

    async def _send_to(
        self,
        connection: ProtocolConnection,
        message: dict[str, Any],
    ) -> None:
        if connection.closed:
            raise RpcError(-32004, "Connection closed")
        if connection is self._owner_connection:
            await self.send(message)
            return
        if connection.sender is None:
            raise RpcError(-32603, "Connection has no transport")
        await connection.sender(message)

    async def send_response(self, request_id: str, result: Any) -> None:
        """发送 JSON-RPC 成功响应。"""
        await self._send_to(
            self._current_connection(),
            {"jsonrpc": "2.0", "result": result, "id": request_id},
        )

    async def send_error(
        self, request_id: str | None, code: int, message: str, data: object | None = None
    ) -> None:
        """发送保留 code/message/data 的 JSON-RPC 错误响应。"""
        error: dict[str, object] = {"code": code, "message": message}
        if -32099 <= code <= -32000:
            normalized = _protocol_error_data(message, data)
            validate_protocol_error_data(normalized)
            error["data"] = normalized
        elif data is not None:
            error["data"] = data
        await self._send_to(
            self._current_connection(),
            {"jsonrpc": "2.0", "error": error, "id": request_id},
        )

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """发送无需响应的通知；v3 业务流只使用 event。"""
        await self._send_to(
            self._current_connection(),
            {"jsonrpc": "2.0", "method": method, "params": params},
        )

    async def _handle_initialize(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """协商 v3 minor、请求能力和可处理 Interaction。"""
        connection = self._current_connection()
        protocol = params.get("protocol")
        if not isinstance(protocol, dict) or protocol.get("major") != PROTOCOL_MAJOR:
            raise RpcError(-32003, "PROTOCOL_MISMATCH", {"supported_major": PROTOCOL_MAJOR})
        min_minor = protocol.get("min_minor")
        max_minor = protocol.get("max_minor")
        if (
            not isinstance(min_minor, int)
            or not isinstance(max_minor, int)
            or max_minor < 0
            or min_minor > max_minor
            or min_minor > PROTOCOL_MINOR
        ):
            raise RpcError(
                -32003,
                "PROTOCOL_MISMATCH",
                {"supported": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR}},
            )
        parsed = InitializeParams.model_validate(params)
        if self._connection_initialized(connection):
            raise RpcError(-32000, "Peer is already initialized")
        negotiated_minor = min(PROTOCOL_MINOR, max_minor)
        async with self._resource_init_lock:
            if not self._resources_ready:
                self._skill_registry = SkillRegistry(self._workspace, home=self._config_home)
                self._load_config()
                await self._connect_mcp_servers()
                self._resources_ready = True
        requested = set(parsed.capabilities.requests)
        enabled = requested.intersection(connection.capability_ceiling)
        if connection.role != "owner":
            enabled.discard(CAPABILITY["HOST_ATTACH"])
        handles = set(parsed.capabilities.handles)
        connection.protocol_minor = negotiated_minor
        connection.interaction_handles = handles
        connection.enabled_capabilities = enabled
        connection.initialized = True
        return {
            "protocol": {"major": PROTOCOL_MAJOR, "minor": negotiated_minor},
            "server": {"name": "za38-agent", "version": __version__},
            "connection": {
                "id": connection.connection_id,
                "role": connection.role,
                "project": {
                    "id": (await self._ensure_thread_store()).project_fingerprint if self._thread_persistence_enabled() else "echo",
                    "label": self._workspace.name,
                },
            },
            "capabilities": {
                "available": list(SERVER_CAPABILITIES),
                "enabled": sorted(enabled),
                "handles": sorted(handles),
            },
            "agent_commands": [],
            "skills_snapshot": self._skill_registry.snapshot(),
            "skill_diagnostics": self._skill_registry.diagnostics[:20],
            "limits": {
                "max_frame_bytes": MAX_FRAME_BYTES,
                "max_tool_payload_bytes": MAX_TOOL_PAYLOAD_BYTES,
            },
            "config_summary": self._config.redacted() if self._config else None,
            "startup_error": (
                {"code": "CONFIGURATION_ERROR", "message": self._startup_error}
                if self._startup_error
                else None
            ),
        }

    async def _connect_mcp_servers(self) -> None:
        """根据配置建立 MCP 服务器连接；失败不阻止启动。"""
        if self._config is None or not self._config.mcp_servers:
            return
        self._mcp_manager = McpConnectionManager(list(self._config.mcp_servers))
        await self._mcp_manager.connect_all()

    async def _handle_run_start(self, params: dict[str, Any], request_id: str) -> None:
        """先确认 run 标识再创建后台任务，保证响应严格早于首事件。"""
        parsed = RunStartParams.model_validate(params)
        message = parsed.message.strip()
        if not message:
            raise RpcError(-32602, "message must be non-empty")
        thread_id = parsed.thread_id
        run_id = parsed.run_id
        async with self._registry_lock:
            existing = self._runs.get(thread_id)
            if existing and existing.status in {"running", "interrupted"}:
                if existing.run_id == run_id and existing.message == message:
                    await self.send_response(
                        request_id,
                        {"thread_id": thread_id, "run_id": run_id, "accepted": True},
                    )
                    return None
                if existing.run_id == run_id:
                    raise RpcError(
                        -32006,
                        "RUN_ID_CONFLICT",
                        {"code": "RUN_ID_CONFLICT", "retryable": False},
                    )
                raise RpcError(
                    -32000,
                    "THREAD_BUSY",
                    {"code": "THREAD_BUSY", "retryable": True},
                )
            if thread_id in self._starting_threads:
                raise RpcError(
                    -32000,
                    "THREAD_BUSY",
                    {"code": "THREAD_BUSY", "retryable": True},
                )
            self._starting_threads.add(thread_id)
        requested_skill = None
        if parsed.requested_skill is not None:
            if self._skill_registry is None:
                self._skill_registry = SkillRegistry(self._workspace, home=self._config_home)
            skill = self._skill_registry.resolve(parsed.requested_skill.id)
            if not skill.user_invocable:
                raise SkillError(f'Skill "{skill.skill_id}" is not user-invocable')
            requested_skill = {"id": skill.skill_id, "args": parsed.requested_skill.args or ""}
        model_bindings = None
        requested_model_selection: dict[str, object] | None = None
        primary_model_binding: dict[str, object] | None = None
        runtime_profile: RuntimeProfile | None = None
        if self._thread_persistence_enabled():
            self._load_config()
            if self._config is None:
                raise RpcError(-32010, self._startup_error or "MODEL_CONFIGURATION_REQUIRED")
            if parsed.model_selection is not None and CAPABILITY["MODELS_SELECT"] not in self._connection_capabilities():
                raise RpcError(-32002, "MODELS_SELECT_CAPABILITY_REQUIRED")
            try:
                (
                    model_bindings,
                    requested_model_selection,
                    primary_model_binding,
                ) = await self._resolve_run_model_bindings(
                    thread_id,
                    self._config,
                    requested_primary_profile=(
                        parsed.model_selection.primary_profile
                        if parsed.model_selection is not None
                        else None
                    ),
                )
                runtime_profile = await self._resolve_runtime_profile(
                    thread_id, self._config, model_bindings
                )
            except (ConfigError, ThreadStoreError) as exc:
                raise RpcError(-32004, str(exc)) from exc
        run = ActiveRun(
            thread_id=thread_id,
            run_id=run_id,
            message=message,
            owner_connection_id=self._current_connection().connection_id,
            requested_skill=requested_skill,
            model_bindings=model_bindings,
            requested_model_selection=requested_model_selection,
            primary_model_binding=primary_model_binding,
            runtime_profile_id=(runtime_profile.profile_key[:12] if runtime_profile else None),
            resolved_runtime_profile=runtime_profile,
        )
        if self._thread_persistence_enabled():
            store = await self._ensure_thread_store()
            if (
                requested_model_selection is None
                or primary_model_binding is None
                or run.runtime_profile_id is None
            ):
                raise RpcError(-32010, "RUN_MODEL_BINDING_UNAVAILABLE")
            try:
                created = await store.record_run_start(
                    thread_id,
                    run_id,
                    message,
                    requested_selection=requested_model_selection,
                    actual_primary_binding=primary_model_binding,
                    runtime_profile_id=run.runtime_profile_id,
                )
            except ThreadStoreError as exc:
                if str(exc) == "RUN_EXECUTION_BINDING_CONFLICT":
                    raise RpcError(
                        -32006,
                        "RUN_ID_CONFLICT",
                        {"code": "RUN_ID_CONFLICT", "retryable": False},
                    ) from exc
                raise RpcError(-32004, str(exc)) from exc
            if not created:
                await self.send_response(
                    request_id, {"thread_id": thread_id, "run_id": run_id, "accepted": True}
                )
                return None
        self._runs[thread_id] = run
        await self.send_response(
            request_id, {"thread_id": thread_id, "run_id": run_id, "accepted": True}
        )
        run.task = asyncio.create_task(self._execute_run(run), name=f"za38-run-{run_id}")
        return None

    async def _handle_run_cancel(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """取消运行，包括正在等待客户端交互的任务。"""
        parsed = RunCancelParams.model_validate(params)
        run = self._require_run(parsed.thread_id, parsed.run_id)
        if run.owner_connection_id != self._current_connection().connection_id:
            raise RpcError(
                -32005,
                "RUN_NOT_OWNER",
                {"code": "RUN_NOT_OWNER", "retryable": False},
            )
        if run.task and not run.task.done():
            run.cancellation_token.cancel()
            run.task.cancel()
            # create_task 尚未获得首个时间片时，协程内部的 CancelledError 分支不会执行；
            # 这里补发唯一终态，保证“刚接受就取消”也不会让客户端永久等待。
            await asyncio.sleep(0)
            if run.status not in {"cancelled", "completed", "failed"} and run.task.cancelled():
                run.status = "cancelled"
                await self._emit(run, EVENT_TYPE["RUN_CANCELLED"], {"reason": "Cancelled by client"})
                self._runs.pop(run.thread_id, None)
            return {"cancelled": True, "run_id": run.run_id}
        return {"cancelled": False, "run_id": run.run_id}

    async def _handle_context_compact(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """在空闲 thread 上按用户命令强制生成结构化摘要，不把能力暴露给模型。"""
        self._require_context_capability()
        parsed = ContextCompactParams.model_validate(params)
        active = self._runs.get(parsed.thread_id)
        if active is not None and active.status in {"running", "interrupted"}:
            raise RpcError(-32000, "CONTEXT_COMPACTION_RUN_ACTIVE")

        store = await self._ensure_thread_store()
        messages = await store.load_context_messages(parsed.thread_id)
        if messages is None:
            raise RpcError(-32004, "THREAD_NOT_RECOVERABLE")
        if not self._uses_default_agent_factory:
            agent = await self._ensure_agent()
            middleware = getattr(self, "_context_compactor", None)
            if agent is None or middleware is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            return await self._compact_with_runtime(
                agent=agent,
                middleware=middleware,
                thread_id=parsed.thread_id,
                messages=messages,
                store=store,
            )

        lease, runtime = await self._acquire_default_runtime(parsed.thread_id)
        try:
            if lease is None or runtime is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            artifacts = self._runtime_artifacts.get(runtime.profile_key)
            if artifacts is None or runtime.graph is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            return await self._compact_with_runtime(
                agent=runtime.graph,
                middleware=artifacts.context_compactor,
                thread_id=parsed.thread_id,
                messages=messages,
                store=store,
            )
        finally:
            await self._release_runtime_lease(lease)

    async def _handle_config_show(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回当前脱敏配置与可重建 RuntimePool 的本地诊断摘要。"""
        if _params:
            raise RpcError(-32602, "config.show does not accept params")
        self._load_config()
        if self._config is None:
            raise RpcError(-32010, self._startup_error or "Configuration is unavailable")
        summary = self._config.redacted()
        summary["runtime_pool_diagnostics"] = await self._runtime_pool_diagnostics()
        return summary

    async def _handle_config_details(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """返回 Settings/Permissions Manager 可展示的脱敏字段和可修改边界。"""
        self._require_config_write_capability()
        ConfigDetailsParams.model_validate(params)
        try:
            return self._config_changes().details()
        except ConfigChangeError as exc:
            raise self._config_change_rpc_error(exc) from exc

    async def _handle_config_preview(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """在不落盘的前提下验证配置更新并返回 CAS revision 与脱敏差异。"""
        self._require_config_write_capability()
        parsed = ConfigPreviewParams.model_validate(params)
        try:
            return self._config_changes().preview(
                [ConfigChange(change.path, change.value) for change in parsed.changes]
            ).to_dict()
        except ConfigChangeError as exc:
            raise self._config_change_rpc_error(exc) from exc

    async def _handle_config_commit(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """按 preview revision 原子提交白名单字段，不允许绕过来源和策略校验。"""
        self._require_config_write_capability()
        parsed = ConfigCommitParams.model_validate(params)
        try:
            result = self._config_changes().commit(
                expected_revision=parsed.expected_revision,
                changes=[ConfigChange(change.path, change.value) for change in parsed.changes],
            )
        except ConfigChangeError as exc:
            raise self._config_change_rpc_error(exc) from exc
        # 当前仅默认模型可安全影响之后创建的 Thread：已经启动的 Run 和既有
        # Runtime 保持原快照；其他 Settings 仍明确要求重启 sidecar。
        if result["applies_to"] == ["new-thread"]:
            self._load_config()
        return result

    async def _handle_mcp_status(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回所有已配置 MCP 服务器的运行时连接状态和工具列表。"""
        if _params:
            raise RpcError(-32602, "mcp.status does not accept params")
        if self._mcp_manager is None:
            return {"servers": [], "total_tools": 0}
        statuses = self._mcp_manager.get_server_statuses()
        total_tools = sum(len(s.get("tool_names", [])) for s in statuses)
        return {"servers": statuses, "total_tools": total_tools}

    async def _handle_mcp_add(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """添加 MCP 服务器到用户配置并尝试热连接。"""
        import re

        from harness_agent.mcp import McpServerConfig
        from harness_agent.mcp_config_writer import add_server_to_config, list_servers_in_config

        parsed = McpAddParams.model_validate(params)
        name = parsed.name

        # 名称合法性
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise RpcError(-32602, f"Invalid server name '{name}': only [a-zA-Z0-9_-] allowed")

        # 必填字段校验
        if parsed.transport == "stdio" and not parsed.command:
            raise RpcError(-32602, "stdio transport requires 'command'")
        if parsed.transport in ("http", "sse") and not parsed.url:
            raise RpcError(-32602, f"{parsed.transport} transport requires 'url'")

        # 重复检查
        config_path = self._user_config_path()
        existing = list_servers_in_config(config_path=config_path)
        if any(s.get("name") == name for s in existing):
            raise RpcError(-32602, f"MCP server '{name}' already exists")

        # 构建 TOML 条目
        server_dict: dict[str, Any] = {"name": name, "transport": parsed.transport}
        if parsed.command:
            server_dict["command"] = parsed.command
        if parsed.args:
            server_dict["args"] = list(parsed.args)
        if parsed.url:
            server_dict["url"] = parsed.url
        if parsed.env:
            server_dict["env"] = dict(parsed.env)
        if parsed.headers:
            server_dict["headers"] = dict(parsed.headers)

        # 持久化配置
        add_server_to_config(server_dict, config_path=config_path)

        # 热连接
        mcp_config = McpServerConfig(
            name=name,
            transport=parsed.transport,
            command=parsed.command,
            args=tuple(parsed.args) if parsed.args else (),
            env=dict(parsed.env) if parsed.env else {},
            url=parsed.url,
            headers=dict(parsed.headers) if parsed.headers else {},
        )
        if self._mcp_manager is None:
            self._mcp_manager = McpConnectionManager([])
        status = await self._mcp_manager.add_server(mcp_config)

        return {
            "added": True,
            "connected": status.get("status") == "connected",
            "tool_names": status.get("tool_names", []),
            "error": status.get("error"),
        }

    async def _handle_mcp_remove(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """从用户配置中删除 MCP 服务器并热断开。"""
        from harness_agent.mcp_config_writer import remove_server_from_config

        parsed = McpRemoveParams.model_validate(params)
        name = parsed.name

        # 从配置文件删除（不存在时抛 ValueError）
        config_path = self._user_config_path()
        try:
            remove_server_from_config(name, config_path=config_path)
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc

        # 热断开
        if self._mcp_manager is not None:
            self._mcp_manager.remove_server(name)

        return {"removed": True}

    async def _handle_host_attachment_create(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """由 Host owner 签发一次性本机 WebSocket attachment。"""
        connection = self._current_connection()
        if connection.role != "owner":
            raise RpcError(
                -32007,
                "HOST_OWNER_REQUIRED",
                {"code": "HOST_OWNER_REQUIRED", "retryable": False},
            )
        parsed = HostAttachmentCreateParams.model_validate(params)
        origin = parsed.origin
        if not (
            origin.startswith("http://127.0.0.1:")
            or origin.startswith("http://localhost:")
        ):
            raise RpcError(-32602, "Attachment origin must be a loopback HTTP origin")
        await self._ensure_websocket_listener()
        token = secrets.token_urlsafe(32)
        expires_at_ms = int(time.time() * 1000) + 60_000
        ceiling = frozenset(
            capability
            for capability in self._connection_capabilities(connection)
            if capability != CAPABILITY["HOST_ATTACH"]
        )
        async with self._attachment_lock:
            now_ms = int(time.time() * 1000)
            self._attachment_grants = {
                key: grant
                for key, grant in self._attachment_grants.items()
                if grant.expires_at_ms > now_ms
            }
            self._attachment_grants[token] = _AttachmentGrant(
                origin=origin,
                expires_at_ms=expires_at_ms,
                capability_ceiling=ceiling,
            )
        socket = self._websocket_server.sockets[0]
        port = socket.getsockname()[1]
        return {
            "endpoint": f"ws://127.0.0.1:{port}",
            "token": token,
            "expires_at_ms": expires_at_ms,
        }

    async def _ensure_websocket_listener(self) -> None:
        """惰性启动只绑定 loopback 的无框架 WebSocket adapter。"""
        if self._websocket_server is not None:
            return
        from websockets.asyncio.server import serve

        self._websocket_server = await serve(
            self._handle_websocket,
            "127.0.0.1",
            0,
            max_size=MAX_FRAME_BYTES,
            max_queue=16,
            compression=None,
        )

    async def _handle_websocket(self, websocket: Any) -> None:
        """认证 attachment 后，将 WebSocket frame 交给同一 protocol dispatcher。"""
        connection: ProtocolConnection | None = None
        writer: asyncio.Task[None] | None = None
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)

        async def send_attached(message: dict[str, Any]) -> None:
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
                raise RpcError(-32603, "Outbound JSON-RPC frame exceeds size limit")
            try:
                queue.put_nowait(encoded)
            except asyncio.QueueFull as exc:
                raise RpcError(-32004, "Attached connection is too slow") from exc

        async def write_frames() -> None:
            while True:
                await websocket.send(await queue.get())

        try:
            raw_auth = await asyncio.wait_for(websocket.recv(), timeout=5)
            if not isinstance(raw_auth, str):
                await websocket.close(code=1008, reason="Attachment rejected")
                return
            auth = json.loads(raw_auth)
            if set(auth) != {"type", "token"} or auth.get("type") != "auth":
                await websocket.close(code=1008, reason="Attachment rejected")
                return
            token = auth.get("token")
            origin = websocket.request.headers.get("Origin")
            async with self._attachment_lock:
                grant = self._attachment_grants.get(token) if isinstance(token, str) else None
                if (
                    grant is None
                    or grant.expires_at_ms <= int(time.time() * 1000)
                    or origin != grant.origin
                ):
                    grant = None
                else:
                    self._attachment_grants.pop(token, None)
            if grant is None:
                await websocket.close(code=1008, reason="Attachment rejected")
                return
            connection = self.create_connection(
                send_attached,
                capability_ceiling=grant.capability_ceiling,
            )
            writer = asyncio.create_task(write_frames(), name="harness-websocket-writer")
            await websocket.send(json.dumps({"type": "ready"}, separators=(",", ":")))
            async for raw in websocket:
                if not isinstance(raw, str):
                    await websocket.close(code=1003, reason="Text frames required")
                    break
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.close(code=1007, reason="Invalid JSON")
                    break
                if not isinstance(message, dict):
                    await websocket.close(code=1007, reason="Invalid JSON-RPC")
                    break
                await self.dispatch_connection(connection, message)
        except (TimeoutError, json.JSONDecodeError):
            await websocket.close(code=1008, reason="Attachment rejected")
        finally:
            if connection is not None:
                await self.close_connection(connection)
            if writer is not None:
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)

    def _user_config_path(self) -> Path:
        """返回用户级配置文件路径。"""
        return Path.home() / ".harness" / "config.toml"

    async def _handle_config_path(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回配置合并路径。"""
        if _params:
            raise RpcError(-32602, "config.path does not accept params")
        self._load_config()
        return {
            "workspace": str(self._workspace),
            "paths": [str(path) for path in self._config.paths] if self._config else [],
            "explicit_path": self._config_path,
        }

    async def _handle_models_list(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """返回 `/model` 可安全展示的 Profile 目录与可选 Thread 绑定摘要。"""
        self._require_models_capability()
        parsed = ModelsListParams.model_validate(params)
        self._load_config()
        config = self._config
        if config is None or config.model_catalog is None:
            raise RpcError(-32010, self._startup_error or "MODEL_CONFIGURATION_REQUIRED")
        result: dict[str, object] = {
            "profiles": [
                profile.picker_summary()
                for _, profile in sorted(config.model_catalog.profiles.items())
            ]
        }
        if parsed.thread_id is not None:
            if CAPABILITY["MODELS_SELECT"] in self._connection_capabilities():
                latest = await (await self._ensure_thread_store()).get_latest_run_execution_binding(
                    parsed.thread_id
                )
                if latest is not None:
                    result["thread_selection"] = latest.requested_selection
                    result["last_run_binding"] = {
                        **latest.actual_primary_binding,
                        "runtime_profile_id": latest.runtime_profile_id,
                    }
            # 未协商 models.select 时只返回不可变绑定摘要。
            result["thread_binding"] = await self._thread_model_binding_summary(parsed.thread_id, config)
        return result

    async def _handle_threads_list(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """返回当前 project 内最近活跃的 thread；thread_id 仅供客户端内部打开。"""
        self._require_threads_capability()
        parsed = ThreadsListParams.model_validate(params)
        threads = await (await self._ensure_thread_store()).list_threads(parsed.limit)
        return {"threads": [_thread_summary_payload(thread) for thread in threads]}

    async def _handle_threads_open(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """读取当前 project 的一个 thread 历史，拒绝跨 project 或无 checkpoint 记录。"""
        self._require_threads_capability()
        parsed = ThreadsOpenParams.model_validate(params)
        try:
            opened = await (await self._ensure_thread_store()).open_thread(parsed.thread_id)
        except ThreadStoreError as exc:
            if str(exc) in {"THREAD_NOT_FOUND", "THREAD_NOT_RECOVERABLE"}:
                raise RpcError(-32004, str(exc)) from exc
            raise
        return {
            "thread": _thread_summary_payload(opened.summary),
            "messages": [_thread_message_payload(message) for message in opened.messages],
        }

    async def _handle_threads_watch(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """仅在 Thread 空闲时原子读取历史并登记当前 Connection 的观察关系。"""
        parsed = ThreadsOpenParams.model_validate(params)
        async with self._registry_lock:
            active = self._runs.get(parsed.thread_id)
            if (
                parsed.thread_id in self._starting_threads
                or active is not None
                and active.status in {"running", "interrupted"}
            ):
                raise RpcError(
                    -32000,
                    "THREAD_BUSY",
                    {"code": "THREAD_BUSY", "retryable": True},
                )
            result = await self._handle_threads_open(params, _id)
            self._connection_watches().add(parsed.thread_id)
            return result

    async def _handle_threads_unwatch(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """移除当前 Connection 的 Thread 观察关系。"""
        parsed = ThreadsOpenParams.model_validate(params)
        watches = self._connection_watches()
        removed = parsed.thread_id in watches
        watches.discard(parsed.thread_id)
        return {"removed": removed}

    def _require_skills(self) -> SkillRegistry:
        """返回初始化时建立的 Skill registry。"""
        if self._skill_registry is None:
            self._skill_registry = SkillRegistry(self._workspace, home=self._config_home)
        return self._skill_registry

    @staticmethod
    def _reject_params(params: Mapping[str, Any], allowed: set[str], method: str) -> None:
        """拒绝管理接口的未知字段，避免 CLI 拼写错误被静默忽略。"""
        unknown = set(params) - allowed
        if unknown:
            raise RpcError(-32602, f"{method} contains unsupported fields: {', '.join(sorted(unknown))}")

    async def _handle_skills_list(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回当前 catalog 的摘要、快照 ID 和诊断。"""
        self._reject_params(params, {"include_disabled"}, "skills.list")
        include_disabled = params.get("include_disabled", True)
        if not isinstance(include_disabled, bool):
            raise RpcError(-32602, "include_disabled must be boolean")
        registry = self._require_skills()
        return {
            "snapshot": registry.snapshot(),
            "skills": registry.list(include_disabled=include_disabled),
            "diagnostics": registry.diagnostics[:20],
        }

    async def _handle_skills_inspect(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回一个 Skill 的安全元数据。"""
        self._reject_params(params, {"id"}, "skills.inspect")
        skill_id = params.get("id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise RpcError(-32602, "id must be a non-empty string")
        return self._require_skills().inspect(skill_id)

    async def _handle_skills_set_enabled(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """保存下一次 thread 生效的 Skill 启停偏好。"""
        self._reject_params(params, {"id", "enabled"}, "skills.set_enabled")
        skill_id = params.get("id")
        enabled = params.get("enabled")
        if not isinstance(skill_id, str) or not skill_id.strip() or not isinstance(enabled, bool):
            raise RpcError(-32602, "id and enabled are required")
        return self._require_skills().set_enabled(skill_id, enabled)

    async def _handle_skills_market_list(self, params: dict[str, Any], _id: str) -> list[dict[str, object]]:
        """列出已安装的企业市场 Provider 或其 catalog。"""
        self._reject_params(params, {"market"}, "skills.market.list")
        market = params.get("market")
        if market is not None and not isinstance(market, str):
            raise RpcError(-32602, "market must be a string")
        return await self._require_skills().marketplace_catalog(market)

    async def _handle_skills_install(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """通过企业 Provider 安装 Skill；Provider 不存在时返回明确错误。"""
        self._reject_params(params, {"market", "name", "version"}, "skills.install")
        market, name, version = params.get("market"), params.get("name"), params.get("version")
        if not isinstance(market, str) or not isinstance(name, str) or (version is not None and not isinstance(version, str)):
            raise RpcError(-32602, "market and name are required strings")
        return await self._require_skills().install(market, name, version)

    async def _handle_skills_update(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """通过企业 Provider 更新市场 Skill。"""
        return await self._handle_skills_install(params, _id)

    async def _handle_skills_remove(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """移除一个已安装市场 Skill。"""
        self._reject_params(params, {"id"}, "skills.remove")
        skill_id = params.get("id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise RpcError(-32602, "id must be a non-empty string")
        return self._require_skills().remove(skill_id)

    def _load_config(self) -> None:
        """刷新配置缓存，并保存用户可修复的错误。"""
        try:
            self._config = load_config(
                workspace=self._workspace,
                config_path=self._config_path,
                home=self._config_home,
            )
            self._startup_error = None
        except ConfigError as exc:
            self._config = None
            self._startup_error = str(exc)

    def _config_changes(self) -> ConfigChangeService:
        """延迟创建受控写服务，使其始终绑定握手后确定的 workspace 与来源。"""
        if self._config_change_service is None:
            self._config_change_service = ConfigChangeService(
                workspace=self._workspace,
                home=self._config_home,
                config_path=self._config_path,
                managed_policy=self._config_change_policy,
            )
        return self._config_change_service

    @staticmethod
    def _config_change_rpc_error(error: ConfigChangeError) -> RpcError:
        """将领域错误映射为不含 TOML 或秘密值的稳定 RPC 响应。"""
        return RpcError(-32012, error.code, error.redacted_data())

    async def _execute_run(self, run: ActiveRun) -> None:
        """执行并自动恢复中断，保证每个 run 只产生一个终态。"""
        started_payload: dict[str, object] = {
            "resumed": False,
            "skills_snapshot_id": self._skill_registry.snapshot_id if self._skill_registry else None,
        }
        if run.primary_model_binding is not None:
            started_payload["primary_model"] = {
                **run.primary_model_binding,
                "runtime_profile_id": run.runtime_profile_id,
            }
            started_payload["runtime_profile_id"] = run.runtime_profile_id
        await self._emit(run, EVENT_TYPE["RUN_STARTED"], started_payload)
        resume: Any | None = None
        try:
            if run.requested_skill is not None:
                registry = self._require_skills()
                loaded = registry.load(run.requested_skill["id"], run.requested_skill.get("args", ""))
                await self._emit(
                    run,
                    EVENT_TYPE["SKILL_LOADED"],
                    {
                        "skill_id": loaded.record.skill_id,
                        "source": loaded.record.source,
                        "version": loaded.record.version,
                        "snapshot_id": registry.snapshot_id,
                    },
                )
                run.message = (
                    f"The user explicitly selected Skill `{loaded.record.skill_id}`. "
                    f"Read `/.harness/skills/{loaded.record.skill_id}/SKILL.md` with read_file before using it.\n\n"
                    f"User request:\n{run.message}"
                )
            if self._uses_default_agent_factory:
                agent = await self._acquire_default_runtime_for_run(run)
            else:
                agent = await self._ensure_agent()
            if agent is None:
                if not self._allow_echo:
                    raise ConfigError(self._startup_error or "Agent is not configured")
                await self._emit(run, EVENT_TYPE["CONTENT_DELTA"], {"text": run.message})
            else:
                while True:
                    resume = await self._stream_agent(agent, run, resume=resume)
                    if resume is None:
                        break
            if self._thread_store is not None:
                await self._thread_store.refresh_thread(run.thread_id)
            await self._drain_context_updates(run)
            run.status = "completed"
            await self._emit(
                run,
                EVENT_TYPE["RUN_COMPLETED"],
                {
                    "usage": run.usage,
                    "duration_ms": round((time.monotonic() - run.started_at) * 1000),
                    "finish_reason": "completed",
                    "context": run.context_summary,
                },
            )
        except asyncio.CancelledError:
            run.status = "cancelled"
            await self._emit(run, EVENT_TYPE["RUN_CANCELLED"], {"reason": "Cancelled by client"})
        except RuntimePoolCapacityError as exc:
            run.status = "failed"
            await self._emit(
                run,
                EVENT_TYPE["RUN_FAILED"],
                {
                    "error": {
                        "code": "RUNTIME_POOL_CAPACITY_EXHAUSTED",
                        "message": str(exc),
                        "retryable": True,
                    }
                },
            )
        except Exception as exc:
            run.status = "failed"
            logger.exception("Agent run failed: %s", run.run_id)
            await self._emit(
                run,
                EVENT_TYPE["RUN_FAILED"],
                {
                    "error": {
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "retryable": False,
                    }
                },
            )
        finally:
            await self._release_run_runtime(run)
            if self._thread_store is not None and run.status != "completed":
                try:
                    await self._thread_store.refresh_thread(run.thread_id)
                except ThreadStoreError:
                    logger.exception("Unable to refresh checkpoint index for thread %s", run.thread_id)
            self._runs.pop(run.thread_id, None)

    async def _ensure_agent(self) -> Any | None:
        """按需构建外部注入的 Agent；默认图必须经 RuntimePool 取得。"""
        # Echo 只用于协议测试。即使当前目录恰好存在模型配置，也必须保持
        # 无网络、无凭据依赖的确定性行为，避免测试机器环境改变结果。
        if self.agent is not None:
            return self.agent
        if self._allow_echo:
            return None
        self._load_config()
        if self._config is None or self._config.model is None:
            return None
        if self._uses_default_agent_factory:
            raise RuntimeError("DEFAULT_AGENT_REQUIRES_RUNTIME_POOL")
        if self._agent_factory is None:  # pragma: no cover - 构造函数不变量。
            raise RuntimeError("AGENT_FACTORY_REQUIRED")
        # 外部注入工厂保持既有单图测试/嵌入契约；生产默认路径由 RuntimePool
        # 提供 per-Profile single-flight，不应再写入 ``self.agent``。
        async with self._agent_build_lock:
            if self.agent is not None:
                return self.agent
            created = self._agent_factory(self._config, self._workspace)
            self.agent = await created if inspect.isawaitable(created) else created
            return self.agent

    async def _acquire_default_runtime_for_run(self, run: ActiveRun) -> Any | None:
        """为一个生产 run 获取共享 Runtime，并将 thread 私有状态写入 RunContext。"""
        lease, runtime = await self._acquire_default_runtime(
            run.thread_id,
            run.model_bindings,
            profile=run.resolved_runtime_profile,
        )
        if runtime is None:
            return None
        try:
            artifacts = self._runtime_artifacts.get(runtime.profile_key)
            spec = self._runtime_build_specs.get(runtime.profile_key)
            if artifacts is None or spec is None or runtime.graph is None:
                raise RuntimeError("RUNTIME_ARTIFACTS_UNAVAILABLE")
            run.run_context = await self._create_run_context(
                run,
                profile=runtime.profile,
                config=spec.config,
                execution_context=artifacts.execution_context,
            )
            run.runtime_run_lease = await lease.run()
            run.runtime_lease = lease
            run.runtime_profile_key = runtime.profile_key
            return runtime.graph
        except Exception:
            await self._release_runtime_lease(lease)
            raise

    async def _acquire_default_runtime(
        self,
        thread_id: str,
        model_bindings: ThreadModelBindings | None = None,
        *,
        profile: RuntimeProfile | None = None,
    ) -> tuple[AgentRuntimeLease | None, AgentRuntime | None]:
        """为 Run 或手动压缩取得按实际模型计算的共享 Runtime。"""
        if self._allow_echo:
            return None, None
        self._load_config()
        config = self._config
        if config is None or config.model is None:
            return None, None
        profile = profile or await self._resolve_runtime_profile(thread_id, config, model_bindings)
        pool = self._ensure_runtime_pool(config)
        lease = await pool.acquire(profile)
        return lease, lease.runtime

    async def _resolve_runtime_profile(
        self,
        thread_id: str,
        config: Za38Config,
        model_bindings: ThreadModelBindings | None = None,
    ) -> RuntimeProfile:
        """按本次实际模型计算可共享 Profile，不把它永久绑定到 Thread。"""
        from harness_agent.agent import (
            default_prompt_template_fingerprint,
            default_tool_catalog_fingerprint,
        )

        store = await self._ensure_thread_store()
        registry = self._require_skills()
        bindings = model_bindings
        if bindings is None:
            bindings, _, _ = await self._resolve_run_model_bindings(thread_id, config)
        if bindings is not None:
            selected_profile_id = bindings.runtime_primary().profile_id
            selected_model = bindings.runtime_primary().settings
        else:
            selected_profile_id = config.model_profile
            selected_model = config.require_model()
        mcp_fp = (
            mcp_config_fingerprint(list(config.mcp_servers))
            if config.mcp_servers
            else component_fingerprint({"transport": "disabled"})
        )
        profile = default_runtime_profile(
            project_fingerprint=store.project_fingerprint,
            model_profile=selected_profile_id,
            model=selected_model,
            tool_catalog_fingerprint=default_tool_catalog_fingerprint(),
            skill_catalog_fingerprint=component_fingerprint(
                {"skill_snapshot_id": registry.snapshot_id}
            ),
            execution=config.execution,
            mcp_fingerprint=mcp_fp,
            middleware_fingerprint=component_fingerprint(
                {
                    "prompt_epoch": 1,
                    "context_window": 1,
                    "workspace_boundary": 1,
                    "interactive_question": "question" in self._connection_handles(),
                }
            ),
            prompt_template_fingerprint=default_prompt_template_fingerprint(),
        )
        # Runtime Profile 只按配置去重保存；thread 级绑定仅用于读取 v4/v5 legacy。
        await store.save_runtime_profile(thread_id, profile, bind_thread=False)

        self._runtime_build_specs[profile.profile_key] = _RuntimeBuildSpec(
            config=config,
            workspace=self._workspace,
            skill_registry=registry,
            model_settings=selected_model,
        )
        return profile

    async def _resolve_run_model_bindings(
        self,
        thread_id: str,
        config: Za38Config,
        *,
        requested_primary_profile: str | None = None,
        legacy_model_profile: str | None = None,
    ) -> tuple[ThreadModelBindings | None, dict[str, object], dict[str, object]]:
        """按请求、最近 Run、legacy 绑定和全新 Thread 默认值顺序解析一次 Run 的主模型。"""
        if (
            requested_primary_profile is not None
            and legacy_model_profile is not None
            and requested_primary_profile != legacy_model_profile
        ):
            raise ConfigError("MODEL_SELECTION_CONFLICT")
        if config.model_catalog is None:
            if requested_primary_profile is not None or legacy_model_profile is not None:
                raise ConfigError("MODEL_CATALOG_UNAVAILABLE")
            if config.model is None or config.model_profile is None:
                raise ConfigError("MODEL_CONFIGURATION_REQUIRED")
            profile_id = config.model_profile
            return (
                None,
                {"primary_profile": profile_id},
                {
                    "profile": {
                        "id": profile_id,
                        "model": config.model.name,
                        "provider_label": config.model.provider_label,
                        "context_window_tokens": config.model.context_window_tokens,
                        "capabilities": sorted(config.model.capabilities),
                        "is_default": True,
                        "available": config.model.api_key_source() != "missing",
                        "unavailable_reason": None,
                        "source": "compatibility",
                    },
                    "source": "config-default",
                },
            )
        store = await self._ensure_thread_store()
        router = ModelRouter(config.model_catalog)
        profile_id = requested_primary_profile or legacy_model_profile
        source = "thread-primary" if requested_primary_profile is not None else "legacy-model-profile"
        if profile_id is None:
            latest = await store.get_latest_run_execution_binding(thread_id)
            if latest is not None:
                profile_id = latest.requested_selection["primary_profile"]
                source = "thread-recovered"
        if profile_id is not None:
            bindings = router.resolve_run(str(profile_id))
        else:
            record = await store.get_model_bindings(thread_id)
            if record is not None:
                bindings = router.from_record(record)
                source = "legacy-binding"
            else:
                # 全新 Thread 必须使用 models.default_profile；roles.executor 仅保留给
                # legacy/多角色兼容映射，不能覆盖用户刚通过 /model 写入的未来默认值。
                bindings = router.resolve_run(config.model_catalog.default_profile)
                source = "config-default"
        primary = bindings.runtime_primary()
        return (
            bindings,
            {"primary_profile": primary.profile_id},
            {"profile": primary.picker_summary(), "source": source},
        )

    async def _thread_model_binding_summary(
        self, thread_id: str, config: Za38Config
    ) -> dict[str, object]:
        """返回 v5 兼容绑定摘要；新 Run 选择和事实由单独的表返回。"""
        store = await self._ensure_thread_store()
        record = await store.get_model_bindings(thread_id)
        if record is not None:
            roles = record.get("roles")
            if isinstance(roles, dict):
                return {"state": "bound", "roles": roles}
        bound = await store.get_runtime_profile(thread_id)
        if bound is None:
            return {"state": "unbound", "roles": {}}
        # v4 及以前的 Runtime 只保存不可逆指纹，不能可靠恢复 Profile 名称。
        return {"state": "legacy", "roles": {}}

    def _ensure_runtime_pool(self, config: Za38Config) -> RuntimePool:
        """延迟创建进程内唯一 Pool；容量策略在 Sidecar 生命周期内保持稳定。"""
        if self._runtime_pool is None:
            settings = config.runtime_pool
            self._runtime_pool = RuntimePool(
                self._build_default_runtime,
                max_profiles=settings.max_profiles,
                idle_ttl_seconds=settings.idle_ttl_seconds,
                close_timeout_seconds=settings.close_timeout_seconds,
            )
        return self._runtime_pool

    async def _build_default_runtime(self, profile: RuntimeProfile) -> AgentRuntime:
        """按 Profile 构建一张 deepagents 图及其共享 middleware，供多个 thread 复用。"""
        spec = self._runtime_build_specs.get(profile.profile_key)
        if spec is None:
            raise RuntimeError("RUNTIME_BUILD_SPEC_MISSING")
        config, workspace = spec.config, spec.workspace
        from harness_agent.agent import create_harness_agent
        from harness_agent.context_window import ContextWindowMiddleware
        from harness_agent.execution import create_execution_context
        from harness_agent.providers.harness_gateway import create_openai_compatible_model

        execution_context = create_execution_context(config.execution, workspace)
        store = await self._ensure_thread_store()
        checkpointer = store.checkpointer
        model_settings = spec.model_settings
        model = create_openai_compatible_model(
            model_settings,
            async_client=await self._provider_client_pool.get_async_client(model_settings),
        )
        context_compactor = ContextWindowMiddleware(
            model,
            context_window_tokens=model_settings.context_window_tokens,
            thread_store=store,
            updates=self._context_updates,
        )
        mcp_tools = self._mcp_manager.get_tools() if self._mcp_manager else []
        graph = create_harness_agent(
            model,
            tools=mcp_tools or None,
            mcp_server_info=True if mcp_tools else None,
            cwd=str(workspace),
            # 无头客户端不协商 question 能力时不注册 ask_user；审批仍由
            # `_request_interaction` 在缺少 approval 能力时 fail closed。
            interactive="question" in self._connection_handles(),
            approval_mode=config.execution.approval_mode,
            execution_context=execution_context,
            skill_registry=spec.skill_registry,
            checkpointer=checkpointer,
            thread_store=store,
            context_updates=self._context_updates,
            context_middleware=context_compactor,
            context_window_tokens=model_settings.context_window_tokens,
            shared_runtime=True,
        )
        self._runtime_artifacts[profile.profile_key] = _RuntimeArtifacts(
            execution_context=execution_context,
            context_compactor=context_compactor,
        )
        resources = RuntimeResourceBundle.from_sequences(
            flushers=(
                RuntimeCloseAdapter(
                    "server-runtime-artifacts",
                    lambda: self._drop_runtime_artifacts(profile.profile_key),
                ),
            )
        )
        return AgentRuntime(
            profile=profile,
            graph=graph,
            resources=resources,
            pinned=config.runtime_pool.pin_default_profile,
        )

    async def _create_run_context(
        self,
        run: ActiveRun,
        *,
        profile: RuntimeProfile,
        config: Za38Config,
        execution_context: Any,
    ) -> RunContext:
        """从持久化状态恢复本轮 PromptEpoch，禁止把 thread 数据保存在共享图中。"""
        from harness_agent.agent import create_prompt_epoch

        store = await self._ensure_thread_store()
        epoch = await store.get_prompt_epoch(run.thread_id)
        if epoch is None:
            epoch = create_prompt_epoch(
                thread_id=run.thread_id,
                system_prompt=None,
                workspace=str(getattr(execution_context, "workspace_path", self._workspace)),
                sandboxed=bool(getattr(execution_context, "sandboxed", False)),
                provider=getattr(execution_context, "provider", None),
                approval_mode=config.execution.approval_mode,
                skill_registry=self._require_skills(),
                enable_memory=True,
                enable_skills=True,
            )
            await store.save_prompt_epoch(epoch)
        return RunContext(
            thread_id=run.thread_id,
            run_id=run.run_id,
            prompt_epoch=epoch,
            approval_mode=config.execution.approval_mode,
            profile_key=profile.profile_key,
            cancellation_token=run.cancellation_token,
        )

    async def _stream_agent(self, agent: Any, run: ActiveRun, *, resume: Any | None) -> Any | None:
        """把 LangGraph 双流转换为领域事件；遇到 interrupt 时等待客户端并返回恢复值。"""
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command

        stream_input: Any = (
            Command(resume=resume)
            if resume is not None
            else {"messages": [HumanMessage(content=run.message)]}
        )
        stream_kwargs: dict[str, Any] = {
            "config": (
                self._thread_store.graph_config(run.thread_id)
                if self._thread_store is not None
                else {"configurable": {"thread_id": run.thread_id}}
            ),
            "stream_mode": ["messages", "updates"],
            "subgraphs": True,
        }
        if run.run_context is not None:
            stream_kwargs["context"] = run.run_context
        async for event in agent.astream(stream_input, **stream_kwargs):
            await self._drain_context_updates(run)
            interaction = self._extract_interaction(event)
            if interaction is not None:
                run.status = "interrupted"
                response = await self._request_interaction(run, interaction)
                run.status = "running"
                await self._emit(
                    run,
                    EVENT_TYPE["INTERACTION_RESOLVED"],
                    {"request_id": interaction.request_id, "type": interaction.type},
                )
                return self._resume_value(interaction, response)
            for event_type, payload in self._translate_stream_event(event, run):
                await self._emit(run, event_type, payload)
        return None

    async def _drain_context_updates(self, run: ActiveRun) -> None:
        """把中间件的预算状态转成顺序化事件；网关未返回缓存 usage 时保持 unknown。"""
        updates = self._context_updates.pop(run.thread_id, [])
        for update in updates:
            payload = update.payload() if hasattr(update, "payload") else dict(update)
            run.context_summary = payload
            await self._emit(run, EVENT_TYPE["CONTEXT_UPDATED"], payload)

    def _translate_stream_event(
        self, event: tuple[Any, ...], run: ActiveRun
    ) -> Iterable[tuple[str, dict[str, Any]]]:
        """翻译文本和工具分片，不让 LangChain 对象跨越协议边界。"""
        if len(event) == 3:
            _namespace, stream_mode, data = event
        elif len(event) == 2:
            stream_mode, data = event
        else:
            return []
        if stream_mode != "messages" or not isinstance(data, tuple) or not data:
            return []
        chunk = data[0]
        self._update_usage(run, getattr(chunk, "usage_metadata", None))
        events: list[tuple[str, dict[str, Any]]] = []
        # dcode 以 LangChain 规范化后的 content_blocks 为准；部分 OpenAI 兼容网关
        # 会在流式首轮只填充该属性，直接读取 content 会产生“有 token、无正文”。
        content = _message_text(chunk)
        if content and type(chunk).__name__ != "ToolMessage":
            events.append((EVENT_TYPE["CONTENT_DELTA"], {"text": content}))
        for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
            tool_id = self._resolve_tool_stream_id(run, tool_chunk)
            if tool_chunk.get("name") and tool_id not in run.started_tool_ids:
                run.started_tool_ids.add(tool_id)
                events.append(
                    (EVENT_TYPE["TOOL_STARTED"], {"tool_call_id": tool_id, "name": str(tool_chunk["name"])})
                )
            if tool_chunk.get("args"):
                arguments = _truncate_text(str(tool_chunk["args"]))
                events.append(
                    (
                        EVENT_TYPE["TOOL_DELTA"],
                        {
                            "tool_call_id": tool_id,
                            "arguments_delta": arguments[0],
                            "truncated": arguments[1],
                            "original_bytes": arguments[2],
                        },
                    )
                )
        if type(chunk).__name__ == "ToolMessage":
            result = _truncate_text(_content_text(getattr(chunk, "content", None)))
            result_id = str(getattr(chunk, "tool_call_id", "") or "")
            tool_id = run.tool_result_ids.get(result_id, result_id) or run.last_tool_id or f"tool-{run.run_id}"
            events.append(
                (
                    EVENT_TYPE["TOOL_COMPLETED"],
                    {
                        "tool_call_id": tool_id,
                        "result": {
                            "content": result[0],
                            "is_error": getattr(chunk, "status", None) == "error",
                            "truncated": result[1],
                            "original_bytes": result[2],
                        },
                    },
                )
            )
        return events

    def _resolve_tool_stream_id(self, run: ActiveRun, chunk: Mapping[str, Any]) -> str:
        """用真实调用 ID 优先关联工具分片，并为缺失 ID 的续片保留 index 映射。"""
        index = chunk.get("index")
        raw_id = str(chunk.get("id") or "")
        if raw_id:
            # LangChain 每轮模型响应都会从 index=0 重新编号。真实 id 到达时必须
            # 覆盖该临时映射，否则第二轮工具会回写第一轮的卡片和执行结果。
            tool_id = run.tool_result_ids.get(raw_id, raw_id)
            run.tool_result_ids[raw_id] = tool_id
            run.tool_stream_ids[f"id:{raw_id}"] = tool_id
            if index is not None:
                run.tool_stream_ids[f"index:{index}"] = tool_id
        else:
            key = f"index:{index}" if index is not None else "current"
            tool_id = run.tool_stream_ids.get(key)
            if tool_id is None:
                tool_id = f"tool-{run.run_id}-{len(run.tool_stream_ids)}"
                run.tool_stream_ids[key] = tool_id
        run.last_tool_id = tool_id
        return tool_id

    def _extract_interaction(self, event: tuple[Any, ...]) -> InteractionSpec | None:
        """从 updates 流提取首个 AskUser 或 HITL interrupt。"""
        if len(event) == 3:
            _namespace, stream_mode, data = event
        elif len(event) == 2:
            stream_mode, data = event
        else:
            return None
        if stream_mode != "updates" or not isinstance(data, Mapping):
            return None
        interrupts = data.get("__interrupt__")
        if not interrupts:
            return None
        interrupt = (interrupts if isinstance(interrupts, (list, tuple)) else [interrupts])[0]
        value = getattr(interrupt, "value", interrupt)
        interrupt_id = str(getattr(interrupt, "id", uuid.uuid4()))
        if isinstance(value, Mapping) and value.get("type") == "ask_user":
            raw_questions = value.get("questions")
            questions = [q for q in raw_questions or [] if isinstance(q, Mapping)]
            normalized = []
            for index, question in enumerate(questions):
                options = [
                    {"label": str(choice.get("value", "")), "value": str(choice.get("value", "")), "description": ""}
                    for choice in question.get("choices", [])
                    if isinstance(choice, Mapping) and choice.get("value")
                ]
                normalized.append(
                    {
                        "id": f"question-{index + 1}",
                        "question": str(question.get("question", "Agent needs input")),
                        "header": "",
                        "body": "",
                        "options": options,
                        "multi_select": False,
                        "allow_other": True,
                    }
                )
            return InteractionSpec(
                request_id=interrupt_id,
                type="question",
                payload={"interrupt_id": interrupt_id, "questions": normalized},
                interrupt_id=interrupt_id,
                questions=questions,
            )
        description = "A tool execution requires approval"
        action_count = 1
        if isinstance(value, Mapping):
            action_requests = value.get("action_requests", [])
            action_count = len(action_requests) if isinstance(action_requests, list) else 1
            descriptions = [
                str(request.get("description"))
                for request in action_requests
                if isinstance(request, Mapping) and request.get("description")
            ]
            if descriptions:
                description = "\n\n".join(descriptions)
        return InteractionSpec(
            request_id=interrupt_id,
            type="approval",
            payload={
                "interrupt_id": interrupt_id,
                "description": description,
                "requests": _bounded_json(value),
                "decisions": ["approve_once", "reject"],
            },
            interrupt_id=interrupt_id,
            action_count=action_count,
        )

    async def _request_interaction(self, run: ActiveRun, spec: InteractionSpec) -> object:
        """只向 Run owner 发送 v3 Interaction；异常时审批拒绝、问答取消。"""
        owner = self._connections.get(
            run.owner_connection_id or self._owner_connection.connection_id
        )
        if owner is None or owner.closed or spec.type not in self._connection_handles(owner):
            logger.info("Interaction %s disabled by capability negotiation", spec.type)
            return (
                {"decision": "reject"}
                if spec.type == "approval"
                else {"answers": {}}
            )
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        owner.pending_requests[spec.request_id] = future
        owner.interaction_specs[spec.request_id] = spec
        method = (
            METHOD["INTERACTION_APPROVAL"]
            if spec.type == "approval"
            else METHOD["INTERACTION_QUESTION"]
        )
        await self._send_to(
            owner,
            {
                "jsonrpc": "2.0",
                "method": method,
                "id": spec.request_id,
                "params": {
                    "thread_id": run.thread_id,
                    "run_id": run.run_id,
                    "timeout_ms": INTERACTION_TIMEOUT_MS,
                    "payload": spec.payload,
                },
            }
        )
        try:
            return await asyncio.wait_for(future, timeout=INTERACTION_TIMEOUT_MS / 1000)
        except (TimeoutError, RpcError, ValidationError) as exc:
            logger.warning("Interaction %s failed closed: %s", spec.request_id, exc)
            return (
                {"decision": "reject"}
                if spec.type == "approval"
                else {"answers": {}}
            )
        finally:
            owner.pending_requests.pop(spec.request_id, None)
            owner.interaction_specs.pop(spec.request_id, None)

    async def _handle_peer_response(self, message: dict[str, Any]) -> None:
        """用客户端 response 解析并恢复对应交互 Future。"""
        connection = self._current_connection()
        request_id = message.get("id")
        if not isinstance(request_id, str):
            await self.send_error(None, -32600, "Response id must be a string")
            return
        future = connection.pending_requests.get(request_id)
        if future is None or future.done():
            await self.send_error(request_id, -32004, "REQUEST_EXPIRED")
            return
        if "error" in message:
            error = message.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), int) or not isinstance(error.get("message"), str):
                future.set_exception(RpcError(-32600, "Invalid JSON-RPC error response"))
                return
            detail = error["message"]
            future.set_exception(RpcError(-32004, str(detail)))
            return
        if "result" not in message:
            future.set_exception(RpcError(-32600, "Response must contain result or error"))
            return
        result = message.get("result")
        try:
            spec = connection.interaction_specs.get(request_id)
            if spec is None:
                raise ValueError("Unknown interaction request")
            method = (
                METHOD["INTERACTION_APPROVAL"]
                if spec.type == "approval"
                else METHOD["INTERACTION_QUESTION"]
            )
            validate_interaction_result(method, result)
            parsed = dict(result) if isinstance(result, dict) else result
        except (ValidationError, ValueError) as exc:
            future.set_exception(RpcError(-32602, f"Invalid interaction response: {exc}"))
            return
        future.set_result(parsed)

    def _resume_value(self, spec: InteractionSpec, response: object) -> dict[str, object]:
        """将语言无关交互结果映射回 LangGraph interrupt resume 契约。"""
        assert isinstance(response, dict)
        if spec.type == "approval":
            decision = response.get("decision")
            langgraph_decision = "approve" if decision in {"approve_once", "approve_thread", "approve_always"} else "reject"
            # 模型可能在一轮中并行发出多个需审批的工具调用，HITL 中间件会把
            # 它们打包为单个 interrupt；用户的一个审批决定需复制到每个挂起
            # 的 tool call，使 decisions 列表长度与 action_count 相等。
            return {
                spec.interrupt_id: {
                    "decisions": [{"type": langgraph_decision}] * spec.action_count
                }
            }
        answers_by_id = response.get("answers", {})
        answers: list[str] = []
        if isinstance(answers_by_id, Mapping):
            for index, _question in enumerate(spec.questions):
                values = answers_by_id.get(f"question-{index + 1}", [])
                answers.append(str(values[0]) if isinstance(values, list) and values else "")
        status = "answered" if any(answers) else "cancelled"
        return {spec.interrupt_id: {"status": status, "answers": answers}}

    async def _emit(self, run: ActiveRun, event_type: str, payload: dict[str, Any]) -> None:
        """生成单调事件并广播给 Run owner 与 Thread observers。"""
        run.sequence += 1
        event = {
            "event_id": str(uuid.uuid4()),
            "type": event_type,
            "thread_id": run.thread_id,
            "run_id": run.run_id,
            "sequence": run.sequence,
            "timestamp_ms": int(time.time() * 1000),
            "payload": payload,
        }
        message = {"jsonrpc": "2.0", "method": METHOD["EVENT"], "params": event}
        targets = [
            connection
            for connection in self._connections.values()
            if not connection.closed
            and (
                connection.connection_id
                == (run.owner_connection_id or self._owner_connection.connection_id)
                or run.thread_id in self._connection_watches(connection)
            )
        ]
        results = await asyncio.gather(
            *(self._send_to(connection, message) for connection in targets),
            return_exceptions=True,
        )
        for connection, result in zip(targets, results, strict=True):
            if isinstance(result, Exception) and connection is not self._owner_connection:
                asyncio.create_task(self.close_connection(connection))

    def _require_run(self, thread_id: str, run_id: str) -> ActiveRun:
        """拒绝过期或跨线程控制请求。"""
        run = self._runs.get(thread_id)
        if run is None or run.run_id != run_id:
            raise RpcError(-32001, "RUN_NOT_FOUND")
        return run

    def _update_usage(self, run: ActiveRun, usage: Any) -> None:
        """合并分片 usage，避免流式计数回退。"""
        if not isinstance(usage, Mapping):
            return
        run.usage["input_tokens"] = max(
            run.usage["input_tokens"], int(usage.get("input_tokens", 0) or 0)
        )
        run.usage["output_tokens"] = max(
            run.usage["output_tokens"], int(usage.get("output_tokens", 0) or 0)
        )

    async def _cancel_all_runs(self) -> None:
        """取消全部运行并等待 finally 清理。"""
        tasks = [run.task for run in self._runs.values() if run.task and not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()

    async def _compact_with_runtime(
        self,
        *,
        agent: Any,
        middleware: Any,
        thread_id: str,
        messages: list[Any],
        store: ThreadStore,
    ) -> dict[str, object]:
        """使用已租用 Runtime 的共享 compactor 改写一个空闲 thread 的 checkpoint。"""
        compacted, update, rewritten = await middleware.compact_now(thread_id, messages)
        # `compact_now` 复用运行期状态缓冲；当前请求直接返回结果，因此必须消费，
        # 防止下一次 Agent run 重复发出过期的 context.updated 事件。
        middleware.consume_updates(thread_id)
        if rewritten:
            from langchain_core.messages import RemoveMessage
            from langgraph.graph.message import REMOVE_ALL_MESSAGES

            await agent.aupdate_state(
                # CompiledStateGraph 将非空 checkpoint_ns 解释为子图路径；项目隔离
                # 由 ProjectScopedAsyncSqliteSaver 在根图空 namespace 上自动补齐。
                store.graph_config(thread_id),
                {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *compacted]},
                as_node="model",
            )
            await store.refresh_thread(thread_id)
        return {"compacted": rewritten, "context": update.payload()}

    async def _release_run_runtime(self, run: ActiveRun) -> None:
        """在 run 的所有终态释放 Runtime lease，并触发排空与空闲 TTL 检查。"""
        run_lease, run.runtime_run_lease = run.runtime_run_lease, None
        lease, run.runtime_lease = run.runtime_lease, None
        profile_key, run.runtime_profile_key = run.runtime_profile_key, None
        if run_lease is not None:
            await run_lease.release()
        if lease is None:
            return
        await self._release_runtime_lease(lease, profile_key=profile_key)

    async def _release_runtime_lease(
        self,
        lease: AgentRuntimeLease | None,
        *,
        profile_key: str | None = None,
    ) -> None:
        """释放非 run 或 run lease；DRAINING Runtime 会在最后一个引用退出后关闭。"""
        if lease is None:
            return
        key = profile_key or lease.runtime.profile_key
        await lease.release()
        pool = self._runtime_pool
        if pool is not None:
            await pool.finalize_draining(key)
            await pool.sweep()

    async def _drop_runtime_artifacts(self, profile_key: str) -> None:
        """清除已关闭 Runtime 的 middleware/执行上下文引用，避免 Sidecar 持有旧资源。"""
        self._runtime_artifacts.pop(profile_key, None)
        self._runtime_build_specs.pop(profile_key, None)

    async def _close_runtime_pool(self) -> None:
        """在关闭 SQLite 前停止 RuntimePool，保证 middleware 不再访问已关闭的 Store。"""
        pool, self._runtime_pool = self._runtime_pool, None
        if pool is not None:
            reports = await pool.aclose()
            failures = [failure for report in reports for failure in report.failures]
            if failures:
                logger.warning("RuntimePool closed with %s resource failures", len(failures))
        self._runtime_artifacts.clear()
        self._runtime_build_specs.clear()
        await self._provider_client_pool.aclose()

    async def _runtime_pool_diagnostics(self) -> dict[str, object]:
        """返回 config.show 的运行池摘要；未初始化/已关闭时不保留旧 Runtime 引用。"""
        pool = self._runtime_pool
        if pool is None:
            return {
                "available": False,
                "state": "not_initialized",
                "memory": {"estimated_bytes": None, "rss_bytes": None, "status": "not_collected"},
            }
        return (await pool.diagnostics()).payload()

    def _threads_enabled(self) -> bool:
        """只有协商了读取能力的交互客户端才启用可恢复 thread 存储。"""
        return CAPABILITY["THREADS_READ"] in self._connection_capabilities() and not self._allow_echo

    def _thread_persistence_enabled(self) -> bool:
        """默认生产图始终持久化 thread；外部注入图保持测试/嵌入调用的无存储契约。"""
        return not self._allow_echo and self._uses_default_agent_factory

    def _require_threads_capability(self) -> None:
        """阻止未协商读取能力的客户端意外读取本地 thread 数据。"""
        if CAPABILITY["THREADS_READ"] not in self._connection_capabilities():
            raise RpcError(-32002, "THREADS_CAPABILITY_REQUIRED")
        if self._allow_echo:
            raise RpcError(-32002, "THREADS_UNAVAILABLE_IN_ECHO_MODE")

    def _require_models_capability(self) -> None:
        """模型目录包含配置摘要，只向显式协商的交互客户端公开。"""
        if CAPABILITY["MODELS_READ"] not in self._connection_capabilities():
            raise RpcError(-32002, "MODELS_CAPABILITY_REQUIRED")
        if self._allow_echo:
            raise RpcError(-32002, "MODELS_UNAVAILABLE_IN_ECHO_MODE")

    def _require_config_write_capability(self) -> None:
        """配置写服务只能由显式协商的 Settings/正式 CLI 客户端调用。"""
        if CAPABILITY["CONFIG_WRITE"] not in self._connection_capabilities():
            raise RpcError(-32002, "CONFIG_WRITE_CAPABILITY_REQUIRED")

    def _require_context_capability(self) -> None:
        """手动压缩会改写本机 checkpoint，必须由显式协商能力的交互客户端发起。"""
        if CAPABILITY["CONTEXT_MANAGE"] not in self._connection_capabilities():
            raise RpcError(-32002, "CONTEXT_CAPABILITY_REQUIRED")
        if not self._thread_persistence_enabled():
            raise RpcError(-32002, "CONTEXT_COMPACTION_UNAVAILABLE")

    async def _ensure_thread_store(self) -> ThreadStore:
        """延迟打开用户级数据库；配置读取不应因为存储创建而被阻塞。"""
        if self._thread_store is None:
            if not self._thread_persistence_enabled():
                raise ThreadStoreError("THREADS_UNAVAILABLE_IN_ECHO_MODE")
            self._thread_store = await ThreadStore.open(
                project=self._workspace,
                home=self._config_home,
            )
        return self._thread_store

    async def _close_thread_store(self) -> None:
        """在 sidecar 生命周期末尾关闭 SQLite 连接和 WAL 句柄。"""
        store, self._thread_store = self._thread_store, None
        if store is not None:
            await store.close()

    def _fail_connection_requests(
        self,
        connection: ProtocolConnection,
        error: Exception,
    ) -> None:
        """连接退出时解除其 Interaction 等待，避免后台任务泄漏。"""
        for future in connection.pending_requests.values():
            if not future.done():
                future.set_exception(error)
        connection.pending_requests.clear()
        connection.interaction_specs.clear()

# 测试仍沿用旧构造名；两者是同一个 Project-scoped Host，不存在第二套协议实现。
JsonRpcServer = AgentHost


def _protocol_error_data(message: str, data: object | None) -> dict[str, object]:
    """把既有领域异常收敛为 v3 稳定错误枚举。"""
    raw = data if isinstance(data, Mapping) else {}
    raw_code = raw.get("code")
    if isinstance(raw_code, str) and raw_code in STABLE_ERROR_CODES:
        stable_code = raw_code
    else:
        stable_code = {
            "PROTOCOL_MISMATCH": "PROTOCOL_VERSION_UNSUPPORTED",
            "PROTOCOL_VERSION_UNSUPPORTED": "PROTOCOL_VERSION_UNSUPPORTED",
            "THREAD_NOT_FOUND": "THREAD_NOT_FOUND",
            "THREAD_NOT_RECOVERABLE": "THREAD_NOT_FOUND",
            "THREAD_BUSY": "THREAD_BUSY",
            "RUN_NOT_FOUND": "RUN_NOT_FOUND",
            "RUN_NOT_OWNER": "RUN_NOT_OWNER",
            "RUN_ID_CONFLICT": "RUN_ID_CONFLICT",
            "REQUEST_EXPIRED": "INTERACTION_EXPIRED",
            "HOST_OWNER_REQUIRED": "HOST_OWNER_REQUIRED",
        }.get(message, "INTERNAL_ERROR")
    result: dict[str, object] = {
        "code": stable_code,
        "retryable": bool(raw.get("retryable", stable_code == "THREAD_BUSY")),
    }
    capability = raw.get("capability")
    if isinstance(capability, str):
        result["capability"] = capability
    details = raw.get("details") if "details" in raw else data
    if details is not None:
        result["details"] = _bounded_json(details)
    return result


def _truncate_text(value: str) -> tuple[str, bool, int]:
    """按 UTF-8 字节安全截断工具输出，并保留原始大小。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return value, False, len(encoded)
    clipped = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return clipped, True, len(encoded)


def _content_text(content: object) -> str:
    """提取 LangChain 内容字段中的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
            if isinstance(item, (str, Mapping))
        )
    return "" if content is None else str(content)


def _message_text(message: object) -> str:
    """优先从 LangChain 标准内容块提取正文，并兼容旧式 content 字段。"""
    blocks = getattr(message, "content_blocks", None)
    if isinstance(blocks, list):
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
        if text:
            return text
    return _content_text(getattr(message, "content", None))


def _json_safe(value: object) -> object:
    """确保中断详情可 JSON 编码，复杂对象降级为字符串。"""
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _thread_summary_payload(summary: Any) -> dict[str, object]:
    """把存储层摘要转换为 JSON-RPC 的 thread 字段，禁止携带原始 project 路径。"""
    return {
        "thread_id": summary.thread_id,
        "created_at_ms": summary.created_at_ms,
        "updated_at_ms": summary.updated_at_ms,
        "first_message": summary.first_message,
        "latest_message": summary.latest_message,
        "message_count": summary.message_count,
    }


def _thread_message_payload(message: Any) -> dict[str, object]:
    """把 checkpoint 归一化消息限制为 TUI 可回放的 project/thread/message 数据。"""
    payload: dict[str, object] = {"kind": message.kind, "content": message.content}
    if message.tool_name is not None:
        payload["tool_name"] = message.tool_name
    return payload


def _bounded_json(value: object) -> object:
    """限制交互详情的 JSON 字节数，避免审批参数撑爆 stdio 与 TUI。"""
    safe = _json_safe(value)
    encoded = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= MAX_TOOL_PAYLOAD_BYTES:
        return safe
    preview = encoded[:MAX_TOOL_PAYLOAD_BYTES].decode("utf-8", errors="ignore")
    return {"truncated": True, "original_bytes": len(encoded), "preview": preview}
