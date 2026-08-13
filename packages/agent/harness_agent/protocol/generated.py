"""由 packages/protocol/schema/v3.json 生成的协议入口，请勿手工修改。"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypeAlias, TypedDict
from harness_agent.protocol.runtime import event_model, schema_model

PROTOCOL_MAJOR = 3
PROTOCOL_MINOR = 4
PROTOCOL_SCHEMA_SHA256 = "4e8fa73106c74a9ec6937dcffc315bef05c9e9de1fb25c99eb43aa211cf93a3a"
MAX_FRAME_BYTES = 8388608
MAX_TOOL_PAYLOAD_BYTES = 1048576
CLIENT_METHODS = ["initialize","run.start","run.cancel","context.compact","config.show","config.path","config.details","config.preview","config.commit","threads.list","threads.open","threads.watch","threads.unwatch","models.list","skills.list","skills.inspect","skills.set_enabled","skills.install","skills.update","skills.remove","skills.market.list","plugins.list","plugins.inspect","plugins.validate","plugins.install","plugins.set_enabled","plugins.remove","agents.list","agents.inspect","teams.list","teams.inspect","teams.generate","teams.run","teams.cancel","mcp.status","mcp.add","mcp.remove","host.attachment.create","host.attachment.revoke","host.control.acquire","host.control.release","host.control.status","compose.inspect","compose.abandon"]
EVENT_TYPES = ["run.started","run.progress","skill.loaded","content.delta","reasoning.delta","tool.started","tool.delta","tool.completed","context.updated","compose.state","compose.summary","compose.work_item","compose.activity","interaction.resolved","run.completed","run.cancelled","run.failed"]
INTERACTION_METHODS = ["interaction.approval","interaction.question"]
SERVER_CAPABILITIES = ["run.cancel","run.multithread","host.control","config.read","config.write","threads.read","context.manage","skills.read","skills.manage","mcp.read","mcp.manage","plugins.read","plugins.manage","agents.read","teams.read","teams.manage","models.read","models.select","host.attach"]
OPERATION_CAPABILITIES = {"initialize":None,"run.start":None,"run.cancel":"run.cancel","context.compact":"context.manage","config.show":"config.read","config.path":"config.read","config.details":"config.write","config.preview":"config.write","config.commit":"config.write","threads.list":"threads.read","threads.open":"threads.read","threads.watch":"threads.read","threads.unwatch":"threads.read","models.list":"models.read","skills.list":"skills.read","skills.inspect":"skills.read","skills.set_enabled":"skills.manage","skills.install":"skills.manage","skills.update":"skills.manage","skills.remove":"skills.manage","skills.market.list":"skills.read","plugins.list":"plugins.read","plugins.inspect":"plugins.read","plugins.validate":"plugins.read","plugins.install":"plugins.manage","plugins.set_enabled":"plugins.manage","plugins.remove":"plugins.manage","agents.list":"agents.read","agents.inspect":"agents.read","teams.list":"teams.read","teams.inspect":"teams.read","teams.generate":"teams.manage","teams.run":"teams.manage","teams.cancel":"teams.manage","mcp.status":"mcp.read","mcp.add":"mcp.manage","mcp.remove":"mcp.manage","host.attachment.create":"host.attach","host.attachment.revoke":"host.attach","host.control.acquire":"host.control","host.control.release":"host.control","host.control.status":"host.control","compose.inspect":"threads.read","compose.abandon":"threads.read"}
CONTROLLED_OPERATIONS = ["run.start","run.cancel","context.compact","config.preview","config.commit","skills.set_enabled","skills.install","skills.update","skills.remove","mcp.add","mcp.remove"]
INTERACTION_HANDLES = {"interaction.approval":"approval","interaction.question":"question"}
ERROR_CODES = {"CONTROL_NOT_HOLDER":{"jsonrpc_code":-32008,"retryable":True},"CONTROL_BUSY":{"jsonrpc_code":-32008,"retryable":True},"CONTROL_RELEASE_BLOCKED":{"jsonrpc_code":-32008,"retryable":True},"ATTACHMENT_NOT_FOUND":{"jsonrpc_code":-32009,"retryable":False},"ATTACHMENT_NOT_ACTIVE":{"jsonrpc_code":-32009,"retryable":False},"CONNECTION_RUN_BUSY":{"jsonrpc_code":-32000,"retryable":True}}
METHOD = {"INITIALIZE":"initialize","RUN_START":"run.start","RUN_CANCEL":"run.cancel","CONTEXT_COMPACT":"context.compact","CONFIG_SHOW":"config.show","CONFIG_PATH":"config.path","CONFIG_DETAILS":"config.details","CONFIG_PREVIEW":"config.preview","CONFIG_COMMIT":"config.commit","THREADS_LIST":"threads.list","THREADS_OPEN":"threads.open","THREADS_WATCH":"threads.watch","THREADS_UNWATCH":"threads.unwatch","MODELS_LIST":"models.list","SKILLS_LIST":"skills.list","SKILLS_INSPECT":"skills.inspect","SKILLS_SET_ENABLED":"skills.set_enabled","SKILLS_INSTALL":"skills.install","SKILLS_UPDATE":"skills.update","SKILLS_REMOVE":"skills.remove","SKILLS_MARKET_LIST":"skills.market.list","PLUGINS_LIST":"plugins.list","PLUGINS_INSPECT":"plugins.inspect","PLUGINS_VALIDATE":"plugins.validate","PLUGINS_INSTALL":"plugins.install","PLUGINS_SET_ENABLED":"plugins.set_enabled","PLUGINS_REMOVE":"plugins.remove","AGENTS_LIST":"agents.list","AGENTS_INSPECT":"agents.inspect","TEAMS_LIST":"teams.list","TEAMS_INSPECT":"teams.inspect","TEAMS_GENERATE":"teams.generate","TEAMS_RUN":"teams.run","TEAMS_CANCEL":"teams.cancel","MCP_STATUS":"mcp.status","MCP_ADD":"mcp.add","MCP_REMOVE":"mcp.remove","HOST_ATTACHMENT_CREATE":"host.attachment.create","HOST_ATTACHMENT_REVOKE":"host.attachment.revoke","HOST_CONTROL_ACQUIRE":"host.control.acquire","HOST_CONTROL_RELEASE":"host.control.release","HOST_CONTROL_STATUS":"host.control.status","COMPOSE_INSPECT":"compose.inspect","COMPOSE_ABANDON":"compose.abandon","EVENT":"event","INTERACTION_APPROVAL":"interaction.approval","INTERACTION_QUESTION":"interaction.question"}
CAPABILITY = {"RUN_CANCEL":"run.cancel","RUN_MULTITHREAD":"run.multithread","HOST_CONTROL":"host.control","CONFIG_READ":"config.read","CONFIG_WRITE":"config.write","THREADS_READ":"threads.read","CONTEXT_MANAGE":"context.manage","SKILLS_READ":"skills.read","SKILLS_MANAGE":"skills.manage","MCP_READ":"mcp.read","MCP_MANAGE":"mcp.manage","PLUGINS_READ":"plugins.read","PLUGINS_MANAGE":"plugins.manage","AGENTS_READ":"agents.read","TEAMS_READ":"teams.read","TEAMS_MANAGE":"teams.manage","MODELS_READ":"models.read","MODELS_SELECT":"models.select","HOST_ATTACH":"host.attach"}
EVENT_TYPE = {"RUN_STARTED":"run.started","RUN_PROGRESS":"run.progress","SKILL_LOADED":"skill.loaded","CONTENT_DELTA":"content.delta","REASONING_DELTA":"reasoning.delta","TOOL_STARTED":"tool.started","TOOL_DELTA":"tool.delta","TOOL_COMPLETED":"tool.completed","CONTEXT_UPDATED":"context.updated","COMPOSE_STATE":"compose.state","COMPOSE_SUMMARY":"compose.summary","COMPOSE_WORK_ITEM":"compose.work_item","COMPOSE_ACTIVITY":"compose.activity","INTERACTION_RESOLVED":"interaction.resolved","RUN_COMPLETED":"run.completed","RUN_CANCELLED":"run.cancelled","RUN_FAILED":"run.failed"}

JsonValueWire: TypeAlias = None | bool | int | float | str | list["JsonValueWire"] | dict[str, "JsonValueWire"]

JsonObjectWire: TypeAlias = dict[str, JsonValueWire]

JsonObjectArrayWire: TypeAlias = list[JsonObjectWire]

class AgentCommandWire(TypedDict):
    id: str
    name: str
    description: str
    argument_hint: str | None
    requested_skill_id: str
    plugin_id: str

class EmptyParamsWire(TypedDict):
    pass

class ProtocolRangeWire(TypedDict):
    major: Literal[3]
    min_minor: int
    max_minor: int

class ClientInfoWire(TypedDict):
    name: str
    version: str
    kind: str

class ClientCapabilitiesWire(TypedDict):
    requests: list[str]
    handles: list[Literal["approval", "question"]]

class InitializeParamsWire(TypedDict):
    protocol: ProtocolRangeWire
    client: ClientInfoWire
    capabilities: ClientCapabilitiesWire

class InitializeResultWire(TypedDict):
    protocol: dict[str, Any]
    server: dict[str, Any]
    connection: dict[str, Any]
    capabilities: dict[str, Any]
    agent_commands: list[AgentCommandWire]
    skills_snapshot: dict[str, Any]
    skill_diagnostics: list[str]
    limits: dict[str, Any]
    config_summary: JsonObjectWire | None
    startup_error: dict[str, Any] | None

class RequestedSkillWire(TypedDict):
    id: str
    args: NotRequired[str]

class ThreadModelSelectionWire(TypedDict):
    primary_profile: str

ApprovalModeWire: TypeAlias = Literal["plan", "default", "auto-edit", "auto", "yolo"]

InteractionModeWire: TypeAlias = Literal["build", "compose"]

class ModelProfileWire(TypedDict):
    id: str
    model: str
    provider_label: str
    context_window_tokens: int
    capabilities: list[str]
    is_default: bool
    available: bool
    unavailable_reason: NotRequired[str | None]
    source: str

class RunPrimaryModelBindingWire(TypedDict):
    profile: ModelProfileWire
    source: str
    runtime_profile_id: str

class RunStartParamsWire(TypedDict):
    mode: InteractionModeWire
    message: str
    thread_id: str
    run_id: str
    requested_skill: NotRequired[RequestedSkillWire]
    model_selection: NotRequired[ThreadModelSelectionWire]
    approval_mode: NotRequired[ApprovalModeWire]

class RunStartResultWire(TypedDict):
    thread_id: str
    run_id: str
    accepted: Literal[True]

class RunCancelParamsWire(TypedDict):
    thread_id: str
    run_id: str

class RunCancelResultWire(TypedDict):
    cancelled: bool
    run_id: str

class ContextCompactParamsWire(TypedDict):
    thread_id: str

class ContextCompactResultWire(TypedDict):
    compacted: bool
    context: JsonObjectWire

class ConfigChangeWire(TypedDict):
    path: str
    value: JsonValueWire

class ConfigPreviewParamsWire(TypedDict):
    changes: list[ConfigChangeWire]

class ConfigCommitParamsWire(TypedDict):
    expected_revision: str
    changes: list[ConfigChangeWire]

class ConfigFieldDetailWire(TypedDict):
    path: str
    value: JsonValueWire
    source: str
    editable: bool
    unavailable_reason: str | None
    applies_to: Literal["new-thread", "restart"]

class ConfigChangeResultWire(TypedDict):
    path: str
    before: JsonValueWire
    after: JsonValueWire

class ConfigDetailsResultWire(TypedDict):
    revision: str
    fields: list[ConfigFieldDetailWire]
    immutable_fields: list[dict[str, Any]]

class ConfigPreviewResultWire(TypedDict):
    revision: str
    changes: list[ConfigChangeResultWire]
    applies_to: list[Literal["new-thread", "restart"]]

ConfigCommitResultWire: TypeAlias = ConfigPreviewResultWire

class ConfigPathResultWire(TypedDict):
    workspace: str
    paths: list[str]
    explicit_path: str | None

class ThreadSummaryWire(TypedDict):
    thread_id: str
    created_at_ms: int
    updated_at_ms: int
    first_message: str
    latest_message: str
    message_count: int

class ComposeActivityRecordWire(TypedDict):
    run_id: str
    event_sequence: int
    activity_id: str
    stage: Literal["understand", "plan", "build", "verify", "review"]
    task_id: NotRequired[str]
    task_title: NotRequired[str]
    attempt: int
    execution_id: NotRequired[str]
    agent_id: NotRequired[str]
    kind: Literal["summary", "tool_terminal", "truncation"]
    label: str
    status: str
    bounded_text: NotRequired[str]
    created_at_ms: int

class ThreadMessageWire(TypedDict):
    kind: Literal["user", "assistant", "tool"]
    content: str
    tool_name: NotRequired[str]

class ThreadsListParamsWire(TypedDict):
    limit: NotRequired[int]

class ThreadsListResultWire(TypedDict):
    threads: list[ThreadSummaryWire]

class ThreadsOpenParamsWire(TypedDict):
    thread_id: str

class ThreadsOpenResultWire(TypedDict):
    thread: ThreadSummaryWire
    messages: list[ThreadMessageWire]
    compose_activities: NotRequired[list[ComposeActivityRecordWire]]
    thread_mode: NotRequired[InteractionModeWire | None]
    work_item: NotRequired[ComposeWorkItemSnapshotWire | None]

class ThreadsUnwatchResultWire(TypedDict):
    removed: bool

class ThreadModelBindingWire(TypedDict):
    state: Literal["bound", "legacy", "unbound"]
    roles: dict[str, ModelProfileWire]

class ModelsListParamsWire(TypedDict):
    thread_id: NotRequired[str]

class ModelsListResultWire(TypedDict):
    profiles: list[ModelProfileWire]
    thread_binding: NotRequired[ThreadModelBindingWire]
    thread_selection: NotRequired[ThreadModelSelectionWire]
    last_run_binding: NotRequired[RunPrimaryModelBindingWire]

class SkillsListParamsWire(TypedDict):
    include_disabled: NotRequired[bool]

class SkillsInspectParamsWire(TypedDict):
    id: str

class SkillsSetEnabledParamsWire(TypedDict):
    id: str
    enabled: bool

class SkillsInstallParamsWire(TypedDict):
    market: str
    name: str
    version: NotRequired[str]

class SkillsMarketListParamsWire(TypedDict):
    market: NotRequired[str]

class SkillsListResultWire(TypedDict):
    snapshot: JsonObjectWire
    skills: JsonObjectArrayWire
    diagnostics: list[str]

class PluginsListParamsWire(TypedDict):
    include_disabled: NotRequired[bool]

class PluginsInspectParamsWire(TypedDict):
    id: str

class PluginsSourceParamsWire(TypedDict):
    source: str
    format: NotRequired[Literal["auto", "agent-plugins-1.0", "claude-code"]]

class PluginsSetEnabledParamsWire(TypedDict):
    id: str
    enabled: bool
    capability_fingerprint: NotRequired[str]

class PluginsRemoveParamsWire(TypedDict):
    id: str
    purge_data: NotRequired[bool]

class AgentSummaryWire(TypedDict):
    id: str
    description: str | None
    purpose: str
    model_profile_id: str
    execution_policy_id: str
    requested_skills: list[str]
    requested_mcp_servers: list[str]
    max_turns: int | None
    source: str
    fingerprint: str

class AgentsListResultWire(TypedDict):
    snapshot_id: str
    agents: list[AgentSummaryWire]
    diagnostics: list[str]

class AgentsInspectParamsWire(TypedDict):
    id: str

class TeamTaskDefinitionWire(TypedDict):
    id: str
    agent_id: str
    depends_on: list[str]
    access: Literal["read", "write"]
    timeout_seconds: float

class TeamDefinitionWire(TypedDict):
    id: str
    description: str | None
    max_parallelism: int
    failure_policy: Literal["fail-fast", "continue", "continue-to-synthesis"]
    tasks: list[TeamTaskDefinitionWire]

class TeamTaskStateWire(TypedDict):
    id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled", "blocked"]
    execution_id: str | None
    result: JsonObjectWire
    error_code: str | None
    attempts: int

class TeamRunWire(TypedDict):
    run_id: str
    team_id: str
    thread_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    terminal_count: int
    tasks: list[TeamTaskStateWire]

class TeamsListResultWire(TypedDict):
    teams: list[TeamDefinitionWire]
    diagnostics: list[str]

class TeamsInspectParamsWire(TypedDict):
    kind: Literal["definition", "run"]
    id: str

class TeamsGenerateParamsWire(TypedDict):
    id: str
    lead_agent_id: str
    worker_agent_ids: list[str]
    max_parallelism: NotRequired[int]

class TeamsRunParamsWire(TypedDict):
    team_id: str
    request: str
    thread_id: str
    run_id: str

class TeamsRunResultWire(TypedDict):
    team_id: str
    run_id: str
    accepted: Literal[True]

class TeamsCancelParamsWire(TypedDict):
    run_id: str

class TeamsCancelResultWire(TypedDict):
    run_id: str
    cancelled: bool

class McpServerStatusWire(TypedDict):
    name: str
    transport: Literal["stdio", "http", "sse"]
    source: NotRequired[str]
    status: Literal["connected", "failed", "skipped"]
    error: NotRequired[str]
    tool_names: list[str]

class McpStatusResultWire(TypedDict):
    servers: list[McpServerStatusWire]
    total_tools: int

McpAddParamsWire: TypeAlias = dict[str, Any] | dict[str, Any]

class McpAddResultWire(TypedDict):
    added: bool
    connected: bool
    tool_names: list[str]
    error: NotRequired[str | None]

class McpRemoveParamsWire(TypedDict):
    name: str

class McpRemoveResultWire(TypedDict):
    removed: bool

class HostAttachmentCreateParamsWire(TypedDict):
    origin: str

class HostAttachmentCreateResultWire(TypedDict):
    attachment_id: str
    endpoint: str
    token: str
    expires_at_ms: int

class HostAttachmentRevokeParamsWire(TypedDict):
    attachment_id: str

class HostAttachmentRevokeResultWire(TypedDict):
    attachment_id: str
    revoked: Literal[True]
    control: ControlStatusWire

class ControlHolderWire(TypedDict):
    connection_id: str
    role: Literal["owner", "attached"]
    attachment_id: str | None

class ControlStatusWire(TypedDict):
    state: Literal["owner", "attached", "revoking"]
    holder: ControlHolderWire

class ComposeActivityScopeWire(TypedDict):
    activity_id: str
    stage: Literal["understand", "plan", "build", "verify", "review"]
    task_id: NotRequired[str]
    task_title: NotRequired[str]
    attempt: int

class EventBaseWire(TypedDict):
    event_id: str
    type: str
    thread_id: str
    run_id: str
    sequence: int
    timestamp_ms: int
    execution_id: NotRequired[str]
    parent_execution_id: NotRequired[str | None]
    agent_id: NotRequired[str]
    compose_scope: NotRequired[ComposeActivityScopeWire]
    payload: JsonObjectWire

class RunStartedPayloadWire(TypedDict):
    mode: InteractionModeWire
    resumed: bool
    skills_snapshot_id: NotRequired[str | None]
    primary_model: NotRequired[RunPrimaryModelBindingWire]
    runtime_profile_id: NotRequired[str | None]

class RunProgressPayloadWire(TypedDict):
    phase: Literal["preparing", "model"]
    elapsed_ms: int

class SkillLoadedPayloadWire(TypedDict):
    skill_id: str
    source: str
    version: str | None
    snapshot_id: str

class ContentDeltaPayloadWire(TypedDict):
    text: str

class ReasoningDeltaPayloadWire(TypedDict):
    text: str

class ToolStartedPayloadWire(TypedDict):
    tool_call_id: str
    name: str

class ToolDeltaPayloadWire(TypedDict):
    tool_call_id: str
    arguments_delta: NotRequired[str]
    output_delta: NotRequired[str]
    truncated: NotRequired[bool]
    original_bytes: NotRequired[int]

class ToolResultWire(TypedDict):
    content: str
    is_error: bool
    truncated: bool
    original_bytes: int

class ToolCompletedPayloadWire(TypedDict):
    tool_call_id: str
    result: ToolResultWire

class ContextPayloadWire(TypedDict):
    action: str
    estimated_tokens: NotRequired[int | None]
    input_cap_tokens: NotRequired[int | None]
    context_window_tokens: NotRequired[int | None]
    dynamic_tokens: NotRequired[int | None]
    cache_status: NotRequired[str | None]
    cached_tokens: NotRequired[int | None]
    miss_reason: NotRequired[str | None]
    artifact_ids: list[str]

class ComposeSummaryPayloadWire(TypedDict):
    status: Literal["passed", "failed", "blocked", "cancelled"]
    text: str

class ComposeStatePayloadWire(TypedDict):
    revision: int
    stage: Literal["understand", "plan", "build", "verify", "review"]
    status: Literal["running", "waiting_user", "blocked", "completed", "failed", "cancelled"]
    stages: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    blocked_reason: NotRequired[str | None]

class ComposeWorkItemSnapshotWire(TypedDict):
    work_item_id: str
    slug: str
    title: str
    revision: int
    status: Literal["active", "waiting_user", "blocked", "completed", "abandoned"]
    current_activity: str
    pending_decision: str | None
    blocked_reason: str | None

class ComposeReadinessSnapshotWire(TypedDict):
    task_confirmed: bool
    spec_confirmed: bool
    plan_confirmed: bool
    todo_executable: bool
    implementation_current: bool
    verification_fresh: bool
    review_fresh: bool
    report_current: bool
    complete: bool

class ComposeInspectParamsWire(TypedDict):
    thread_id: str
    work_item_id: NotRequired[str]

class ComposeInspectResultWire(TypedDict):
    work_item: ComposeWorkItemSnapshotWire | None

class ComposeAbandonParamsWire(TypedDict):
    thread_id: str
    work_item_id: str
    expected_revision: int
    reason: NotRequired[str]

class ComposeAbandonResultWire(TypedDict):
    work_item: ComposeWorkItemSnapshotWire

class ComposeWorkItemPayloadWire(TypedDict):
    thread_id: str
    work_item: ComposeWorkItemSnapshotWire

class ComposeActivityPayloadWire(TypedDict):
    thread_id: str
    work_item_id: str
    activity_id: str
    kind: str
    status: str
    attempt: int

class InteractionResolvedPayloadWire(TypedDict):
    request_id: str
    type: Literal["approval", "question"]

class UsageWire(TypedDict):
    input_tokens: int
    output_tokens: int

class RunCompletedPayloadWire(TypedDict):
    usage: UsageWire
    duration_ms: int
    finish_reason: str
    context: JsonObjectWire

class RunCancelledPayloadWire(TypedDict):
    reason: str

class RunFailureWire(TypedDict):
    code: str
    message: str
    retryable: bool

class RunFailedPayloadWire(TypedDict):
    error: RunFailureWire

class InteractionBaseWire(TypedDict):
    thread_id: str
    run_id: str
    timeout_ms: int
    payload: JsonObjectWire

class FileDiffPresentationWire(TypedDict):
    kind: Literal["file_diff"]
    operation: Literal["write", "edit", "delete"]
    path: str
    added_lines: int
    removed_lines: int
    truncated: bool
    unified_diff: str

class ApprovalRequestWire(TypedDict):
    thread_id: str
    run_id: str
    timeout_ms: int
    execution_id: NotRequired[str]
    parent_execution_id: NotRequired[str | None]
    agent_id: NotRequired[str]
    compose_scope: NotRequired[ComposeActivityScopeWire]
    payload: dict[str, Any]

class ApprovalResponseWire(TypedDict):
    decision: Literal["approve_once", "approve_thread", "approve_project", "reject", "reject_with_feedback"]
    feedback: NotRequired[str]

class QuestionWire(TypedDict):
    id: str
    question: str
    header: str
    body: str
    options: list[dict[str, Any]]
    multi_select: bool
    allow_other: bool

class QuestionRequestWire(TypedDict):
    thread_id: str
    run_id: str
    timeout_ms: int
    execution_id: NotRequired[str]
    parent_execution_id: NotRequired[str | None]
    agent_id: NotRequired[str]
    compose_scope: NotRequired[ComposeActivityScopeWire]
    payload: dict[str, Any]

class QuestionResponseWire(TypedDict):
    answers: dict[str, list[str]]

class ProtocolErrorDataWire(TypedDict):
    code: str
    retryable: bool
    capability: NotRequired[str]
    details: NotRequired[JsonValueWire]

ConfigShowParamsWire = EmptyParamsWire
ConfigShowResultWire = JsonObjectWire
ConfigPathParamsWire = EmptyParamsWire
ConfigDetailsParamsWire = EmptyParamsWire
ThreadsWatchParamsWire = ThreadsOpenParamsWire
ThreadsWatchResultWire = ThreadsOpenResultWire
ThreadsUnwatchParamsWire = ThreadsOpenParamsWire
SkillsInspectResultWire = JsonObjectWire
SkillsSetEnabledResultWire = JsonObjectWire
SkillsInstallResultWire = JsonObjectWire
SkillsUpdateParamsWire = SkillsInstallParamsWire
SkillsUpdateResultWire = JsonObjectWire
SkillsRemoveParamsWire = SkillsInspectParamsWire
SkillsRemoveResultWire = JsonObjectWire
SkillsMarketListResultWire = JsonObjectArrayWire
PluginsListResultWire = JsonObjectWire
PluginsInspectResultWire = JsonObjectWire
PluginsValidateParamsWire = PluginsSourceParamsWire
PluginsValidateResultWire = JsonObjectWire
PluginsInstallParamsWire = PluginsSourceParamsWire
PluginsInstallResultWire = JsonObjectWire
PluginsSetEnabledResultWire = JsonObjectWire
PluginsRemoveResultWire = JsonObjectWire
AgentsListParamsWire = EmptyParamsWire
AgentsInspectResultWire = AgentSummaryWire
TeamsListParamsWire = EmptyParamsWire
TeamsInspectResultWire = JsonObjectWire
TeamsGenerateResultWire = TeamDefinitionWire
McpStatusParamsWire = EmptyParamsWire
HostControlAcquireParamsWire = EmptyParamsWire
HostControlAcquireResultWire = ControlStatusWire
HostControlReleaseParamsWire = EmptyParamsWire
HostControlReleaseResultWire = ControlStatusWire
HostControlStatusParamsWire = EmptyParamsWire
HostControlStatusResultWire = ControlStatusWire

InitializeParams = schema_model("#/$defs/initializeParams", name="InitializeParams")
InitializeResult = schema_model("#/$defs/initializeResult", name="InitializeResult")
RunStartParams = schema_model("#/$defs/runStartParams", name="RunStartParams")
RunStartResult = schema_model("#/$defs/runStartResult", name="RunStartResult")
RunCancelParams = schema_model("#/$defs/runCancelParams", name="RunCancelParams")
RunCancelResult = schema_model("#/$defs/runCancelResult", name="RunCancelResult")
ContextCompactParams = schema_model("#/$defs/contextCompactParams", name="ContextCompactParams")
ContextCompactResult = schema_model("#/$defs/contextCompactResult", name="ContextCompactResult")
ConfigShowParams = schema_model("#/$defs/emptyParams", name="ConfigShowParams")
ConfigShowResult = schema_model("#/$defs/jsonObject", name="ConfigShowResult")
ConfigPathParams = schema_model("#/$defs/emptyParams", name="ConfigPathParams")
ConfigPathResult = schema_model("#/$defs/configPathResult", name="ConfigPathResult")
ConfigDetailsParams = schema_model("#/$defs/emptyParams", name="ConfigDetailsParams")
ConfigDetailsResult = schema_model("#/$defs/configDetailsResult", name="ConfigDetailsResult")
ConfigPreviewParams = schema_model("#/$defs/configPreviewParams", name="ConfigPreviewParams")
ConfigPreviewResult = schema_model("#/$defs/configPreviewResult", name="ConfigPreviewResult")
ConfigCommitParams = schema_model("#/$defs/configCommitParams", name="ConfigCommitParams")
ConfigCommitResult = schema_model("#/$defs/configCommitResult", name="ConfigCommitResult")
ThreadsListParams = schema_model("#/$defs/threadsListParams", name="ThreadsListParams")
ThreadsListResult = schema_model("#/$defs/threadsListResult", name="ThreadsListResult")
ThreadsOpenParams = schema_model("#/$defs/threadsOpenParams", name="ThreadsOpenParams")
ThreadsOpenResult = schema_model("#/$defs/threadsOpenResult", name="ThreadsOpenResult")
ThreadsWatchParams = schema_model("#/$defs/threadsOpenParams", name="ThreadsWatchParams")
ThreadsWatchResult = schema_model("#/$defs/threadsOpenResult", name="ThreadsWatchResult")
ThreadsUnwatchParams = schema_model("#/$defs/threadsOpenParams", name="ThreadsUnwatchParams")
ThreadsUnwatchResult = schema_model("#/$defs/threadsUnwatchResult", name="ThreadsUnwatchResult")
ModelsListParams = schema_model("#/$defs/modelsListParams", name="ModelsListParams")
ModelsListResult = schema_model("#/$defs/modelsListResult", name="ModelsListResult")
SkillsListParams = schema_model("#/$defs/skillsListParams", name="SkillsListParams")
SkillsListResult = schema_model("#/$defs/skillsListResult", name="SkillsListResult")
SkillsInspectParams = schema_model("#/$defs/skillsInspectParams", name="SkillsInspectParams")
SkillsInspectResult = schema_model("#/$defs/jsonObject", name="SkillsInspectResult")
SkillsSetEnabledParams = schema_model("#/$defs/skillsSetEnabledParams", name="SkillsSetEnabledParams")
SkillsSetEnabledResult = schema_model("#/$defs/jsonObject", name="SkillsSetEnabledResult")
SkillsInstallParams = schema_model("#/$defs/skillsInstallParams", name="SkillsInstallParams")
SkillsInstallResult = schema_model("#/$defs/jsonObject", name="SkillsInstallResult")
SkillsUpdateParams = schema_model("#/$defs/skillsInstallParams", name="SkillsUpdateParams")
SkillsUpdateResult = schema_model("#/$defs/jsonObject", name="SkillsUpdateResult")
SkillsRemoveParams = schema_model("#/$defs/skillsInspectParams", name="SkillsRemoveParams")
SkillsRemoveResult = schema_model("#/$defs/jsonObject", name="SkillsRemoveResult")
SkillsMarketListParams = schema_model("#/$defs/skillsMarketListParams", name="SkillsMarketListParams")
SkillsMarketListResult = schema_model("#/$defs/jsonObjectArray", name="SkillsMarketListResult")
PluginsListParams = schema_model("#/$defs/pluginsListParams", name="PluginsListParams")
PluginsListResult = schema_model("#/$defs/jsonObject", name="PluginsListResult")
PluginsInspectParams = schema_model("#/$defs/pluginsInspectParams", name="PluginsInspectParams")
PluginsInspectResult = schema_model("#/$defs/jsonObject", name="PluginsInspectResult")
PluginsValidateParams = schema_model("#/$defs/pluginsSourceParams", name="PluginsValidateParams")
PluginsValidateResult = schema_model("#/$defs/jsonObject", name="PluginsValidateResult")
PluginsInstallParams = schema_model("#/$defs/pluginsSourceParams", name="PluginsInstallParams")
PluginsInstallResult = schema_model("#/$defs/jsonObject", name="PluginsInstallResult")
PluginsSetEnabledParams = schema_model("#/$defs/pluginsSetEnabledParams", name="PluginsSetEnabledParams")
PluginsSetEnabledResult = schema_model("#/$defs/jsonObject", name="PluginsSetEnabledResult")
PluginsRemoveParams = schema_model("#/$defs/pluginsRemoveParams", name="PluginsRemoveParams")
PluginsRemoveResult = schema_model("#/$defs/jsonObject", name="PluginsRemoveResult")
AgentsListParams = schema_model("#/$defs/emptyParams", name="AgentsListParams")
AgentsListResult = schema_model("#/$defs/agentsListResult", name="AgentsListResult")
AgentsInspectParams = schema_model("#/$defs/agentsInspectParams", name="AgentsInspectParams")
AgentsInspectResult = schema_model("#/$defs/agentSummary", name="AgentsInspectResult")
TeamsListParams = schema_model("#/$defs/emptyParams", name="TeamsListParams")
TeamsListResult = schema_model("#/$defs/teamsListResult", name="TeamsListResult")
TeamsInspectParams = schema_model("#/$defs/teamsInspectParams", name="TeamsInspectParams")
TeamsInspectResult = schema_model("#/$defs/jsonObject", name="TeamsInspectResult")
TeamsGenerateParams = schema_model("#/$defs/teamsGenerateParams", name="TeamsGenerateParams")
TeamsGenerateResult = schema_model("#/$defs/teamDefinition", name="TeamsGenerateResult")
TeamsRunParams = schema_model("#/$defs/teamsRunParams", name="TeamsRunParams")
TeamsRunResult = schema_model("#/$defs/teamsRunResult", name="TeamsRunResult")
TeamsCancelParams = schema_model("#/$defs/teamsCancelParams", name="TeamsCancelParams")
TeamsCancelResult = schema_model("#/$defs/teamsCancelResult", name="TeamsCancelResult")
McpStatusParams = schema_model("#/$defs/emptyParams", name="McpStatusParams")
McpStatusResult = schema_model("#/$defs/mcpStatusResult", name="McpStatusResult")
McpAddParams = schema_model("#/$defs/mcpAddParams", name="McpAddParams")
McpAddResult = schema_model("#/$defs/mcpAddResult", name="McpAddResult")
McpRemoveParams = schema_model("#/$defs/mcpRemoveParams", name="McpRemoveParams")
McpRemoveResult = schema_model("#/$defs/mcpRemoveResult", name="McpRemoveResult")
HostAttachmentCreateParams = schema_model("#/$defs/hostAttachmentCreateParams", name="HostAttachmentCreateParams")
HostAttachmentCreateResult = schema_model("#/$defs/hostAttachmentCreateResult", name="HostAttachmentCreateResult")
HostAttachmentRevokeParams = schema_model("#/$defs/hostAttachmentRevokeParams", name="HostAttachmentRevokeParams")
HostAttachmentRevokeResult = schema_model("#/$defs/hostAttachmentRevokeResult", name="HostAttachmentRevokeResult")
HostControlAcquireParams = schema_model("#/$defs/emptyParams", name="HostControlAcquireParams")
HostControlAcquireResult = schema_model("#/$defs/controlStatus", name="HostControlAcquireResult")
HostControlReleaseParams = schema_model("#/$defs/emptyParams", name="HostControlReleaseParams")
HostControlReleaseResult = schema_model("#/$defs/controlStatus", name="HostControlReleaseResult")
HostControlStatusParams = schema_model("#/$defs/emptyParams", name="HostControlStatusParams")
HostControlStatusResult = schema_model("#/$defs/controlStatus", name="HostControlStatusResult")
ComposeInspectParams = schema_model("#/$defs/composeInspectParams", name="ComposeInspectParams")
ComposeInspectResult = schema_model("#/$defs/composeInspectResult", name="ComposeInspectResult")
ComposeAbandonParams = schema_model("#/$defs/composeAbandonParams", name="ComposeAbandonParams")
ComposeAbandonResult = schema_model("#/$defs/composeAbandonResult", name="ComposeAbandonResult")

EventEnvelope = event_model()
ApprovalResponse = schema_model("#/$defs/approvalResponse", name="ApprovalResponse")
QuestionResponse = schema_model("#/$defs/questionResponse", name="QuestionResponse")
