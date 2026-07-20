/** za38 v2 跨进程协议公开入口：生成类型、稳定常量与轻量运行时断言。 */

export * from "./generated"

import {
  CLIENT_METHODS,
  EVENT_TYPES,
  PROTOCOL_MAJOR,
  PROTOCOL_MINOR,
  type EventEnvelope,
  type ContextCompactParams,
  type InitializeParams,
  type InteractionRequestEnvelope,
  type JsonRpcMessage,
  type ThreadsListParams,
  type ThreadsOpenParams,
} from "./generated"

export const Method = {
  INITIALIZE: "initialize",
  RUN_START: "run.start",
  RUN_CANCEL: "run.cancel",
  CONTEXT_COMPACT: "context.compact",
  CONFIG_SHOW: "config.show",
  CONFIG_PATH: "config.path",
  THREADS_LIST: "threads.list",
  THREADS_OPEN: "threads.open",
  SKILLS_LIST: "skills.list",
  SKILLS_INSPECT: "skills.inspect",
  SKILLS_SET_ENABLED: "skills.set_enabled",
  SKILLS_INSTALL: "skills.install",
  SKILLS_UPDATE: "skills.update",
  SKILLS_REMOVE: "skills.remove",
  SKILLS_MARKET_LIST: "skills.market.list",
  SHUTDOWN: "shutdown",
  EVENT: "event",
  REQUEST: "request",
} as const

export const PROTOCOL_VERSION = { major: PROTOCOL_MAJOR, minor: PROTOCOL_MINOR } as const

/** 校验初始化请求中决定兼容性的核心字段。完整契约由共享 fixture 在两端验证。 */
export function assertInitializeParams(value: unknown): asserts value is InitializeParams {
  const params = objectValue(value, "initialize params")
  const protocol = objectValue(params.protocol, "initialize protocol")
  if (protocol.major !== PROTOCOL_MAJOR || !integer(protocol.min_minor) || !integer(protocol.max_minor)) {
    throw new Error("initialize protocol 版本范围无效")
  }
  if ((protocol.min_minor as number) > (protocol.max_minor as number)) throw new Error("initialize minor 范围无效")
  const client = objectValue(params.client, "initialize client")
  if (typeof client.name !== "string" || typeof client.version !== "string") throw new Error("initialize client 无效")
  if (!Array.isArray(params.capabilities) || !params.capabilities.every(item => typeof item === "string")) {
    throw new Error("initialize capabilities 无效")
  }
  rejectExtra(params, ["protocol", "client", "capabilities", "cwd", "config_path"], "initialize params")
}

/** 校验恢复选择器读取 thread 摘要时的分页参数。 */
export function assertThreadsListParams(value: unknown): asserts value is ThreadsListParams {
  const params = objectValue(value, "threads.list params")
  if (params.limit !== undefined && (!integer(params.limit) || (params.limit as number) < 1 || (params.limit as number) > 200)) {
    throw new Error("threads.list.limit 无效")
  }
  rejectExtra(params, ["limit"], "threads.list params")
}

/** 校验仅供 TUI 内部使用的 thread_id，用户界面不接受该字段作为文本输入。 */
export function assertThreadsOpenParams(value: unknown): asserts value is ThreadsOpenParams {
  const params = objectValue(value, "threads.open params")
  if (typeof params.thread_id !== "string" || !params.thread_id) throw new Error("threads.open.thread_id 无效")
  rejectExtra(params, ["thread_id"], "threads.open params")
}

/** 校验只接受 TUI 当前 thread 内部 ID 的手动上下文压缩请求。 */
export function assertContextCompactParams(value: unknown): asserts value is ContextCompactParams {
  const params = objectValue(value, "context.compact params")
  if (typeof params.thread_id !== "string" || !params.thread_id) throw new Error("context.compact.thread_id 无效")
  rejectExtra(params, ["thread_id"], "context.compact params")
}

/** 校验 Agent 推送的统一事件信封，并允许未来新增未知事件类型。 */
export function assertEventEnvelope(value: unknown): asserts value is EventEnvelope {
  const event = objectValue(value, "event params")
  for (const field of ["event_id", "type", "thread_id", "run_id"] as const) {
    if (typeof event[field] !== "string" || !event[field]) throw new Error(`event.${field} 无效`)
  }
  if (!integer(event.sequence) || (event.sequence as number) < 1) throw new Error("event.sequence 无效")
  if (!integer(event.timestamp_ms) || (event.timestamp_ms as number) < 0) throw new Error("event.timestamp_ms 无效")
  objectValue(event.payload, "event.payload")
  if (event.source !== undefined) objectValue(event.source, "event.source")
  if (event.extensions !== undefined) objectValue(event.extensions, "event.extensions")
  rejectExtra(event, ["event_id", "type", "thread_id", "run_id", "sequence", "timestamp_ms", "source", "payload", "extensions"], "event")
  validateKnownEventPayload(event.type as string, event.payload as Record<string, unknown>)
}

