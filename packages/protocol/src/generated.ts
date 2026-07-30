/** 此文件由 packages/protocol/schema/v3.json 生成，请勿手工修改。 */

export const PROTOCOL_MAJOR = 3 as const
export const PROTOCOL_MINOR = 0 as const
export const PROTOCOL_SCHEMA_SHA256 = "f4adaa242e1b825c6fd52ee285d7f4135da8cddcf505393b53d8607b2da038db" as const
export const MAX_FRAME_BYTES = 8388608 as const
export const MAX_TOOL_PAYLOAD_BYTES = 1048576 as const
export const CLIENT_METHODS = ["initialize","run.start","run.cancel","context.compact","config.show","config.path","config.details","config.preview","config.commit","threads.list","threads.open","threads.watch","threads.unwatch","models.list","skills.list","skills.inspect","skills.set_enabled","skills.install","skills.update","skills.remove","skills.market.list","mcp.status","mcp.add","mcp.remove","host.attachment.create"] as const
export const EVENT_TYPES = ["run.started","skill.loaded","content.delta","tool.started","tool.delta","tool.completed","context.updated","interaction.resolved","run.completed","run.cancelled","run.failed"] as const
export const INTERACTION_METHODS = ["interaction.approval","interaction.question"] as const
export const SERVER_CAPABILITIES = ["run.cancel","run.multithread","config.read","config.write","threads.read","context.manage","skills.read","skills.manage","mcp.read","mcp.manage","models.read","models.select","host.attach"] as const
export const OPERATION_CAPABILITIES = {"initialize":null,"run.start":null,"run.cancel":"run.cancel","context.compact":"context.manage","config.show":"config.read","config.path":"config.read","config.details":"config.write","config.preview":"config.write","config.commit":"config.write","threads.list":"threads.read","threads.open":"threads.read","threads.watch":"threads.read","threads.unwatch":"threads.read","models.list":"models.read","skills.list":"skills.read","skills.inspect":"skills.read","skills.set_enabled":"skills.manage","skills.install":"skills.manage","skills.update":"skills.manage","skills.remove":"skills.manage","skills.market.list":"skills.read","mcp.status":"mcp.read","mcp.add":"mcp.manage","mcp.remove":"mcp.manage","host.attachment.create":"host.attach"} as const
export const INTERACTION_HANDLES = {"interaction.approval":"approval","interaction.question":"question"} as const
export const Capability = {"RUN_CANCEL":"run.cancel","RUN_MULTITHREAD":"run.multithread","CONFIG_READ":"config.read","CONFIG_WRITE":"config.write","THREADS_READ":"threads.read","CONTEXT_MANAGE":"context.manage","SKILLS_READ":"skills.read","SKILLS_MANAGE":"skills.manage","MCP_READ":"mcp.read","MCP_MANAGE":"mcp.manage","MODELS_READ":"models.read","MODELS_SELECT":"models.select","HOST_ATTACH":"host.attach"} as const
export const EventType = {"RUN_STARTED":"run.started","SKILL_LOADED":"skill.loaded","CONTENT_DELTA":"content.delta","TOOL_STARTED":"tool.started","TOOL_DELTA":"tool.delta","TOOL_COMPLETED":"tool.completed","CONTEXT_UPDATED":"context.updated","INTERACTION_RESOLVED":"interaction.resolved","RUN_COMPLETED":"run.completed","RUN_CANCELLED":"run.cancelled","RUN_FAILED":"run.failed"} as const

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
  MCP_STATUS: "mcp.status",
  MCP_ADD: "mcp.add",
  MCP_REMOVE: "mcp.remove",
  HOST_ATTACHMENT_CREATE: "host.attachment.create",
  EVENT: "event",
  INTERACTION_APPROVAL: "interaction.approval",
  INTERACTION_QUESTION: "interaction.question",
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
export type EmptyParams = {  }
export type ProtocolRange = { "major": 3; "min_minor": number; "max_minor": number }
export type ClientInfo = { "name": string; "version": string; "kind": string }
export type ClientCapabilities = { "requests": Array<string>; "handles": Array<"approval" | "question"> }
export type InitializeParams = { "protocol": ProtocolRange; "client": ClientInfo; "capabilities": ClientCapabilities }
export type InitializeResult = { "protocol": { "major": 3; "minor": number }; "server": { "name": string; "version": string }; "connection": { "id": string; "role": "owner" | "attached"; "project": { "id": string; "label": string } }; "capabilities": { "available": Array<string>; "enabled": Array<string>; "handles": Array<"approval" | "question"> }; "agent_commands": Array<JsonObject>; "skills_snapshot": { "id": string; "count": number }; "skill_diagnostics": Array<string>; "limits": { "max_frame_bytes": number; "max_tool_payload_bytes": number }; "config_summary": (JsonObject) | (null); "startup_error": ({ "code": string; "message": string }) | (null) }
export type RequestedSkill = { "id": string; "args"?: string }
export type ThreadModelSelection = { "primary_profile": string }
export type ModelProfile = { "id": string; "model": string; "provider_label": string; "context_window_tokens": number; "capabilities": Array<string>; "is_default": boolean; "available": boolean; "unavailable_reason"?: string | null; "source": string }
export type RunPrimaryModelBinding = { "profile": ModelProfile; "source": string; "runtime_profile_id": string }
export type RunStartParams = { "message": string; "thread_id": string; "run_id": string; "requested_skill"?: RequestedSkill; "model_selection"?: ThreadModelSelection }
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
export type ThreadMessage = { "kind": "user" | "assistant" | "tool"; "content": string; "tool_name"?: string }
export type ThreadsListParams = { "limit"?: number }
export type ThreadsListResult = { "threads": Array<ThreadSummary> }
export type ThreadsOpenParams = { "thread_id": string }
export type ThreadsOpenResult = { "thread": ThreadSummary; "messages": Array<ThreadMessage> }
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
export type McpServerStatus = { "name": string; "transport": "stdio" | "http" | "sse"; "status": "connected" | "failed" | "skipped"; "error"?: string; "tool_names": Array<string> }
export type McpStatusResult = { "servers": Array<McpServerStatus>; "total_tools": number }
export type McpAddParams = ({ "name": string; "transport": "stdio"; "command": string; "args"?: Array<string>; "env"?: Record<string, string> }) | ({ "name": string; "transport": "http" | "sse"; "url": string; "headers"?: Record<string, string> })
export type McpAddResult = { "added": boolean; "connected": boolean; "tool_names": Array<string>; "error"?: string | null }
export type McpRemoveParams = { "name": string }
export type McpRemoveResult = { "removed": boolean }
export type HostAttachmentCreateParams = { "origin": string }
export type HostAttachmentCreateResult = { "endpoint": string; "token": string; "expires_at_ms": number }
export type EventBase = { "event_id": string; "type": string; "thread_id": string; "run_id": string; "sequence": number; "timestamp_ms": number; "execution_id"?: string; "parent_execution_id"?: string | null; "agent_id"?: string; "payload": JsonObject }
export type RunStartedPayload = { "resumed": boolean; "skills_snapshot_id"?: string | null; "primary_model"?: RunPrimaryModelBinding; "runtime_profile_id"?: string | null }
export type SkillLoadedPayload = { "skill_id": string; "source": string; "version": string | null; "snapshot_id": string }
export type ContentDeltaPayload = { "text": string }
export type ToolStartedPayload = { "tool_call_id": string; "name": string }
export type ToolDeltaPayload = { "tool_call_id": string; "arguments_delta"?: string; "output_delta"?: string; "truncated"?: boolean; "original_bytes"?: number }
export type ToolResult = { "content": string; "is_error": boolean; "truncated": boolean; "original_bytes": number }
export type ToolCompletedPayload = { "tool_call_id": string; "result": ToolResult }
export type ContextPayload = { "action": string; "estimated_tokens"?: number | null; "input_cap_tokens"?: number | null; "context_window_tokens"?: number | null; "dynamic_tokens"?: number | null; "cache_status"?: string | null; "cached_tokens"?: number | null; "miss_reason"?: string | null; "artifact_ids": Array<string> }
export type InteractionResolvedPayload = { "request_id": string; "type": "approval" | "question" }
export type Usage = { "input_tokens": number; "output_tokens": number }
export type RunCompletedPayload = { "usage": Usage; "duration_ms": number; "finish_reason": string; "context": JsonObject }
export type RunCancelledPayload = { "reason": string }
export type RunFailure = { "code": string; "message": string; "retryable": boolean }
export type RunFailedPayload = { "error": RunFailure }
export type InteractionBase = { "thread_id": string; "run_id": string; "timeout_ms": number; "payload": JsonObject }
export type ApprovalRequest = { "thread_id": string; "run_id": string; "timeout_ms": number; "payload": { "interrupt_id": string; "description": string; "requests": JsonValue; "decisions": Array<"approve_once" | "approve_thread" | "approve_always" | "reject" | "reject_with_feedback"> } }
export type ApprovalResponse = { "decision": "approve_once" | "approve_thread" | "approve_always" | "reject" | "reject_with_feedback"; "feedback"?: string }
export type Question = { "id": string; "question": string; "header": string; "body": string; "options": Array<{ "label": string; "value": string; "description": string }>; "multi_select": boolean; "allow_other": boolean }
export type QuestionRequest = { "thread_id": string; "run_id": string; "timeout_ms": number; "payload": { "interrupt_id": string; "questions": Array<Question> } }
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
export type McpStatusParams = EmptyParams

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
  "mcp.status": { params: McpStatusParams; result: McpStatusResult }
  "mcp.add": { params: McpAddParams; result: McpAddResult }
  "mcp.remove": { params: McpRemoveParams; result: McpRemoveResult }
  "host.attachment.create": { params: HostAttachmentCreateParams; result: HostAttachmentCreateResult }
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
  payload: P
}
export type AgentEvent =
  | AgentEventOf<"run.started", RunStartedPayload>
  | AgentEventOf<"skill.loaded", SkillLoadedPayload>
  | AgentEventOf<"content.delta", ContentDeltaPayload>
  | AgentEventOf<"tool.started", ToolStartedPayload>
  | AgentEventOf<"tool.delta", ToolDeltaPayload>
  | AgentEventOf<"tool.completed", ToolCompletedPayload>
  | AgentEventOf<"context.updated", ContextPayload>
  | AgentEventOf<"interaction.resolved", InteractionResolvedPayload>
  | AgentEventOf<"run.completed", RunCompletedPayload>
  | AgentEventOf<"run.cancelled", RunCancelledPayload>
  | AgentEventOf<"run.failed", RunFailedPayload>
export type EventEnvelope = AgentEvent

export interface InteractionMap {
  "interaction.approval": { params: ApprovalRequest; result: ApprovalResponse }
  "interaction.question": { params: QuestionRequest; result: QuestionResponse }
}
export type InteractionMethod = keyof InteractionMap
export type InteractionRequest = {
  [M in InteractionMethod]: { method: M; id: string; params: InteractionMap[M]["params"] }
}[InteractionMethod]
