/** 把 sidecar 流事件折叠为可被 OpenTUI 渲染的确定性状态。 */

import type { EventEnvelope, InteractionRequestEnvelope } from "@za38/protocol"

export type MessageRole = "user" | "assistant" | "system"

export type ConversationMessage = {
  id: string
  role: MessageRole
  content: string
  runId?: string
  streaming?: boolean
}

export type ToolCard = {
  id: string
  runId: string
  name: string
  detail: string
  status: "running" | "completed" | "failed"
}

/**
 * JSON-RPC 的 sequence 是唯一可靠的时间顺序。保留统一时间线可避免工具调用
 * 被单独收集后统一渲染到回答末尾，破坏用户理解 Agent 执行过程的因果关系。
 */
export type TimelineItem =
  | { type: "message"; message: ConversationMessage }
  | { type: "tool"; tool: ToolCard }

export type ActiveRun = {
  threadId: string
  runId: string
}

export type PendingApproval = {
  requestId: string
  description: string
  requests?: unknown
}

export type PendingQuestion = {
  requestId: string
  questionId: string
  question: string
  options: Array<{ name: string; value: string }>
}

export type TuiState = {
  threadId?: string
  activeRun?: ActiveRun
  timeline: TimelineItem[]
  status: string
  pendingApproval?: PendingApproval
  pendingQuestion?: PendingQuestion
  lastRun?: RunSummary
  sequences: Record<string, number>
}

export type RunSummary = {
  runId: string
  outcome: "completed" | "cancelled" | "failed"
  durationMs?: number
  usage?: { inputTokens: number; outputTokens: number }
}

/** 创建无会话内容的初始状态，可选保留待恢复的线程标识。 */
export function createInitialState(threadId?: string): TuiState {
  return {
    threadId,
    timeline: [],
    status: "就绪",
    sequences: {},
  }
}

/** 空状态不应被欢迎文本污染，/clear 后才能可靠地回到沉浸式首页。 */
export function isHomeState(state: TuiState): boolean {
  return !state.activeRun
    && !state.pendingApproval
    && !state.pendingQuestion
    && state.timeline.length === 0
}

/** 在发送 run.start 前先登记 run，避免首个流事件与 JSON-RPC 响应相邻到达时丢失。 */
export function startRun(state: TuiState, run: ActiveRun, prompt: string): TuiState {
  return {
    ...state,
    threadId: run.threadId,
    activeRun: run,
    pendingApproval: undefined,
    pendingQuestion: undefined,
    lastRun: undefined,
    status: "正在思考",
    timeline: [
      ...state.timeline,
      { type: "message", message: { id: `user-${run.runId}`, role: "user", content: prompt, runId: run.runId } },
      { type: "message", message: { id: `assistant-${run.runId}`, role: "assistant", content: "", runId: run.runId, streaming: true } },
    ],
  }
}

/** 将协议或本地系统通知追加到统一时间线。 */
export function appendNotice(state: TuiState, message: string): TuiState {
  return {
    ...state,
    timeline: [...state.timeline, { type: "message", message: { id: `system-${crypto.randomUUID()}`, role: "system", content: message } }],
  }
}

/** 清空当前线程并返回沉浸式首页初始状态。 */
export function clearThread(state: TuiState): TuiState {
  return createInitialState()
}

/** 将运行状态切换为取消中，等待 sidecar 返回终态事件。 */
export function markCancelling(state: TuiState): TuiState {
  return { ...state, status: "正在取消" }
}

/** 清除审批/提问中断并把运行标记为继续执行。 */
export function clearPendingInteraction(state: TuiState): TuiState {
  return { ...state, pendingApproval: undefined, pendingQuestion: undefined, status: "正在继续执行" }
}

/** 用失败终态结束指定运行，并把错误文本追加到时间线末尾。 */
export function markRunFailed(state: TuiState, runId: string, message: string): TuiState {
  if (state.activeRun?.runId !== runId) return state
  return {
    ...state,
    activeRun: undefined,
    pendingApproval: undefined,
    pendingQuestion: undefined,
    status: "执行失败",
    lastRun: { runId, outcome: "failed" },
    timeline: finishAssistant(state.timeline, runId, `\n错误：${message}`),
  }
}