/** 校验需要客户端响应的审批或问答信封。 */
export function assertInteractionRequest(value: unknown): asserts value is InteractionRequestEnvelope {
  const request = objectValue(value, "request params")
  for (const field of ["request_id", "thread_id", "run_id"] as const) {
    if (typeof request[field] !== "string" || !request[field]) throw new Error(`request.${field} 无效`)
  }
  if (request.type !== "approval" && request.type !== "question") throw new Error("request.type 无效")
  if (!integer(request.sequence) || (request.sequence as number) < 1) throw new Error("request.sequence 无效")
  if (!integer(request.timeout_ms) || (request.timeout_ms as number) < 1) throw new Error("request.timeout_ms 无效")
  objectValue(request.payload, "request.payload")
  rejectExtra(request, ["request_id", "type", "thread_id", "run_id", "sequence", "timeout_ms", "payload"], "request")
  validateInteractionPayload(request.type, request.payload as Record<string, unknown>)
}

/** 对 JSON-RPC 信封做方向无关的基础校验。 */
export function assertJsonRpcMessage(value: unknown): asserts value is JsonRpcMessage {
  const message = objectValue(value, "JSON-RPC message")
  if (message.jsonrpc !== "2.0") throw new Error("jsonrpc 必须为 2.0")
  const hasMethod = typeof message.method === "string"
  const hasResult = "result" in message
  const hasError = "error" in message
  if (hasMethod) {
    if (message.id !== undefined && typeof message.id !== "string") throw new Error("JSON-RPC id 必须为字符串")
    return
  }
  if ((message.id !== null && typeof message.id !== "string") || Number(hasResult) + Number(hasError) !== 1) throw new Error("JSON-RPC response 无效")
}

export function isClientMethod(value: string): boolean {
  return (CLIENT_METHODS as readonly string[]).includes(value)
}

export function isKnownEventType(value: string): boolean {
  return (EVENT_TYPES as readonly string[]).includes(value)
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(`${label} 必须为对象`)
  return value as Record<string, unknown>
}

function integer(value: unknown): boolean {
  return typeof value === "number" && Number.isInteger(value)
}

function rejectExtra(value: Record<string, unknown>, allowed: readonly string[], label: string): void {
  const extra = Object.keys(value).find(key => !allowed.includes(key))
  if (extra) throw new Error(`${label} 包含未知字段：${extra}`)
}

function validateKnownEventPayload(type: string, payload: Record<string, unknown>): void {
  const fields: Record<string, readonly string[]> = {
    "run.started": ["resumed", "skills_snapshot_id"],
    "skill.loaded": ["skill_id", "source", "version", "snapshot_id"],
    "content.delta": ["text"],
    "thinking.delta": ["text"],
    "tool.started": ["tool_call_id", "name"],
    "tool.delta": ["tool_call_id", "arguments_delta", "output_delta", "truncated", "original_bytes"],
    "tool.completed": ["tool_call_id", "result"],
    "context.updated": ["action", "estimated_tokens", "input_cap_tokens", "context_window_tokens", "dynamic_tokens", "cache_status", "cached_tokens", "miss_reason", "artifact_ids"],
    "interaction.resolved": ["request_id", "type"],
    "run.completed": ["usage", "duration_ms", "finish_reason", "context"],
    "run.cancelled": ["reason"],
    "run.failed": ["error"],
  }
  const allowed = fields[type]
  if (!allowed) return
  rejectExtra(payload, allowed, `${type} payload`)
  if (["content.delta", "thinking.delta"].includes(type) && typeof payload.text !== "string") throw new Error(`${type}.text 无效`)
  if (type === "skill.loaded" && (typeof payload.skill_id !== "string" || typeof payload.source !== "string" || typeof payload.snapshot_id !== "string")) throw new Error("skill.loaded payload 无效")
  if (type.startsWith("tool.") && typeof payload.tool_call_id !== "string") throw new Error(`${type}.tool_call_id 无效`)
  if (type === "tool.started" && typeof payload.name !== "string") throw new Error("tool.started.name 无效")
  if (type === "tool.completed") objectValue(payload.result, "tool.completed.result")
  if (type === "run.failed") objectValue(payload.error, "run.failed.error")
}

function validateInteractionPayload(type: unknown, payload: Record<string, unknown>): void {
  if (type === "approval") {
    rejectExtra(payload, ["interrupt_id", "description", "requests", "decisions"], "approval payload")
    if (typeof payload.interrupt_id !== "string" || typeof payload.description !== "string" || !Array.isArray(payload.decisions)) throw new Error("approval payload 无效")
    return
  }
  rejectExtra(payload, ["interrupt_id", "questions"], "question payload")
  if (typeof payload.interrupt_id !== "string" || !Array.isArray(payload.questions)) throw new Error("question payload 无效")
}
