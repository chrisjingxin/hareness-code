/** 此文件由 packages/protocol/scripts/generate.ts 生成，请勿手工修改。 */

export const PROTOCOL_MAJOR = 2 as const
export const PROTOCOL_MINOR = 1 as const
export const MAX_FRAME_BYTES = 8388608 as const
export const MAX_TOOL_PAYLOAD_BYTES = 1048576 as const
export const CLIENT_METHODS = ["initialize","run.start","run.cancel","config.show","config.path","skills.list","skills.inspect","skills.set_enabled","skills.install","skills.update","skills.remove","skills.market.list","shutdown"] as const
export const SERVER_METHODS = ["event","request"] as const
export const SERVER_CAPABILITIES = ["run.cancel","run.multithread","interactive.approval","interactive.question","config.read","skills.read","skills.manage"] as const
export const EVENT_TYPES = ["run.started","skill.loaded","content.delta","thinking.delta","tool.started","tool.delta","tool.completed","interaction.resolved","run.completed","run.cancelled","run.failed"] as const

export type JsonObject = Record<string, unknown>
export type JsonRpcErrorObject = { code: number; message: string; data?: unknown }
export type JsonRpcRequest = { jsonrpc: "2.0"; method: string; params?: JsonObject; id: string }
export type JsonRpcNotification = { jsonrpc: "2.0"; method: string; params?: JsonObject }
export type JsonRpcResponse = { jsonrpc: "2.0"; result?: unknown; error?: JsonRpcErrorObject; id: string | null }
export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse

export interface InitializeParams { protocol: { major: 2; min_minor: number; max_minor: number }; client: { name: string; version: string }; capabilities: string[]; cwd?: string; config_path?: string }
export interface InitializeResult { protocol: { major: 2; minor: number }; server: { name: string; version: string }; server_capabilities: string[]; enabled_capabilities: string[]; agent_commands: Array<{ name: string; description: string; aliases: string[] }>; skills_snapshot: { id: string; count: number }; skill_diagnostics: string[]; limits: { max_frame_bytes: number; max_tool_payload_bytes: number }; config_summary: JsonObject | null; startup_error: { code: string; message: string } | null }
export interface RequestedSkill { id: string; args?: string }
export interface RunStartParams { message: string; thread_id?: string; run_id?: string; requested_skill?: RequestedSkill }
export interface RunStartResult { thread_id: string; run_id: string; accepted: boolean }
export interface RunCancelParams { thread_id: string; run_id: string }
export interface RunCancelResult { cancelled: boolean; run_id: string }
export interface EventEnvelope { event_id: string; type: string; thread_id: string; run_id: string; sequence: number; timestamp_ms: number; source?: { kind: "root" | "subagent" | "background"; id?: string; parent_tool_call_id?: string }; payload: JsonObject; extensions?: JsonObject }
export interface InteractionRequestEnvelope { request_id: string; type: "approval" | "question"; thread_id: string; run_id: string; sequence: number; timeout_ms: number; payload: JsonObject }
export interface ApprovalResponse { type: "approval"; request_id: string; decision: "approve_once" | "approve_session" | "reject"; feedback?: string }
export interface QuestionResponse { type: "question"; request_id: string; answers: Record<string, string[]> }
export type InteractionResponse = ApprovalResponse | QuestionResponse
