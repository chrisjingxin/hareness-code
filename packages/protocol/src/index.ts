/** Version 1 JSON-RPC contract for the Bun presentation layer and Python sidecar. */

export const PROTOCOL_VERSION = 1 as const

export interface JsonRpcRequest {
  jsonrpc: "2.0"
  method: string
  params?: Record<string, unknown>
  id: number
}

export interface JsonRpcResponse {
  jsonrpc: "2.0"
  result?: unknown
  error?: { code: number; message: string }
  id: number | string | null
}

export interface JsonRpcNotification {
  jsonrpc: "2.0"
  method: string
  params?: Record<string, unknown>
}

export type JsonRpcMessage = JsonRpcRequest | JsonRpcResponse | JsonRpcNotification

export const Method = {
  INITIALIZE: "initialize",
  QUERY: "query",
  CANCEL: "cancel",
  RESPOND: "respond",
  CONFIG_SHOW: "config.show",
  CONFIG_PATH: "config.path",
  SHUTDOWN: "shutdown",
} as const

export const Notification = {
  RUN_STARTED: "run/started",
  MESSAGE_DELTA: "message/delta",
  TOOL_STARTED: "tool/started",
  TOOL_UPDATED: "tool/updated",
  TOOL_COMPLETED: "tool/completed",
  PLAN_UPDATED: "plan/updated",
  APPROVAL_REQUESTED: "approval/requested",
  QUESTION_REQUESTED: "question/requested",
  HEARTBEAT: "heartbeat",
  RUN_COMPLETED: "run/completed",
  RUN_CANCELLED: "run/cancelled",
  RUN_FAILED: "run/failed",
  LOG: "log",
} as const

export interface InitializeParams {
  client_info: { name: string; version: string }
  cwd?: string
  config_path?: string
}

export interface InitializeResult {
  server_info: { name: string; version: string }
  protocol_version: number
  capabilities: Record<string, boolean>
  config: Record<string, unknown> | null
  startup_error: string | null
}

export interface QueryParams {
  message: string
  thread_id?: string
  run_id?: string
}

export interface QueryResult {
  thread_id: string
  run_id: string
  accepted: boolean
}

export interface RunEventBase {
  thread_id: string
  run_id: string
  sequence: number
}

export interface MessageDeltaParams extends RunEventBase {
  text: string
}

export interface RunCompletedParams extends RunEventBase {
  usage: { input_tokens: number; output_tokens: number }
  duration_ms: number
}

export interface RunFailedParams extends RunEventBase {
  code: string
  message: string
}

export interface ApprovalRequestedParams extends RunEventBase {
  interrupt_id: string
  description: string
  requests: unknown
}

export interface QuestionOption {
  label: string
  value: string
}

export interface QuestionRequestedParams extends RunEventBase {
  interrupt_id: string
  question: string
  options: QuestionOption[]
  questions?: unknown
}
