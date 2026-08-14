/** 此文件由 packages/protocol/schema/v3.json 生成，请勿手工修改。 */

export const PROTOCOL_MAJOR = 3 as const
export const PROTOCOL_MINOR = 4 as const
export const PROTOCOL_SCHEMA_SHA256 = "7048d1734dcb6ed955d37552760a7efa5a54ef83bfce6bf11c36f5808c94ed58" as const
export const MAX_FRAME_BYTES = 8388608 as const
export const MAX_TOOL_PAYLOAD_BYTES = 1048576 as const
export const CLIENT_METHODS = ["initialize","run.start","run.cancel","context.compact","config.show","config.path","config.details","config.preview","config.commit","threads.list","threads.open","threads.watch","threads.unwatch","models.list","skills.list","skills.inspect","skills.set_enabled","skills.install","skills.update","skills.remove","skills.market.list","plugins.list","plugins.inspect","plugins.validate","plugins.install","plugins.set_enabled","plugins.remove","agents.list","agents.inspect","teams.list","teams.inspect","teams.generate","teams.run","teams.cancel","mcp.status","mcp.add","mcp.remove","host.attachment.create","host.attachment.revoke","host.control.acquire","host.control.release","host.control.status","compose.inspect","compose.abandon"] as const
export const EVENT_TYPES = ["run.started","run.progress","skill.loaded","content.delta","reasoning.delta","tool.started","tool.delta","tool.completed","context.updated","compose.state","compose.summary","compose.work_item","compose.activity","interaction.resolved","run.completed","run.cancelled","run.failed"] as const
export const INTERACTION_METHODS = ["interaction.approval","interaction.question","interaction.directory_trust"] as const
export const SERVER_CAPABILITIES = ["run.cancel","run.multithread","host.control","config.read","config.write","threads.read","context.manage","skills.read","skills.manage","mcp.read","mcp.manage","plugins.read","plugins.manage","agents.read","teams.read","teams.manage","models.read","models.select","host.attach"] as const
export const OPERATION_CAPABILITIES = {"initialize":null,"run.start":null,"run.cancel":"run.cancel","context.compact":"context.manage","config.show":"config.read","config.path":"config.read","config.details":"config.write","config.preview":"config.write","config.commit":"config.write","threads.list":"threads.read","threads.open":"threads.read","threads.watch":"threads.read","threads.unwatch":"threads.read","models.list":"models.read","skills.list":"skills.read","skills.inspect":"skills.read","skills.set_enabled":"skills.manage","skills.install":"skills.manage","skills.update":"skills.manage","skills.remove":"skills.manage","skills.market.list":"skills.read","plugins.list":"plugins.read","plugins.inspect":"plugins.read","plugins.validate":"plugins.read","plugins.install":"plugins.manage","plugins.set_enabled":"plugins.manage","plugins.remove":"plugins.manage","agents.list":"agents.read","agents.inspect":"agents.read","teams.list":"teams.read","teams.inspect":"teams.read","teams.generate":"teams.manage","teams.run":"teams.manage","teams.cancel":"teams.manage","mcp.status":"mcp.read","mcp.add":"mcp.manage","mcp.remove":"mcp.manage","host.attachment.create":"host.attach","host.attachment.revoke":"host.attach","host.control.acquire":"host.control","host.control.release":"host.control","host.control.status":"host.control","compose.inspect":"threads.read","compose.abandon":"threads.read"} as const
export const CONTROLLED_OPERATIONS = ["run.start","run.cancel","context.compact","config.preview","config.commit","skills.set_enabled","skills.install","skills.update","skills.remove","mcp.add","mcp.remove"] as const
export const INTERACTION_HANDLES = {"interaction.approval":"approval","interaction.question":"question","interaction.directory_trust":"directory_trust"} as const
export const ERROR_CODES = {"CONTROL_NOT_HOLDER":{"jsonrpcCode":-32008,"retryable":true},"CONTROL_BUSY":{"jsonrpcCode":-32008,"retryable":true},"CONTROL_RELEASE_BLOCKED":{"jsonrpcCode":-32008,"retryable":true},"ATTACHMENT_NOT_FOUND":{"jsonrpcCode":-32009,"retryable":false},"ATTACHMENT_NOT_ACTIVE":{"jsonrpcCode":-32009,"retryable":false},"CONNECTION_RUN_BUSY":{"jsonrpcCode":-32000,"retryable":true}} as const
export type ErrorCode = keyof typeof ERROR_CODES
export const Capability = {"RUN_CANCEL":"run.cancel","RUN_MULTITHREAD":"run.multithread","HOST_CONTROL":"host.control","CONFIG_READ":"config.read","CONFIG_WRITE":"config.write","THREADS_READ":"threads.read","CONTEXT_MANAGE":"context.manage","SKILLS_READ":"skills.read","SKILLS_MANAGE":"skills.manage","MCP_READ":"mcp.read","MCP_MANAGE":"mcp.manage","PLUGINS_READ":"plugins.read","PLUGINS_MANAGE":"plugins.manage","AGENTS_READ":"agents.read","TEAMS_READ":"teams.read","TEAMS_MANAGE":"teams.manage","MODELS_READ":"models.read","MODELS_SELECT":"models.select","HOST_ATTACH":"host.attach"} as const
export const EventType = {"RUN_STARTED":"run.started","RUN_PROGRESS":"run.progress","SKILL_LOADED":"skill.loaded","CONTENT_DELTA":"content.delta","REASONING_DELTA":"reasoning.delta","TOOL_STARTED":"tool.started","TOOL_DELTA":"tool.delta","TOOL_COMPLETED":"tool.completed","CONTEXT_UPDATED":"context.updated","COMPOSE_STATE":"compose.state","COMPOSE_SUMMARY":"compose.summary","COMPOSE_WORK_ITEM":"compose.work_item","COMPOSE_ACTIVITY":"compose.activity","INTERACTION_RESOLVED":"interaction.resolved","RUN_COMPLETED":"run.completed","RUN_CANCELLED":"run.cancelled","RUN_FAILED":"run.failed"} as const