/** 接收 Agent 的反向交互请求，并让它与流事件共享同一 sequence。 */
export function applyInteractionRequest(state: TuiState, request: InteractionRequestEnvelope): TuiState {
  const active = state.activeRun
  if (!active || active.threadId !== request.thread_id || active.runId !== request.run_id) return state
  const next = acceptSequence(state, request.thread_id, request.run_id, request.sequence)
  if (!next) return state
  if (request.type === "approval") {
    return {
      ...next,
      status: "等待工具审批",
      pendingApproval: {
        requestId: request.request_id,
        description: stringValue(request.payload.description, "有操作需要你的审批"),
        requests: request.payload.requests,
      },
    }
  }
  return {
    ...next,
    status: "等待你的回答",
    pendingQuestion: questionRequest(request),
  }
}

/** 丢弃旧 run、重复帧和乱序帧；序号缺口以系统通知报告但不崩溃。 */
export function applyAgentEvent(state: TuiState, event: EventEnvelope): TuiState {
  const active = state.activeRun
  if (!active || active.threadId !== event.thread_id || active.runId !== event.run_id) return state
  const next = acceptSequence(state, event.thread_id, event.run_id, event.sequence)
  if (!next) return state
  const payload = event.payload
  const runId = event.run_id

  switch (event.type) {
    case "run.started":
      return { ...next, status: payload.resumed ? "已恢复执行" : "正在思考" }
    case "content.delta":
      return typeof payload.text === "string"
        ? { ...next, timeline: appendAssistantDelta(next.timeline, runId, payload.text), status: "正在生成" }
        : next
    case "thinking.delta":
      return { ...next, status: "正在思考" }
    case "tool.started":
      return {
        ...next,
        status: "正在调用工具",
        timeline: updateTool(next.timeline, {
          id: stringValue(payload.tool_call_id, `tool-${runId}`),
          runId,
          name: stringValue(payload.name, "tool"),
          detail: "",
          status: "running",
        }),
      }
    case "tool.delta":
      return {
        ...next,
        timeline: updateToolDetail(next.timeline, stringValue(payload.tool_call_id, `tool-${runId}`), stringValue(payload.arguments_delta ?? payload.output_delta, "")),
      }
    case "tool.completed":
      {
        const result = objectRecord(payload.result)
        const toolId = stringValue(payload.tool_call_id, `tool-${runId}`)
        return {
          ...next,
          timeline: updateTool(next.timeline, {
            id: toolId,
            runId,
            name: toolName(next.timeline, toolId),
            detail: stringValue(result.content, ""),
            status: result.is_error === true ? "failed" : "completed",
          }),
        }
      }
    case "interaction.resolved":
      return {
        ...next,
        pendingApproval: undefined,
        pendingQuestion: undefined,
        status: "正在继续执行",
      }
    case "run.completed":
      return {
        ...next,
        activeRun: undefined,
        pendingApproval: undefined,
        pendingQuestion: undefined,
        status: "已完成",
        lastRun: {
          runId,
          outcome: "completed",
          durationMs: numberValue(payload.duration_ms),
          usage: usageValue(payload.usage),
        },
        timeline: finishAssistant(next.timeline, runId),
      }
    case "run.cancelled":
      return {
        ...next,
        activeRun: undefined,
        pendingApproval: undefined,
        pendingQuestion: undefined,
        status: "已取消",
        lastRun: { runId, outcome: "cancelled" },
        timeline: finishAssistant(next.timeline, runId, `\n已取消：${stringValue(payload.reason, "用户取消")}`),
      }
    case "run.failed":
      return markRunFailed(next, runId, stringValue(objectRecord(payload.error).message, "Agent 运行失败"))
    default:
      return next
  }
}

/** 返回更新过 sequence 的状态；重复/倒序返回 null，缺口则追加可见诊断。 */
function acceptSequence(state: TuiState, threadId: string, runId: string, sequence: number): TuiState | null {
  const key = `${threadId}:${runId}`
  const previous = state.sequences[key] ?? 0
  if (sequence <= previous) return null
  const withSequence = { ...state, sequences: { ...state.sequences, [key]: sequence } }
  return previous > 0 && sequence > previous + 1
    ? appendNotice(withSequence, `协议序号缺口：${previous} → ${sequence}`)
    : withSequence
}

