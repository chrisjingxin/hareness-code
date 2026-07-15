/** 把 sidecar 流事件折叠为可被 OpenTUI 渲染的确定性状态。 */

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
  interruptId: string
  description: string
  requests?: unknown
}

export type PendingQuestion = {
  interruptId: string
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

type RunEvent = {
  thread_id?: unknown
  run_id?: unknown
  sequence?: unknown
  [key: string]: unknown
}

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

/** 在发送 query 前先登记 run，避免首个流事件与 JSON-RPC 响应同批到达时丢失。 */
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

export function appendNotice(state: TuiState, message: string): TuiState {
  return {
    ...state,
    timeline: [...state.timeline, { type: "message", message: { id: `system-${crypto.randomUUID()}`, role: "system", content: message } }],
  }
}

export function clearThread(state: TuiState): TuiState {
  return createInitialState()
}

export function markCancelling(state: TuiState): TuiState {
  return { ...state, status: "正在取消" }
}

export function clearPendingInteraction(state: TuiState): TuiState {
  return { ...state, pendingApproval: undefined, pendingQuestion: undefined, status: "正在继续执行" }
}

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

/** 丢弃旧 run、重复帧和乱序帧，确保界面不会被过期 sidecar 输出污染。 */
export function applyAgentEvent(state: TuiState, method: string, payload: RunEvent): TuiState {
  const active = state.activeRun
  const threadId = typeof payload.thread_id === "string" ? payload.thread_id : ""
  const runId = typeof payload.run_id === "string" ? payload.run_id : ""
  if (!active || active.threadId !== threadId || active.runId !== runId) return state

  const sequence = typeof payload.sequence === "number" ? payload.sequence : undefined
  const sequenceKey = `${threadId}:${runId}`
  if (sequence !== undefined && sequence <= (state.sequences[sequenceKey] ?? 0)) return state
  const next = sequence === undefined
    ? state
    : { ...state, sequences: { ...state.sequences, [sequenceKey]: sequence } }

  switch (method) {
    case "run/started":
      return { ...next, status: payload.resumed ? "已恢复执行" : "正在思考" }
    case "message/delta":
      return typeof payload.text === "string"
        ? { ...next, timeline: appendAssistantDelta(next.timeline, runId, payload.text), status: "正在生成" }
        : next
    case "tool/started":
      return {
        ...next,
        status: "正在调用工具",
        timeline: updateTool(next.timeline, {
          id: stringValue(payload.tool_id, `tool-${runId}`),
          runId,
          name: stringValue(payload.tool_name, "tool"),
          detail: "",
          status: "running",
        }),
      }
    case "tool/updated":
      return {
        ...next,
        timeline: updateToolDetail(next.timeline, stringValue(payload.tool_id, `tool-${runId}`), stringValue(payload.chunk, "")),
      }
    case "tool/completed":
      return {
        ...next,
        timeline: updateTool(next.timeline, {
          id: stringValue(payload.tool_id, `tool-${runId}`),
          runId,
          name: toolName(next.timeline, stringValue(payload.tool_id, `tool-${runId}`)),
          detail: stringValue(payload.result, ""),
          status: payload.error === true ? "failed" : "completed",
        }),
      }
    case "approval/requested":
      return {
        ...next,
        status: "等待工具审批",
        pendingApproval: {
          interruptId: stringValue(payload.interrupt_id, ""),
          description: stringValue(payload.description, "有操作需要你的审批"),
          requests: payload.requests,
        },
      }
    case "question/requested":
      {
        const question = questionRequest(payload)
        return {
          ...next,
          status: "等待你的回答",
          pendingQuestion: question,
        }
      }
    case "run/completed":
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
    case "run/cancelled":
      return {
        ...next,
        activeRun: undefined,
        pendingApproval: undefined,
        pendingQuestion: undefined,
        status: "已取消",
        lastRun: { runId, outcome: "cancelled" },
        timeline: finishAssistant(next.timeline, runId, `\n已取消：${stringValue(payload.reason, "用户取消")}`),
      }
    case "run/failed":
      return markRunFailed(next, runId, stringValue(payload.message, "Agent 运行失败"))
    default:
      return next
  }
}

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

function updateTool(timeline: TimelineItem[], tool: ToolCard): TimelineItem[] {
  const index = timeline.findIndex(item => item.type === "tool" && item.tool.id === tool.id)
  if (index < 0) return [...timeline, { type: "tool", tool }]
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool" ? { ...item, tool: { ...item.tool, ...tool } } : item
  ))
}

function updateToolDetail(timeline: TimelineItem[], toolId: string, chunk: string): TimelineItem[] {
  const index = timeline.findIndex(item => item.type === "tool" && item.tool.id === toolId)
  if (index < 0) return [...timeline, { type: "tool", tool: { id: toolId, runId: "", name: "tool", detail: chunk, status: "running" } }]
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool" ? { ...item, tool: { ...item.tool, detail: item.tool.detail + chunk } } : item
  ))
}

function toolName(timeline: TimelineItem[], toolId: string): string {
  const item = timeline.find((entry): entry is Extract<TimelineItem, { type: "tool" }> => (
    entry.type === "tool" && entry.tool.id === toolId
  ))
  return item?.tool.name ?? "tool"
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function usageValue(value: unknown): { inputTokens: number; outputTokens: number } | undefined {
  if (!value || typeof value !== "object") return undefined
  const usage = value as Record<string, unknown>
  const inputTokens = numberValue(usage.input_tokens)
  const outputTokens = numberValue(usage.output_tokens)
  if (inputTokens === undefined || outputTokens === undefined) return undefined
  return { inputTokens, outputTokens }
}

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

function questionRequest(payload: RunEvent): PendingQuestion {
  const questions = payload.questions
  const firstQuestion = Array.isArray(questions) && questions[0] && typeof questions[0] === "object"
    ? questions[0] as Record<string, unknown>
    : undefined
  return {
    interruptId: stringValue(payload.interrupt_id, ""),
    question: stringValue(firstQuestion?.question ?? payload.question, "Agent 需要补充信息"),
    options: questionOptions(firstQuestion?.choices ?? payload.options),
  }
}