export const Method = {
  INITIALIZE: "initialize",
  RUN_START: "run.start",
  RUN_CANCEL: "run.cancel",
  CONTEXT_COMPACT: "context.compact",
  CONFIG_SHOW: "config.show",
  CONFIG_PATH: "config.path",
  CONFIG_DETAILS: "config.details",
  CONFIG_PREVIEW: "config.preview",
  CONFIG_COMMIT: "config.commit",
  THREADS_LIST: "threads.list",
  THREADS_OPEN: "threads.open",
  THREADS_WATCH: "threads.watch",
  THREADS_UNWATCH: "threads.unwatch",
  MODELS_LIST: "models.list",
  SKILLS_LIST: "skills.list",
  SKILLS_INSPECT: "skills.inspect",
  SKILLS_SET_ENABLED: "skills.set_enabled",
  SKILLS_INSTALL: "skills.install",
  SKILLS_UPDATE: "skills.update",
  SKILLS_REMOVE: "skills.remove",
  SKILLS_MARKET_LIST: "skills.market.list",
  PLUGINS_LIST: "plugins.list",
  PLUGINS_INSPECT: "plugins.inspect",
  PLUGINS_VALIDATE: "plugins.validate",
  PLUGINS_INSTALL: "plugins.install",
  PLUGINS_SET_ENABLED: "plugins.set_enabled",
  PLUGINS_REMOVE: "plugins.remove",
  AGENTS_LIST: "agents.list",
  AGENTS_INSPECT: "agents.inspect",
  TEAMS_LIST: "teams.list",
  TEAMS_INSPECT: "teams.inspect",
  TEAMS_GENERATE: "teams.generate",
  TEAMS_RUN: "teams.run",
  TEAMS_CANCEL: "teams.cancel",
  MCP_STATUS: "mcp.status",
  MCP_ADD: "mcp.add",
  MCP_REMOVE: "mcp.remove",
  HOST_ATTACHMENT_CREATE: "host.attachment.create",
  HOST_ATTACHMENT_REVOKE: "host.attachment.revoke",
  HOST_CONTROL_ACQUIRE: "host.control.acquire",
  HOST_CONTROL_RELEASE: "host.control.release",
  HOST_CONTROL_STATUS: "host.control.status",
  COMPOSE_INSPECT: "compose.inspect",
  COMPOSE_ABANDON: "compose.abandon",
  EVENT: "event",
  INTERACTION_APPROVAL: "interaction.approval",
  INTERACTION_QUESTION: "interaction.question",
  INTERACTION_DIRECTORY_TRUST: "interaction.directory_trust",
} as const

