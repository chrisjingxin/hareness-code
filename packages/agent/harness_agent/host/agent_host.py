"""Harness v3 Agent Host：承载项目级运行资源与协议连接。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from jsonschema.exceptions import ValidationError

from harness_agent import __version__
from harness_agent.diagnostic_log.runtime import DiagnosticLog, ensure_log, safe_context_value
from harness_agent.host.attachments import AttachmentManager
from harness_agent.host.control_lease import (
    ActivityFacts,
    ControlLease,
    ControlLeaseError,
)
from harness_agent.host.connection import (
    ProtocolConnection,
    ProtocolInteractionAdapter,
    RpcError,
    interaction_method,
)
from harness_agent.runtime.agent_engine import (
    AgentEngine,
    AgentEngineLease,
    AgentEngineCloseAdapter,
    AgentEnginePool,
    AgentEnginePoolCapacityError,
    AgentEngineResourceBundle,
)
from harness_agent.config.config import ConfigError, DiagnosticsSettings, Za38Config, load_config
from harness_agent.config.settings import (
    CredentialBackend,
    SettingBinding,
    SettingsError,
    SettingsResolver,
    SettingsSnapshot,
    SettingsStore,
    create_platform_credential_backend,
)
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
    AgentExecutionBinding,
    ExecutionBindingError,
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
    ResolvedExecutionBinding,
    RunExecutionBinding,
    ThreadExecutionSelection,
    describe_thread_binding,
    resolve_execution_binding,
)
from harness_agent.runtime.agent_catalog import (
    AgentCatalog,
    AgentCatalogError,
    DelegationPolicy,
    PluginAgentSource,
)
from harness_agent.protocol.generated import (
    MAX_FRAME_BYTES,
    MAX_TOOL_PAYLOAD_BYTES,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    CAPABILITY,
    CONTROLLED_OPERATIONS,
    ERROR_CODES,
    EVENT_TYPE,
    METHOD,
    OPERATION_CAPABILITIES,
    OPERATION_MIN_MINOR,
    SERVER_CAPABILITIES,
    ApprovalResponse,
    AgentsInspectParams,
    CommandsBindParams,
    ComposeAbandonParams,
    ComposeInspectParams,
    ContextCompactParams,
    ConfigCommitParams,
    ConfigDetailsParams,
    ConfigPreviewParams,
    HostAttachmentRevokeParams,
    HostAttachmentCreateParams,
    InitializeParams,
    ModelsListParams,
    McpAddParams,
    McpRemoveParams,
    PluginsInspectParams,
    PluginsInstallParams,
    PluginsListParams,
    PluginsRemoveParams,
    PluginsSetEnabledParams,
    PluginsValidateParams,
    QuestionResponse,
    RunCancelParams,
    RunStartParams,
    SettingsListParams,
    SettingsRemoveParams,
    SettingsSetParams,
    TeamsCancelParams,
    TeamsGenerateParams,
    TeamsInspectParams,
    TeamsRunParams,
    ThreadsListParams,
    ThreadsOpenParams,
    ThreadsSideQuestionParams,
)
from harness_agent.threads.thread_persistence import workspace_fingerprint
from harness_agent.protocol.runtime import (
    validate_interaction_result,
    validate_operation_params,
    validate_operation_result,
    validate_protocol_error_data,
)
from harness_agent.extensions.plugin_skills import (
    LoadedSkill,
    PluginSkillSource,
    SkillError,
    SkillRegistry,
)
from harness_agent.plugins import (
    PluginError,
    PluginManager,
    PluginRuntimeCatalog,
    PluginRuntimeManager,
)
from harness_agent.plugins.manager import PluginReauthorizationSummary
from harness_agent.plugins.model import ExtensionCatalogSnapshot, catalog_snapshot_id
from harness_agent.plugins.runtime import HookRuntimeFailure
from harness_agent.runtime.agent_spec import (
    ResolvedAgentSpec,
    resolve_builtin_main_agent_spec,
    resolve_plugin_agent_spec,
    skill_catalog_fingerprint,
)
from harness_agent.extensions.skills import SkillCatalogManager, SkillError as CatalogSkillError
from harness_agent.extensions.mcp import (
    McpConfigError,
    McpConfigSnapshot,
    McpConnectionManager,
    McpServerConfig,
    build_mcp_snapshot,
)

from harness_agent.runtime.run_context import RunCancellationToken, RunContext
from harness_agent.runtime.agent_engine_profile import AgentEngineProfile
from harness_agent.runtime.resource_ownership import (
    ResourceScope,
    SharedResourceLease,
    SharedResourceOwner,
)
from harness_agent.threads.context_lifecycle import (
    ContextBlock,
    ContextLifecycle,
    ContextRefreshError,
)
from harness_agent.threads.deferred_store import ThreadDeferredToolStore
from harness_agent.threads.snapshots import ThreadSnapshotStore
from harness_agent.threads.thread_persistence import ThreadPersistence, ThreadPersistenceError
from harness_agent.tools.file_tool_metrics import FileToolMetrics
from harness_agent.compose.stage_agents import ManagedStageAgentPort
from harness_agent.compose.document_store import ComposeDocumentStore
from harness_agent.compose.models import ThreadMode
from harness_agent.compose.session import (
    ComposeSession,
    ComposeSessionError,
    ComposeSessionPorts,
)
from harness_agent.extensions.providers.harness_gateway import ProviderClientPool
from harness_agent.host.run_coordinator import (
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
)
from harness_agent.host.run_execution import _bounded_json
from harness_agent.runtime.team_coordinator import (
    TeamCoordinator,
    TeamDefinition,
    TeamError,
    TeamRun,
    TeamRunStatus,
    TeamTaskState,
    generate_fanout_team,
)

if TYPE_CHECKING:
    from harness_agent.threads.runtime_state import RuntimeExecutionPolicy

logger = logging.getLogger(__name__)


class PluginReauthorizationRequired(ConfigError):
    """明确请求 stale Plugin 能力时返回的可操作门禁错误。"""

    def __init__(self, summary: PluginReauthorizationSummary) -> None:
        """保存只含 Plugin 身份与 capability fingerprint 的脱敏事实。"""
        self.summary = summary
        super().__init__(_plugin_reauthorization_message(summary))


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
    "THREAD_MODE_LOCKED",
    "COMPOSE_WORK_ITEM_NOT_FOUND",
    "COMPOSE_WORK_ITEM_THREAD_MISMATCH",
    "COMPOSE_WORK_ITEM_REVISION_CONFLICT",
    *ERROR_CODES,
}
CONTROL_RPC_CODES = {
    name: entry["jsonrpc_code"] for name, entry in ERROR_CODES.items()
}
ATTACHMENT_CAPABILITY_ALLOWLIST = frozenset(
    {
        CAPABILITY["HOST_CONTROL"],
        CAPABILITY["RUN_CANCEL"],
        CAPABILITY["CONFIG_READ"],
        CAPABILITY["CONFIG_WRITE"],
        CAPABILITY["THREADS_READ"],
        CAPABILITY["CONTEXT_MANAGE"],
        CAPABILITY["SKILLS_READ"],
        CAPABILITY["SKILLS_MANAGE"],
        CAPABILITY["MCP_READ"],
        CAPABILITY["MCP_MANAGE"],
        CAPABILITY["MODELS_READ"],
        CAPABILITY["MODELS_SELECT"],
    }
)


def _parse_raw_command_invocation(raw_invocation: str) -> tuple[str, str] | None:
    """提取 Slash 的名称和规范化参数；不改写原始调用字符串。"""
    value = raw_invocation.lstrip()
    if not value.startswith("/") or value.startswith("//"):
        return None
    match = re.fullmatch(r"/([^\s/]+)([\s\S]*)", value)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def _validate_command_invocation(
    command: StartRun,
    requested: RequestedSkill,
    record: Any,
    *,
    resolved_command_name: str | None,
) -> None:
    """校验 CLI 已提交的 exact binding，不在 Host 重算 UI 命令表。"""
    if not requested.raw_invocation or not requested.command_name:
        raise SkillError("COMMAND_INVOCATION_CONTRACT_REQUIRED")
    if not resolved_command_name:
        raise SkillError("COMMAND_INVOCATION_BINDING_REQUIRED")
    if requested.raw_invocation != command.message:
        raise SkillError("COMMAND_INVOCATION_RAW_MISMATCH")
    parsed = _parse_raw_command_invocation(requested.raw_invocation)
    if parsed is None:
        raise SkillError("COMMAND_INVOCATION_INVALID")
    raw_name, raw_args = parsed
    if (
        getattr(record, "skill_id", None) != requested.skill_id
        or getattr(record, "kind", None) != "command"
        or requested.command_name.casefold() != raw_name.casefold()
        or requested.command_name.casefold() != resolved_command_name.casefold()
        or raw_name.casefold() != resolved_command_name.casefold()
    ):
        raise SkillError("COMMAND_INVOCATION_IDENTITY_MISMATCH")
    if requested.args.strip() != raw_args:
        raise SkillError("COMMAND_INVOCATION_ARGS_MISMATCH")


@dataclass(slots=True)
class _AgentEngineArtifacts:
    """AgentEngine 图之外的共享 middleware 与执行上下文，由同一 AgentEngine 负责释放。"""

    execution_context: Any
    context_compactor: Any
    mcp_lease: SharedResourceLease[McpConnectionManager] | None = None


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
        settings_backend: CredentialBackend | None = None,
        settings_workspace_roots: Sequence[Path | str] | None = None,
        settings_policy_version: str = "settings-policy-v1",
        config_path: str | None = None,
        connection_id: str | None = None,
        connection_role: str = "owner",
        diagnostic_log: DiagnosticLog | None = None,
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
        # 运行工具继续使用 canonical realpath；Settings binding 另外保留用户
        # 选择的 lexical workspace path，以便 symlink 切换不能复用旧 credential。
        self._settings_workspace = (workspace or Path.cwd()).expanduser().absolute()
        self._workspace = self._settings_workspace.resolve()
        self._diagnostic_log = ensure_log(diagnostic_log)
        # ponytail: Host 固定绑定一个 workspace，先用一把锁覆盖跨 Profile 图；
        # worktree/sandbox 有稳定资源身份或吞吐证明不足时再按资源拆分。
        self._tool_concurrency_lock = AsyncRWLock()
        from harness_agent.policy.workspace_roots import WorkspaceRootRegistry

        # 额外工作目录 registry：内容可变，不进入执行资源池 fingerprint。
        self._workspace_root_registry = WorkspaceRootRegistry(
            self._workspace,
            project_dir=self._workspace,
            load_persisted=True,
        )
        # 在 Host 启动时冻结只读 roots/policy 输入；Settings store 不再自行扫描
        # 可变 registry，Run/generation 期间也不会因外部授权变化而漂移。
        frozen_settings_roots = tuple(
            Path(item).expanduser().absolute()
            for item in (
                settings_workspace_roots
                if settings_workspace_roots is not None
                else tuple(root.path for root in self._workspace_root_registry.roots())
            )
        )
        self._config_path = config_path or os.environ.get("HARNESS_AGENT_CONFIG_PATH")
        self._connection_role = connection_role
        self._config_home = config_home
        self._config: Za38Config | None = None
        self._config_change_policy = config_change_policy or ManagedConfigPolicy()
        self._config_change_service: ConfigChangeService | None = None
        self._startup_error: str | None = None
        self._skill_catalog_manager = SkillCatalogManager(
            self._workspace,
            home=self._config_home,
        )
        self._skill_registry: SkillRegistry | None = None
        self._skill_registry_source_signature: tuple[str, str] | None = None
        self._plugin_manager = PluginManager(home=self._config_home)
        self._plugin_catalog_snapshot: ExtensionCatalogSnapshot | None = None
        self._plugin_reauthorization_required: tuple[str, ...] = ()
        self._plugin_reauthorization_index: dict[str, PluginReauthorizationSummary] = {}
        self._plugin_skill_sources: tuple[PluginSkillSource, ...] = ()
        self._plugin_agent_sources: tuple[PluginAgentSource, ...] = ()
        self._plugin_context_blocks_by_source: dict[str, tuple[ContextBlock, ...]] = {}
        self._plugin_team_definitions: tuple[TeamDefinition, ...] = ()
        self._generated_team_definitions: dict[str, TeamDefinition] = {}
        self._active_team_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_team_tokens: dict[str, RunCancellationToken] = {}
        self._plugin_mcp_servers: tuple[McpServerConfig, ...] = ()
        # Settings overlay 只在当前 Host/generation 现场应用；保留不含 secret
        # 的 immutable component base，刷新失败时可以精确移除旧 overlay。
        self._base_plugin_mcp_servers: tuple[McpServerConfig, ...] = ()
        self._base_plugin_runtime_catalog = PluginRuntimeCatalog()
        self._mcp_diagnostics: tuple[str, ...] = ()
        self._plugin_runtime_catalog = PluginRuntimeCatalog()
        self._plugin_runtime_manager: PluginRuntimeManager | None = None
        self._plugin_runtime_start_lock = asyncio.Lock()
        self._plugin_diagnostics: tuple[str, ...] = ()
        # Settings backend 由平台适配器或离线 fake 提供；平台适配器只延迟加载，
        # 不在 Host 构造时读取凭据。不可证明能力时仍 fail closed，不回退 shell。
        resolved_settings_home = (self._config_home or Path.home()).expanduser().resolve()
        effective_settings_backend = settings_backend or create_platform_credential_backend(
            metadata_root=resolved_settings_home / ".harness" / "settings" / "v1",
        )
        self._settings_user_store = SettingsStore(
            home=self._config_home,
            backend=effective_settings_backend,
            workspace_roots=frozen_settings_roots,
            policy_version=settings_policy_version,
        )
        self._settings_workspace_store = SettingsStore(
            home=self._config_home,
            workspace=self._settings_workspace,
            backend=effective_settings_backend,
            workspace_roots=frozen_settings_roots,
            policy_version=settings_policy_version,
        )
        self._settings_resolver = SettingsResolver(
            user=self._settings_user_store,
            workspace=self._settings_workspace_store,
        )
        self._settings_bindings: tuple[SettingBinding, ...] = ()
        self._settings_diagnostics: tuple[str, ...] = ()
        self._settings_blocked_plugin_ids: frozenset[str] = frozenset()
        self._settings_snapshot = SettingsSnapshot.not_loaded()
        self._agent_catalog: AgentCatalog | None = None
        self._thread_persistence: ThreadPersistence | None = None
        # Snapshot 只存在于 Host 进程内；不复用 SQLite，也不跨 Host/进程恢复。
        self._snapshot_store = ThreadSnapshotStore()
        # Deferred 工具 reveal 状态按 Thread 隔离存储于 Host 生命周期内。
        self._deferred_tool_store = ThreadDeferredToolStore()
        # 文件工具指标同样只保留在 Host；它不携带任何源码、路径或 Snapshot 句柄。
        self._file_tool_metrics = FileToolMetrics()
        self._agent_engine_pool: AgentEnginePool | None = None
        self._mcp_manager: McpConnectionManager | None = None
        self._mcp_owner: SharedResourceOwner[McpConnectionManager] | None = None
        self._retired_mcp_owners: list[SharedResourceOwner[McpConnectionManager]] = []
        self._profile_mcp_owners: dict[
            str,
            SharedResourceOwner[McpConnectionManager],
        ] = {}
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
        self._control_lease = ControlLease(self._owner_connection.connection_id)
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
            context_updates_provider=self._take_context_updates,
            project_dir=self._workspace,
            workspace_root_registry=self._workspace_root_registry,
            compose_services_provider=self._provide_compose_services,
            diagnostic_log=diagnostic_log,
        )
        self._handlers = {
            METHOD["INITIALIZE"]: self._handle_initialize,
            METHOD["COMMANDS_BIND"]: self._handle_commands_bind,
            METHOD["RUN_START"]: self._handle_run_start,
            METHOD["RUN_CANCEL"]: self._handle_run_cancel,
            METHOD["CONTEXT_COMPACT"]: self._handle_context_compact,
            METHOD["CONFIG_SHOW"]: self._handle_config_show,
            METHOD["CONFIG_PATH"]: self._handle_config_path,
            METHOD["CONFIG_DETAILS"]: self._handle_config_details,
            METHOD["CONFIG_PREVIEW"]: self._handle_config_preview,
            METHOD["CONFIG_COMMIT"]: self._handle_config_commit,
            METHOD["SETTINGS_LIST"]: self._handle_settings_list,
            METHOD["SETTINGS_SET"]: self._handle_settings_set,
            METHOD["SETTINGS_REMOVE"]: self._handle_settings_remove,
            METHOD["MODELS_LIST"]: self._handle_models_list,
            METHOD["THREADS_LIST"]: self._handle_threads_list,
            METHOD["THREADS_OPEN"]: self._handle_threads_open,
            METHOD["THREADS_WATCH"]: self._handle_threads_watch,
            METHOD["THREADS_UNWATCH"]: self._handle_threads_unwatch,
            METHOD["THREADS_SIDE_QUESTION"]: self._handle_threads_side_question,
            METHOD["SKILLS_LIST"]: self._handle_skills_list,
            METHOD["SKILLS_INSPECT"]: self._handle_skills_inspect,
            METHOD["SKILLS_SET_ENABLED"]: self._handle_skills_set_enabled,
            METHOD["SKILLS_INSTALL"]: self._handle_skills_install,
            METHOD["SKILLS_UPDATE"]: self._handle_skills_update,
            METHOD["SKILLS_REMOVE"]: self._handle_skills_remove,
            METHOD["SKILLS_MARKET_LIST"]: self._handle_skills_market_list,
            METHOD["PLUGINS_LIST"]: self._handle_plugins_list,
            METHOD["PLUGINS_INSPECT"]: self._handle_plugins_inspect,
            METHOD["PLUGINS_VALIDATE"]: self._handle_plugins_validate,
            METHOD["PLUGINS_INSTALL"]: self._handle_plugins_install,
            METHOD["PLUGINS_SET_ENABLED"]: self._handle_plugins_set_enabled,
            METHOD["PLUGINS_REMOVE"]: self._handle_plugins_remove,
            METHOD["AGENTS_LIST"]: self._handle_agents_list,
            METHOD["AGENTS_INSPECT"]: self._handle_agents_inspect,
            METHOD["TEAMS_LIST"]: self._handle_teams_list,
            METHOD["TEAMS_INSPECT"]: self._handle_teams_inspect,
            METHOD["TEAMS_GENERATE"]: self._handle_teams_generate,
            METHOD["TEAMS_RUN"]: self._handle_teams_run,
            METHOD["TEAMS_CANCEL"]: self._handle_teams_cancel,
            METHOD["MCP_STATUS"]: self._handle_mcp_status,
            METHOD["MCP_ADD"]: self._handle_mcp_add,
            METHOD["MCP_REMOVE"]: self._handle_mcp_remove,
            METHOD["HOST_ATTACHMENT_CREATE"]: self._handle_host_attachment_create,
            METHOD["HOST_ATTACHMENT_REVOKE"]: self._handle_host_attachment_revoke,
            METHOD["HOST_CONTROL_ACQUIRE"]: self._handle_host_control_acquire,
            METHOD["HOST_CONTROL_RELEASE"]: self._handle_host_control_release,
            METHOD["HOST_CONTROL_STATUS"]: self._handle_host_control_status,
            METHOD["COMPOSE_INSPECT"]: self._handle_compose_inspect,
            METHOD["COMPOSE_ABANDON"]: self._handle_compose_abandon,
        }
        self._attachments = AttachmentManager(
            create_connection=self.create_connection,
            dispatch_connection=self.dispatch_connection,
            close_connection=self.close_connection,
            register_attachment=self._control_lease.register_attachment,
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
        was_running = self._running
        self._running = False
        await self._run_coordinator.close()
        # SettingsSnapshot 属于 Host/generation；Run terminal 不会释放它，只有
        # Host close（或未来 generation replacement）才结束其生命周期。
        self._clear_settings_overlays()
        self._settings_snapshot.release()
        for token in self._active_team_tokens.values():
            token.cancel()
        if self._active_team_tasks:
            await asyncio.gather(
                *tuple(self._active_team_tasks.values()),
                return_exceptions=True,
            )
        self._active_team_tasks.clear()
        self._active_team_tokens.clear()
        if self._run_event_tasks:
            await asyncio.gather(*tuple(self._run_event_tasks), return_exceptions=True)
        pending_requests = sum(
            len(connection.pending_requests) for connection in self._connections.values()
        )
        for connection in list(self._connections.values()):
            connection.closed = True
            self._fail_connection_requests(
                connection,
                RpcError(-32004, "Peer connection closed"),
            )
        await self._attachments.close()
        # AgentEngine 先释放自己的图和共享租约，Host owner 再关闭 MCP、
        # workspace/sandbox、Provider transport，最后才关闭 ThreadPersistence。
        await self._close_agent_engine_pool()
        if self._mcp_connect_task is not None:
            await asyncio.gather(self._mcp_connect_task, return_exceptions=True)
            self._mcp_connect_task = None
        if self._plugin_runtime_manager is not None:
            await self._plugin_runtime_manager.aclose()
            self._plugin_runtime_manager = None
        owners = [
            *self._retired_mcp_owners,
            *([self._mcp_owner] if self._mcp_owner is not None else []),
        ]
        for owner in owners:
            await owner.aclose()
        if not owners and self._mcp_manager is not None:
            # 初始化尚未完成或测试替换 manager 时没有 SharedResourceOwner，仍须
            # 直接释放当前 MCP 连接，不能因 owner 缺失而泄漏子进程/transport。
            await self._mcp_manager.close_all()
        self._retired_mcp_owners.clear()
        self._mcp_owner = None
        self._mcp_manager = None
        if self._workspace_execution_resources is not None:
            await self._workspace_execution_resources.aclose()
            self._workspace_execution_resources = None
        await self._provider_client_pool.aclose()
        self._snapshot_store.close()
        if was_running:
            self._diagnostic_log.info(
                "ipc.transport.closed",
                {
                    "side": "server",
                    "outcome": "completed",
                    "pending_requests": pending_requests,
                },
            )
        await self._close_thread_persistence()

    async def close_connection(self, connection: ProtocolConnection) -> None:
        """释放 attached Connection，并取消仅由它拥有的 active Runs。"""
        if connection.closed:
            return
        attachment_id: str | None = None
        if connection.role == "attached":
            # 先标记 attachment 撤销并拒绝新 permit，再收敛 Interaction 与 Run。
            attachment_id = await self._control_lease.connection_disconnected(
                connection.connection_id
            )
        connection.closed = True
        connection.watched_threads.clear()
        self._fail_connection_requests(connection, RpcError(-32004, "Peer connection closed"))
        await self._run_coordinator.owner_disconnected(
            ConnectionRef(connection.connection_id)
        )
        self._connections.pop(connection.connection_id, None)
        if attachment_id is not None:
            await self._control_lease.complete_revoke(attachment_id)

    def create_connection(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        role: str = "attached",
        capability_ceiling: Iterable[str] = SERVER_CAPABILITIES,
        attachment_id: str | None = None,
    ) -> ProtocolConnection:
        """建立轻量协议连接；Project 资源仍由当前 Host 唯一持有。"""
        connection = ProtocolConnection(
            connection_id=str(uuid.uuid4()),
            role=role,
            sender=sender,
            capability_ceiling=frozenset(capability_ceiling),
        )
        self._connections[connection.connection_id] = connection
        if attachment_id is not None:
            # 显式 attachment 登记只用于测试/嵌入路径；生产路径由
            # AttachmentManager 在 WebSocket 认证完成后登记。
            self._control_lease.register_attachment_sync(
                attachment_id,
                connection.connection_id,
            )
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
            self._log_ipc_request(method, request_id, time.monotonic(), success=False)
            return
        started_at = time.monotonic()
        ipc_ok = False
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
            required_minor = OPERATION_MIN_MINOR.get(method)
            if (
                required_minor is not None
                and connection.protocol_minor < required_minor
            ):
                error_code = (
                    "SETTINGS_PROTOCOL_MINOR_REQUIRED"
                    if method.startswith("settings.")
                    else "PROTOCOL_MINOR_REQUIRED"
                )
                raise RpcError(
                    -32003,
                    error_code,
                    {
                        "code": error_code,
                        "retryable": False,
                        "details": {
                            "method": method,
                            "required_minor": required_minor,
                            "negotiated_minor": connection.protocol_minor,
                        },
                    },
                )
            validate_operation_params(method, params)
            required_capability = OPERATION_CAPABILITIES.get(method)
            if required_capability and required_capability not in self._connection_capabilities(connection):
                error_code = (
                    "SETTINGS_CAPABILITY_REQUIRED"
                    if method.startswith("settings.")
                    else "CAPABILITY_REQUIRED"
                )
                raise RpcError(
                    -32002,
                    error_code,
                    {"code": error_code, "retryable": False, "capability": required_capability},
                )
            if method in CONTROLLED_OPERATIONS:
                async with self._control_lease.permit(connection.connection_id):
                    result = await handler(params, request_id)
            else:
                result = await handler(params, request_id)
            if result is not None:
                try:
                    validate_operation_result(method, result)
                except ValidationError:
                    # 响应 schema 不匹配属于 sidecar 内部错误，不能让异常穿出
                    # JSON-RPC 主循环并关闭 stdio；否则客户端只能看到 transport closed。
                    logger.exception("Invalid handler result for %s", method)
                    await self.send_error(request_id, -32603, "Invalid handler result")
                    return
                await self.send_response(request_id, result)
            ipc_ok = True
        except ValidationError as exc:
            settings_value_error = _settings_value_schema_error(method, params, exc)
            if settings_value_error is not None:
                settings_error = SettingsError(settings_value_error, field="value")
                await self.send_error(
                    request_id,
                    int(ERROR_CODES[settings_value_error]["jsonrpc_code"]),
                    settings_value_error,
                    settings_error.redacted_data(),
                )
                return
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
        except (SkillError, CatalogSkillError) as exc:
            message = str(exc)
            data = (
                {"code": message, "retryable": False}
                if message.startswith(("COMMAND_INVOCATION_", "PLUGIN_COMMAND_"))
                else None
            )
            await self.send_error(request_id, -32602, message, data)
        except PluginError as exc:
            details = {"field": exc.field} if exc.field is not None else None
            await self.send_error(
                request_id,
                -32040,
                exc.code,
                {"code": exc.code, "retryable": False, "details": details},
            )
        except TeamError as exc:
            data: dict[str, object] = {
                "code": exc.code,
                "retryable": False,
            }
            if exc.details is not None:
                data["details"] = exc.details
            await self.send_error(
                request_id,
                -32050,
                exc.code,
                data,
            )
        except AgentCatalogError as exc:
            code = str(exc).split(":", 1)[0]
            await self.send_error(
                request_id,
                -32041,
                code,
                {"code": code, "retryable": False},
            )
        except ThreadPersistenceError as exc:
            # 初始化失败时不能只返回一个笼统错误码；CLI 启动阶段没有可用的
            # 业务上下文，必须把持久化层的稳定诊断码放进 message，便于用户
            # 区分迁移、权限、损坏和版本过新的数据库。原始异常仍通过 data
            # 返回给具备结构化错误处理能力的客户端。
            detail = str(exc) or type(exc).__name__
            await self.send_error(
                request_id,
                -32020,
                f"THREAD_STORE_UNAVAILABLE: {detail}",
                {"code": detail},
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
        except ControlLeaseError as exc:
            await self.send_error(
                request_id,
                CONTROL_RPC_CODES.get(exc.code, -32008),
                exc.code,
                {"code": exc.code, "retryable": exc.retryable},
            )
        except RpcError as exc:
            await self.send_error(request_id, exc.code, exc.message, exc.data)
        except PluginReauthorizationRequired as exc:
            await self.send_error(
                request_id,
                -32004,
                "PLUGIN_REAUTHORIZATION_REQUIRED",
                _plugin_reauthorization_error_data(exc.summary),
            )
        except Exception as exc:  # pragma: no cover - 最后的协议隔离层。
            logger.exception("Unhandled JSON-RPC handler error for %s", method)
            await self.send_error(request_id, -32603, f"{type(exc).__name__}: {exc}")
        finally:
            self._log_ipc_request(method, request_id, started_at, success=ipc_ok)

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

    def _log_ipc_request(
        self,
        method: str,
        request_id: str,
        started_at: float,
        *,
        success: bool,
    ) -> None:
        """记录 server 侧 IPC 请求终态；不含 raw frame 或错误原文。"""
        duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
        log = self._diagnostic_log
        rpc_id = safe_context_value(request_id)
        if rpc_id is not None:
            log = log.child({"rpc_request_id": rpc_id})
        safe_method = safe_context_value(method) or "unknown_method"
        if success:
            log.info(
                "ipc.request.completed",
                {"side": "server", "method": safe_method, "duration_ms": duration_ms},
            )
            return
        log.error(
            "ipc.request.failed",
            {
                "side": "server",
                "method": safe_method,
                "duration_ms": duration_ms,
                "failure_stage": "handler",
                "error_type": "RpcError",
                "retryable": False,
                "summary_code": "ipc_request_failed",
            },
        )

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
        started_at = time.monotonic()
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
                reservation = await self._reserve_agent_engine_snapshot()
                try:
                    await self._refresh_skill_catalog_locked()
                finally:
                    await reservation.release()
                self._skill_registry = self._build_skill_registry()
                self._load_config()
                self._refresh_settings_snapshot()
                # MCP 连接不阻塞 initialize 响应；后台建立连接
                self._mcp_connect_task = asyncio.ensure_future(self._connect_mcp_servers())
                self._resources_ready = True
        registry = self._require_skills()
        requested = set(parsed.capabilities.requests)
        enabled = requested.intersection(connection.capability_ceiling)
        if connection.role != "owner":
            enabled.discard(CAPABILITY["HOST_ATTACH"])
        handles = set(parsed.capabilities.handles)
        connection.protocol_minor = negotiated_minor
        connection.interaction_handles = handles
        connection.enabled_capabilities = enabled
        connection.initialized = True
        project_id = workspace_fingerprint(self._workspace)
        # 仅在客户端明确请求 thread 读取能力时打开用户级 SQLite；插件/Skill
        # 管理命令不应因本地历史库权限或迁移状态而无法启动。
        if CAPABILITY["THREADS_READ"] in enabled and self._thread_persistence_enabled():
            project_id = (await self._ensure_thread_persistence()).project_fingerprint
        result = {
            "protocol": {"major": PROTOCOL_MAJOR, "minor": negotiated_minor},
            "server": {"name": "za38-agent", "version": __version__},
            "connection": {
                "id": connection.connection_id,
                "role": connection.role,
                "project": {
                    "id": project_id,
                    "label": self._workspace.name,
                },
            },
            "capabilities": {
                "available": (
                    list(connection.capability_ceiling)
                    if connection.role != "owner"
                    else list(SERVER_CAPABILITIES)
                ),
                "enabled": sorted(enabled),
                "handles": sorted(handles),
            },
            "agent_commands": registry.agent_commands(),
            "static_command_preview": self._plugin_manager.static_preview()["commands"],
            "skills_snapshot": registry.snapshot(),
            "skill_diagnostics": registry.diagnostics[:20],
            "limits": {
                "max_frame_bytes": MAX_FRAME_BYTES,
                "max_tool_payload_bytes": MAX_TOOL_PAYLOAD_BYTES,
            },
            "diagnostics": (
                self._config.diagnostics.redacted()
                if self._config is not None
                else DiagnosticsSettings(
                    level=(
                        os.environ["HARNESS_LOG_LEVEL"]
                        if os.environ.get("HARNESS_LOG_LEVEL") in {"debug", "info", "warn", "error"}
                        else "info"
                    )
                ).redacted()
            ),
            "config_summary": self._config.redacted() if self._config else None,
            "startup_error": (
                {"code": "CONFIGURATION_ERROR", "message": self._startup_error}
                if self._startup_error
                else None
            ),
        }
        self._diagnostic_log.info(
            "ipc.initialize.completed",
            {
                "side": "server",
                "duration_ms": max(0, round((time.monotonic() - started_at) * 1000)),
                "protocol_minor": negotiated_minor,
            },
        )
        return result

    async def _handle_commands_bind(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """登记 CLI 对本次 Host snapshot 得出的唯一 command 名称映射。"""
        connection = self._current_connection()
        if connection.command_binding_snapshot_id is not None:
            raise RpcError(
                -32602,
                "COMMAND_BINDING_ALREADY_SET",
                {"code": "COMMAND_BINDING_ALREADY_SET", "retryable": False},
            )
        parsed = CommandsBindParams.model_validate(params)
        await self._refresh_control_plane_catalog()
        registry = self._require_skills()
        if parsed.snapshot_id != registry.snapshot_id:
            raise RpcError(
                -32602,
                "COMMAND_BINDING_SNAPSHOT_MISMATCH",
                {"code": "COMMAND_BINDING_SNAPSHOT_MISMATCH", "retryable": False},
            )

        expected_ids = {
            str(command["id"])
            for command in registry.agent_commands()
            if isinstance(command, dict) and isinstance(command.get("id"), str)
        }
        bound: dict[str, str] = {}
        names: set[str] = set()
        for binding in parsed.bindings:
            command_id = str(binding.id)
            command_name = str(binding.name)
            folded_name = command_name.casefold()
            if command_id in bound:
                raise RpcError(
                    -32602,
                    "COMMAND_BINDING_DUPLICATE_ID",
                    {"code": "COMMAND_BINDING_DUPLICATE_ID", "retryable": False},
                )
            if folded_name in names:
                raise RpcError(
                    -32602,
                    "COMMAND_BINDING_DUPLICATE_NAME",
                    {"code": "COMMAND_BINDING_DUPLICATE_NAME", "retryable": False},
                )
            if command_id not in expected_ids:
                raise RpcError(
                    -32602,
                    "COMMAND_BINDING_UNKNOWN_ID",
                    {"code": "COMMAND_BINDING_UNKNOWN_ID", "retryable": False},
                )
            bound[command_id] = command_name
            names.add(folded_name)
        if set(bound) != expected_ids:
            raise RpcError(
                -32602,
                "COMMAND_BINDING_SET_MISMATCH",
                {"code": "COMMAND_BINDING_SET_MISMATCH", "retryable": False},
            )
        connection.command_binding_snapshot_id = parsed.snapshot_id
        connection.command_bindings = MappingProxyType(bound)
        return {"snapshot_id": parsed.snapshot_id, "accepted": True}

    async def _connect_mcp_servers(self) -> None:
        """根据配置建立 MCP 服务器连接并构建初始 snapshot；失败不阻止启动。"""
        async with self._mcp_state_lock:
            try:
                config_snapshot = self._config_changes().read_mcp_snapshot()
            except ConfigChangeError:
                # 配置在 initialize 阶段已失败时仍以空快照启动，不阻塞协议握手。
                config_snapshot = build_mcp_snapshot([], "missing")
            snapshot = self._combine_mcp_snapshot(config_snapshot)
            self._mcp_snapshot = snapshot
            await self._replace_mcp_generation(snapshot)

    async def _replace_mcp_generation(
        self,
        snapshot: McpConfigSnapshot,
    ) -> list[dict[str, object]]:
        """建立新 MCP generation，再让旧 owner 延迟到最后借用者退出后关闭。

        调用方必须持有 `_mcp_state_lock`，从而保证新 spec 不会同时绑定旧 manager
        和新 snapshot。
        """
        manager = McpConnectionManager(snapshot, diagnostic_log=self._diagnostic_log)
        await manager.connect_all()
        owner = SharedResourceOwner(
            manager,
            name=f"mcp-{snapshot.digest[:12]}",
            scope=ResourceScope.HOST,
            fingerprint=snapshot.digest,
            close=lambda resource: resource.close_all(),
        )
        previous = self._mcp_owner
        self._mcp_manager = manager
        self._mcp_owner = owner
        if previous is not None:
            self._retired_mcp_owners.append(previous)
            await previous.retire()
        return manager.get_server_statuses()

    async def _invalidate_mcp_profiles(self, snapshot: McpConfigSnapshot) -> None:
        """让仍绑定旧 MCP 快照的角色图进入 DRAINING。"""
        pool = self._agent_engine_pool
        if pool is None:
            return
        await pool.invalidate_outdated(
            resource="mcp",
            current_fingerprint=snapshot.digest,
            reason="snapshot_changed",
        )

    async def _ensure_mcp_connected(self) -> None:
        """等待后台 MCP 连接任务完成（若仍在运行）。"""
        task = self._mcp_connect_task
        if task is not None and not task.done():
            await task

    async def _start_plugin_runtime(self) -> None:
        """启动 Monitor；坏 Monitor 已在 runtime catalog 中隔离，不阻止 Host。"""
        manager = self._plugin_runtime_manager
        if manager is None:
            return
        try:
            await manager.start()
        except Exception:
            logger.exception("Plugin runtime startup failed")

    async def _ensure_plugin_runtime_started(self) -> None:
        """在首次构图前启动 Monitor，确保短生命周期 Host 不泄漏后台进程。"""
        async with self._plugin_runtime_start_lock:
            await self._start_plugin_runtime()

    async def _handle_run_start(self, params: dict[str, Any], request_id: str) -> None:
        """把协议输入转换成 StartRun，并让 Coordinator 先完成受理再启动事件流。"""
        parsed = RunStartParams.model_validate(params)
        message = parsed.message
        if not message.strip():
            raise RpcError(-32602, "message must be non-empty")
        if (
            parsed.model_selection is not None
            and CAPABILITY["MODELS_SELECT"] not in self._connection_capabilities()
        ):
            raise RpcError(-32002, "MODELS_SELECT_CAPABILITY_REQUIRED")

        requested_skill = (
            RequestedSkill(
                parsed.requested_skill.id,
                parsed.requested_skill.args or "",
                parsed.requested_skill.raw_invocation,
                parsed.requested_skill.command_name,
            )
            if parsed.requested_skill is not None
            else None
        )
        connection = self._current_connection()
        command = StartRun(
            thread_id=parsed.thread_id,
            run_id=parsed.run_id,
            message=message,
            mode=parsed.mode,
            requested_skill=requested_skill,
            connection_id=connection.connection_id,
            command_binding_snapshot_id=connection.command_binding_snapshot_id,
            command_bindings=connection.command_bindings,
            protocol_minor=connection.protocol_minor,
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
                ConnectionRef(connection.connection_id),
                allow_multithread=(
                    CAPABILITY["RUN_MULTITHREAD"] in self._connection_capabilities()
                ),
            )
        except PluginReauthorizationRequired as exc:
            raise RpcError(
                -32004,
                "PLUGIN_REAUTHORIZATION_REQUIRED",
                _plugin_reauthorization_error_data(exc.summary),
            ) from exc
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
            reservation = await self._reserve_agent_engine_snapshot()
            try:
                registry = await self._refresh_skill_catalog_locked()
                self._raise_if_stale_plugin_request(command)
                return RunPreparation(
                    skill_snapshot_id=registry.snapshot_id,
                    skill_registry=registry,
                    requested_skill=self._prepare_requested_skill(command, registry),
                    catalog_skill_ids=tuple(record.skill_id for record in registry.records),
                )
            finally:
                await reservation.release()
        reservation = await self._reserve_agent_engine_snapshot()
        try:
            registry = await self._refresh_skill_catalog_locked()
            self._raise_if_stale_plugin_request(command)
            requested_skill = self._prepare_requested_skill(command, registry)
            self._load_config()
            if self._config is None:
                raise ConfigError(self._startup_error or "MODEL_CONFIGURATION_REQUIRED")
            resolved = await self._resolve_execution_binding(
                command.thread_id,
                self._config,
                persistence=persistence,
                requested_primary_profile=command.requested_primary_profile,
            )
            spec = await self._resolve_agent_engine_spec(
                command.thread_id,
                self._config,
                resolved,
                persistence=persistence,
                skill_registry=registry,
                approval_mode=command.requested_approval_mode,
            )
            # Policy resolution may narrow the source catalog to a role-level
            # immutable view.  The Run must carry that exact view into the
            # Context/virtual backend; otherwise a delegate could retain the
            # unrestricted catalog even though its capability envelope was
            # narrowed during spec resolution.
            effective_registry = spec.skill_registry
            if command.requested_skill is not None:
                requested_skill = self._prepare_requested_skill(
                    command,
                    effective_registry,
                )
            profile = spec.runtime_profile
            context_snapshot = ContextLifecycle(
                self._workspace,
                home=self._config_home,
            ).prepare(
                thread_id=command.thread_id,
                spec=spec,
                stable_reference_blocks=tuple(
                    block
                    for blocks in self._plugin_context_blocks_by_source.values()
                    for block in blocks
                ),
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
            return RunPreparation(
                resolved_execution_binding=resolved,
                execution_binding=binding,
                agent_engine_profile=profile,
                skill_snapshot_id=effective_registry.snapshot_id,
                skill_registry=effective_registry,
                requested_skill=requested_skill,
                context_snapshot=context_snapshot,
                idle_duration_ms=idle_duration_ms,
                catalog_skill_ids=tuple(
                    record.skill_id for record in effective_registry.records
                ),
                catalog_mcp_ids=tuple(server.name for server in spec.mcp_snapshot.servers),
                catalog_plugin_ids=tuple(
                    plugin.plugin_id
                    for plugin in (
                        self._plugin_catalog_snapshot.plugins
                        if self._plugin_catalog_snapshot is not None
                        else ()
                    )
                ),
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
            try:
                await self._release_run_agent_engine(run)
            finally:
                plugin_runtime = self._plugin_runtime_manager
                if plugin_runtime is not None and run.run_context is not None:
                    plugin_runtime.clear_run_context(run.run_context)

        if agent is None and not self._allow_echo:
            raise ConfigError(self._startup_error or "Agent is not configured")
        persistence = run.persistence
        if (
            persistence is not None
            and agent is not None
            and callable(getattr(agent, "aupdate_state", None))
        ):
            from harness_agent.threads.context_projection import (
                ContextProjector,
                tail_user_exclude_id,
            )

            try:
                projector = ContextProjector(persistence)
                records = await persistence.load_transcript(run.thread_id)
                projection = await projector.project(
                    run.thread_id,
                    exclude_record_id=tail_user_exclude_id(records, run.run_id),
                )
                await projector.sync_cache(agent, run.thread_id, projection=projection)
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
        from harness_agent.threads.context_projection import ContextProjector, artifact_references
        from harness_agent.threads.runtime_state import (
            RuntimeExecutionPolicy,
            RuntimeStateRehydrator,
        )

        persistence = await self._ensure_thread_persistence()
        projection = await ContextProjector(persistence).project(thread_id)
        messages = list(projection.messages)
        load_snapshot = getattr(persistence, "load_latest_context_snapshot", None)
        snapshot = await load_snapshot(thread_id) if callable(load_snapshot) else None
        load_graph_state = getattr(persistence, "load_langgraph_state", None)
        graph_state = await load_graph_state(thread_id) if callable(load_graph_state) else {}
        if not self._uses_default_agent_factory:
            runtime_state = RuntimeStateRehydrator.capture(
                graph_state,
                None,
                projection.messages,
                artifact_ids=artifact_references(projection.messages),
                context_snapshot=snapshot,
            )
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
                projection=projection,
                run_context_snapshot=snapshot,
                runtime_state=runtime_state,
            )

        lease, engine = await self._acquire_default_agent_engine(thread_id)
        try:
            if lease is None or engine is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            artifacts = self._agent_engine_artifacts.get(engine.profile_key)
            spec = self._resolved_agent_specs.get(engine.profile_key)
            if artifacts is None or spec is None or engine.graph is None:
                raise RpcError(-32010, "CONTEXT_COMPACTION_UNAVAILABLE")
            current_execution_policy = RuntimeExecutionPolicy.from_resolved_spec(spec)
            runtime_state = RuntimeStateRehydrator.capture(
                graph_state,
                None,
                projection.messages,
                artifact_ids=artifact_references(projection.messages),
                context_snapshot=snapshot,
                current_execution_policy=current_execution_policy,
            )
            return await self._compact_with_agent_engine(
                agent=engine.graph,
                middleware=artifacts.context_compactor,
                thread_id=thread_id,
                messages=messages,
                persistence=persistence,
                projection=projection,
                run_context_snapshot=snapshot,
                runtime_state=runtime_state,
                current_execution_policy=current_execution_policy,
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
        summary["file_tool_metrics"] = self._file_tool_metrics.snapshot().payload()
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
        await self._refresh_control_plane_catalog()
        # 后台连接任务可能尚未完成，先等待它
        await self._ensure_mcp_connected()
        async with self._mcp_state_lock:
            manager = self._mcp_manager
            if manager is None:
                result = {
                    "servers": [],
                    "total_tools": 0,
                    "static_preview": self._plugin_manager.static_preview()["mcp"],
                }
                if self._mcp_diagnostics:
                    result["diagnostics"] = list(self._mcp_diagnostics)
                return result
            statuses = manager.get_server_statuses()
        total_tools = sum(len(s.get("tool_names", [])) for s in statuses)
        result = {
            "servers": statuses,
            "total_tools": total_tools,
            "static_preview": self._plugin_manager.static_preview()["mcp"],
        }
        if self._mcp_diagnostics:
            result["diagnostics"] = list(self._mcp_diagnostics)
        return result

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
                    config_snapshot = self._config_changes().add_mcp_server(
                        mcp_config,
                        expected_revision=current.revision,
                    )
                except ConfigChangeError as exc:
                    raise RpcError(-32602, str(exc), exc.redacted_data()) from exc
                snapshot = self._combine_mcp_snapshot(config_snapshot)
                self._mcp_snapshot = snapshot
                statuses = await self._replace_mcp_generation(snapshot)
                status = next(
                    (item for item in statuses if item.get("name") == mcp_config.name),
                    {},
                )
        await self._invalidate_mcp_profiles(snapshot)

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
                    config_snapshot = self._config_changes().remove_mcp_server(
                        name,
                        expected_revision=current.revision,
                    )
                except ConfigChangeError as exc:
                    raise RpcError(-32602, str(exc), exc.redacted_data()) from exc
                snapshot = self._combine_mcp_snapshot(config_snapshot)
                self._mcp_snapshot = snapshot
                await self._replace_mcp_generation(snapshot)
        await self._invalidate_mcp_profiles(snapshot)

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
            if capability in ATTACHMENT_CAPABILITY_ALLOWLIST
        )
        return await self._attachments.create(parsed.origin, ceiling)

    async def _handle_host_attachment_revoke(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """由 owner 按 attachment_id 撤销未消费、认证中或已连接的 attachment。"""
        connection = self._current_connection()
        if connection.role != "owner":
            raise RpcError(
                -32007,
                "HOST_OWNER_REQUIRED",
                {"code": "HOST_OWNER_REQUIRED", "retryable": False},
            )
        parsed = HostAttachmentRevokeParams.model_validate(params)
        # 先阻止新 permit，再使 token 失效并关闭 socket，最后等待 Run 收敛。
        await self._control_lease.begin_revoke(parsed.attachment_id)
        attached_connection = await self._attachments.revoke(parsed.attachment_id)
        if attached_connection is not None:
            await self.close_connection(attached_connection)
        status = await self._control_lease.complete_revoke(parsed.attachment_id)
        return {
            "attachment_id": parsed.attachment_id,
            "revoked": True,
            "control": status.to_record(),
        }

    async def _handle_host_control_acquire(
        self,
        _params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """由已认证 attached Connection 原子接管 holder。"""
        connection = self._current_connection()
        attachment_id = self._control_lease.attachment_id_for(
            connection.connection_id
        )
        if attachment_id is None:
            raise ControlLeaseError("ATTACHMENT_NOT_ACTIVE")
        status = await self._control_lease.acquire(
            connection.connection_id,
            attachment_id,
            lambda: self._control_activity(self._owner_connection.connection_id),
        )
        return status.to_record()

    async def _handle_host_control_release(
        self,
        _params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """由当前 attached holder 在无未收敛工作时把控制权归还 owner。"""
        connection = self._current_connection()
        status = await self._control_lease.release(
            connection.connection_id,
            lambda: self._control_activity(connection.connection_id),
        )
        return status.to_record()

    async def _handle_host_control_status(
        self,
        _params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """返回当前 holder 事实；只读，不改变任何状态。"""
        return self._control_lease.status().to_record()

    async def _control_activity(self, connection_id: str) -> ActivityFacts:
        """汇总指定 Connection 的 Run 与未收敛 Interaction 事实。"""
        connection = self._connections.get(connection_id)
        return ActivityFacts(
            starting_or_active_runs=(
                1
                if await self._run_coordinator.connection_active(connection_id)
                else 0
            ),
            pending_interactions=(
                len(connection.pending_requests) if connection is not None else 0
            ),
        )

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
        persistence = await self._ensure_thread_persistence()
        try:
            opened = await persistence.open_thread(parsed.thread_id)
        except ThreadPersistenceError as exc:
            if str(exc) in {"THREAD_NOT_FOUND", "THREAD_NOT_RECOVERABLE"}:
                raise RpcError(-32004, str(exc)) from exc
            raise
        thread_mode = await persistence.compose_progress_store().load_thread_mode(parsed.thread_id)
        progress: dict[str, object] | None = None
        if thread_mode is ThreadMode.COMPOSE:
            progress = await self._compose_session(persistence).inspect(
                thread_id=parsed.thread_id
            )
        return {
            "thread": _thread_summary_payload(opened.summary),
            "messages": [_thread_message_payload(message) for message in opened.messages],
            "thread_mode": thread_mode.value if thread_mode is not None else None,
            "compose_progress": progress,
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

    async def _handle_threads_side_question(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """执行轻量只读单轮问答（/btw），基于当前 Thread 历史快照，0 工具，不写存储。"""
        if CAPABILITY["THREADS_READ"] not in self._connection_capabilities():
            raise RpcError(
                -32002,
                "CAPABILITY_REQUIRED",
                {"code": "CAPABILITY_REQUIRED", "retryable": False, "capability": "threads.read"},
            )
        parsed = ThreadsSideQuestionParams.model_validate(params)
        if self._allow_echo:
            return {
                "reply_text": f"echo: {parsed.question}",
                "model_profile_id": parsed.model_profile_id or "echo",
            }

        persistence = await self._ensure_thread_persistence()
        history_text_blocks: list[str] = []
        try:
            opened = await persistence.open_thread(parsed.thread_id)
            for msg in opened.messages:
                if msg.role in ("user", "assistant") and msg.content:
                    history_text_blocks.append(f"{msg.role.upper()}: {msg.content}")
        except Exception:
            pass

        self._load_config()
        if self._config is None:
            raise ConfigError(self._startup_error or "MODEL_CONFIGURATION_REQUIRED")

        model_profile_id = parsed.model_profile_id
        if model_profile_id is None:
            try:
                persisted = await persistence.load_run_state(parsed.thread_id)
                if persisted.latest_run is not None:
                    model_profile_id = persisted.latest_run.requested_selection.profile_id
            except Exception:
                pass

        if model_profile_id is None and self._config.model_catalog is not None:
            model_profile_id = self._config.model_catalog.default_profile

        model_settings = self._config.require_model(model_profile_id)

        history_context = "\n\n".join(history_text_blocks)
        system_reminder = (
            "<btw>\n"
            "This is an ephemeral side question for the current interactive session.\n"
            "Answer briefly and directly using the conversation context already provided.\n"
            "NEVER use tools.\n"
            "NEVER ask follow-up questions.\n"
            "</btw>"
        )

        prompt_parts = [system_reminder]
        if history_context:
            prompt_parts.append(f"Conversation Context:\n{history_context}")
        prompt_parts.append(f"Question:\n{parsed.question}")
        full_prompt = "\n\n".join(prompt_parts)

        from harness_agent.extensions.providers.harness_gateway import create_openai_compatible_model
        from langchain_core.messages import HumanMessage

        provider_lease = await self._provider_client_pool.acquire(model_settings)
        try:
            model = create_openai_compatible_model(
                model_settings,
                async_client=provider_lease.value,
            )
            response = await model.ainvoke([HumanMessage(content=full_prompt)])
            reply_text = response.content if isinstance(response.content, str) else str(response.content)
        finally:
            await provider_lease.release()

        return {
            "reply_text": reply_text.strip(),
            "model_profile_id": model_profile_id or "default",
        }

    async def _handle_compose_inspect(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """只读投影 Compose Thread 的当前 Work Item；不触发分类或 typed 交互。"""
        self._require_threads_capability()
        parsed = ComposeInspectParams.model_validate(params)
        persistence = await self._ensure_thread_persistence()
        await self._require_compose_thread(persistence, parsed.thread_id)
        try:
            projection = await self._compose_session(persistence).inspect(
                thread_id=parsed.thread_id,
            )
        except ComposeSessionError as exc:
            raise _compose_rpc_error(exc) from exc
        return {"progress": projection}

    async def _handle_compose_abandon(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """废弃当前薄进度；文档保留。"""
        self._require_threads_capability()
        parsed = ComposeAbandonParams.model_validate(params)
        persistence = await self._ensure_thread_persistence()
        await self._require_compose_thread(persistence, parsed.thread_id)
        try:
            projection = await self._compose_session(persistence).abandon(
                thread_id=parsed.thread_id,
                reason=parsed.reason,
            )
        except ComposeSessionError as exc:
            raise _compose_rpc_error(exc) from exc
        return {"progress": projection}

    def _compose_session(self, persistence: ThreadPersistence) -> ComposeSession:
        """只读 Session；inspect/abandon 不跑 Grill。"""
        async def _noop_grill(request: object, slug: str) -> None:
            del request, slug

        return ComposeSession(
            ComposeSessionPorts(
                store=persistence.compose_progress_store(),
                workspace=self._workspace,
                run_grill=_noop_grill,
            )
        )

    async def _require_compose_thread(self, persistence: ThreadPersistence, thread_id: str) -> None:
        """Compose RPC 只能用于已冻结为 Compose 的 Thread。"""
        thread_mode = await persistence.compose_progress_store().load_thread_mode(thread_id)
        if thread_mode is None:
            raise RpcError(
                -32004,
                "THREAD_NOT_FOUND",
                {"code": "THREAD_NOT_FOUND", "retryable": False},
            )
        if thread_mode is not ThreadMode.COMPOSE:
            raise RpcError(
                -32000,
                "THREAD_MODE_LOCKED",
                {"code": "THREAD_MODE_LOCKED", "retryable": False},
            )


    def _prepare_requested_skill(
        self,
        command: StartRun,
        registry: SkillRegistry,
    ) -> LoadedSkill | None:
        """从本次 Run 的唯一 Registry 解析并预加载 requested Skill。"""
        requested = command.requested_skill
        if requested is None:
            return None
        record = registry.resolve(requested.skill_id)
        if not record.user_invocable:
            raise SkillError(f'Skill "{record.skill_id}" is not user-invocable')
        if record.kind == "command":
            if command.protocol_minor < PROTOCOL_MINOR:
                raise SkillError("PLUGIN_COMMAND_PROTOCOL_MINOR_REQUIRED")
            _validate_command_invocation(
                command,
                requested,
                record,
                resolved_command_name=self._resolved_command_name_for_run(
                    command,
                    record.skill_id,
                ),
            )
        return registry.load(record.skill_id, requested.args)

    def _raise_if_stale_plugin_request(self, command: StartRun) -> None:
        """只拦截明确请求 stale Plugin 能力的 Run，不把 blocked index 当全局开关。"""
        requested = command.requested_skill
        if requested is None:
            return
        summary = self._reauthorization_for_identity(requested.skill_id)
        if summary is not None:
            raise PluginReauthorizationRequired(summary)

    def _reauthorization_for_identity(
        self,
        identity: str,
    ) -> PluginReauthorizationSummary | None:
        """按稳定 component ID 查找门禁事实；不读取包路径或运行时目录。"""
        for summary in self._plugin_reauthorization_index.values():
            if identity in summary.component_ids or identity.startswith(
                f"plugin/{summary.plugin_id}/"
            ):
                return summary
        return None

    def _blocked_plugin_agent_messages(self) -> dict[str, str]:
        """返回 stale Agent 的不可执行提示，不把它们加入 Agent catalog。"""
        return {
            agent_id: _plugin_reauthorization_message(summary)
            for summary in self._plugin_reauthorization_index.values()
            for agent_id in summary.agent_ids
        }

    def _resolved_command_name_for_run(self, command: StartRun, skill_id: str) -> str:
        """读取 run.start 时冻结的本连接、当前 snapshot CLI exact 名称。"""
        if not command.command_binding_snapshot_id or command.command_bindings is None:
            raise SkillError("COMMAND_INVOCATION_BINDING_REQUIRED")
        registry = self._skill_registry
        if registry is None:
            raise SkillError("COMMAND_INVOCATION_BINDING_REQUIRED")
        if command.command_binding_snapshot_id != registry.snapshot_id:
            raise SkillError("COMMAND_INVOCATION_BINDING_STALE")
        resolved_name = command.command_bindings.get(skill_id)
        if not resolved_name:
            raise SkillError("COMMAND_INVOCATION_BINDING_REQUIRED")
        return resolved_name

    async def _refresh_skill_catalog_locked(self) -> SkillRegistry:
        """在 Host snapshot reservation 内刷新并定向排空旧 Skill Profile。"""
        # SkillCatalogManager 负责安全恢复/校验市场安装；真正交给 Run 的
        # registry 还要合并同一 Plugin catalog 的 Skill 来源。
        self._skill_catalog_manager.refresh()
        previous = self._skill_registry
        plugin_refresh = self._plugin_manager.refresh_catalog()
        self._plugin_reauthorization_required = plugin_refresh.reauthorization_required
        self._plugin_reauthorization_index = {
            summary.plugin_id: summary for summary in plugin_refresh.reauthorization
        }
        previous_plugin = self._plugin_catalog_snapshot
        if (
            previous_plugin is None
            or previous_plugin.snapshot_id != plugin_refresh.catalog.snapshot_id
            or bool(plugin_refresh.changed_plugin_ids)
        ):
            previous_mcp_digest = (
                self._mcp_snapshot.digest if self._mcp_snapshot is not None else None
            )
            previous_plugin_manager = self._plugin_runtime_manager
            if previous_plugin_manager is not None:
                await previous_plugin_manager.aclose()
            self._set_plugin_catalog(plugin_refresh.catalog)
            if self._resources_ready:
                self._refresh_settings_snapshot()
                await self._refresh_mcp_generation_locked(previous_mcp_digest)
        registry = self._build_skill_registry()
        self._skill_registry = registry
        if previous is None or previous.snapshot_id == registry.snapshot_id:
            return registry
        pool = self._agent_engine_pool
        if pool is not None:
            # Profile 的 Skill 指纹同时包含 capability policy 生成的 view
            # 指纹；这里只持有新的 catalog，无法从 profile 反推出旧 view，
            # 因而 catalog 发生变化时必须排空所有旧 Skill Profile。否则会
            # 用不完整的 catalog 指纹误判，留下旧图继续被复用。
            await pool.invalidate(
                lambda _profile: True,
                reason="skill_catalog_changed",
            )
        return registry

    async def _refresh_skill_catalog(self) -> SkillRegistry:
        """为管理 RPC 建立同一 snapshot boundary，并在完成后释放锁。"""
        reservation = await self._reserve_agent_engine_snapshot()
        try:
            return await self._refresh_skill_catalog_locked()
        finally:
            await reservation.release()

    async def _refresh_control_plane_catalog(self) -> SkillRegistry:
        """刷新控制面目录；保留测试/嵌入方显式注入的非 Plugin 目录。"""
        snapshot = self._plugin_catalog_snapshot
        known_plugin_ids = (
            {plugin.plugin_id for plugin in snapshot.plugins}
            if snapshot is not None
            else set()
        )
        injected_sources = (
            *self._plugin_skill_sources,
            *self._plugin_agent_sources,
        )
        if (
            self._skill_registry is not None
            and snapshot is None
        ) or any(source.plugin_id not in known_plugin_ids for source in injected_sources):
            # 某些嵌入式调用方会在构造后直接注入一个只读 catalog；它不属于
            # PluginStore，刷新会错误地把该调用方自己的来源清空。
            return self._skill_registry or self._build_skill_registry()
        return await self._refresh_skill_catalog()

    def _require_skills(self) -> SkillRegistry:
        """返回初始化时建立的 Skill registry。"""
        if self._skill_registry is None:
            self._skill_registry = self._build_skill_registry()
        return self._skill_registry

    def _build_skill_registry(self) -> SkillRegistry:
        """一次读取 Plugin catalog，并装配同一启动快照的 Skill 与 MCP 来源。"""
        canonical = self._skill_catalog_manager.current
        if canonical is not None and self._plugin_catalog_snapshot is not None:
            signature = (canonical.snapshot_id, self._plugin_catalog_snapshot.snapshot_id)
            if self._skill_registry is not None and self._skill_registry_source_signature == signature:
                return self._skill_registry
        if self._plugin_catalog_snapshot is None:
            diagnostics: list[str] = []
            try:
                catalog = self._plugin_manager.catalog()
            except PluginError as exc:
                catalog = ExtensionCatalogSnapshot(
                    snapshot_id=catalog_snapshot_id(0, ()),
                    registry_revision=0,
                    plugins=(),
                )
                diagnostics.append(f"plugin:catalog: {exc.code}: {exc}")
            self._set_plugin_catalog(catalog, diagnostics=diagnostics)
        registry = SkillRegistry(
            self._workspace,
            home=self._config_home,
            plugin_sources=self._plugin_skill_sources,
            plugin_diagnostics=self._plugin_diagnostics,
        )
        canonical = self._skill_catalog_manager.current
        self._skill_registry_source_signature = (
            canonical.snapshot_id if canonical is not None else registry.snapshot_id,
            self._plugin_catalog_snapshot.snapshot_id if self._plugin_catalog_snapshot is not None else "none",
        )
        return registry

    def _set_plugin_catalog(
        self,
        catalog: ExtensionCatalogSnapshot,
        *,
        diagnostics: list[str] | None = None,
    ) -> None:
        """从一次已刷新 catalog 重建所有 Plugin runtime 来源。"""
        collected = diagnostics if diagnostics is not None else []
        settings_result = self._plugin_manager.setting_bindings(catalog)
        self._settings_bindings = settings_result.bindings
        self._settings_diagnostics = settings_result.diagnostics
        self._settings_blocked_plugin_ids = frozenset(settings_result.blocked_plugin_ids)
        skill_result = self._plugin_manager.skill_sources(catalog)
        agent_result = self._plugin_manager.agent_sources(catalog)
        team_result = self._plugin_manager.team_definitions(catalog)
        runtime_catalog = self._plugin_manager.runtime_catalog(
            catalog,
            workspace=self._workspace,
            blocked_plugin_ids=settings_result.blocked_plugin_ids,
        )
        mcp_result = self._plugin_manager.mcp_servers(
            catalog,
            workspace=self._workspace,
            blocked_plugin_ids=settings_result.blocked_plugin_ids,
        )
        self._plugin_catalog_snapshot = catalog
        self._plugin_skill_sources = skill_result.sources
        self._plugin_agent_sources = agent_result.sources
        self._plugin_context_blocks_by_source = self._plugin_manager.context_blocks_by_source(
            catalog
        )
        self._plugin_team_definitions = team_result.teams
        self._plugin_runtime_catalog = runtime_catalog
        self._base_plugin_runtime_catalog = runtime_catalog
        self._plugin_runtime_manager = PluginRuntimeManager(runtime_catalog)
        self._base_plugin_mcp_servers = mcp_result.servers
        self._plugin_mcp_servers = self._base_plugin_mcp_servers
        self._mcp_diagnostics = mcp_result.diagnostics
        collected.extend(skill_result.diagnostics)
        collected.extend(agent_result.diagnostics)
        collected.extend(team_result.diagnostics)
        collected.extend(settings_result.diagnostics)
        collected.extend(runtime_catalog.diagnostics)
        collected.extend(mcp_result.diagnostics)
        self._plugin_diagnostics = tuple(collected)
        self._agent_catalog = None
        self._resolved_agent_specs.clear()
        self._agent_engine_artifacts.clear()
        self._skill_registry_source_signature = None
        for diagnostic in self._plugin_diagnostics:
            logging.getLogger(__name__).warning("Plugin runtime component disabled: %s", diagnostic)

    async def _refresh_mcp_generation_locked(self, previous_digest: str | None) -> None:
        """Plugin catalog 变化后重建当前 MCP generation，避免旧 server 继续被 Run 使用。"""
        if not self._resources_ready:
            return
        async with self._mcp_state_lock:
            try:
                config_snapshot = self._config_changes().read_mcp_snapshot()
            except ConfigChangeError:
                config_snapshot = build_mcp_snapshot([], "missing")
            snapshot = self._combine_mcp_snapshot(config_snapshot)
            if (
                self._mcp_snapshot is not None
                and self._mcp_snapshot.digest == snapshot.digest
                and previous_digest == snapshot.digest
            ):
                return
            self._mcp_snapshot = snapshot
            await self._replace_mcp_generation(snapshot)
        await self._invalidate_mcp_profiles(snapshot)

    def _require_agent_catalog(self, config: Za38Config) -> AgentCatalog:
        """从同一个启动期 Plugin snapshot 建立 canonical Agent/Policy catalog。"""
        if self._agent_catalog is None:
            if config.model_catalog is None:
                raise RuntimeError("MODEL_CATALOG_REQUIRED")
            self._require_skills()
            self._agent_catalog = AgentCatalog(
                model_catalog=config.model_catalog,
                sources=self._plugin_agent_sources,
            )
            for diagnostic in self._agent_catalog.diagnostics:
                logger.warning("Plugin Agent disabled: %s", diagnostic)
        return self._agent_catalog

    def _combine_mcp_snapshot(
        self,
        config_snapshot: McpConfigSnapshot,
    ) -> McpConfigSnapshot:
        """用用户配置 revision 合并固定 Plugin MCP，用户同名项优先且不被覆盖。"""
        names = {server.name for server in config_snapshot.servers}
        plugin_servers: list[McpServerConfig] = []
        for server in self._plugin_mcp_servers:
            if server.name in names:
                logger.warning(
                    "Plugin MCP %r disabled because a user MCP has the same canonical name",
                    server.name,
                )
                continue
            names.add(server.name)
            plugin_servers.append(server)
        return build_mcp_snapshot(
            (*config_snapshot.servers, *plugin_servers),
            config_snapshot.revision,
        )

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
        registry = await self._refresh_skill_catalog()
        return {
            "snapshot": registry.snapshot(),
            "skills": registry.list(include_disabled=include_disabled),
            "static_preview": self._plugin_manager.static_preview()["skills"],
            "diagnostics": registry.diagnostics[:20],
        }

    def _refresh_settings_snapshot(self) -> None:
        """在 Host/generation 边界解析 Settings；失败时只阻断子进程注入。"""
        try:
            result = self._plugin_manager.setting_bindings(self._plugin_catalog_snapshot)
            self._settings_bindings = result.bindings
            self._settings_diagnostics = result.diagnostics
            self._settings_blocked_plugin_ids = frozenset(result.blocked_plugin_ids)
            snapshot = self._settings_resolver.resolve(self._settings_bindings)
            self._settings_diagnostics = tuple(
                dict.fromkeys((*self._settings_diagnostics, *snapshot.diagnostics))
            )
            self._settings_blocked_plugin_ids = frozenset(
                {*self._settings_blocked_plugin_ids, *snapshot.blocked_plugin_ids}
            )
            previous = self._settings_snapshot
            self._settings_snapshot = snapshot
            previous.release()
            self._block_settings_consumers()
            self._apply_settings_snapshot_to_children()
        except SettingsError as exc:
            self._settings_diagnostics = (*self._settings_diagnostics, exc.code)
            self._settings_blocked_plugin_ids = frozenset(
                {*self._settings_blocked_plugin_ids, *(binding.plugin_id for binding in self._settings_bindings)}
            )
            previous = self._settings_snapshot
            self._settings_snapshot = SettingsSnapshot.not_loaded()
            previous.release()
            self._block_settings_consumers()
            self._apply_settings_snapshot_to_children()

    def _block_settings_consumers(self) -> None:
        """当 Settings declaration/backend 不可验证时撤销相关 executable consumers。"""
        blocked = self._settings_blocked_plugin_ids
        retained_mcp: list[McpServerConfig] = []
        blocked_mcp: set[str] = set()
        # 过滤必须基于不含 Settings overlay 的 base；否则 backend 失败后
        # 以当前 server 反向保存会把旧 generation 的 secret 再带回新环境。
        source_servers = self._base_plugin_mcp_servers
        for server in source_servers:
            if server.source.startswith("plugin:") and server.source.removeprefix("plugin:") in blocked:
                blocked_mcp.add(server.source.removeprefix("plugin:"))
                continue
            retained_mcp.append(server)
        self._plugin_mcp_servers = tuple(retained_mcp)
        if blocked_mcp:
            self._mcp_diagnostics = tuple(
                dict.fromkeys(
                    (
                        *self._mcp_diagnostics,
                        *(f"plugin:{plugin_id}: SETTINGS_CONSUMER_BLOCKED" for plugin_id in sorted(blocked_mcp)),
                    )
                )
            )

        catalog = self._base_plugin_runtime_catalog
        retained_hooks = tuple(item for item in catalog.hooks if item.plugin_id not in blocked)
        retained_lsp = tuple(item for item in catalog.lsp_servers if item.plugin_id not in blocked)
        retained_monitors = tuple(item for item in catalog.monitors if item.plugin_id not in blocked)
        retained_failures = tuple(item for item in catalog.hook_failures if item.plugin_id not in blocked)
        blocked_hook_plugins = {
            item.plugin_id
            for item in catalog.hooks
            if item.plugin_id in blocked
        }
        blocked_hook_plugins.update(
            item.plugin_id
            for item in catalog.hook_failures
            if item.plugin_id in blocked
        )
        if blocked_hook_plugins:
            retained_failures += tuple(
                HookRuntimeFailure(
                    plugin_id=plugin_id,
                    source_id=None,
                    event="SubagentStop",
                    matcher="*",
                    code="SETTINGS_CONSUMER_BLOCKED",
                )
                for plugin_id in sorted(blocked_hook_plugins)
            )
        filtered_catalog = replace(
            catalog,
            hooks=retained_hooks,
            lsp_servers=retained_lsp,
            monitors=retained_monitors,
            hook_failures=retained_failures,
            diagnostics=tuple(
                dict.fromkeys(
                    (
                        *catalog.diagnostics,
                        *(f"plugin:{plugin_id}: SETTINGS_CONSUMER_BLOCKED" for plugin_id in sorted(blocked)),
                    )
                )
            ),
        )
        if filtered_catalog != self._plugin_runtime_catalog:
            self._plugin_runtime_catalog = filtered_catalog
            # initialize 期间 runtime manager 尚未启动；刷新期间若已有 manager，
            # 先清空旧 overlay，再换成同一 base 的新过滤结果。这样 stale→healthy
            # 的下一次 generation 不会继续沿用被阻断的旧 catalog。
            previous_manager = self._plugin_runtime_manager
            if previous_manager is not None:
                previous_manager.clear_settings_environment()
            self._plugin_runtime_manager = PluginRuntimeManager(self._plugin_runtime_catalog)

    def _settings_environment_by_plugin(self) -> dict[str, dict[str, str]]:
        """从当前 Host/generation snapshot 构造按 Plugin 分组的 child-only overlay。"""
        if self._settings_snapshot.state != "loaded":
            return {}
        result: dict[str, dict[str, str]] = {}
        for binding in self._settings_bindings:
            if binding.plugin_id in self._settings_blocked_plugin_ids:
                continue
            value = self._settings_snapshot.value_for(binding.setting_id)
            if value is None:
                continue
            result.setdefault(binding.plugin_id, {})[binding.env_var] = value
        return result

    def _apply_settings_snapshot_to_children(self) -> None:
        """把 Settings 只绑定到 Qwen MCP/Hook/LSP，不改变 Host 全局环境。"""
        values_by_plugin = self._settings_environment_by_plugin()
        runtime_manager = self._plugin_runtime_manager
        if runtime_manager is not None:
            runtime_manager.set_settings_environment(values_by_plugin)
        self._restore_mcp_setting_overlays()
        self._plugin_mcp_servers = tuple(
            replace(
                server,
                env={
                    **dict(server.env),
                    **values_by_plugin.get(
                        server.source.removeprefix("plugin:"),
                        {},
                    ),
                },
            )
            if server.source.startswith("plugin:")
            else server
            for server in self._base_plugin_mcp_servers
            if not (
                server.source.startswith("plugin:")
                and server.source.removeprefix("plugin:")
                in self._settings_blocked_plugin_ids
            )
        )

    def _clear_settings_overlays(self) -> None:
        """清理 Host/generation child overlay，并恢复不含 secret 的 MCP base。"""
        runtime_manager = self._plugin_runtime_manager
        if runtime_manager is not None:
            runtime_manager.clear_settings_environment()
        self._restore_mcp_setting_overlays()
        self._plugin_mcp_servers = self._base_plugin_mcp_servers

    def _restore_mcp_setting_overlays(self) -> None:
        """恢复当前及仍可被外部引用的 MCP snapshot 到 immutable base env。

        Settings overlay 只属于 Host/generation 的 child execution。MCP generation
        替换后，旧 snapshot 可能仍被 lease、owner 或测试/诊断调用方引用；只清理
        `_plugin_mcp_servers` 会把旧对象中的 secret 留到 Host close。这里沿所有
        Host 持有的 snapshot/owner seam 收集配置对象，并按 `(source, name)` 找回
        不含 secret 的 base；找不到 base 时采用空 env 的 fail-closed 结果。
        """
        base_by_key = {
            (server.source, server.name): server
            for server in self._base_plugin_mcp_servers
        }
        servers: list[McpServerConfig] = []
        seen: set[int] = set()

        def add_servers(server_values: object) -> None:
            """只读取已持有的 snapshot，不枚举外部配置或 credential store。"""
            if server_values is None:
                return
            for server in server_values:
                if not isinstance(server, McpServerConfig) or id(server) in seen:
                    continue
                seen.add(id(server))
                servers.append(server)

        add_servers(self._plugin_mcp_servers)
        if self._mcp_snapshot is not None:
            add_servers(self._mcp_snapshot.servers)
        if self._mcp_manager is not None:
            try:
                add_servers(self._mcp_manager.snapshot.servers)
            except (AttributeError, RuntimeError):
                pass
        owners = [
            *self._retired_mcp_owners,
            *([self._mcp_owner] if self._mcp_owner is not None else []),
        ]
        for owner in owners:
            resource = getattr(owner, "resource", None)
            if resource is not None:
                try:
                    add_servers(resource.snapshot.servers)
                except (AttributeError, RuntimeError):
                    pass

        for server in servers:
            base = base_by_key.get((server.source, server.name))
            if base is None and not server.source.startswith("plugin:"):
                continue
            object.__setattr__(
                server,
                "env",
                MappingProxyType(dict(base.env)) if base is not None else MappingProxyType({}),
            )

    def _settings_binding_for_params(self, parsed: Any) -> SettingBinding:
        """按完整 immutable identity 选择当前 catalog 的唯一 declaration。"""
        summary = self._plugin_reauthorization_index.get(parsed.plugin_id)
        if summary is not None:
            raise PluginReauthorizationRequired(summary)
        for binding in self._settings_bindings:
            if (
                binding.plugin_id == parsed.plugin_id
                and binding.package_digest == parsed.package_digest
                and binding.declaration_digest == parsed.declaration_digest
                and binding.setting_key == parsed.setting_key
                and binding.env_var == parsed.env_var
            ):
                return binding
        raise SettingsError("SETTINGS_DECLARATION_STALE", field="declaration_digest")

    def _settings_store_for_scope(self, scope: str) -> SettingsStore:
        """选择当前 Host 已绑定的 user/workspace metadata store。"""
        if scope == "user":
            return self._settings_user_store
        if scope == "workspace":
            return self._settings_workspace_store
        raise SettingsError("SETTINGS_SCOPE_INVALID", field="scope")

    def _settings_rpc_error(self, exc: SettingsError) -> RpcError:
        """把 Settings 领域错误映射为稳定 JSON-RPC code/data，绝不携带 value。"""
        entry = ERROR_CODES.get(exc.code)
        rpc_code = int(entry["jsonrpc_code"]) if entry is not None else -32602
        return RpcError(rpc_code, exc.code, exc.redacted_data())

    async def _handle_settings_list(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """返回 live store 与当前 Host snapshot 的脱敏 Settings summary。"""
        try:
            parsed = SettingsListParams.model_validate(params)
            self._require_skills()
            # 管理读取沿用 initialize 时冻结的 catalog/generation binding；不在
            # list 中重新扫描插件，避免 list 与当前运行 snapshot 发生串线。
            result = self._settings_store_for_scope(parsed.scope).list(
                scope=parsed.scope,
                declarations=self._settings_bindings,
                snapshot=self._settings_snapshot,
            )
            return result
        except SettingsError as exc:
            raise self._settings_rpc_error(exc) from exc

    async def _handle_settings_set(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """以 Host 校验的 declaration 写入 credential，结果只对 next Host 生效。"""
        try:
            parsed = SettingsSetParams.model_validate(params)
            self._require_skills()
            binding = self._settings_binding_for_params(parsed)
            store = self._settings_store_for_scope(parsed.scope)
            mutation = store.set(
                scope=parsed.scope,
                plugin_id=binding.plugin_id,
                package_digest=binding.package_digest,
                declaration_digest=binding.declaration_digest,
                setting_key=binding.setting_key,
                env_var=binding.env_var,
                value=parsed.value,
                expected_store_revision=parsed.expected_store_revision,
                name=binding.declaration.name,
                description=binding.declaration.description,
                sensitive=binding.declaration.sensitive,
                required=False,
                consumer_scope="extension-wide",
            )
            listing = store.list(
                scope=parsed.scope,
                declarations=self._settings_bindings,
                snapshot=self._settings_snapshot,
            )
            summary = next(
                item for item in listing["settings"]
                if isinstance(item, dict) and item.get("setting_id") == binding.setting_id
            )
            mutation["store_revision"] = listing["store_revision"]
            mutation["runtime_snapshot"] = listing["runtime_snapshot"]
            mutation["summary"] = summary
            mutation["diagnostics"] = list(self._settings_diagnostics)
            return mutation
        except SettingsError as exc:
            raise self._settings_rpc_error(exc) from exc

    async def _handle_settings_remove(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """按 exact identity 写 tombstone 并删除精确 credential account。"""
        try:
            parsed = SettingsRemoveParams.model_validate(params)
            self._require_skills()
            binding = self._settings_binding_for_params(parsed)
            store = self._settings_store_for_scope(parsed.scope)
            mutation = store.remove(
                scope=parsed.scope,
                plugin_id=binding.plugin_id,
                package_digest=binding.package_digest,
                declaration_digest=binding.declaration_digest,
                setting_key=binding.setting_key,
                env_var=binding.env_var,
                expected_store_revision=parsed.expected_store_revision,
                name=binding.declaration.name,
                description=binding.declaration.description,
                sensitive=binding.declaration.sensitive,
            )
            listing = store.list(
                scope=parsed.scope,
                declarations=self._settings_bindings,
                snapshot=self._settings_snapshot,
            )
            summary = next(
                item for item in listing["settings"]
                if isinstance(item, dict) and item.get("setting_id") == binding.setting_id
            )
            mutation["store_revision"] = listing["store_revision"]
            mutation["runtime_snapshot"] = listing["runtime_snapshot"]
            mutation["summary"] = summary
            mutation["diagnostics"] = list(self._settings_diagnostics)
            return mutation
        except SettingsError as exc:
            raise self._settings_rpc_error(exc) from exc

    async def _handle_skills_inspect(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """返回一个 Skill 的安全元数据。"""
        self._reject_params(params, {"id"}, "skills.inspect")
        skill_id = params.get("id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise RpcError(-32602, "id must be a non-empty string")
        return (await self._refresh_skill_catalog()).inspect(skill_id)

    async def _handle_skills_set_enabled(self, params: dict[str, Any], _id: str) -> dict[str, Any]:
        """保存下一顶层 Run 生效的 Skill 启停偏好。"""
        self._reject_params(params, {"id", "enabled"}, "skills.set_enabled")
        skill_id = params.get("id")
        enabled = params.get("enabled")
        if not isinstance(skill_id, str) or not skill_id.strip() or not isinstance(enabled, bool):
            raise RpcError(-32602, "id and enabled are required")
        reservation = await self._reserve_agent_engine_snapshot()
        try:
            await self._refresh_skill_catalog_locked()
            return self._skill_catalog_manager.set_enabled(skill_id, enabled)
        finally:
            await reservation.release()

    async def _handle_skills_market_list(self, params: dict[str, Any], _id: str) -> list[dict[str, object]]:
        """列出已安装的企业市场 Provider 或其 catalog。"""
        self._reject_params(params, {"market"}, "skills.market.list")
        market = params.get("market")
        if market is not None and not isinstance(market, str):
            raise RpcError(-32602, "market must be a string")
        return await self._skill_catalog_manager.marketplace_catalog(market)

    async def _handle_skills_install(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """通过企业 Provider 安装 Skill；Provider 不存在时返回明确错误。"""
        self._reject_params(params, {"market", "name", "version"}, "skills.install")
        market, name, version = params.get("market"), params.get("name"), params.get("version")
        if not isinstance(market, str) or not isinstance(name, str) or (version is not None and not isinstance(version, str)):
            raise RpcError(-32602, "market and name are required strings")
        reservation = await self._reserve_agent_engine_snapshot()
        try:
            return await self._skill_catalog_manager.install(market, name, version)
        finally:
            await reservation.release()

    async def _handle_skills_update(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """通过企业 Provider 更新市场 Skill。"""
        return await self._handle_skills_install(params, _id)

    async def _handle_skills_remove(self, params: dict[str, Any], _id: str) -> dict[str, object]:
        """移除一个已安装市场 Skill。"""
        self._reject_params(params, {"id"}, "skills.remove")
        skill_id = params.get("id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise RpcError(-32602, "id must be a non-empty string")
        reservation = await self._reserve_agent_engine_snapshot()
        try:
            await self._refresh_skill_catalog_locked()
            return self._skill_catalog_manager.remove(skill_id)
        finally:
            await reservation.release()

    async def _handle_plugins_list(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """列出 Plugin registry 与 enabled catalog，不返回宿主文件路径。"""
        parsed = PluginsListParams.model_validate(params)
        await self._refresh_control_plane_catalog()
        return self._plugin_manager.list(include_disabled=parsed.include_disabled)

    async def _handle_plugins_inspect(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """返回一个安装 Plugin 的兼容性和 trust 摘要。"""
        parsed = PluginsInspectParams.model_validate(params)
        await self._refresh_control_plane_catalog()
        return self._plugin_manager.inspect(parsed.id)

    async def _handle_plugins_validate(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """离线校验本地目录或 zip，不修改 PluginStore。"""
        parsed = PluginsValidateParams.model_validate(params)
        return self._plugin_manager.validate(
            self._plugin_source_path(parsed.source),
            format=parsed.format,
        )

    async def _handle_plugins_install(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """copy-on-install 本地 Plugin，新记录始终为 disabled。"""
        parsed = PluginsInstallParams.model_validate(params)
        return self._plugin_manager.install(
            self._plugin_source_path(parsed.source),
            format=parsed.format,
        )

    async def _handle_plugins_set_enabled(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """按 capability fingerprint 显式启用或停用 Plugin。"""
        parsed = PluginsSetEnabledParams.model_validate(params)
        return self._plugin_manager.set_enabled(
            parsed.id,
            enabled=parsed.enabled,
            capability_fingerprint=parsed.capability_fingerprint,
        )

    async def _handle_plugins_remove(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """先按当前 Plugin identity 清理 Settings，再移除安装记录。"""
        parsed = PluginsRemoveParams.model_validate(params)
        try:
            return self._plugin_manager.remove(
                parsed.id,
                purge_data=parsed.purge_data,
                settings_cleanup=self._uninstall_plugin_settings,
            )
        except SettingsError as exc:
            raise self._settings_rpc_error(exc) from exc

    def _uninstall_plugin_settings(
        self,
        plugin: Any,
        plugin_registry_revision: int,
    ) -> dict[str, object]:
        """以 Plugin registry revision/package identity 绑定 Settings uninstall。"""
        # 卸载是管理面动作，必须按 registry 中的安装记录解析声明；停用或
        # trust 失效只应阻止 runtime，不能让已有 credential 逃过 purge。
        settings_result = self._plugin_manager.setting_bindings_for_uninstall(plugin)
        bindings = tuple(
            binding
            for binding in settings_result.bindings
            if binding.plugin_id == plugin.plugin_id
        )
        if not bindings and not settings_result.diagnostics:
            return {
                "operation": "uninstall",
                "plugin_id": plugin.plugin_id,
                "package_digest": plugin.package_digest,
                "plugin_registry_revision": plugin_registry_revision,
                "store_revision": self._settings_user_store.list(scope="user")["store_revision"],
                "removed": [],
                "partial": [],
                "diagnostics": list(settings_result.diagnostics),
            }
        if not self._settings_user_store._backend_is_available():  # noqa: SLF001 - same domain boundary
            raise SettingsError("SETTINGS_BACKEND_UNAVAILABLE", field="backend")
        user_binding = self._settings_user_store.user_binding_digest
        user_index = self._settings_user_store._read_index("user", user_binding)  # noqa: SLF001
        if user_index is None:
            return {
                "operation": "uninstall",
                "plugin_id": plugin.plugin_id,
                "package_digest": plugin.package_digest,
                "plugin_registry_revision": plugin_registry_revision,
                "store_revision": 0,
                "removed": [],
                "partial": [],
                "diagnostics": [],
            }
        try:
            result = self._settings_user_store.uninstall_plugin(
                plugin_id=plugin.plugin_id,
                package_digest=plugin.package_digest,
                expected_store_revision=user_index.revision,
                workspace_stores={
                    self._settings_workspace_store.workspace_binding_digest:
                    self._settings_workspace_store,
                },
            )
        except SettingsError as exc:
            if exc.code == "SETTINGS_RECORD_NOT_FOUND":
                return {
                    "operation": "uninstall",
                    "plugin_id": plugin.plugin_id,
                    "package_digest": plugin.package_digest,
                    "plugin_registry_revision": plugin_registry_revision,
                    "store_revision": user_index.revision,
                    "removed": [],
                    "partial": [],
                    "diagnostics": [],
                }
            raise
        result["plugin_id"] = plugin.plugin_id
        result["package_digest"] = plugin.package_digest
        result["plugin_registry_revision"] = plugin_registry_revision
        if settings_result.diagnostics:
            result["diagnostics"] = list(
                dict.fromkeys(
                    (
                        *result.get("diagnostics", []),
                        *settings_result.diagnostics,
                    )
                )
            )
        return result

    async def _handle_agents_list(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """列出当前启动快照中可派发的内置与 Plugin Agent，不返回 Prompt 正文。"""
        self._reject_params(params, set(), "agents.list")
        await self._refresh_control_plane_catalog()
        catalog = self._agent_catalog_for_control_plane()
        return {
            "snapshot_id": catalog.snapshot_id,
            "agents": catalog.list_agents(),
            "static_preview": self._plugin_manager.static_preview()["agents"],
            "diagnostics": [*self._plugin_diagnostics, *catalog.diagnostics],
        }

    async def _handle_agents_inspect(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """返回一个 Agent 的脱敏定义摘要。"""
        parsed = AgentsInspectParams.model_validate(params)
        await self._refresh_control_plane_catalog()
        return self._agent_catalog_for_control_plane().require_agent(parsed.id).summary()

    async def _handle_teams_list(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """列出固定与当前 Host 已确认生成的 TeamDefinition。"""
        self._reject_params(params, set(), "teams.list")
        await self._refresh_control_plane_catalog()
        self._agent_catalog_for_control_plane()
        return {
            "teams": [
                _team_definition_payload(definition)
                for definition in self._all_team_definitions()
            ],
            "diagnostics": list(self._plugin_diagnostics),
        }

    async def _handle_teams_inspect(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """按类型查看 TeamDefinition 或可恢复 TeamRun。"""
        parsed = TeamsInspectParams.model_validate(params)
        await self._refresh_control_plane_catalog()
        if parsed.kind == "definition":
            return _team_definition_payload(self._require_team_definition(parsed.id))
        persistence = await self._ensure_thread_persistence()
        run = await persistence.team_state_store().load(parsed.id)
        if run is None:
            raise TeamError("TEAM_RUN_NOT_FOUND")
        return _team_run_payload(run)

    async def _handle_teams_generate(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """由已验证 AgentDefinition 生成固定 fanout Team，并保存当前 Host 预览。"""
        parsed = TeamsGenerateParams.model_validate(params)
        await self._refresh_control_plane_catalog()
        if any(team.team_id == parsed.id for team in self._plugin_team_definitions):
            raise TeamError("TEAM_ID_CONFLICT")
        catalog = self._agent_catalog_for_control_plane()
        definition = generate_fanout_team(
            team_id=parsed.id,
            agents=catalog.agents,
            lead_agent_id=parsed.lead_agent_id,
            worker_agent_ids=tuple(parsed.worker_agent_ids),
            max_parallelism=parsed.max_parallelism,
        )
        existing = self._generated_team_definitions.get(definition.team_id)
        if existing is not None and existing != definition:
            raise TeamError("TEAM_ID_CONFLICT")
        self._generated_team_definitions[definition.team_id] = definition
        return _team_definition_payload(definition)

    async def _handle_teams_run(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """受理一个后台 TeamRun；成员只能来自启动期 Agent catalog。"""
        parsed = TeamsRunParams.model_validate(params)
        await self._refresh_control_plane_catalog()
        if parsed.run_id in self._active_team_tasks:
            raise TeamError("TEAM_RUN_BUSY")
        definition = self._require_team_definition(parsed.team_id)
        config = self._config
        if config is None or config.model_catalog is None:
            raise TeamError("TEAM_MODEL_CATALOG_REQUIRED")
        catalog = self._agent_catalog_for_control_plane()
        known_agents = {agent.agent_id for agent in catalog.agents}
        if any(task.agent_id not in known_agents for task in definition.tasks):
            raise TeamError("TEAM_AGENT_NOT_FOUND")

        persistence = await self._ensure_thread_persistence()
        store = persistence.team_state_store()
        existing = await store.load(parsed.run_id)
        parent_ref = (
            existing.parent_ref
            if existing is not None
            else ExecutionRef(
                thread_id=parsed.thread_id,
                run_id=parsed.run_id,
                execution_id=f"team-root-{parsed.run_id}",
            )
        )
        if (
            existing is not None
            and (
                existing.team_id != definition.team_id
                or existing.parent_ref.thread_id != parsed.thread_id
            )
        ):
            raise TeamError("TEAM_RUN_IDENTITY_CONFLICT")
        if existing is not None and existing.status.terminal:
            return {
                "team_id": definition.team_id,
                "run_id": parsed.run_id,
                "accepted": True,
            }

        binding = await self._resolve_execution_binding(parsed.thread_id, config)
        parent_spec = await self._resolve_agent_engine_spec(
            parsed.thread_id,
            config,
            binding,
        )
        delegation_policy = parent_spec.effective_policy.delegation
        if delegation_policy is None or not delegation_policy.enabled:
            raise TeamError("TEAM_DELEGATION_DISABLED")

        from harness_agent.runtime.agent_delegation import AgentDelegator

        registry = self._run_coordinator.execution_registry
        targets = await self._plugin_delegation_targets(parent_spec)
        target_ids = {target.agent_id for target in targets}
        if any(task.agent_id not in target_ids for task in definition.tasks):
            raise TeamError("TEAM_AGENT_UNAVAILABLE")
        await registry.accept(
            AgentExecutionBinding(
                ref=parent_ref,
                agent_id="team-coordinator",
                mode=ExecutionMode.MANAGED,
                depth=0,
                model=parent_spec.model_view,
                policy_fingerprint=parent_spec.effective_policy.fingerprint,
                engine_profile_key=parent_spec.runtime_profile.profile_key,
                definition_fingerprint=definition.team_id,
            )
        )
        await registry.start(parent_ref)
        if existing is None:
            await store.save(
                TeamRun(
                    run_id=parsed.run_id,
                    team_id=definition.team_id,
                    parent_ref=parent_ref,
                    status=TeamRunStatus.RUNNING,
                    tasks=tuple(
                        TeamTaskState(task.task_id)
                        for task in definition.tasks
                    ),
                )
            )
        token = RunCancellationToken()
        coordinator = TeamCoordinator(
            AgentDelegator(
                registry,
                targets=targets,
                blocked_target_messages=self._blocked_plugin_agent_messages(),
            ),
            store=store,
        )
        task = asyncio.create_task(
            self._execute_team_run(
                coordinator=coordinator,
                definition=definition,
                run_id=parsed.run_id,
                parent_ref=parent_ref,
                request=parsed.request,
                delegation_policy=delegation_policy,
                cancellation_token=token,
            ),
            name=f"harness-team-{definition.team_id}-{parsed.run_id}",
        )
        self._active_team_tokens[parsed.run_id] = token
        self._active_team_tasks[parsed.run_id] = task
        return {
            "team_id": definition.team_id,
            "run_id": parsed.run_id,
            "accepted": True,
        }

    async def _handle_teams_cancel(
        self,
        params: dict[str, Any],
        _id: str,
    ) -> dict[str, object]:
        """协作式取消活动 Team，不影响其他 Run。"""
        parsed = TeamsCancelParams.model_validate(params)
        token = self._active_team_tokens.get(parsed.run_id)
        if token is None:
            persistence = await self._ensure_thread_persistence()
            existing = await persistence.team_state_store().load(parsed.run_id)
            if existing is None:
                raise TeamError("TEAM_RUN_NOT_FOUND")
            return {"run_id": parsed.run_id, "cancelled": False}
        token.cancel()
        return {"run_id": parsed.run_id, "cancelled": True}

    def _plugin_source_path(self, source: str) -> Path:
        """把相对安装来源解释为 Host workspace 下的显式本地路径。"""
        path = Path(source).expanduser()
        return path if path.is_absolute() else self._workspace / path

    def _agent_catalog_for_control_plane(self) -> AgentCatalog:
        """返回初始化时固定的 Agent catalog，配置缺失时不构造半成品响应。"""
        config = self._config
        if config is None or config.model_catalog is None:
            raise AgentCatalogError("AGENT_MODEL_CATALOG_REQUIRED")
        return self._require_agent_catalog(config)

    def _all_team_definitions(self) -> tuple[TeamDefinition, ...]:
        """合并 Plugin 固定 Team 与当前 Host 的生成预览，拒绝 ID 遮蔽。"""
        definitions: dict[str, TeamDefinition] = {}
        for definition in self._plugin_team_definitions:
            if definition.team_id in definitions:
                raise TeamError("TEAM_ID_CONFLICT")
            definitions[definition.team_id] = definition
        for team_id, definition in self._generated_team_definitions.items():
            if team_id in definitions:
                raise TeamError("TEAM_ID_CONFLICT")
            definitions[team_id] = definition
        return tuple(definitions[key] for key in sorted(definitions))

    def _require_team_definition(self, team_id: str) -> TeamDefinition:
        """按稳定 ID 读取 TeamDefinition。"""
        summary = self._reauthorization_for_identity(team_id)
        if summary is None:
            summary = next(
                (
                    candidate
                    for candidate in self._plugin_reauthorization_index.values()
                    if team_id in candidate.team_ids
                ),
                None,
            )
        if summary is not None:
            raise TeamError(
                "PLUGIN_REAUTHORIZATION_REQUIRED",
                _plugin_reauthorization_message(summary),
                details=_plugin_reauthorization_error_data(summary)["details"],
            )
        for definition in self._all_team_definitions():
            if definition.team_id == team_id:
                return definition
        raise TeamError("TEAM_DEFINITION_NOT_FOUND")

    async def _execute_team_run(
        self,
        *,
        coordinator: TeamCoordinator,
        definition: TeamDefinition,
        run_id: str,
        parent_ref: ExecutionRef,
        request: str,
        delegation_policy: DelegationPolicy,
        cancellation_token: RunCancellationToken,
    ) -> None:
        """后台执行 Team，并把协调根 execution 收敛到同一个终态。"""
        registry = self._run_coordinator.execution_registry
        terminal_status = ExecutionStatus.FAILED
        try:
            result = await coordinator.run(
                definition,
                run_id=run_id,
                parent_ref=parent_ref,
                request=request,
                delegation_policy=delegation_policy,
                cancellation_token=cancellation_token,
            )
            terminal_status = {
                TeamRunStatus.COMPLETED: ExecutionStatus.COMPLETED,
                TeamRunStatus.CANCELLED: ExecutionStatus.CANCELLED,
                TeamRunStatus.FAILED: ExecutionStatus.FAILED,
            }[result.status]
        except Exception:
            logger.exception("Team run %s failed outside coordinator state machine", run_id)
            await registry.cancel_run(parent_ref)
        finally:
            current = await registry.get(parent_ref)
            if current is not None and not current.status.terminal:
                await registry.finalize(parent_ref, status=terminal_status)
            try:
                await registry.seal_run(parent_ref)
                await registry.discard_run(parent_ref)
            except Exception:
                logger.exception("Team run %s execution registry cleanup failed", run_id)
            self._active_team_tasks.pop(run_id, None)
            self._active_team_tokens.pop(run_id, None)

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
            "CONNECTION_RUN_BUSY": -32000,
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
        lease: AgentEngineLease | None = None
        try:
            lease, engine = await self._acquire_default_agent_engine(
                run.thread_id,
                run.resolved_execution_binding,
                profile=run.resolved_agent_engine_profile,
                snapshot_reservation=reservation,
            )
            if engine is None:
                return None
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
        except BaseException:
            if lease is not None:
                await self._release_agent_engine_lease(lease)
            raise
        finally:
            if reservation is not None:
                await reservation.release()

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
            registry = await self._refresh_skill_catalog_locked()
            profile = await self._resolve_agent_engine_profile(
                thread_id,
                config,
                resolved_binding,
                skill_registry=registry,
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
        persistence: ThreadPersistence | None = None,
        skill_registry: SkillRegistry,
        approval_mode: ApprovalMode | None = None,
    ) -> ResolvedAgentSpec:
        """截取一次角色解析快照，Profile、审批策略和 builder 都从它派生。"""
        persistence = persistence or await self._ensure_thread_persistence()
        await self._ensure_mcp_connected()
        async with self._mcp_state_lock:
            mcp_snapshot = self._mcp_snapshot or build_mcp_snapshot([], "missing")
            mcp_tools = tuple(self._mcp_manager.get_tools()) if self._mcp_manager else ()
        execution = (
            replace(config.execution, approval_mode=approval_mode)
            if approval_mode is not None
            else config.execution
        )
        mcp_owner = self._mcp_owner
        agent_catalog = (
            self._require_agent_catalog(config)
            if config.model_catalog is not None
            else None
        )
        spec = resolve_builtin_main_agent_spec(
            project_fingerprint=persistence.project_fingerprint,
            workspace=self._workspace,
            binding=resolved_binding,
            execution=execution,
            skill_registry=skill_registry,
            mcp_snapshot=mcp_snapshot,
            mcp_tools=mcp_tools,
            interactive="question" in self._connection_handles(),
            pinned=config.agent_engine_pool.pin_default_profile,
            delegation_agent_ids=(
                tuple(definition.agent_id for definition in agent_catalog.agents)
                if agent_catalog is not None
                else ()
            ),
        )
        profile = spec.runtime_profile
        if mcp_owner is not None:
            self._profile_mcp_owners.setdefault(profile.profile_key, mcp_owner)
        await persistence.persist_agent_engine_profile(profile)
        # 同一 Key 保留第一次解析出的对象，保证 Pool builder 与 RunContext
        # 取回的是同一个快照，而不是后续请求重新拼出的近似对象。
        return self._resolved_agent_specs.setdefault(profile.profile_key, spec)

    async def _resolve_agent_engine_profile(
        self,
        thread_id: str,
        config: Za38Config,
        resolved_binding: ResolvedExecutionBinding,
        *,
        skill_registry: SkillRegistry,
    ) -> AgentEngineProfile:
        """兼容现有调用方，只返回由 ResolvedAgentSpec 生成的 Profile。"""
        return (
            await self._resolve_agent_engine_spec(
                thread_id,
                config,
                resolved_binding,
                skill_registry=skill_registry,
            )
        ).runtime_profile

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
        persistence: ThreadPersistence | None = None,
        requested_primary_profile: str | None = None,
    ) -> ResolvedExecutionBinding:
        """读取 Thread 状态并通过 execution_binding module 解析根模型。"""
        persistence = persistence or await self._ensure_thread_persistence()
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
                diagnostic_log=self._diagnostic_log,
            )
        return self._agent_engine_pool

    def _ensure_workspace_execution_resources(self) -> Any:
        """在首次默认构图时加载并创建 workspace 资源 owner。"""
        if self._workspace_execution_resources is None:
            from harness_agent.runtime.execution import WorkspaceExecutionResourcePool

            self._workspace_execution_resources = WorkspaceExecutionResourcePool()
        return self._workspace_execution_resources

    async def _provide_compose_services(self, run: Any) -> EngineDriverServices | None:
        """按 Run 上下文组装 Work Item engine 依赖；配置缺失时返回 None。"""
        config = self._config
        if config is None or config.model_catalog is None:
            return None
        try:
            pool = self._ensure_agent_engine_pool(config)
        except Exception:
            logger.exception("Compose services unavailable")
            return None
        from harness_agent.compose.engine_services import EngineDriverServices
        from harness_agent.compose.verification import ManagedVerificationPort
        from harness_agent.policy.permission_rules import load_rules, merge_rules

        def compose_rules() -> list[Any]:
            """验证命令与 Agent 工具看到同一份规则（session + 持久化）。"""
            persisted = load_rules(project_dir=self._workspace)
            persisted["session"] = self._run_coordinator.session_rules
            return merge_rules(persisted)

        async def cleanup_stage_execution(checkpoint_thread_id: str) -> None:
            """同时清理 stage 的 SQLite checkpoint 与进程内 Snapshot scope。"""
            try:
                await persistence.delete_execution_checkpoint(checkpoint_thread_id)
            finally:
                self._snapshot_store.close_thread(checkpoint_thread_id)

        return EngineDriverServices(
            stage_agent=ManagedStageAgentPort(
                registry=self._run_coordinator.execution_registry,
                pool=pool,
                resolve_spec=self._resolve_compose_stage_spec,
                config_home=self._config_home,
                workspace=self._workspace,
                checkpoint_cleanup=cleanup_stage_execution,
            ),
            parent_ref=run.root_execution_ref,
            workspace_root=str(self._workspace),
            verification=ManagedVerificationPort(
                pool=self._ensure_workspace_execution_resources(),
                settings=config.execution,
                workspace=self._workspace,
                rules_provider=compose_rules,
                rwlock=self._tool_concurrency_lock,
                now_ms=lambda: int(time.time() * 1000),
            ),
            # 组装发生在 adapter 执行前，run.agent_engine_profile_key 尚未由
            # runtime 获取填充；直接使用受理阶段解析出的 AgentEngineProfile key，
            # 与 _resolved_agent_specs 的缓存键一致。
            profile_key=(
                run.resolved_agent_engine_profile.profile_key
                if run.resolved_agent_engine_profile is not None
                else (run.agent_engine_profile_key or "")
            ),
            cancellation_token=run.cancellation_token,
            diagnostic_log=getattr(run, "diagnostic_log", None),
        )

    def _resolve_compose_stage_spec(
        self,
        profile_key: str,
        *,
        headless: bool = False,
        readonly: bool = False,
        planning: bool = False,
    ) -> ResolvedAgentSpec | None:
        """返回按 profile key 缓存的主 Agent spec；Compose 复用同一可信 spec。

        - headless：stage 图关闭 ask_user，提问只能走 workflow typed 通道；
        - readonly：Reviewer 只读能力视图；
        - planning：Understand/Plan 只读能力视图。
        两种派生 spec 都注册独立 profile key，让 AgentEnginePool 构建独立引擎。
        """
        spec = self._resolved_agent_specs.get(profile_key)
        if spec is None:
            return None
        if planning:
            from harness_agent.runtime.agent_spec import (
                restrict_spec_to_read_only_stage,
            )

            restricted = restrict_spec_to_read_only_stage(spec)
            self._resolved_agent_specs.setdefault(
                restricted.runtime_profile.profile_key, restricted
            )
            return restricted
        if headless and not readonly:
            from harness_agent.runtime.agent_spec import (
                restrict_spec_to_headless_stage,
            )

            derived = restrict_spec_to_headless_stage(spec)
            self._resolved_agent_specs.setdefault(
                derived.runtime_profile.profile_key, derived
            )
            return derived
        if readonly:
            from harness_agent.runtime.agent_spec import restrict_spec_to_read_only

            restricted = restrict_spec_to_read_only(spec)
            self._resolved_agent_specs.setdefault(
                restricted.runtime_profile.profile_key, restricted
            )
            return restricted
        return spec

    async def _plugin_delegation_targets(
        self,
        parent_spec: ResolvedAgentSpec,
    ) -> tuple[Any, ...]:
        """把 Plugin Agent spec 注册为复用 AgentEnginePool 的 Managed target。"""
        if getattr(parent_spec, "agent_id", "main") != "main":
            return ()
        config = self._config
        if config is None or config.model_catalog is None:
            return ()
        from harness_agent.diagnostic_log.runtime import bind_execution_log
        from harness_agent.runtime.agent_delegation import (
            AgentDelegationError,
            DelegationTarget,
            child_execution_ref,
            current_delegation_call,
        )
        from harness_agent.runtime.managed_agent_executor import (
            FailClosedManagedObserver,
            ManagedAgentExecutionError,
            ManagedAgentExecutor,
            ManagedAgentRequest,
            acquire_pooled_agent_runtime,
        )

        catalog = self._require_agent_catalog(config)
        persistence = await self._ensure_thread_persistence()
        registry = self._require_skills()
        async with self._mcp_state_lock:
            mcp_snapshot = self._mcp_snapshot or build_mcp_snapshot([], "missing")
            mcp_tools = tuple(self._mcp_manager.get_tools()) if self._mcp_manager else ()
            mcp_owner = self._mcp_owner
        pool = self._ensure_agent_engine_pool(config)
        targets: list[DelegationTarget] = []
        for definition in catalog.agents:
            try:
                child_spec = resolve_plugin_agent_spec(
                    definition=definition,
                    catalog=catalog,
                    parent_policy=parent_spec.effective_policy,
                    model_catalog=config.model_catalog,
                    project_fingerprint=persistence.project_fingerprint,
                    workspace=self._workspace,
                    execution=config.execution,
                    skill_registry=registry,
                    mcp_snapshot=mcp_snapshot,
                    mcp_tools=mcp_tools,
                    interactive=False,
                    inherited_model_profile_id=parent_spec.model_profile_id,
                )
            except (AgentCatalogError, ConfigError, ValueError) as exc:
                logger.warning(
                    "Plugin Agent %s disabled during resolution: %s",
                    definition.agent_id,
                    exc,
                )
                continue
            child_profile = child_spec.runtime_profile
            self._resolved_agent_specs.setdefault(child_profile.profile_key, child_spec)
            if mcp_owner is not None:
                self._profile_mcp_owners.setdefault(child_profile.profile_key, mcp_owner)
            await persistence.persist_agent_engine_profile(child_profile)

            async def invoke(
                command: Any,
                *,
                resolved: ResolvedAgentSpec = child_spec,
                profile: AgentEngineProfile = child_profile,
                plugin_source: str = definition.source,
            ) -> Mapping[str, Any]:
                """构造 capture_only request，并由统一 executor 运行 Plugin Agent。"""
                from harness_agent.plugins.runtime import (
                    SubagentStopController,
                    SubagentStopError,
                    SubagentStopRequest,
                )

                child_ref = child_execution_ref(command)
                checkpoint_thread_id = child_ref.checkpoint_thread_id(
                    resolved.project_fingerprint
                )
                parent_log = None
                try:
                    parent_log = getattr(
                        current_delegation_call().run_context, "diagnostic_log", None
                    )
                except AgentDelegationError:
                    parent_log = None
                child_log = bind_execution_log(
                    parent_log,
                    thread_id=child_ref.thread_id,
                    run_id=child_ref.run_id,
                    execution_id=child_ref.execution_id,
                    parent_execution_id=child_ref.parent_execution_id,
                    agent_id=resolved.agent_id,
                )
                context_snapshot = ContextLifecycle(
                    resolved.workspace,
                    home=self._config_home,
                ).prepare(
                    thread_id=child_ref.thread_id,
                    spec=resolved,
                    stable_reference_blocks=self._plugin_context_blocks_by_source.get(
                        plugin_source,
                        (),
                    ),
                )
                context = RunContext(
                    thread_id=child_ref.thread_id,
                    run_id=child_ref.run_id,
                    context_snapshot=context_snapshot,
                    skill_registry=resolved.skill_registry,
                    approval_mode=(
                        resolved.effective_policy.approval_mode
                        or resolved.execution.approval_mode
                    ),
                    profile_key=resolved.runtime_profile.profile_key,
                    checkpoint_thread_id=checkpoint_thread_id,
                    execution_id=child_ref.execution_id,
                    parent_execution_id=child_ref.parent_execution_id,
                    agent_id=resolved.agent_id,
                    execution_mode=ExecutionMode.MANAGED,
                    cancellation_token=command.cancellation_token,
                    delegation_policy=resolved.effective_policy.delegation,
                    workspace_root_registry=(
                        self._workspace_root_registry.readonly_view()
                        if self._workspace_root_registry is not None
                        else None
                    ),
                    snapshot_store=self._snapshot_store,
                    # Deferred reveal 属于本次 child execution；不能用父
                    # Thread 的 Host store，否则 sibling 会继承已 reveal 工具。
                    deferred_tool_store=ThreadDeferredToolStore(),
                    diagnostic_log=child_log,
                )
                checkpoint_namespace = child_ref.checkpoint_namespace(
                    resolved.project_fingerprint
                )
                hook_runtime = self._plugin_runtime_manager

                def clear_child_process_state() -> None:
                    """释放 child 的 Hook feedback 与进程内 Snapshot scope。"""
                    if hook_runtime is not None:
                        hook_runtime.clear_execution_context(context)
                    self._snapshot_store.close_thread(checkpoint_thread_id)

                async def acquire_runtime():
                    """把 Plugin Profile 的 pool lease 收敛到 Managed executor。"""
                    return await acquire_pooled_agent_runtime(
                        pool=pool,
                        profile=profile,
                        run_context=context,
                        graph_config=lambda namespace: {
                            "configurable": {
                                "thread_id": checkpoint_thread_id,
                                "checkpoint_ns": namespace,
                            }
                        },
                        checkpoint_cleanup=(
                            lambda: persistence.delete_execution_checkpoint(
                                checkpoint_thread_id
                            )
                        ),
                        on_release=clear_child_process_state,
                    )

                snapshot_id = getattr(resolved.skill_registry, "snapshot_id", None)
                hook_source_id = (
                    plugin_source.removeprefix("plugin:")
                    if plugin_source.startswith("plugin:")
                    else ""
                )
                matched_hook_failure = next(
                    (
                        failure
                        for failure in self._plugin_runtime_catalog.hook_failures
                        if failure.event == "SubagentStop"
                        and failure.matches(resolved.agent_id)
                        and (failure.source_id or failure.plugin_id) == hook_source_id
                    ),
                    None,
                )
                has_subagent_stop = bool(
                    matched_hook_failure is not None
                    or (
                        hook_runtime is not None
                        and any(
                            definition.event == "SubagentStop"
                            and definition.matches(resolved.agent_id)
                            and (definition.source_id or definition.plugin_id)
                            == hook_source_id
                            for definition in self._plugin_runtime_catalog.hooks
                        )
                    )
                )
                async def unavailable_hook_runner(
                    *_args: object,
                    **_kwargs: object,
                ) -> tuple[object, ...]:
                    """构造失败时的占位 runner；failure_code 会先行终止 gate。"""
                    return ()

                stop_controller = (
                    SubagentStopController(
                        hook_runner=(
                            hook_runtime.hooks.run
                            if hook_runtime is not None
                            else unavailable_hook_runner
                        ),
                        interaction_port=lambda interaction: self._run_coordinator.request_child_interaction(
                            RunRef(child_ref.thread_id, child_ref.run_id),
                            interaction,
                        ),
                        failure_code=(
                            matched_hook_failure.code
                            if matched_hook_failure is not None
                            else None
                        ),
                        diagnostic_log=child_log,
                    )
                    if has_subagent_stop
                    else None
                )

                async def final_output_gate(final: Any) -> Any:
                    """在同一 child checkpoint 前运行 Qwen SubagentStop。"""
                    if stop_controller is None:
                        from harness_agent.runtime.managed_agent_executor import (
                            FinalOutputGateDecision,
                        )

                        return FinalOutputGateDecision(action="allow")
                    stop_request = SubagentStopRequest(
                        plugin_id=hook_source_id,
                        agent_id=resolved.agent_id,
                        agent_type="qwen-code",
                        last_output=final.final_content,
                        workspace="/.harness/workspace",
                        stop_hook_active=stop_controller.block_count > 0,
                        execution_id=child_ref.execution_id,
                        parent_execution_id=child_ref.parent_execution_id,
                        checkpoint_namespace=checkpoint_namespace,
                        is_cancelled=lambda: command.cancellation_token.cancelled,
                    )
                    return await stop_controller.evaluate(stop_request)

                managed_request = ManagedAgentRequest(
                    execution_ref=child_ref.execution_id,
                    parent_execution_ref=child_ref.parent_execution_id,
                    run_id=child_ref.run_id,
                    input=command.task,
                    checkpoint_namespace=checkpoint_namespace,
                    output_policy="capture_only",
                    runtime_provider=acquire_runtime,
                    is_cancelled=lambda: command.cancellation_token.cancelled,
                    idempotency_key=command.idempotency_key,
                    agent_spec=resolved,
                    interaction_policy=resolved.effective_policy,
                    timeout_seconds=command.timeout_seconds,
                    required_skill_snapshot_ids=(snapshot_id,)
                    if isinstance(snapshot_id, str) and snapshot_id
                    else (),
                    diagnostic_log=child_log,
                    final_output_gate=final_output_gate if stop_controller is not None else None,
                )
                try:
                    result = await ManagedAgentExecutor().execute(
                        managed_request,
                        FailClosedManagedObserver(),
                    )
                except ManagedAgentExecutionError as exc:
                    if exc.code == "RUN_CANCELLED":
                        raise asyncio.CancelledError from exc
                    raise AgentDelegationError(
                        "PLUGIN_AGENT_EXECUTION_FAILED", exc.code
                    ) from exc
                except SubagentStopError as exc:
                    if command.cancellation_token.cancelled:
                        raise asyncio.CancelledError from exc
                    raise AgentDelegationError(exc.code) from exc
                output: dict[str, object] = {"final": result.final_content}
                if result.warning:
                    output["warning"] = result.warning
                return output

            targets.append(
                DelegationTarget(
                    agent_id=definition.agent_id,
                    mode=ExecutionMode.MANAGED,
                    runner=invoke,
                    description=definition.description or definition.purpose,
                    model=child_spec.model_view,
                    policy_fingerprint=child_spec.effective_policy.fingerprint,
                    engine_profile_key=child_profile.profile_key,
                    definition_fingerprint=definition.fingerprint,
                )
            )
        return tuple(targets)

    async def _build_default_agent_engine(self, profile: AgentEngineProfile) -> AgentEngine:
        """按 Profile key 取回同一 ResolvedAgentSpec，再构建共享图。"""
        await self._ensure_plugin_runtime_started()
        spec = self._resolved_agent_specs.get(profile.profile_key)
        if spec is None:
            raise RuntimeError("RUNTIME_RESOLVED_AGENT_SPEC_MISSING")
        if profile.profile_key != spec.runtime_profile.profile_key:
            raise RuntimeError("RUNTIME_PROFILE_SPEC_MISMATCH")
        if profile.mcp_config_fingerprint != spec.mcp_snapshot.digest:
            raise RuntimeError("RUNTIME_MCP_SNAPSHOT_MISMATCH")
        profile_skill_fingerprint = getattr(profile, "skill_catalog_fingerprint", None)
        expected_skill_fingerprint = skill_catalog_fingerprint(
            spec.skill_registry,
            view_fingerprint=spec.skill_view_fingerprint,
        )
        if (
            profile_skill_fingerprint is not None
            and profile_skill_fingerprint != expected_skill_fingerprint
        ):
            raise RuntimeError("RUNTIME_SKILL_SNAPSHOT_MISMATCH")
        from harness_agent.runtime.agent import create_harness_agent
        from harness_agent.threads.context_window import ContextWindowMiddleware
        from harness_agent.extensions.providers.harness_gateway import create_openai_compatible_model
        from harness_agent.threads.runtime_state import RuntimeStateRehydrator

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
            # `spec.tools` 是已经过父/Plugin Policy 交集的 immutable MCP 工具视图。
            # 空视图不需要 MCP manager：Plugin Agent 未声明 MCP 时没有对应的
            # manager resource，不能拿空子 snapshot 去 acquire 一个不存在的 key。
            # 非空视图借用当前完整 Host resource，但只投影已授权的工具；任何
            # snapshot 或工具缺失都 fail closed，不能把完整 manager 工具泄漏给 child。
            resolved_mcp_names = tuple(
                str(getattr(tool, "name", ""))
                if not isinstance(tool, dict)
                else str(tool.get("name", ""))
                for tool in spec.tools
            )
            if any(not name for name in resolved_mcp_names) or len(
                set(resolved_mcp_names)
            ) != len(resolved_mcp_names):
                raise RuntimeError("RUNTIME_MCP_TOOL_VIEW_INVALID")
            capability_mcp_names = getattr(spec.capability_view, "mcp_tool_names", None)
            if capability_mcp_names is not None and set(capability_mcp_names) != set(
                resolved_mcp_names
            ):
                raise RuntimeError("RUNTIME_MCP_TOOL_VIEW_MISMATCH")
            mcp_tools: list[Any] = []
            if resolved_mcp_names:
                if not spec.mcp_snapshot.servers:
                    raise RuntimeError("RUNTIME_MCP_TOOL_VIEW_MISMATCH")
                if self._mcp_manager is None:
                    raise RuntimeError("RUNTIME_MCP_MANAGER_REQUIRED")
                current_snapshot = self._mcp_manager.snapshot
                current_servers = {
                    server.name: server for server in current_snapshot.servers
                }
                if any(
                    current_servers.get(server.name) != server
                    for server in spec.mcp_snapshot.servers
                ):
                    raise RuntimeError("MCP_RESOURCE_SNAPSHOT_UNAVAILABLE")
                # 子 Profile 的 digest 是过滤视图的 digest，manager 只注册完整
                # Host snapshot；借用完整 resource 后再按 canonical tool name
                # 过滤，lease 仍保证其底层连接在 Engine 生命周期内有效。
                mcp_lease = await self._mcp_manager.acquire(current_snapshot)
                allowed_mcp_names = set(resolved_mcp_names)
                available_tools = tuple(mcp_lease.value.tools)
                mcp_tools = [
                    tool
                    for tool in available_tools
                    if str(getattr(tool, "name", "")) in allowed_mcp_names
                ]
                if {
                    str(getattr(tool, "name", "")) for tool in mcp_tools
                } != allowed_mcp_names or len(mcp_tools) != len(allowed_mcp_names):
                    raise RuntimeError("RUNTIME_MCP_TOOL_VIEW_UNAVAILABLE")
            execution_context = workspace_lease.value
            delegation_targets = await self._plugin_delegation_targets(spec)
            model = create_openai_compatible_model(
                model_settings,
                async_client=provider_lease.value,
            )

            async def runtime_state_provider(
                thread_id: str,
                run_context: RunContext | None,
                messages: list[Any] | tuple[Any, ...],
            ) -> Any:
                """每次压缩从真实 LangGraph channel 重新读取结构化运行态。"""
                graph_state = await persistence.load_langgraph_state(thread_id)
                return RuntimeStateRehydrator.capture(
                    graph_state,
                    run_context,
                    messages,
                )

            context_compactor = ContextWindowMiddleware(
                model,
                context_window_tokens=model_settings.context_window_tokens,
                thread_persistence=persistence,
                updates=self._context_updates,
                runtime_state_provider=runtime_state_provider,
            )

            def _get_current_rules() -> list[PermissionRule]:
                """获取当前会话的所有规则（session 内存 + project/user/system 持久化）。"""
                from harness_agent.policy.permission_rules import load_rules, merge_rules

                persisted = load_rules(project_dir=self._workspace)
                persisted["session"] = self._run_coordinator.session_rules
                return merge_rules(persisted)

            approval_mode = spec.effective_policy.approval_mode or spec.execution.approval_mode
            classifier = self._resolve_approval_classifier(
                approval_mode,
                getattr(spec.execution, "approval_classifier", None),
                model,
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
                approval_mode=approval_mode,
                classifier=classifier,
                execution_context=execution_context,
                skill_registry=spec.skill_registry,
                checkpointer=checkpointer,
                thread_persistence=persistence,
                context_updates=self._context_updates,
                context_middleware=context_compactor,
                context_window_tokens=model_settings.context_window_tokens,
                shared_engine=True,
                concurrency_lock=self._tool_concurrency_lock,
                capability_view=spec.capability_view,
                execution_registry=self._run_coordinator.execution_registry,
                delegation_model=spec.model_view,
                delegation_targets=delegation_targets,
                blocked_target_messages=self._blocked_plugin_agent_messages(),
                plugin_runtime=self._plugin_runtime_manager,
                workspace_root_registry=self._workspace_root_registry,
                rules_provider=_get_current_rules,
                defer_tools=(
                    self._config.tools.defer if self._config is not None else "auto"
                ),
                snapshot_store=self._snapshot_store,
                file_tool_metrics=self._file_tool_metrics,
            )
            self._agent_engine_artifacts[profile.profile_key] = _AgentEngineArtifacts(
                execution_context=execution_context,
                context_compactor=context_compactor,
                mcp_lease=mcp_lease,
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

    def _build_approval_classifier(self, profile_id: str) -> Any:
        """为 AUTO 模式构建 LLM 安全分类器；不可用时返回 None 降级为人工确认。

        profile 不存在或 API Key 缺失只记录警告并优雅降级，
        不让分类器配置错误阻断 Agent 引擎构建。
        """
        from harness_agent.extensions.providers.harness_gateway import create_openai_compatible_model
        from harness_agent.policy.classifier import SafetyClassifier

        config = self._config
        if config is None or config.model_catalog is None:
            logger.warning(
                "approval classifier profile %s unavailable: model catalog missing", profile_id
            )
            return None
        try:
            profile = config.model_catalog.require_profile(profile_id)
        except ConfigError:
            logger.warning(
                "approval classifier profile %s not found; falling back to manual approval",
                profile_id,
            )
            return None
        settings = profile.settings
        if settings.api_key_source() == "missing":
            logger.warning(
                "approval classifier profile %s has no API key; falling back to manual approval",
                profile_id,
            )
            return None
        # 分类调用发生在工具审批路径上，使用更短超时并禁用重试，避免慢网关放大工具等待；
        # replace 需显式回传保存在 InitVar 中的 TOML 降级密钥，否则替换后密钥会丢失。
        classifier_settings = replace(
            settings,
            api_key=settings._api_key,
            timeout_seconds=min(settings.timeout_seconds, 10.0),
            max_retries=0,
        )
        model = create_openai_compatible_model(classifier_settings)
        return SafetyClassifier(model)

    def _resolve_approval_classifier(
        self,
        approval_mode: str,
        classifier_profile_id: str | None,
        model: Any,
    ) -> Any:
        """为 AUTO 模式解析 LLM 安全分类器；未配专用 profile 或 profile 不可用时回退到主模型。"""
        if approval_mode != "auto":
            return None
        classifier = None
        if classifier_profile_id:
            classifier = self._build_approval_classifier(classifier_profile_id)
        if classifier is None and model is not None:
            from harness_agent.policy.classifier import SafetyClassifier

            classifier = SafetyClassifier(model)
        return classifier

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
        registry = run.preparation.skill_registry
        if registry is None or registry is not spec.skill_registry:
            raise RuntimeError("RUN_SKILL_SNAPSHOT_SPEC_MISMATCH")
        if snapshot.skill_snapshot_id != registry.snapshot_id:
            raise RuntimeError("RUN_CONTEXT_SKILL_SNAPSHOT_MISMATCH")
        from harness_agent.threads.context_pressure import ModelCallLifecycle

        adapter = ProtocolInteractionAdapter(self)

        async def interaction_port(spec: Any) -> Any:
            """child HITL 复用 owner Connection 的 reverse Interaction。"""
            return await adapter.request(run.owner, run.ref, spec)

        def event_port(
            event_type: str,
            payload: Mapping[str, object],
            execution_id: str | None,
            parent_execution_id: str | None,
            agent_id: str | None,
        ) -> None:
            """child 过程事件走父 Run 同一 sequence，信封带 child 身份。"""
            self._run_coordinator.emit_run_event(
                run,
                event_type,
                dict(payload),
                execution_id=execution_id,
                parent_execution_id=parent_execution_id,
                agent_id=agent_id,
            )

        def record_approval(tool_name: str, tool_args: dict[str, Any], decision: str) -> None:
            """把 child 审批决定记入父 Run 同一套 session/project 规则。"""
            self._run_coordinator._record_approval_rule(tool_name, tool_args, decision)

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
            skill_registry=registry,
            delegation_policy=spec.effective_policy.delegation,
            snapshot_store=self._snapshot_store,
            deferred_tool_store=self._deferred_tool_store,
            approval_presentations=run.approval_presentations,
            workspace_root_registry=self._workspace_root_registry,
            interaction_port=interaction_port,
            event_port=event_port,
            record_approval=record_approval,
            diagnostic_log=run.diagnostic_log,
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
            validate_interaction_result(interaction_method(spec.type), result)
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
        projection: Any | None = None,
        run_context_snapshot: Any | None = None,
        runtime_state: Any | None = None,
        current_execution_policy: RuntimeExecutionPolicy | None = None,
    ) -> dict[str, object]:
        """使用已租用 AgentEngine 的 compactor 提交投影并刷新缓存。"""
        from harness_agent.threads.context_compaction import CompressionRequest, CompressionResult
        from harness_agent.threads.context_projection import ModelProjection
        from harness_agent.threads.context_window import ContextUpdate

        typed_service = getattr(middleware, "compactor", None)
        if typed_service is None:
            raise RuntimeError("CONTEXT_COMPACTION_TYPED_SERVICE_REQUIRED")
        if not isinstance(projection, ModelProjection):
            raise RuntimeError("CONTEXT_COMPACTION_PROJECTION_REQUIRED")
        typed_result = await middleware.compact_now(
            CompressionRequest(
                thread_id=thread_id,
                trigger="manual",
                projection=projection,
                run_context_snapshot=run_context_snapshot,
                runtime_state=runtime_state,
                current_execution_policy=current_execution_policy,
            )
        )
        if not isinstance(typed_result, CompressionResult):
            raise RuntimeError("CONTEXT_COMPACTION_TYPED_RESULT_INVALID")
        compacted = list(typed_result.projected_messages)
        rewritten = typed_result.compressed
        updates = middleware.consume_updates(thread_id)
        update = updates[-1] if updates else ContextUpdate(
            thread_id=thread_id,
            action=typed_result.action,
            estimated_tokens=typed_result.estimated_tokens,
            input_cap_tokens=typed_result.input_cap_tokens,
            context_window_tokens=getattr(middleware, "_window", 0),
            dynamic_tokens=typed_result.estimated_tokens,
            artifact_ids=typed_result.artifact_ids,
            miss_reason=typed_result.reason,
        )
        # `compact_now` 复用运行期状态缓冲；当前请求直接返回结果，因此必须消费，
        # 防止下一次 Agent run 重复发出过期的 context.updated 事件。
        if rewritten:
            from harness_agent.threads.context_projection import ContextProjector

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
        artifacts = self._agent_engine_artifacts.pop(profile_key, None)
        if artifacts is not None and artifacts.mcp_lease is not None:
            await artifacts.mcp_lease.release()
        self._resolved_agent_specs.pop(profile_key, None)
        self._profile_mcp_owners.pop(profile_key, None)

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
        self._profile_mcp_owners.clear()

    async def _agent_engine_pool_diagnostics(self) -> dict[str, object]:
        """返回 config.show 的运行池摘要；未初始化/已关闭时不保留旧 AgentEngine 引用。"""
        pool = self._agent_engine_pool
        if pool is None:
            return {
                "available": False,
                "state": "not_initialized",
                "memory": {"estimated_bytes": None, "rss_bytes": None, "status": "not_collected"},
            }
        payload = (await pool.diagnostics()).payload()
        owners = [
            *self._retired_mcp_owners,
            *([self._mcp_owner] if self._mcp_owner is not None else []),
        ]
        shared_resources = []
        for owner in owners:
            snapshot = await owner.snapshot()
            shared_resources.append(
                {
                    "name": snapshot.name,
                    "scope": snapshot.scope.value,
                    "fingerprint_id": snapshot.fingerprint[:12],
                    "borrowers": snapshot.borrowers,
                    "retired": snapshot.retired,
                    "closed": snapshot.closed,
                }
            )
        payload["shared_resources"] = shared_resources
        return payload

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
            self._thread_persistence.bind_diagnostic_log(self._diagnostic_log)
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


def _team_definition_payload(definition: TeamDefinition) -> dict[str, object]:
    """将 TeamDefinition 转成不含输入模板正文的控制面摘要。"""
    return {
        "id": definition.team_id,
        "description": definition.description,
        "max_parallelism": definition.max_parallelism,
        "failure_policy": str(definition.failure_policy),
        "tasks": [
            {
                "id": task.task_id,
                "agent_id": task.agent_id,
                "depends_on": list(task.depends_on),
                "access": str(task.access),
                "timeout_seconds": task.timeout_seconds,
            }
            for task in definition.tasks
        ],
    }


def _team_run_payload(run: TeamRun) -> dict[str, object]:
    """将持久化 TeamRun 转成有界结构化状态，不返回成员消息或 Prompt。"""
    return {
        "run_id": run.run_id,
        "team_id": run.team_id,
        "thread_id": run.parent_ref.thread_id,
        "status": str(run.status),
        "terminal_count": run.terminal_count,
        "tasks": [
            {
                "id": task.task_id,
                "status": str(task.status),
                "execution_id": task.execution_id,
                "result": dict(task.result),
                "error_code": task.error_code,
                "attempts": task.attempts,
            }
            for task in run.tasks
        ],
    }


def _plugin_reauthorization_message(summary: PluginReauthorizationSummary) -> str:
    """生成可操作且脱敏的 stale Plugin 门禁错误。"""
    return (
        "PLUGIN_REAUTHORIZATION_REQUIRED: "
        f"plugin_id={summary.plugin_id}; "
        f"authorization_state={summary.authorization_state}; "
        f"capability_fingerprint={summary.capability_fingerprint}; "
        "action=run `harness plugins inspect "
        f"{summary.plugin_id}`, then `harness plugins enable {summary.plugin_id} "
        f"--capability-fingerprint {summary.capability_fingerprint}`"
    )


def _plugin_reauthorization_error_data(
    summary: PluginReauthorizationSummary,
) -> dict[str, object]:
    """返回 Protocol 已支持的 error.details 结构，不暴露安装路径或秘密。"""
    return {
        "code": "PLUGIN_REAUTHORIZATION_REQUIRED",
        "retryable": False,
        "details": {
            "plugin_id": summary.plugin_id,
            "authorization_state": summary.authorization_state,
            "capability_fingerprint": summary.capability_fingerprint,
            "action": {
                "inspect": f"harness plugins inspect {summary.plugin_id}",
                "enable": (
                    f"harness plugins enable {summary.plugin_id} "
                    f"--capability-fingerprint {summary.capability_fingerprint}"
                ),
            },
        },
    }


def _protocol_error_data(message: str, data: object | None) -> dict[str, object]:
    """把既有领域异常收敛为 v3 稳定错误枚举。"""
    raw = data if isinstance(data, Mapping) else {}
    raw_code = raw.get("code")
    if isinstance(raw_code, str) and (
        raw_code in STABLE_ERROR_CODES
        or raw_code.startswith(("PLUGIN_", "AGENT_", "TEAM_"))
    ):
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


def _settings_value_schema_error(
    method: str,
    params: Mapping[str, object],
    error: ValidationError,
) -> str | None:
    """把 settings value 的 schema 失败映射到共享 validator 的稳定错误。"""
    if method != METHOD["SETTINGS_SET"] or tuple(error.absolute_path) != ("value",):
        return None
    value = params.get("value")
    if error.validator == "maxLength":
        return "SETTINGS_VALUE_TOO_LARGE"
    if isinstance(value, str):
        try:
            if len(value.encode("utf-8")) > 65_536:
                return "SETTINGS_VALUE_TOO_LARGE"
        except UnicodeEncodeError:
            return "SETTINGS_VALUE_INVALID"
    if error.validator in {"pattern", "type"}:
        return "SETTINGS_VALUE_INVALID"
    return None


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
    if message.created_at_ms is not None:
        payload["created_at_ms"] = message.created_at_ms
    return payload


def _compose_rpc_error(error: ComposeSessionError) -> RpcError:
    """把 Compose 稳定错误码原样透传为 JSON-RPC 错误。"""
    return RpcError(-32004, error.code, {"code": error.code, "retryable": False})
