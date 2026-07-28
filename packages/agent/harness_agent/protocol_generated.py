"""由 packages/protocol/schema/v3.json 生成的协议入口，请勿手工修改。"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypeAlias, TypedDict
from harness_agent.protocol_runtime import event_model, schema_model

PROTOCOL_MAJOR = 3
PROTOCOL_MINOR = 0
PROTOCOL_SCHEMA_SHA256 = "d55de07aae261473376114b36183c5919f53dee6bf7ad19276b05ac95cdde7cb"
MAX_FRAME_BYTES = 8388608
MAX_TOOL_PAYLOAD_BYTES = 1048576
CLIENT_METHODS = ["initialize","run.start","run.cancel","context.compact","config.show","config.path","config.details","config.preview","config.commit","threads.list","threads.open","threads.watch","threads.unwatch","models.list","skills.list","skills.inspect","skills.set_enabled","skills.install","skills.update","skills.remove","skills.market.list","mcp.status","mcp.add","mcp.remove","host.attachment.create"]
EVENT_TYPES = ["run.started","skill.loaded","content.delta","tool.started","tool.delta","tool.completed","context.updated","interaction.resolved","run.completed","run.cancelled","run.failed"]
INTERACTION_METHODS = ["interaction.approval","interaction.question"]
SERVER_CAPABILITIES = ["run.cancel","run.multithread","config.read","config.write","threads.read","context.manage","skills.read","skills.manage","mcp.read","mcp.manage","models.read","models.select","host.attach"]
OPERATION_CAPABILITIES = {"initialize":None,"run.start":None,"run.cancel":"run.cancel","context.compact":"context.manage","config.show":"config.read","config.path":"config.read","config.details":"config.write","config.preview":"config.write","config.commit":"config.write","threads.list":"threads.read","threads.open":"threads.read","threads.watch":"threads.read","threads.unwatch":"threads.read","models.list":"models.read","skills.list":"skills.read","skills.inspect":"skills.read","skills.set_enabled":"skills.manage","skills.install":"skills.manage","skills.update":"skills.manage","skills.remove":"skills.manage","skills.market.list":"skills.read","mcp.status":"mcp.read","mcp.add":"mcp.manage","mcp.remove":"mcp.manage","host.attachment.create":"host.attach"}
INTERACTION_HANDLES = {"interaction.approval":"approval","interaction.question":"question"}
METHOD = {"INITIALIZE":"initialize","RUN_START":"run.start","RUN_CANCEL":"run.cancel","CONTEXT_COMPACT":"context.compact","CONFIG_SHOW":"config.show","CONFIG_PATH":"config.path","CONFIG_DETAILS":"config.details","CONFIG_PREVIEW":"config.preview","CONFIG_COMMIT":"config.commit","THREADS_LIST":"threads.list","THREADS_OPEN":"threads.open","THREADS_WATCH":"threads.watch","THREADS_UNWATCH":"threads.unwatch","MODELS_LIST":"models.list","SKILLS_LIST":"skills.list","SKILLS_INSPECT":"skills.inspect","SKILLS_SET_ENABLED":"skills.set_enabled","SKILLS_INSTALL":"skills.install","SKILLS_UPDATE":"skills.update","SKILLS_REMOVE":"skills.remove","SKILLS_MARKET_LIST":"skills.market.list","MCP_STATUS":"mcp.status","MCP_ADD":"mcp.add","MCP_REMOVE":"mcp.remove","HOST_ATTACHMENT_CREATE":"host.attachment.create","EVENT":"event","INTERACTION_APPROVAL":"interaction.approval","INTERACTION_QUESTION":"interaction.question"}
CAPABILITY = {"RUN_CANCEL":"run.cancel","RUN_MULTITHREAD":"run.multithread","CONFIG_READ":"config.read","CONFIG_WRITE":"config.write","THREADS_READ":"threads.read","CONTEXT_MANAGE":"context.manage","SKILLS_READ":"skills.read","SKILLS_MANAGE":"skills.manage","MCP_READ":"mcp.read","MCP_MANAGE":"mcp.manage","MODELS_READ":"models.read","MODELS_SELECT":"models.select","HOST_ATTACH":"host.attach"}
EVENT_TYPE = {"RUN_STARTED":"run.started","SKILL_LOADED":"skill.loaded","CONTENT_DELTA":"content.delta","TOOL_STARTED":"tool.started","TOOL_DELTA":"tool.delta","TOOL_COMPLETED":"tool.completed","CONTEXT_UPDATED":"context.updated","INTERACTION_RESOLVED":"interaction.resolved","RUN_COMPLETED":"run.completed","RUN_CANCELLED":"run.cancelled","RUN_FAILED":"run.failed"}

JsonValueWire: TypeAlias = None | bool | int | float | str | list["JsonValueWire"] | dict[str, "JsonValueWire"]

JsonObjectWire: TypeAlias = dict[str, JsonValueWire]

JsonObjectArrayWire: TypeAlias = list[JsonObjectWire]

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
    agent_commands: list[JsonObjectWire]
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
    message: str
    thread_id: str
    run_id: str
    requested_skill: NotRequired[RequestedSkillWire]
    model_selection: NotRequired[ThreadModelSelectionWire]

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

class McpServerStatusWire(TypedDict):
    name: str
    transport: Literal["stdio", "http", "sse"]
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
    endpoint: str
    token: str
    expires_at_ms: int

class EventBaseWire(TypedDict):
    event_id: str
    type: str
    thread_id: str
    run_id: str
    sequence: int
    timestamp_ms: int
    payload: JsonObjectWire

class RunStartedPayloadWire(TypedDict):
    resumed: bool
    skills_snapshot_id: NotRequired[str | None]
    primary_model: NotRequired[RunPrimaryModelBindingWire]
    runtime_profile_id: NotRequired[str | None]

class SkillLoadedPayloadWire(TypedDict):
    skill_id: str
    source: str
    version: str | None
    snapshot_id: str

class ContentDeltaPayloadWire(TypedDict):
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

class ApprovalRequestWire(TypedDict):
    thread_id: str
    run_id: str
    timeout_ms: int
    payload: dict[str, Any]

class ApprovalResponseWire(TypedDict):
    decision: Literal["approve_once", "approve_thread", "approve_always", "reject", "reject_with_feedback"]
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
McpStatusParamsWire = EmptyParamsWire

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
McpStatusParams = schema_model("#/$defs/emptyParams", name="McpStatusParams")
McpStatusResult = schema_model("#/$defs/mcpStatusResult", name="McpStatusResult")
McpAddParams = schema_model("#/$defs/mcpAddParams", name="McpAddParams")
McpAddResult = schema_model("#/$defs/mcpAddResult", name="McpAddResult")
McpRemoveParams = schema_model("#/$defs/mcpRemoveParams", name="McpRemoveParams")
McpRemoveResult = schema_model("#/$defs/mcpRemoveResult", name="McpRemoveResult")
HostAttachmentCreateParams = schema_model("#/$defs/hostAttachmentCreateParams", name="HostAttachmentCreateParams")
HostAttachmentCreateResult = schema_model("#/$defs/hostAttachmentCreateResult", name="HostAttachmentCreateResult")

EventEnvelope = event_model()
ApprovalResponse = schema_model("#/$defs/approvalResponse", name="ApprovalResponse")
QuestionResponse = schema_model("#/$defs/questionResponse", name="QuestionResponse")