export const PROTOCOL_VERSION = { major: PROTOCOL_MAJOR, minor: PROTOCOL_MINOR } as const

export type JsonRpcErrorObject = { code: number; message: string; data?: unknown }
export type JsonRpcRequest = { jsonrpc: "2.0"; method: string; params?: JsonObject; id: string }
export type JsonRpcNotification = { jsonrpc: "2.0"; method: string; params?: JsonObject }
export type JsonRpcResponse = { jsonrpc: "2.0"; result?: unknown; error?: JsonRpcErrorObject; id: string | null }
export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }
export type JsonObject = Record<string, JsonValue>
export type JsonObjectArray = Array<JsonObject>
export type AgentCommand = { "id": string; "name": string; "description": string; "argument_hint": string | null; "requested_skill_id": string; "plugin_id": string }
export type EmptyParams = {  }
export type ProtocolRange = { "major": 3; "min_minor": number; "max_minor": number }
export type ClientInfo = { "name": string; "version": string; "kind": string }
export type ClientCapabilities = { "requests": Array<string>; "handles": Array<"approval" | "question" | "directory_trust"> }
export type InitializeParams = { "protocol": ProtocolRange; "client": ClientInfo; "capabilities": ClientCapabilities }
export type InitializeResult = { "protocol": { "major": 3; "minor": number }; "server": { "name": string; "version": string }; "connection": { "id": string; "role": "owner" | "attached"; "project": { "id": string; "label": string } }; "capabilities": { "available": Array<string>; "enabled": Array<string>; "handles": Array<"approval" | "question" | "directory_trust"> }; "agent_commands": Array<AgentCommand>; "skills_snapshot": { "id": string; "count": number }; "skill_diagnostics": Array<string>; "limits": { "max_frame_bytes": number; "max_tool_payload_bytes": number }; "config_summary": (JsonObject) | (null); "startup_error": ({ "code": string; "message": string }) | (null) }
export type RequestedSkill = { "id": string; "args"?: string }
export type ThreadModelSelection = { "primary_profile": string }
export type ApprovalMode = "plan" | "default" | "auto-edit" | "auto" | "yolo"
export type InteractionMode = "build" | "compose"
export type ModelProfile = { "id": string; "model": string; "provider_label": string; "context_window_tokens": number; "capabilities": Array<string>; "is_default": boolean; "available": boolean; "unavailable_reason"?: string | null; "source": string }
export type RunPrimaryModelBinding = { "profile": ModelProfile; "source": string; "runtime_profile_id": string }
export type RunStartParams = { "mode": InteractionMode; "message": string; "thread_id": string; "run_id": string; "requested_skill"?: RequestedSkill; "model_selection"?: ThreadModelSelection; "approval_mode"?: ApprovalMode }
export type RunStartResult = { "thread_id": string; "run_id": string; "accepted": true }
export type RunCancelParams = { "thread_id": string; "run_id": string }
export type RunCancelResult = { "cancelled": boolean; "run_id": string }
export type ContextCompactParams = { "thread_id": string }
export type ContextCompactResult = { "compacted": boolean; "context": JsonObject }
export type ConfigChange = { "path": string; "value": JsonValue }
export type ConfigPreviewParams = { "changes": Array<ConfigChange> }
export type ConfigCommitParams = { "expected_revision": string; "changes": Array<ConfigChange> }
export type ConfigFieldDetail = { "path": string; "value": JsonValue; "source": string; "editable": boolean; "unavailable_reason": string | null; "applies_to": "new-thread" | "restart" }
export type ConfigChangeResult = { "path": string; "before": JsonValue; "after": JsonValue }
export type ConfigDetailsResult = { "revision": string; "fields": Array<ConfigFieldDetail>; "immutable_fields": Array<{ "path": string; "reason": string }> }
export type ConfigPreviewResult = { "revision": string; "changes": Array<ConfigChangeResult>; "applies_to": Array<"new-thread" | "restart"> }
export type ConfigCommitResult = ConfigPreviewResult
export type ConfigPathResult = { "workspace": string; "paths": Array<string>; "explicit_path": string | null }
export type ThreadSummary = { "thread_id": string; "created_at_ms": number; "updated_at_ms": number; "first_message": string; "latest_message": string; "message_count": number }
export type ComposeActivityRecord = { "run_id": string; "event_sequence": number; "activity_id": string; "stage": "understand" | "plan" | "build" | "verify" | "review"; "task_id"?: string; "task_title"?: string; "attempt": number; "execution_id"?: string; "agent_id"?: string; "kind": "summary" | "tool_terminal" | "truncation"; "label": string; "status": string; "bounded_text"?: string; "created_at_ms": number }
export type ThreadMessage = { "kind": "user" | "assistant" | "tool"; "content": string; "tool_name"?: string }
export type ThreadsListParams = { "limit"?: number }
export type ThreadsListResult = { "threads": Array<ThreadSummary> }
export type ThreadsOpenParams = { "thread_id": string }
export type ThreadsOpenResult = { "thread": ThreadSummary; "messages": Array<ThreadMessage>; "compose_activities"?: Array<ComposeActivityRecord>; "thread_mode"?: (InteractionMode) | (null); "work_item"?: (ComposeWorkItemSnapshot) | (null) }
export type ThreadsUnwatchResult = { "removed": boolean }
export type ThreadModelBinding = { "state": "bound" | "legacy" | "unbound"; "roles": Record<string, ModelProfile> }
export type ModelsListParams = { "thread_id"?: string }
export type ModelsListResult = { "profiles": Array<ModelProfile>; "thread_binding"?: ThreadModelBinding; "thread_selection"?: ThreadModelSelection; "last_run_binding"?: RunPrimaryModelBinding }
export type SkillsListParams = { "include_disabled"?: boolean }
export type SkillsInspectParams = { "id": string }
export type SkillsSetEnabledParams = { "id": string; "enabled": boolean }
export type SkillsInstallParams = { "market": string; "name": string; "version"?: string }
export type SkillsMarketListParams = { "market"?: string }
export type SkillsListResult = { "snapshot": JsonObject; "skills": JsonObjectArray; "diagnostics": Array<string> }
export type PluginsListParams = { "include_disabled"?: boolean }
export type PluginsInspectParams = { "id": string }
export type PluginsSourceParams = { "source": string; "format"?: "auto" | "agent-plugins-1.0" | "claude-code" }
export type PluginsSetEnabledParams = { "id": string; "enabled": boolean; "capability_fingerprint"?: string }
export type PluginsRemoveParams = { "id": string; "purge_data"?: boolean }
export type AgentSummary = { "id": string; "description": string | null; "purpose": string; "model_profile_id": string; "execution_policy_id": string; "requested_skills": Array<string>; "requested_mcp_servers": Array<string>; "max_turns": number | null; "source": string; "fingerprint": string }
export type AgentsListResult = { "snapshot_id": string; "agents": Array<AgentSummary>; "diagnostics": Array<string> }
export type AgentsInspectParams = { "id": string }
export type TeamTaskDefinition = { "id": string; "agent_id": string; "depends_on": Array<string>; "access": "read" | "write"; "timeout_seconds": number }
export type TeamDefinition = { "id": string; "description": string | null; "max_parallelism": number; "failure_policy": "fail-fast" | "continue" | "continue-to-synthesis"; "tasks": Array<TeamTaskDefinition> }
export type TeamTaskState = { "id": string; "status": "pending" | "running" | "completed" | "failed" | "cancelled" | "blocked"; "execution_id": string | null; "result": JsonObject; "error_code": string | null; "attempts": number }
export type TeamRun = { "run_id": string; "team_id": string; "thread_id": string; "status": "running" | "completed" | "failed" | "cancelled"; "terminal_count": number; "tasks": Array<TeamTaskState> }
export type TeamsListResult = { "teams": Array<TeamDefinition>; "diagnostics": Array<string> }
export type TeamsInspectParams = { "kind": "definition" | "run"; "id": string }
export type TeamsGenerateParams = { "id": string; "lead_agent_id": string; "worker_agent_ids": Array<string>; "max_parallelism"?: number }
export type TeamsRunParams = { "team_id": string; "request": string; "thread_id": string; "run_id": string }
export type TeamsRunResult = { "team_id": string; "run_id": string; "accepted": true }
export type TeamsCancelParams = { "run_id": string }
export type TeamsCancelResult = { "run_id": string; "cancelled": boolean }
export type McpServerStatus = { "name": string; "transport": "stdio" | "http" | "sse"; "source"?: string; "status": "connected" | "failed" | "skipped"; "error"?: string; "tool_names": Array<string> }
export type McpStatusResult = { "servers": Array<McpServerStatus>; "total_tools": number }
export type McpAddParams = ({ "name": string; "transport": "stdio"; "command": string; "args"?: Array<string>; "env"?: Record<string, string> }) | ({ "name": string; "transport": "http" | "sse"; "url": string; "headers"?: Record<string, string> })
export type McpAddResult = { "added": boolean; "connected": boolean; "tool_names": Array<string>; "error"?: string | null }
export type McpRemoveParams = { "name": string }
export type McpRemoveResult = { "removed": boolean }
export type HostAttachmentCreateParams = { "origin": string }
export type HostAttachmentCreateResult = { "attachment_id": string; "endpoint": string; "token": string; "expires_at_ms": number }
export type HostAttachmentRevokeParams = { "attachment_id": string }
export type HostAttachmentRevokeResult = { "attachment_id": string; "revoked": true; "control": ControlStatus }
export type ControlHolder = { "connection_id": string; "role": "owner" | "attached"; "attachment_id": string | null }
export type ControlStatus = { "state": "owner" | "attached" | "revoking"; "holder": ControlHolder }
export type ComposeActivityScope = { "activity_id": string; "stage": "understand" | "plan" | "build" | "verify" | "review"; "task_id"?: string; "task_title"?: string; "attempt": number }
export type EventBase = { "event_id": string; "type": string; "thread_id": string; "run_id": string; "sequence": number; "timestamp_ms": number; "execution_id"?: string; "parent_execution_id"?: string | null; "agent_id"?: string; "compose_scope"?: ComposeActivityScope; "payload": JsonObject }
export type RunStartedPayload = { "mode": InteractionMode; "resumed": boolean; "skills_snapshot_id"?: string | null; "primary_model"?: RunPrimaryModelBinding; "runtime_profile_id"?: string | null }
export type RunProgressPayload = { "phase": "preparing" | "model"; "elapsed_ms": number }
export type SkillLoadedPayload = { "skill_id": string; "source": string; "version": string | null; "snapshot_id": string }
export type ContentDeltaPayload = { "text": string }
export type ReasoningDeltaPayload = { "text": string }
export type ToolStartedPayload = { "tool_call_id": string; "name": string }
export type ToolDeltaPayload = { "tool_call_id": string; "arguments_delta"?: string; "output_delta"?: string; "truncated"?: boolean; "original_bytes"?: number }
export type ToolResult = { "content": string; "is_error": boolean; "truncated": boolean; "original_bytes": number }
export type ToolCompletedPayload = { "tool_call_id": string; "result": ToolResult }
export type ContextPayload = { "action": string; "estimated_tokens"?: number | null; "input_cap_tokens"?: number | null; "context_window_tokens"?: number | null; "dynamic_tokens"?: number | null; "cache_status"?: string | null; "cached_tokens"?: number | null; "miss_reason"?: string | null; "artifact_ids": Array<string> }
export type ComposeSummaryPayload = { "status": "passed" | "failed" | "blocked" | "cancelled"; "text": string }
export type ComposeStatePayload = { "revision": number; "stage": "understand" | "plan" | "build" | "verify" | "review"; "status": "running" | "waiting_user" | "blocked" | "completed" | "failed" | "cancelled"; "stages": Array<{ "id": "understand" | "plan" | "build" | "verify" | "review"; "status": "pending" | "running" | "waiting_user" | "passed" | "failed" | "cancelled" | "blocked"; "attempts": number }>; "tasks": Array<{ "id": string; "title": string; "status": "pending" | "running" | "passed" | "failed" | "cancelled" }>; "evidence": Array<{ "label": string; "status": "pending" | "running" | "passed" | "failed" | "cancelled" }>; "blocked_reason"?: string | null }
export type ComposeWorkItemSnapshot = { "work_item_id": string; "slug": string; "title": string; "revision": number; "status": "active" | "waiting_user" | "blocked" | "completed" | "abandoned"; "current_activity": string; "pending_decision": string | null; "blocked_reason": string | null }
export type ComposeReadinessSnapshot = { "task_confirmed": boolean; "spec_confirmed": boolean; "plan_confirmed": boolean; "todo_executable": boolean; "implementation_current": boolean; "verification_fresh": boolean; "review_fresh": boolean; "report_current": boolean; "complete": boolean }
export type ComposeInspectParams = { "thread_id": string; "work_item_id"?: string }
export type ComposeInspectResult = { "work_item": (ComposeWorkItemSnapshot) | (null) }
export type ComposeAbandonParams = { "thread_id": string; "work_item_id": string; "expected_revision": number; "reason"?: string }
export type ComposeAbandonResult = { "work_item": ComposeWorkItemSnapshot }
export type ComposeWorkItemPayload = { "thread_id": string; "work_item": ComposeWorkItemSnapshot }
export type ComposeActivityPayload = { "thread_id": string; "work_item_id": string; "activity_id": string; "kind": string; "status": string; "attempt": number }
export type InteractionResolvedPayload = { "request_id": string; "type": "approval" | "question" | "directory_trust" }
export type Usage = { "input_tokens": number; "output_tokens": number }
export type RunCompletedPayload = { "usage": Usage; "duration_ms": number; "finish_reason": string; "context": JsonObject }
export type RunCancelledPayload = { "reason": string }
export type RunFailure = { "code": string; "message": string; "retryable": boolean }
export type RunFailedPayload = { "error": RunFailure }
export type InteractionBase = { "thread_id": string; "run_id": string; "timeout_ms": number; "payload": JsonObject }
export type FileDiffPresentation = { "kind": "file_diff"; "operation": "write" | "edit" | "delete"; "path": string; "added_lines": number; "removed_lines": number; "truncated": boolean; "unified_diff": string }
export type DirectoryTrustDecision = "allow_session" | "deny"
export type DirectoryTrustRequest = { "thread_id": string; "run_id": string; "timeout_ms": number; "payload": { "interrupt_id": string; "directory": string; "target_path": string; "tool_name": string; "access": "read" | "write"; "shadows_workspace": boolean; "decisions": Array<DirectoryTrustDecision> } }
export type DirectoryTrustResponse = { "decision": DirectoryTrustDecision }
export type ApprovalRequest = { "thread_id": string; "run_id": string; "timeout_ms": number; "execution_id"?: string; "parent_execution_id"?: string | null; "agent_id"?: string; "compose_scope"?: ComposeActivityScope; "payload": { "interrupt_id": string; "description": string; "requests": JsonValue; "decisions": Array<"approve_once" | "approve_thread" | "approve_project" | "reject" | "reject_with_feedback">; "presentation"?: FileDiffPresentation } }
export type ApprovalResponse = { "decision": "approve_once" | "approve_thread" | "approve_project" | "reject" | "reject_with_feedback"; "feedback"?: string }
export type Question = { "id": string; "question": string; "header": string; "body": string; "options": Array<{ "label": string; "value": string; "description": string }>; "multi_select": boolean; "allow_other": boolean }
export type QuestionRequest = { "thread_id": string; "run_id": string; "timeout_ms": number; "execution_id"?: string; "parent_execution_id"?: string | null; "agent_id"?: string; "compose_scope"?: ComposeActivityScope; "payload": { "interrupt_id": string; "questions": Array<Question> } }
export type QuestionResponse = { "answers": Record<string, Array<string>> }
export type ProtocolErrorData = { "code": string; "retryable": boolean; "capability"?: string; "details"?: JsonValue }

