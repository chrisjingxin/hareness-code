/** JSON-RPC 2.0 message types shared between Node and Python. */

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
  id: number
}

export interface JsonRpcNotification {
  jsonrpc: "2.0"
  method: string
  params?: Record<string, unknown>
}

export type JsonRpcMessage = JsonRpcRequest | JsonRpcResponse | JsonRpcNotification

/** Node → Python request methods */
export const Method = {
  INITIALIZE: "initialize",
  QUERY: "query",
  CANCEL: "cancel",
  RESPOND: "respond",
  SHUTDOWN: "shutdown",
} as const

/** Python → Node notification methods */
export const Notification = {
  STREAM_TEXT: "stream/text",
  STREAM_TOOL_START: "stream/tool_start",
  STREAM_TOOL_CHUNK: "stream/tool_chunk",
  STREAM_TOOL_RESULT: "stream/tool_result",
  STREAM_PLAN: "stream/plan",
  STREAM_DONE: "stream/done",
  STREAM_ERROR: "stream/error",
  STREAM_INTERRUPT: "stream/interrupt",
  LOG: "log",
} as const

export interface InitializeParams {
  client_info: { name: string; version: string }
}

export interface InitializeResult {
  server_info: { name: string; version: string }
  capabilities: { streaming: boolean; hitl: boolean }
}

export interface QueryParams {
  message: string
  thread_id?: string
}

export interface QueryResult {
  thread_id: string
  accepted: boolean
}

export interface StreamTextParams {
  text: string
  thread_id: string
}

export interface StreamToolStartParams {
  tool_name: string
  tool_id: string
  args: Record<string, unknown>
}

export interface StreamToolResultParams {
  tool_id: string
  result: string
  error?: string
}

export interface StreamDoneParams {
  thread_id: string
  usage: { input_tokens: number; output_tokens: number }
}

export interface StreamErrorParams {
  message: string
  code: string
}