/** 将增量追加到末尾回答；工具之后收到文本时创建新的回答项。 */
function appendAssistantDelta(timeline: TimelineItem[], runId: string, text: string): TimelineItem[] {
  const index = timeline.findLastIndex(item => item.type === "message" && item.message.role === "assistant" && item.message.runId === runId)
  if (index < 0) {
    return [...timeline, { type: "message", message: { id: `assistant-${runId}-${crypto.randomUUID()}`, role: "assistant", content: text, runId, streaming: true } }]
  }
  const item = timeline[index]
  if (item?.type === "message" && index === timeline.length - 1) {
    return timeline.map((entry, itemIndex) => (
      itemIndex === index && entry.type === "message"
        ? { ...entry, message: { ...entry.message, content: entry.message.content + text } }
        : entry
    ))
  }
  return [...timeline, { type: "message", message: { id: `assistant-${runId}-${crypto.randomUUID()}`, role: "assistant", content: text, runId, streaming: true } }]
}

/** 结束同一 run 的所有流式回答，并在取消/失败时追加终态文本。 */
function finishAssistant(timeline: TimelineItem[], runId: string, suffix = ""): TimelineItem[] {
  const settled = timeline.map(entry => {
    if (entry.type !== "message" || entry.message.role !== "assistant" || entry.message.runId !== runId) return entry
    return {
      ...entry,
      message: {
        ...entry.message,
        streaming: false,
      },
    }
  })
  if (!suffix) return settled
  // 终态错误必须排在最后一个工具之后，不能回写到已完成的回答片段中。
  return [
    ...settled,
    {
      type: "message",
      message: {
        id: `assistant-${runId}-terminal-${crypto.randomUUID()}`,
        role: "assistant",
        content: suffix.trimStart(),
        runId,
        streaming: false,
      },
    },
  ]
}

/** 插入或合并工具卡片，保持其第一次出现时的时间线位置。 */
function updateTool(timeline: TimelineItem[], tool: ToolCard): TimelineItem[] {
  const index = timeline.findIndex(item => item.type === "tool" && item.tool.id === tool.id)
  if (index < 0) return [...timeline, { type: "tool", tool }]
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool" ? { ...item, tool: { ...item.tool, ...tool } } : item
  ))
}

/** 将工具输出 chunk 追加到对应工具；缺失 started 事件时创建兜底卡片。 */
function updateToolDetail(timeline: TimelineItem[], toolId: string, chunk: string): TimelineItem[] {
  const index = timeline.findIndex(item => item.type === "tool" && item.tool.id === toolId)
  if (index < 0) return [...timeline, { type: "tool", tool: { id: toolId, runId: "", name: "tool", detail: chunk, status: "running" } }]
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool" ? { ...item, tool: { ...item.tool, detail: item.tool.detail + chunk } } : item
  ))
}

/** 从时间线读取工具名称，完成事件缺少名称时使用安全回退。 */
function toolName(timeline: TimelineItem[], toolId: string): string {
  const item = timeline.find((entry): entry is Extract<TimelineItem, { type: "tool" }> => (
    entry.type === "tool" && entry.tool.id === toolId
  ))
  return item?.tool.name ?? "tool"
}

/** 从不可信事件 payload 读取字符串字段。 */
function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback
}

/** 读取有限数值字段，过滤 NaN、Infinity 和其他类型。 */
function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

/** 将协议 usage 转成前端 camelCase 摘要。 */
function usageValue(value: unknown): { inputTokens: number; outputTokens: number } | undefined {
  if (!value || typeof value !== "object") return undefined
  const usage = value as Record<string, unknown>
  const inputTokens = numberValue(usage.input_tokens)
  const outputTokens = numberValue(usage.output_tokens)
  if (inputTokens === undefined || outputTokens === undefined) return undefined
  return { inputTokens, outputTokens }
}

/** 兼容字符串和对象两种提问选项格式。 */
function questionOptions(value: unknown): Array<{ name: string; value: string }> {
  if (!Array.isArray(value)) return []
  return value.flatMap(option => {
    if (typeof option === "string") return [{ name: option, value: option }]
    if (option && typeof option === "object") {
      const record = option as Record<string, unknown>
      const label = stringValue(record.label ?? record.value, "")
      return label ? [{ name: label, value: label }] : []
    }
    return []
  })
}

/** 从完整问题组提取当前 UI 能渲染的首题和选项。 */
function questionRequest(request: InteractionRequestEnvelope): PendingQuestion {
  const questions = request.payload.questions
  const firstQuestion = Array.isArray(questions) && questions[0] && typeof questions[0] === "object"
    ? questions[0] as Record<string, unknown>
    : undefined
  return {
    requestId: request.request_id,
    questionId: stringValue(firstQuestion?.id, "question-1"),
    question: stringValue(firstQuestion?.question, "Agent 需要补充信息"),
    options: questionOptions(firstQuestion?.options),
  }
}

function objectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