export type ConfigShowParams = EmptyParams
export type ConfigShowResult = JsonObject
export type ConfigPathParams = EmptyParams
export type ConfigDetailsParams = EmptyParams
export type ThreadsWatchParams = ThreadsOpenParams
export type ThreadsWatchResult = ThreadsOpenResult
export type ThreadsUnwatchParams = ThreadsOpenParams
export type SkillsInspectResult = JsonObject
export type SkillsSetEnabledResult = JsonObject
export type SkillsInstallResult = JsonObject
export type SkillsUpdateParams = SkillsInstallParams
export type SkillsUpdateResult = JsonObject
export type SkillsRemoveParams = SkillsInspectParams
export type SkillsRemoveResult = JsonObject
export type SkillsMarketListResult = JsonObjectArray
export type PluginsListResult = JsonObject
export type PluginsInspectResult = JsonObject
export type PluginsValidateParams = PluginsSourceParams
export type PluginsValidateResult = JsonObject
export type PluginsInstallParams = PluginsSourceParams
export type PluginsInstallResult = JsonObject
export type PluginsSetEnabledResult = JsonObject
export type PluginsRemoveResult = JsonObject
export type AgentsListParams = EmptyParams
export type AgentsInspectResult = AgentSummary
export type TeamsListParams = EmptyParams
export type TeamsInspectResult = JsonObject
export type TeamsGenerateResult = TeamDefinition
export type McpStatusParams = EmptyParams
export type HostControlAcquireParams = EmptyParams
export type HostControlAcquireResult = ControlStatus
export type HostControlReleaseParams = EmptyParams
export type HostControlReleaseResult = ControlStatus
export type HostControlStatusParams = EmptyParams
export type HostControlStatusResult = ControlStatus

