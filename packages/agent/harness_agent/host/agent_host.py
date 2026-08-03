"""Harness v3 Agent Host：承载项目级运行资源与协议连接。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jsonschema.exceptions import ValidationError

from harness_agent import __version__
from harness_agent.host.attachments import AttachmentManager
from harness_agent.host.connection import (
    ProtocolConnection,
    ProtocolInteractionAdapter,
    RpcError,
)
from harness_agent.runtime.agent_engine import (
    AgentEngine,
    AgentEngineLease,
    AgentEngineCloseAdapter,
    AgentEnginePool,
    AgentEnginePoolCapacityError,
    AgentEngineResourceBundle,
)
from harness_agent.config.config import ConfigError, Za38Config, load_config
from harness_agent.policy.approval_mode import ApprovalMode
from harness_agent.policy.concurrency import AsyncRWLock
from harness_agent.policy.permission_rules import PermissionRule
from harness_agent.config.config_change_service import (
    ConfigChange,
    ConfigChangeError,
    ConfigChangeService,
    ManagedConfigPolicy,
)
from harness_agent.runtime.execution_binding import (
    ExecutionBindingError,
    ExecutionMode,
    ResolvedExecutionBinding,
    RunExecutionBinding,
    ThreadExecutionSelection,
    describe_thread_binding,
    resolve_execution_binding,
)
from harness_agent.protocol.generated import (
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
from harness_agent.protocol.runtime import (
    validate_interaction_result,
    validate_operation_params,
    validate_operation_result,
    validate_protocol_error_data,
)
from harness_agent.extensions.skills import SkillError, SkillRegistry
from harness_agent.runtime.agent_spec import ResolvedAgentSpec, resolve_builtin_main_agent_spec
from harness_agent.extensions.mcp import (
    McpConfigError,
    McpConfigSnapshot,
    McpConnectionManager,
    McpServerConfig,
    build_mcp_snapshot,
)
from harness_agent.runtime.run_context import RunContext
from harness_agent.runtime.agent_engine_profile import (
    AgentEngineProfile,
)
from harness_agent.context_lifecycle import ContextLifecycle, ContextRefreshError
from harness_agent.thread_persistence import ThreadPersistence, ThreadPersistenceError
from harness_agent.providers.harness_gateway import ProviderClientPool
from harness_agent.run_coordinator import (
    AgentEvent,
    ConnectionRef,
    RunCoordinator,
    RunError,
    RunExecution,
    RunPreparation,
    RunRuntime,
    RunState,
    RunRef,
    RequestedSkill,
    StartRun,
    _bounded_json,
)

logger = logging.getLogger(__name__)
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


@dataclass(slots=True)
class _AgentEngineArtifacts:
    """AgentEngine 图之外的共享 middleware 与执行上下文，由同一 AgentEngine 负责释放。"""

    execution_context: Any
    context_compactor: Any


class _AgentEngineSnapshotReservation:
    """Keep the Host snapshot boundary from resolution through pool acquire."""

    def __init__(self, lock: asyncio.Lock) -> None:
        """The caller must have acquired ``lock`` before constructing this token."""
        self._lock = lock
        self._released = False

    async def release(self) -> None:
        """Release the reservation exactly once on success, failure, or cancellation."""
        if self._released:
            return
        self._released = True
        self._lock.release()


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
    interaction_specs: dict[str, InteractionRequest] = field(default_factory=dict)
    closed: bool = False


class _ProtocolInteractionAdapter:
    """把类型化 Interaction 映射为 owner Connection 上的 JSON-RPC reverse request。"""

    def __init__(self, host: "AgentHost") -> None:
        """保存 Host 引用；RunCoordinator 不会看到该 transport 对象。"""
        self._host = host

    async def request(
        self,
        owner: ConnectionRef,
        run: RunRef,
        interaction: InteractionRequest,
    ) -> InteractionResult:
        """向 owner 请求审批/问答，超时、断开和缺少 capability 均安全降级。"""
        connection = self._host._connections.get(owner.connection_id)
        if (
            connection is None
            or connection.closed
            or interaction.type not in self._host._connection_handles(connection)
        ):
            logger.info("Interaction %s disabled by capability negotiation", interaction.request_id)
            return InteractionResult(self._default_value(interaction), expired=True)

        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        connection.pending_requests[interaction.request_id] = future
        connection.interaction_specs[interaction.request_id] = interaction
        method = (
            METHOD["INTERACTION_APPROVAL"]
            if interaction.type == "approval"
            else METHOD["INTERACTION_QUESTION"]
        )
        try:
            await self._host._send_to(
                connection,
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "id": interaction.request_id,
                    "params": {
                        "thread_id": run.thread_id,
                        "run_id": run.run_id,
                        "timeout_ms": INTERACTION_TIMEOUT_MS,
                        "payload": dict(interaction.payload),
                    },
                },
            )
            return InteractionResult(
                await asyncio.wait_for(future, timeout=INTERACTION_TIMEOUT_MS / 1000)
            )
        except (TimeoutError, RpcError, ValidationError) as exc:
            logger.warning("Interaction %s failed closed: %s", interaction.request_id, exc)
            return InteractionResult(self._default_value(interaction), expired=True)
        finally:
            connection.pending_requests.pop(interaction.request_id, None)
            connection.interaction_specs.pop(interaction.request_id, None)

    @staticmethod
    def _default_value(interaction: InteractionRequest) -> dict[str, object]:
        """返回 LangGraph 可接受的安全默认交互结果。"""
        return {"decision": "reject"} if interaction.type == "approval" else {"answers": {}}


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
        self._agent_engine_snapshot_lock = asyncio.Lock()
        self._run_event_tasks: set[asyncio.Task[None]] = set()
        self._workspace = (workspace or Path.cwd()).resolve()
        # ponytail: Host 固定绑定一个 workspace，先用一把锁覆盖跨 Profile 图；
        # worktree/sandbox 有稳定资源身份或吞吐证明不足时再按资源拆分。
        self._tool_concurrency_lock = AsyncRWLock()
        self._config_path = config_path or os.environ.get("HARNESS_AGENT_CONFIG_PATH")
        self._connection_role = connection_role
        self._config_home = config_home
        self._config: Za38Config | None = None
        self._config_change_policy = config_change_policy or ManagedConfigPolicy()
        self._config_change_service: ConfigChangeService | None = None
        self._startup_error: str | None = None
        self._skill_registry: SkillRegistry | None = None
        self._thread_persistence: ThreadPersistence | None = None
        self._agent_engine_pool: AgentEnginePool | None = None
        self._mcp_manager: McpConnectionManager | None = None
        self._mcp_snapshot: McpConfigSnapshot | None = None
        self._mcp_connect_task: asyncio.Task[None] | None = None
        self._mcp_state_lock = asyncio.Lock()
        # execution.py 会加载 deepagents；保持其惰性导入，避免拖慢 initialize
        # 前的 sidecar 启动路径。资源池在第一次默认构图时建立。
        self._workspace_execution_resources: Any | None = None
        # Profile key 只索引构建时解析出的同一个 spec，避免再次解释配置。
        self._resolved_agent_specs: dict[str, ResolvedAgentSpec] = {}
        self._agent_engine_artifacts: dict[str, _AgentEngineArtifacts] = {}
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
        self._run_coordinator = RunCoordinator(
            persistence_provider=self._run_persistence_provider,
            preparation_provider=self._prepare_run,
            runtime_provider=self._acquire_run_runtime,
            interaction_port=ProtocolInteractionAdapter(self),
            skill_registry_provider=self._require_skills,
            context_updates_provider=self._take_context_updates,
            project_dir=self._workspace,
        )
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
        self._attachments = AttachmentManager(
            create_connection=self.create_connection,
            dispatch_connection=self.dispatch_connection,
            close_connection=self.close_connection,
        )

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
        await self._run_coordinator.close()
        if self._run_event_tasks:
            await asyncio.gather(*tuple(self._run_event_tasks), return_exceptions=True)
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
        # AgentEngine 先释放自己的图和共享租约，Host owner 再关闭 MCP、
        # workspace/sandbox、Provider transport，最后才关闭 ThreadPersistence。
        await self._close_agent_engine_pool()
        if self._mcp_manager is not None:
            await self._mcp_manager.close_all()
            self._mcp_manager = None
        if self._workspace_execution_resources is not None:
            await self._workspace_execution_resources.aclose()
        await self._provider_client_pool.aclose()
        await self._close_thread_persistence()

    async def close_connection(self, connection: ProtocolConnection) -> None:
        """释放 attached Connection，并取消仅由它拥有的 active Runs。"""
        if connection.closed:
            return
        connection.closed = True
        connection.watched_threads.clear()
        self._fail_connection_requests(connection, RpcError(-32004, "Peer connection closed"))
        await self._run_coordinator.owner_disconnected(
            ConnectionRef(connection.connection_id)
        )
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
        except ThreadPersistenceError as exc:
            await self.send_error(
                request_id,
                -32020,
                "THREAD_STORE_UNAVAILABLE",
                {"code": str(exc)},
            )
        except AgentEnginePoolCapacityError as exc:
            await self.send_error(
                request_id,
                -32030,
                "RUNTIME_POOL_CAPACITY_EXHAUSTED",
                {"code": str(exc)},
            )
        except RunError as exc:
            rpc_error = self._run_rpc_error(exc)
            await self.send_error(request_id, rpc_error.code, rpc_error.message, rpc_error.data)
        except RpcError as exc:
            await self.send_error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - 最后的协议隔离层。
            logger.exception("Unhandled JSON-RPC handler error for %s", method)
            await self.send_error(request_id, -32603, f"{type(exc).__name__}: {exc}")
        else:
            if result is not None:
                validate_operation_result(method, result)
                await self.send_response(request_id, result)

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
                # MCP 连接不阻塞 initialize 响应；后台建立连接
                self._mcp_connect_task = asyncio.ensure_future(self._connect_mcp_servers())
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
                    "id": (await self._ensure_thread_persistence()).project_fingerprint if self._thread_persistence_enabled() else "echo",
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
        """根据配置建立 MCP 服务器连接并构建初始 snapshot；失败不阻止启动。"""
        async with self._mcp_state_lock:
            try:
                snapshot = self._config_changes().read_mcp_snapshot()
            except ConfigChangeError:
                # 配置在 initialize 阶段已失败时仍以空快照启动，不阻塞协议握手。
                snapshot = build_mcp_snapshot([], "missing")
            self._mcp_snapshot = snapshot
            self._mcp_manager = McpConnectionManager(snapshot)
            await self._mcp_manager.connect_all()

    async def _ensure_mcp_connected(self) -> None:
        """等待后台 MCP 连接任务完成（若仍在运行）。"""
        task = self._mcp_connect_task
        if task is not None and not task.done():
            await task

    async def _handle_run_start(self, params: dict[str, Any], request_id: str) -> None:
        """把协议输入转换成 StartRun，并让 Coordinator 先完成受理再启动事件流。"""
        parsed = RunStartParams.model_validate(params)
        message = parsed.message.strip()
        if not message:
            raise RpcError(-32602, "message must be non-empty")
        if (
            parsed.model_selection is not None
            and CAPABILITY["MODELS_SELECT"] not in self._connection_capabilities()
        ):
            raise RpcError(-32002, "MODELS_SELECT_CAPABILITY_REQUIRED")

        requested_skill = (
            RequestedSkill(parsed.requested_skill.id, parsed.requested_skill.args or "")
            if parsed.requested_skill is not None
            else None
        )
        command = StartRun(
            thread_id=parsed.thread_id,
            run_id=parsed.run_id,
            message=message,
            requested_skill=requested_skill,
            requested_primary_profile=(
                parsed.model_selection.primary_profile
                if parsed.model_selection is not None
                else None
            ),
            requested_approval_mode=parsed.approval_mode,
        )
        try:
            execution = await self._run_coordinator.start(
                command,
                ConnectionRef(self._current_connection().connection_id),
            )
        except (
            ConfigError,
            ContextRefreshError,
            ExecutionBindingError,
            ThreadPersistenceError,
        ) as exc:
            raise RpcError(-32004, str(exc)) from exc

        await self.send_response(
            request_id,
            {
                "thread_id": execution.ref.thread_id,
                "run_id": execution.ref.run_id,
                "accepted": execution.accepted,
            },
        )
        task = asyncio.create_task(
            self._fanout_run_execution(execution),
            name=f"harness-run-events-{execution.ref.run_id}",
        )
        self._run_event_tasks.add(task)
        task.add_done_callback(self._run_event_tasks.discard)

    async def _handle_run_cancel(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """通过 Coordinator 取消 Run，统一处理 owner 校验和刚受理即取消。"""
        parsed = RunCancelParams.model_validate(params)
        result = await self._run_coordinator.cancel(
            RunRef(parsed.thread_id, parsed.run_id),
            ConnectionRef(self._current_connection().connection_id),
        )
        return {"cancelled": result.cancelled, "run_id": result.run_id}

    async def _run_persistence_provider(self) -> ThreadPersistence | None:
        """只为默认生产 Run 打开 Thread 持久化；echo/注入 Agent 保持轻量路径。"""
        if not self._thread_persistence_enabled():
            return None
        return await self._ensure_thread_persistence()

    async def _prepare_run(
        self,
        command: StartRun,
        persistence: ThreadPersistence | None,
    ) -> RunPreparation:
        """在登记 Run 前解析模型、Profile、Skill 和 Context 快照。"""
        if persistence is None:
            return RunPreparation(skill_snapshot_id=self._require_skills().snapshot_id)
        reservation = await self._reserve_agent_engine_snapshot()
        try:
            self._load_config()
            if self._config is None:
                raise ConfigError(self._startup_error or "MODEL_CONFIGURATION_REQUIRED")
            resolved = await self._resolve_execution_binding(
                command.thread_id,
                self._config,
                requested_primary_profile=command.requested_primary_profile,
            )
            spec = await self._resolve_agent_engine_spec(
                command.thread_id,
                self._config,
                resolved,
            )
            profile = spec.runtime_profile
            context_snapshot = ContextLifecycle(
                self._workspace,
                home=self._config_home,
            ).prepare(
                thread_id=command.thread_id,
                spec=spec,
            )
            idle_duration_ms = await self._top_level_idle_duration_ms(
                persistence, command.thread_id
            )
            binding = resolved.bind_run(
                thread_id=command.thread_id,
                run_id=command.run_id,
                runtime_profile_id=profile.profile_key[:12],
                created_at_ms=int(time.time() * 1000),
                context_snapshot_id=context_snapshot.snapshot_id,
            )
            registry = self._require_skills()
            return RunPreparation(
                resolved_execution_binding=resolved,
                execution_binding=binding,
                agent_engine_profile=profile,
                skill_snapshot_id=registry.snapshot_id,
                context_snapshot=context_snapshot,
                idle_duration_ms=idle_duration_ms,
                snapshot_reservation=reservation,
            )
        except BaseException:
            await reservation.release()
            raise

    async def _top_level_idle_duration_ms(
        self, persistence: ThreadPersistence, thread_id: str
    ) -> int | None:
        """只为新的顶层 Run 读取可证明的 Thread 空闲时长。"""
        updated_at_ms = await persistence.load_thread_activity_ms(thread_id)
        now_ms = int(time.time() * 1000)
        if (
            not isinstance(updated_at_ms, int)
            or isinstance(updated_at_ms, bool)
            or updated_at_ms <= 0
            or now_ms < updated_at_ms
        ):
            return None
        return now_ms - updated_at_ms

    async def _acquire_run_runtime(self, run: RunState) -> RunRuntime:
        """把 AgentEngine/注入 Agent 的差异收敛成 Coordinator 可消费的 Runtime。"""
        if self._uses_default_agent_factory:
            agent = await self._acquire_default_agent_engine_for_run(run)
        else:
            agent = await self._ensure_agent()

        async def release() -> None:
            await self._release_run_agent_engine(run)

        if agent is None and not self._allow_echo:
            raise ConfigError(self._startup_error or "Agent is not configured")
        persistence = run.persistence
        if (
            persistence is not None
            and agent is not None
            and callable(getattr(agent, "aupdate_state", None))
        ):
            from harness_agent.context_projection import ContextProjector

            try:
                await ContextProjector(persistence).sync_cache(
                    agent,
                    run.thread_id,
                    exclude_record_id=f"run:{run.run_id}:user",
                )
            except BaseException:
                # Runtime 尚未返回 Coordinator，失败路径必须在此释放
                # 已取得的 AgentEngine/run lease，避免投影损坏变成资源泄漏。
                await self._release_run_agent_engine(run)
                raise
        graph_config = (
            persistence.graph_config
            if persistence is not None
            else lambda thread_id: {"configurable": {"thread_id": thread_id}}
        )
        return RunRuntime(
            agent=agent,
            run_context=run.run_context,
            graph_config=graph_config,
            release=release,
        )

    def _take_context_updates(self, thread_id: str) -> list[Any]:
        """消费指定 Thread 的中间件更新，避免跨 Run 重复广播。"""
        return self._context_updates.pop(thread_id, [])

    async def _fanout_run_execution(self, execution: RunExecution) -> None:
        """把领域事件广播给 owner 和已 watch 该 Thread 的连接。"""
        async for event in execution.events:
            await self._fanout_agent_event(execution.owner, event)

    async def _fanout_agent_event(self, owner: ConnectionRef, event: AgentEvent) -> None:
        """将不携带 transport 的 AgentEvent 映射成现有 event notification。"""
        message = {
            "jsonrpc": "2.0",
            "method": METHOD["EVENT"],
            "params": event.record(),
        }
        targets = [
            connection
            for connection in self._connections.values()
            if not connection.closed
            and (
                connection.connection_id == owner.connection_id
                or event.thread_id in self._connection_watches(connection)
            )
        ]
        results = await asyncio.gather(
            *(self._send_to(connection, message) for connection in targets),
            return_exceptions=True,
        )
        for connection, result in zip(targets, results, strict=True):
            if isinstance(result, Exception) and connection is not self._owner_connection:
                asyncio.create_task(self.close_connection(connection))

    async def _handle_context_compact(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """在空闲 thread 上按用户命令强制生成结构化摘要，不把能力暴露给模型。"""
        self._require_context_capability()
        parsed = ContextCompactParams.model_validate(params)
        try:
            async with self._run_coordinator.idle_thread(parsed.thread_id):
                return await self._compact_idle_thread(parsed.thread_id)
        except RunError as exc:
            if exc.code == "THREAD_BUSY":
                raise RpcError(-32000, "CONTEXT_COMPACTION_RUN_ACTIVE") from exc
            raise

    async def _compact_idle_thread(self, thread_id: str) -> dict[str, object]:
        """在 Coordinator 已锁定为空闲的窗口内完成压缩。"""
        from harness_agent.context_projection import ContextProjector

        persistence = await self._ensure_thread_persistence()
        projection = await ContextProjector(persistence).project(thread_id)
        messages = list(projection.messages)
        if not self._uses_default_agent_factory:
            agent = await self._ensure_agent()
            middleware = getattr(self, "_context_compactor", None)
            if agent is None or middleware is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            return await self._compact_with_agent_engine(
                agent=agent,
                middleware=middleware,
                thread_id=thread_id,
                messages=messages,
                persistence=persistence,
            )

        lease, engine = await self._acquire_default_agent_engine(thread_id)
        try:
            if lease is None or engine is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            artifacts = self._agent_engine_artifacts.get(engine.profile_key)
            if artifacts is None or engine.graph is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            return await self._compact_with_agent_engine(
                agent=engine.graph,
                middleware=artifacts.context_compactor,
                thread_id=thread_id,
                messages=messages,
                persistence=persistence,
            )
        finally:
            await self._release_agent_engine_lease(lease)

    async def _handle_config_show(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回当前脱敏配置与可重建 AgentEnginePool 的本地诊断摘要。"""
        if _params:
            raise RpcError(-32602, "config.show does not accept params")
        self._load_config()
        if self._config is None:
            raise RpcError(-32010, self._startup_error or "Configuration is unavailable")
        summary = self._config.redacted()
        summary["runtime_pool_diagnostics"] = await self._agent_engine_pool_diagnostics()
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
        # AgentEngine 保持原快照；其他 Settings 仍明确要求重启 sidecar。
        if result["applies_to"] == ["new-thread"]:
            self._load_config()
        return result

    async def _handle_mcp_status(self, _params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回所有已配置 MCP 服务器的运行时连接状态和工具列表。"""
        if _params:
            raise RpcError(-32602, "mcp.status does not accept params")
        # 后台连接任务可能尚未完成，先等待它
        await self._ensure_mcp_connected()
        async with self._mcp_state_lock:
            manager = self._mcp_manager
            if manager is None:
                return {"servers": [], "total_tools": 0}
            statuses = manager.get_server_statuses()
        total_tools = sum(len(s.get("tool_names", [])) for s in statuses)
        return {"servers": statuses, "total_tools": total_tools}

    async def _handle_mcp_add(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """通过 ConfigChangeService 添加 MCP 服务器并尝试热连接。"""
        parsed = McpAddParams.model_validate(params)
        try:
            mcp_config = McpServerConfig.from_mapping(parsed.model_dump())
        except McpConfigError as exc:
            raise RpcError(-32602, str(exc), {"code": exc.code, "field": exc.field}) from exc

        await self._ensure_mcp_connected()
        async with self._agent_engine_snapshot_lock:
            async with self._mcp_state_lock:
                current = self._mcp_snapshot or self._config_changes().read_mcp_snapshot()
            try:
                snapshot = self._config_changes().add_mcp_server(
                    mcp_config,
                    expected_revision=current.revision,
                )
            except ConfigChangeError as exc:
                raise RpcError(-32602, str(exc), exc.redacted_data()) from exc

            async with self._mcp_state_lock:
                self._mcp_snapshot = snapshot
                if self._mcp_manager is None:
                    self._mcp_manager = McpConnectionManager(snapshot)
                statuses = await self._mcp_manager.apply_snapshot(snapshot)
                status = next((item for item in statuses if item.get("name") == mcp_config.name), {})
            await self._invalidate_profiles_for_snapshot(snapshot, reason="mcp_snapshot_changed")

        return {
            "added": True,
            "connected": status.get("status") == "connected",
            "tool_names": status.get("tool_names", []),
            "error": status.get("error"),
        }

    async def _handle_mcp_remove(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """通过 ConfigChangeService 删除 MCP 服务器并热断开。"""
        parsed = McpRemoveParams.model_validate(params)
        name = parsed.name

        await self._ensure_mcp_connected()
        async with self._agent_engine_snapshot_lock:
            async with self._mcp_state_lock:
                current = self._mcp_snapshot or self._config_changes().read_mcp_snapshot()
            try:
                snapshot = self._config_changes().remove_mcp_server(
                    name,
                    expected_revision=current.revision,
                )
            except ConfigChangeError as exc:
                raise RpcError(-32602, str(exc), exc.redacted_data()) from exc

            async with self._mcp_state_lock:
                self._mcp_snapshot = snapshot
                if self._mcp_manager is not None:
                    await self._mcp_manager.apply_snapshot(snapshot)
            await self._invalidate_profiles_for_snapshot(snapshot, reason="mcp_snapshot_changed")

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
        ceiling = frozenset(
            capability
            for capability in self._connection_capabilities(connection)
            if capability != CAPABILITY["HOST_ATTACH"]
        )
        return await self._attachments.create(parsed.origin, ceiling)

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
            persisted = await (await self._ensure_thread_persistence()).load_run_state(
                parsed.thread_id
            )
            if CAPABILITY["MODELS_SELECT"] in self._connection_capabilities():
                latest = persisted.latest_run
                if latest is not None:
                    result["thread_selection"] = latest.requested_selection.to_record()
                    result["last_run_binding"] = latest.protocol_primary_model()
            # 未协商 models.select 时只返回不可变绑定摘要。
            result["thread_binding"] = describe_thread_binding(persisted).to_record()
        return result

    async def _handle_threads_list(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """返回当前 project 内最近活跃的 thread；thread_id 仅供客户端内部打开。"""
        self._require_threads_capability()
        parsed = ThreadsListParams.model_validate(params)
        threads = await (await self._ensure_thread_persistence()).list_threads(parsed.limit)
        return {"threads": [_thread_summary_payload(thread) for thread in threads]}

    async def _handle_threads_open(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """读取当前 project 的一个 thread Transcript 历史，不以 checkpoint 兜底。"""
        self._require_threads_capability()
        parsed = ThreadsOpenParams.model_validate(params)
        try:
            opened = await (await self._ensure_thread_persistence()).open_thread(parsed.thread_id)
        except ThreadPersistenceError as exc:
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
        async with self._run_coordinator.idle_thread(parsed.thread_id):
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

    @staticmethod
    def _run_rpc_error(error: RunError) -> RpcError:
        """把 Run module 的稳定错误码适配成现有 JSON-RPC 错误。"""
        rpc_codes = {
            "THREAD_BUSY": -32000,
            "RUN_NOT_FOUND": -32001,
            "RUN_NOT_OWNER": -32005,
            "RUN_ID_CONFLICT": -32006,
            "HOST_CLOSED": -32004,
            "MODEL_CONFIGURATION_REQUIRED": -32010,
            "RUN_MODEL_BINDING_UNAVAILABLE": -32010,
        }
        data: dict[str, object] = {
            "code": error.code,
            "retryable": error.retryable,
        }
        if error.details is not None:
            data["details"] = error.details
        return RpcError(rpc_codes.get(error.code, -32004), error.code, data)

    async def _ensure_agent(self) -> Any | None:
        """按需构建外部注入的 Agent；默认图必须经 AgentEnginePool 取得。"""
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
        # 外部注入工厂保持既有单图测试/嵌入契约；生产默认路径由 AgentEnginePool
        # 提供 per-Profile single-flight，不应再写入 ``self.agent``。
        async with self._agent_build_lock:
            if self.agent is not None:
                return self.agent
            created = self._agent_factory(self._config, self._workspace)
            self.agent = await created if inspect.isawaitable(created) else created
            return self.agent

    async def _acquire_default_agent_engine_for_run(self, run: RunState) -> Any | None:
        """为一个生产 run 获取共享 AgentEngine，并将 thread 私有状态写入 RunContext。"""
        reservation = run.preparation.snapshot_reservation
        try:
            lease, engine = await self._acquire_default_agent_engine(
                run.thread_id,
                run.resolved_execution_binding,
                profile=run.resolved_agent_engine_profile,
                snapshot_reservation=reservation,
            )
        finally:
            if reservation is not None:
                await reservation.release()
        if engine is None:
            return None
        try:
            artifacts = self._agent_engine_artifacts.get(engine.profile_key)
            spec = self._resolved_agent_specs.get(engine.profile_key)
            if artifacts is None or spec is None or engine.graph is None:
                raise RuntimeError("RUNTIME_ARTIFACTS_UNAVAILABLE")
            run.run_context = await self._create_run_context(
                run,
                profile=engine.profile,
                spec=spec,
                execution_context=artifacts.execution_context,
            )
            run.agent_engine_run_lease = await lease.run()
            run.agent_engine_lease = lease
            run.agent_engine_profile_key = engine.profile_key
            return engine.graph
        except Exception:
            await self._release_agent_engine_lease(lease)
            raise

    async def _acquire_default_agent_engine(
        self,
        thread_id: str,
        resolved_binding: ResolvedExecutionBinding | None = None,
        *,
        profile: AgentEngineProfile | None = None,
        snapshot_reservation: _AgentEngineSnapshotReservation | None = None,
    ) -> tuple[AgentEngineLease | None, AgentEngine | None]:
        """为 Run 或手动压缩取得按实际模型计算的共享 AgentEngine。"""
        if self._allow_echo:
            return None, None
        if snapshot_reservation is None:
            reservation = await self._reserve_agent_engine_snapshot()
            try:
                return await self._acquire_default_agent_engine_locked(
                    thread_id,
                    resolved_binding,
                    profile=profile,
                )
            finally:
                await reservation.release()
        return await self._acquire_default_agent_engine_locked(
            thread_id,
            resolved_binding,
            profile=profile,
        )

    async def _reserve_agent_engine_snapshot(self) -> _AgentEngineSnapshotReservation:
        """Reserve the Host boundary shared by snapshot producers and engine acquires."""
        await self._agent_engine_snapshot_lock.acquire()
        return _AgentEngineSnapshotReservation(self._agent_engine_snapshot_lock)

    async def _acquire_default_agent_engine_locked(
        self,
        thread_id: str,
        resolved_binding: ResolvedExecutionBinding | None = None,
        *,
        profile: AgentEngineProfile | None = None,
    ) -> tuple[AgentEngineLease | None, AgentEngine | None]:
        """Resolve and acquire while the caller owns the Host snapshot reservation."""
        self._load_config()
        config = self._config
        if config is None or config.model is None:
            return None, None
        if profile is None:
            resolved_binding = resolved_binding or await self._resolve_execution_binding(
                thread_id,
                config,
            )
            profile = await self._resolve_agent_engine_profile(
                thread_id,
                config,
                resolved_binding,
            )
        pool = self._ensure_agent_engine_pool(config)
        lease = await pool.acquire(profile)
        return lease, lease.engine

    async def _resolve_agent_engine_spec(
        self,
        thread_id: str,
        config: Za38Config,
        resolved_binding: ResolvedExecutionBinding,
        *,
        approval_mode: ApprovalMode | None = None,
    ) -> ResolvedAgentSpec:
        """截取一次角色解析快照，Profile 和 builder 都从它派生。

        ``approval_mode`` 为 Run 级覆盖：TUI 切换审批模式时按模式派生独立
        Profile，引擎池据此缓存各自强制策略的图，配置值保持不动。
        """
        persistence = await self._ensure_thread_persistence()
        registry = self._require_skills()
        await self._ensure_mcp_connected()
        async with self._mcp_state_lock:
            mcp_snapshot = self._mcp_snapshot or build_mcp_snapshot([], "missing")
            mcp_tools = tuple(self._mcp_manager.get_tools()) if self._mcp_manager else ()
        execution = (
            replace(config.execution, approval_mode=approval_mode)
            if approval_mode is not None
            else config.execution
        )
        spec = resolve_builtin_main_agent_spec(
            project_fingerprint=persistence.project_fingerprint,
            workspace=self._workspace,
            binding=resolved_binding,
            execution=execution,
            skill_registry=registry,
            mcp_snapshot=mcp_snapshot,
            mcp_tools=mcp_tools,
            interactive="question" in self._connection_handles(),
            pinned=config.agent_engine_pool.pin_default_profile,
        )
        profile = spec.runtime_profile
        await persistence.persist_agent_engine_profile(profile)
        # 同一 Key 保留第一次解析出的对象，保证 Pool builder 与 RunContext
        # 取回的是同一个快照，而不是后续请求重新拼出的近似对象。
        return self._resolved_agent_specs.setdefault(profile.profile_key, spec)

    async def _resolve_agent_engine_profile(
        self,
        thread_id: str,
        config: Za38Config,
        resolved_binding: ResolvedExecutionBinding,
    ) -> AgentEngineProfile:
        """兼容现有调用方，只返回由 ResolvedAgentSpec 生成的 Profile。"""
        return (await self._resolve_agent_engine_spec(thread_id, config, resolved_binding)).runtime_profile

    async def _invalidate_profiles_for_snapshot(
        self,
        snapshot: McpConfigSnapshot,
        *,
        reason: str,
    ) -> None:
        """Invalidate old MCP profiles and reap idle resources within the update boundary."""
        pool = self._agent_engine_pool
        if pool is not None:
            await pool.invalidate(
                lambda profile: profile.mcp_config_fingerprint != snapshot.digest,
                reason=reason,
            )
        manager = self._mcp_manager
        if manager is not None:
            reap = getattr(manager, "reap", None)
            if callable(reap):
                result = reap()
                if inspect.isawaitable(result):
                    await result

    async def _resolve_execution_binding(
        self,
        thread_id: str,
        config: Za38Config,
        *,
        requested_primary_profile: str | None = None,
    ) -> ResolvedExecutionBinding:
        """读取 Thread 状态并通过 execution_binding module 解析根模型。"""
        persistence = await self._ensure_thread_persistence()
        requested = (
            ThreadExecutionSelection(requested_primary_profile)
            if requested_primary_profile is not None
            else None
        )
        persisted = await persistence.load_run_state(thread_id)
        return resolve_execution_binding(config, requested, persisted)

    def _ensure_agent_engine_pool(self, config: Za38Config) -> AgentEnginePool:
        """延迟创建进程内唯一 Pool；容量策略在 Sidecar 生命周期内保持稳定。"""
        if self._agent_engine_pool is None:
            settings = config.agent_engine_pool
            self._agent_engine_pool = AgentEnginePool(
                self._build_default_agent_engine,
                max_profiles=settings.max_profiles,
                idle_ttl_seconds=settings.idle_ttl_seconds,
                close_timeout_seconds=settings.close_timeout_seconds,
            )
        return self._agent_engine_pool

    def _ensure_workspace_execution_resources(self) -> Any:
        """在首次默认构图时加载并创建 workspace 资源 owner。"""
        if self._workspace_execution_resources is None:
            from harness_agent.execution import WorkspaceExecutionResourcePool

            self._workspace_execution_resources = WorkspaceExecutionResourcePool()
        return self._workspace_execution_resources

    async def _build_default_agent_engine(self, profile: AgentEngineProfile) -> AgentEngine:
        """按 Profile key 取回同一 ResolvedAgentSpec，再构建共享图。"""
        spec = self._resolved_agent_specs.get(profile.profile_key)
        if spec is None:
            raise RuntimeError("RUNTIME_RESOLVED_AGENT_SPEC_MISSING")
        if profile.profile_key != spec.runtime_profile.profile_key:
            raise RuntimeError("RUNTIME_PROFILE_SPEC_MISMATCH")
        if profile.mcp_config_fingerprint != spec.mcp_snapshot.digest:
            raise RuntimeError("RUNTIME_MCP_SNAPSHOT_MISMATCH")
        from harness_agent.agent import create_harness_agent
        from harness_agent.context_window import ContextWindowMiddleware
        from harness_agent.providers.harness_gateway import create_openai_compatible_model

        persistence = await self._ensure_thread_persistence()
        checkpointer = persistence.checkpointer
        model_settings = spec.model_settings
        provider_lease = None
        workspace_lease = None
        mcp_lease = None
        try:
            provider_lease = await self._provider_client_pool.acquire(model_settings)
            workspace_resources = self._ensure_workspace_execution_resources()
            workspace_lease = await workspace_resources.acquire(
                profile.sandbox_config_fingerprint,
                spec.execution,
                spec.workspace,
            )
            if self._mcp_manager is None:
                raise RuntimeError("RUNTIME_MCP_MANAGER_REQUIRED")
            mcp_lease = await self._mcp_manager.acquire(spec.mcp_snapshot)
            mcp_tools = list(mcp_lease.value.tools)
            execution_context = workspace_lease.value
            model = create_openai_compatible_model(
                model_settings,
                async_client=provider_lease.value,
            )
            context_compactor = ContextWindowMiddleware(
                model,
                context_window_tokens=model_settings.context_window_tokens,
                thread_persistence=persistence,
                updates=self._context_updates,
            )
            graph = create_harness_agent(
                model,
                tools=mcp_tools or None,
                mcp_server_info=True if mcp_tools else None,
                cwd=str(spec.workspace),
                # 无头客户端不协商 question 能力时不注册 ask_user；审批仍由
                # `_ProtocolInteractionAdapter` 在缺少 approval 能力时 fail closed。
                interactive=spec.interactive,
                enable_ask_user=spec.enable_ask_user,
                enable_memory=spec.enable_memory,
                enable_skills=spec.enable_skills,
                approval_mode=spec.effective_policy.approval_mode or spec.execution.approval_mode,
                execution_context=execution_context,
                skill_registry=spec.skill_registry,
                checkpointer=checkpointer,
                thread_persistence=persistence,
                context_updates=self._context_updates,
                context_middleware=context_compactor,
                context_window_tokens=model_settings.context_window_tokens,
                shared_engine=True,
                concurrency_lock=self._tool_concurrency_lock,
            )
            self._agent_engine_artifacts[profile.profile_key] = _AgentEngineArtifacts(
                execution_context=execution_context,
                context_compactor=context_compactor,
            )
            resources = AgentEngineResourceBundle.from_sequences(
                flushers=(
                    AgentEngineCloseAdapter(
                        "server-runtime-artifacts",
                        lambda: self._drop_agent_engine_artifacts(profile.profile_key),
                    ),
                ),
                shared_leases=tuple(
                    lease
                    for lease in (provider_lease, workspace_lease, mcp_lease)
                    if lease is not None
                ),
            )
            return AgentEngine(
                profile=profile,
                graph=graph,
                resources=resources,
                pinned=spec.pinned,
            )
        except Exception:
            for lease in (mcp_lease, workspace_lease, provider_lease):
                if lease is not None:
                    await lease.release()
            raise

    async def _create_run_context(
        self,
        run: RunState,
        *,
        profile: AgentEngineProfile,
        spec: ResolvedAgentSpec,
        execution_context: Any,
    ) -> RunContext:
        """把准备阶段已生成的 snapshot 注入共享图，Run 内不重新读取来源。"""
        snapshot = run.preparation.context_snapshot
        if snapshot is None:
            raise RuntimeError("RUN_CONTEXT_SNAPSHOT_UNAVAILABLE")
        from harness_agent.context_pressure import ModelCallLifecycle

        return RunContext(
            thread_id=run.thread_id,
            run_id=run.run_id,
            context_snapshot=snapshot,
            model_call_lifecycle=ModelCallLifecycle(
                next_call_type=(
                    "subagent"
                    if run.root_execution_ref.parent_execution_id is not None
                    else "top_level_initial"
                ),
                idle_duration_ms=run.preparation.idle_duration_ms,
            ),
            approval_mode=spec.effective_policy.approval_mode or spec.execution.approval_mode,
            profile_key=profile.profile_key,
            execution_id=run.root_execution_ref.execution_id,
            parent_execution_id=run.root_execution_ref.parent_execution_id,
            agent_id=spec.agent_id,
            execution_mode=ExecutionMode.MANAGED,
            cancellation_token=run.cancellation_token,
        )

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

    async def _compact_with_agent_engine(
        self,
        *,
        agent: Any,
        middleware: Any,
        thread_id: str,
        messages: list[Any],
        persistence: ThreadPersistence,
    ) -> dict[str, object]:
        """使用已租用 AgentEngine 的 compactor 提交投影并刷新缓存。"""
        compacted, update, rewritten = await middleware.compact_now(thread_id, messages)
        # `compact_now` 复用运行期状态缓冲；当前请求直接返回结果，因此必须消费，
        # 防止下一次 Agent run 重复发出过期的 context.updated 事件。
        middleware.consume_updates(thread_id)
        if rewritten:
            from harness_agent.context_projection import ContextProjector

            projected = await ContextProjector(persistence).sync_cache(
                agent, thread_id
            )
            if tuple(compacted) != projected.messages:
                raise RuntimeError("COMPRESSION_PROJECTION_COMMIT_MISMATCH")
            await persistence.complete_run(thread_id)
        return {"compacted": rewritten, "context": update.payload()}

    async def _release_run_agent_engine(self, run: RunState) -> None:
        """在 run 的所有终态释放 AgentEngine lease，并触发排空与空闲 TTL 检查。"""
        run_lease, run.agent_engine_run_lease = run.agent_engine_run_lease, None
        lease, run.agent_engine_lease = run.agent_engine_lease, None
        profile_key, run.agent_engine_profile_key = run.agent_engine_profile_key, None
        if run_lease is not None:
            await run_lease.release()
        if lease is None:
            return
        await self._release_agent_engine_lease(lease, profile_key=profile_key)

    async def _release_agent_engine_lease(
        self,
        lease: AgentEngineLease | None,
        *,
        profile_key: str | None = None,
    ) -> None:
        """释放非 run 或 run lease；DRAINING AgentEngine 会在最后一个引用退出后关闭。"""
        if lease is None:
            return
        key = profile_key or lease.engine.profile_key
        await lease.release()
        pool = self._agent_engine_pool
        if pool is not None:
            await pool.finalize_draining(key)
            await pool.sweep()
        if self._mcp_manager is not None:
            await self._mcp_manager.reap()
        if self._workspace_execution_resources is not None:
            await self._workspace_execution_resources.reap()

    async def _drop_agent_engine_artifacts(self, profile_key: str) -> None:
        """清除已关闭 AgentEngine 的 middleware/执行上下文引用，避免 Sidecar 持有旧资源。"""
        self._agent_engine_artifacts.pop(profile_key, None)
        self._resolved_agent_specs.pop(profile_key, None)

    async def _close_agent_engine_pool(self) -> None:
        """在关闭 SQLite 前停止 AgentEnginePool，保证 middleware 不再访问已关闭的 Persistence。"""
        pool, self._agent_engine_pool = self._agent_engine_pool, None
        if pool is not None:
            reports = await pool.aclose()
            failures = [failure for report in reports for failure in report.failures]
            if failures:
                logger.warning("AgentEnginePool closed with %s resource failures", len(failures))
        self._agent_engine_artifacts.clear()
        self._resolved_agent_specs.clear()

    async def _agent_engine_pool_diagnostics(self) -> dict[str, object]:
        """返回 config.show 的运行池摘要；未初始化/已关闭时不保留旧 AgentEngine 引用。"""
        pool = self._agent_engine_pool
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

    async def _ensure_thread_persistence(self) -> ThreadPersistence:
        """延迟打开用户级数据库；配置读取不应因为存储创建而被阻塞。"""
        if self._thread_persistence is None:
            if not self._thread_persistence_enabled():
                raise ThreadPersistenceError("THREADS_UNAVAILABLE_IN_ECHO_MODE")
            self._thread_persistence = await ThreadPersistence.open(
                project=self._workspace,
                home=self._config_home,
            )
        return self._thread_persistence

    async def _close_thread_persistence(self) -> None:
        """在 sidecar 生命周期末尾关闭 SQLite 连接和 WAL 句柄。"""
        persistence, self._thread_persistence = self._thread_persistence, None
        if persistence is not None:
            await persistence.close()

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
    """把 Transcript 消息限制为 TUI 可回放的 project/thread/message 数据。"""
    payload: dict[str, object] = {"kind": message.kind, "content": message.content}
    if message.tool_name is not None:
        payload["tool_name"] = message.tool_name
    return payload