export interface OperationMap {
  "initialize": { params: InitializeParams; result: InitializeResult }
  "run.start": { params: RunStartParams; result: RunStartResult }
  "run.cancel": { params: RunCancelParams; result: RunCancelResult }
  "context.compact": { params: ContextCompactParams; result: ContextCompactResult }
  "config.show": { params: ConfigShowParams; result: ConfigShowResult }
  "config.path": { params: ConfigPathParams; result: ConfigPathResult }
  "config.details": { params: ConfigDetailsParams; result: ConfigDetailsResult }
  "config.preview": { params: ConfigPreviewParams; result: ConfigPreviewResult }
  "config.commit": { params: ConfigCommitParams; result: ConfigCommitResult }
  "threads.list": { params: ThreadsListParams; result: ThreadsListResult }
  "threads.open": { params: ThreadsOpenParams; result: ThreadsOpenResult }
  "threads.watch": { params: ThreadsWatchParams; result: ThreadsWatchResult }
  "threads.unwatch": { params: ThreadsUnwatchParams; result: ThreadsUnwatchResult }
  "models.list": { params: ModelsListParams; result: ModelsListResult }
  "skills.list": { params: SkillsListParams; result: SkillsListResult }
  "skills.inspect": { params: SkillsInspectParams; result: SkillsInspectResult }
  "skills.set_enabled": { params: SkillsSetEnabledParams; result: SkillsSetEnabledResult }
  "skills.install": { params: SkillsInstallParams; result: SkillsInstallResult }
  "skills.update": { params: SkillsUpdateParams; result: SkillsUpdateResult }
  "skills.remove": { params: SkillsRemoveParams; result: SkillsRemoveResult }
  "skills.market.list": { params: SkillsMarketListParams; result: SkillsMarketListResult }
  "plugins.list": { params: PluginsListParams; result: PluginsListResult }
  "plugins.inspect": { params: PluginsInspectParams; result: PluginsInspectResult }
  "plugins.validate": { params: PluginsValidateParams; result: PluginsValidateResult }
  "plugins.install": { params: PluginsInstallParams; result: PluginsInstallResult }
  "plugins.set_enabled": { params: PluginsSetEnabledParams; result: PluginsSetEnabledResult }
  "plugins.remove": { params: PluginsRemoveParams; result: PluginsRemoveResult }
  "agents.list": { params: AgentsListParams; result: AgentsListResult }
  "agents.inspect": { params: AgentsInspectParams; result: AgentsInspectResult }
  "teams.list": { params: TeamsListParams; result: TeamsListResult }
  "teams.inspect": { params: TeamsInspectParams; result: TeamsInspectResult }
  "teams.generate": { params: TeamsGenerateParams; result: TeamsGenerateResult }
  "teams.run": { params: TeamsRunParams; result: TeamsRunResult }
  "teams.cancel": { params: TeamsCancelParams; result: TeamsCancelResult }
  "mcp.status": { params: McpStatusParams; result: McpStatusResult }
  "mcp.add": { params: McpAddParams; result: McpAddResult }
  "mcp.remove": { params: McpRemoveParams; result: McpRemoveResult }
  "host.attachment.create": { params: HostAttachmentCreateParams; result: HostAttachmentCreateResult }
  "host.attachment.revoke": { params: HostAttachmentRevokeParams; result: HostAttachmentRevokeResult }
  "host.control.acquire": { params: HostControlAcquireParams; result: HostControlAcquireResult }
  "host.control.release": { params: HostControlReleaseParams; result: HostControlReleaseResult }
  "host.control.status": { params: HostControlStatusParams; result: HostControlStatusResult }
  "compose.inspect": { params: ComposeInspectParams; result: ComposeInspectResult }
  "compose.abandon": { params: ComposeAbandonParams; result: ComposeAbandonResult }
}
export type OperationName = keyof OperationMap

export type AgentEventOf<T extends string, P> = {
  event_id: string
  type: T
  thread_id: string
  run_id: string
  sequence: number
  timestamp_ms: number
  execution_id?: string
  parent_execution_id?: string | null
  agent_id?: string
  compose_scope?: ComposeActivityScope
  payload: P
}
export type AgentEvent =
  | AgentEventOf<"run.started", RunStartedPayload>
  | AgentEventOf<"run.progress", RunProgressPayload>
  | AgentEventOf<"skill.loaded", SkillLoadedPayload>
  | AgentEventOf<"content.delta", ContentDeltaPayload>
  | AgentEventOf<"reasoning.delta", ReasoningDeltaPayload>
  | AgentEventOf<"tool.started", ToolStartedPayload>
  | AgentEventOf<"tool.delta", ToolDeltaPayload>
  | AgentEventOf<"tool.completed", ToolCompletedPayload>
  | AgentEventOf<"context.updated", ContextPayload>
  | AgentEventOf<"compose.state", ComposeStatePayload>
  | AgentEventOf<"compose.summary", ComposeSummaryPayload>
  | AgentEventOf<"compose.work_item", ComposeWorkItemPayload>
  | AgentEventOf<"compose.activity", ComposeActivityPayload>
  | AgentEventOf<"interaction.resolved", InteractionResolvedPayload>
  | AgentEventOf<"run.completed", RunCompletedPayload>
  | AgentEventOf<"run.cancelled", RunCancelledPayload>
  | AgentEventOf<"run.failed", RunFailedPayload>
export type EventEnvelope = AgentEvent

export interface InteractionMap {
  "interaction.approval": { params: ApprovalRequest; result: ApprovalResponse }
  "interaction.question": { params: QuestionRequest; result: QuestionResponse }
  "interaction.directory_trust": { params: DirectoryTrustRequest; result: DirectoryTrustResponse }
}
export type InteractionMethod = keyof InteractionMap
export type InteractionRequest = {
  [M in InteractionMethod]: { method: M; id: string; params: InteractionMap[M]["params"] }
}[InteractionMethod]
