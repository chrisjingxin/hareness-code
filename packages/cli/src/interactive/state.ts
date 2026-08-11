/** Interactive Core 的纯 reducer：把 sidecar 流事件折叠为稳定领域状态。 */

import { EventType, type EventEnvelope, type InteractionRequestEnvelope } from "@za38/protocol"
import type { IdGenerator } from "./ports/id-generator"

export type MessageRole = "user" | "assistant" | "system"

/** 工作模式在 Run 受理时冻结；同一 Thread 相邻 Run 之间可在空闲时切换。 */
export type WorkMode = "build" | "compose"

export type ComposeStageId = "understand" | "plan" | "build" | "verify" | "review"

/** compose.state 的有界完整 projection；revision 单调递增，迟到帧被拒绝。 */
export type ComposeProjection = {
  revision: number
  stage: ComposeStageId
  status: "running" | "waiting_user" | "blocked" | "completed" | "failed" | "cancelled"
  stages: Array<{ id: ComposeStageId; status: string; attempts: number }>
  tasks: Array<{ id: string; title: string; status: string }>
  evidence: Array<{ label: string; status: string }>
  blockedReason: string | null
}

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
  arguments: string
  output: string
  status: "running" | "completed" | "failed"
}

export type InteractionCard = {
  id: string
  runId: string
  type: "approval" | "question"
  status: "pending" | "approved" | "rejected" | "answered" | "resolved" | "cancelled"
  description?: string
  requests?: unknown
  question?: string
  options?: Array<{ name: string; value: string }>
}

/**
 * JSON-RPC 的 sequence 是唯一可靠的时间顺序。
 */
export type TimelineItem =
  | { type: "message"; message: ConversationMessage }
  | { type: "tool"; tool: ToolCard }
  | { type: "reasoning"; reasoning: ReasoningCard }
  | { type: "interaction"; interaction: InteractionCard }

export type ActiveRun = {
  threadId: string
  runId: string
}

export type RunSummary = {
  runId: string
  outcome: "completed" | "cancelled" | "failed"
  durationMs?: number
  usage?: { inputTokens: number; outputTokens: number }
  context?: { action: string; estimatedTokens?: number; inputCapTokens?: number }
  /** Compose Run 的终态快照：完整 projection，供 Timeline 结果摘要（失败/取消后仍可见）。 */
  composeSummary?: ComposeProjection
}

/** 当前 Run 的事实模型阶段；不描述未观测到的内部步骤。 */
export type RunProgress = {
  phase: "preparing" | "model"
  elapsedMs: number
}

/** 时间线中的思考条目：按流式顺序与消息/工具交错，冻结后可折叠。 */
export type ReasoningCard = {
  id: string
  runId: string
  text: string
  active: boolean
}

export type RestoredThreadMessage = {
  kind: "user" | "assistant" | "tool"
  content: string
  toolName?: string
}

/**
 * 运行活动仅使用领域 kind 表达语义，Presenter 层负责将 kind 转换为具体语言的展示文案。
 */
export type InteractiveActivity =
  | { kind: "home" }
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "running" }
  | { kind: "waiting-interaction" }
  | { kind: "cancelling" }
  | { kind: "completed" }
  | { kind: "cancelled" }
  | { kind: "failed" }

let defaultCounter = 0
const defaultIdGenerator: IdGenerator = {
  uuid: () => `id-${++defaultCounter}`,
}

/** 共享 reducer 的内部状态；currentThreadId 用 null 明确表示空首页。 */
export type InteractiveState = {
  currentThreadId: string | null
  activeRun: ActiveRun | null
  timeline: TimelineItem[]
  activity: InteractiveActivity
  /** 仅当前 Run 可见的事实进度，不进入 Timeline 或 Thread 历史。 */
  runProgress: RunProgress | null
  lastRun?: RunSummary
  sequences: Record<string, number>
  /** 当前 Thread 下一次 Run 的工作模式；Run 受理后冻结。 */
  workMode: WorkMode
  /** 当前 active Run 的 Compose 投影；仅接受 revision 更新的帧。 */
  composeState: ComposeProjection | null
}

/** 创建无 thread 内容的初始状态；显式 null 进入空首页。 */
export function createInitialState(threadId: string | null = null, workMode: WorkMode = "build"): InteractiveState {
  return {
    currentThreadId: threadId,
    activeRun: null,
    timeline: [],
    activity: { kind: threadId ? "idle" : "home" },
    runProgress: null,
    sequences: {},
    workMode,
    composeState: null,
  }
}

/** 空闲时切换下一次 Run 的工作模式；busy 门禁由 feature/controller 负责。 */
export function setWorkMode(state: InteractiveState, mode: WorkMode): InteractiveState {
  if (state.workMode === mode) return state
  return { ...state, workMode: mode }
}

/** 折叠一帧 compose.state projection；revision 不递增的迟到帧被拒绝。 */
export function applyComposeState(state: InteractiveState, payload: unknown): InteractiveState {
  const active = state.activeRun
  if (!active) return state
  const projection = parseComposeProjection(payload)
  if (!projection) return state
  const current = state.composeState
  if (current !== null && projection.revision <= current.revision) return state
  const activity: InteractiveActivity =
    projection.status === "waiting_user"
      ? { kind: "waiting-interaction" }
      : { kind: "running" }
  return { ...state, composeState: projection, activity }
}

const COMPOSE_STAGE_IDS: readonly ComposeStageId[] = ["understand", "plan", "build", "verify", "review"]
const COMPOSE_STATUSES = new Set(["running", "waiting_user", "blocked", "completed", "failed", "cancelled"])

function parseComposeProjection(value: unknown): ComposeProjection | null {
  if (!value || typeof value !== "object") return null
  const raw = value as Record<string, unknown>
  if (!Number.isInteger(raw.revision) || (raw.revision as number) < 0) return null
  if (typeof raw.stage !== "string" || !COMPOSE_STAGE_IDS.includes(raw.stage as ComposeStageId)) return null
  if (typeof raw.status !== "string" || !COMPOSE_STATUSES.has(raw.status)) return null
  if (!Array.isArray(raw.stages) || !Array.isArray(raw.tasks) || !Array.isArray(raw.evidence)) return null
  const stages = raw.stages.map(item => {
    const entry = item as Record<string, unknown>
    if (typeof entry.id !== "string" || !COMPOSE_STAGE_IDS.includes(entry.id as ComposeStageId)) return null
    if (typeof entry.status !== "string" || typeof entry.attempts !== "number") return null
    return { id: entry.id as ComposeStageId, status: entry.status, attempts: entry.attempts }
  })
  const tasks = raw.tasks.map(item => {
    const entry = item as Record<string, unknown>
    if (typeof entry.id !== "string" || typeof entry.title !== "string" || typeof entry.status !== "string") return null
    return { id: entry.id, title: entry.title, status: entry.status }
  })
  const evidence = raw.evidence.map(item => {
    const entry = item as Record<string, unknown>
    if (typeof entry.label !== "string" || typeof entry.status !== "string") return null
    return { label: entry.label, status: entry.status }
  })
  if (stages.some(item => item === null) || tasks.some(item => item === null) || evidence.some(item => item === null)) return null
  return {
    revision: raw.revision as number,
    stage: raw.stage as ComposeStageId,
    status: raw.status as ComposeProjection["status"],
    stages: stages as ComposeProjection["stages"],
    tasks: tasks as ComposeProjection["tasks"],
    evidence: evidence as ComposeProjection["evidence"],
    blockedReason: typeof raw.blocked_reason === "string" ? raw.blocked_reason : null,
  }
}

/** 空状态不应被欢迎文本污染，/new 后才能可靠地回到沉浸式首页。 */
export function isHomeState(state: { activeRun: InteractiveState["activeRun"]; timeline: readonly TimelineItem[]; activity: InteractiveState["activity"] }): boolean {
  return !state.activeRun
    && state.timeline.length === 0
    && state.activity.kind !== "waiting-interaction"
}

/** 在发送 run.start 前先登记 run。 */
export function startRun(state: InteractiveState, run: ActiveRun, prompt: string): InteractiveState {
  return {
    ...state,
    currentThreadId: run.threadId,
    activeRun: run,
    lastRun: undefined,
    activity: { kind: "starting" },
    runProgress: { phase: "preparing", elapsedMs: 0 },
    timeline: [
      ...state.timeline,
      { type: "message", message: { id: `user-${run.runId}`, role: "user", content: prompt, runId: run.runId } },
    ],
  }
}

/** 将协议或本地系统通知追加到统一时间线。 */
export function appendNotice(state: InteractiveState, message: string, idGenerator: IdGenerator = defaultIdGenerator): InteractiveState {
  return {
    ...state,
    timeline: [...state.timeline, { type: "message", message: { id: `system-${idGenerator.uuid()}`, role: "system", content: message } }],
  }
}

/** 清空当前 thread 并返回沉浸式首页初始状态；Work Mode 是会话级选择。 */
export function clearThread(state: InteractiveState): InteractiveState {
  return createInitialState(null, state.workMode)
}

/**
 * 原子替换当前 thread 的历史，清除旧运行、交互和 sequence；
 * workMode 是会话级选择，跨 thread 切换保留。
 */
export function restoreThread(
  threadId: string,
  messages: readonly RestoredThreadMessage[],
  workMode: WorkMode = "build",
): InteractiveState {
  const restoredRunId = `restored-${threadId}`
  const timeline: TimelineItem[] = messages.map((message, index) => {
    const id = `restored-${index + 1}`
    if (message.kind === "tool") {
      return {
        type: "tool",
        tool: {
          id,
          runId: restoredRunId,
          name: message.toolName || "tool",
          arguments: "",
          output: message.content,
          status: "completed",
        },
      }
    }
    return {
      type: "message",
      message: {
        id,
        role: message.kind,
        content: message.content,
        runId: restoredRunId,
        streaming: false,
      },
    }
  })
  return {
    currentThreadId: threadId,
    activeRun: null,
    timeline,
    // 恢复已完成（timeline 已构建）：活动状态必须是 idle，不得停在 restoring。
    activity: { kind: "idle" },
    runProgress: null,
    sequences: {},
    workMode,
    composeState: null,
  }
}

/** 根据后端 RPC 注册反向 Interaction。 */
export function applyInteractionRequest(state: InteractiveState, envelope: InteractionRequestEnvelope): InteractiveState {
  const active = state.activeRun
  if (!active || active.threadId !== envelope.thread_id || active.runId !== envelope.run_id) return state

  const existingIndex = state.timeline.findIndex(item => item.type === "interaction" && item.interaction.id === envelope.request_id)

  const req = (envelope as unknown as { request?: Record<string, unknown> }).request ?? (envelope as unknown as Record<string, unknown>)
  const kind = (req.type ?? req.kind) as string | undefined

  if (kind === "approval") {
    const payload = req.payload && typeof req.payload === "object" ? req.payload as Record<string, unknown> : {}
    const description = typeof payload.description === "string"
      ? payload.description
      : (req.prompt ?? req.reason ?? "") as string
    const card: InteractionCard = {
      id: envelope.request_id,
      runId: envelope.run_id,
      type: "approval",
      status: "pending",
      description,
      requests: req,
    }
    const timeline = existingIndex >= 0
      ? state.timeline.map((item, index) => index === existingIndex ? { type: "interaction" as const, interaction: card } : item)
      : [...state.timeline, { type: "interaction" as const, interaction: card }]
    return {
      ...state,
      activity: { kind: "waiting-interaction" },
      timeline,
    }
  }

  if (kind === "question") {
    const questions = req.questions as Array<{ header?: string; question?: string; options?: Array<{ label: string; value: string }> }> | undefined
    const firstQuestion = questions?.[0]
    const card: InteractionCard = {
      id: envelope.request_id,
      runId: envelope.run_id,
      type: "question",
      status: "pending",
      question: firstQuestion?.header || firstQuestion?.question || "",
      options: firstQuestion?.options?.map(opt => ({ name: opt.label, value: opt.value })),
    }
    const timeline = existingIndex >= 0
      ? state.timeline.map((item, index) => index === existingIndex ? { type: "interaction" as const, interaction: card } : item)
      : [...state.timeline, { type: "interaction" as const, interaction: card }]
    return {
      ...state,
      activity: { kind: "waiting-interaction" },
      timeline,
    }
  }

  return state
}

/** 标识用户正在请求取消当前运行。 */
export function markCancelling(state: InteractiveState): InteractiveState {
  if (!state.activeRun) return state
  return {
    ...state,
    activity: { kind: "cancelling" },
  }
}

/** 终态快照：Compose 失败/取消/完成后仍保留最后一份完整投影供 Timeline 展示。 */
function composeSummaryOf(state: InteractiveState): RunSummary["composeSummary"] {
  const projection = state.composeState
  if (!projection) return undefined
  return { ...projection }
}

/** 将指定的交互标记为超时已处理。 */
export function markInteractionTimeout(state: InteractiveState, requestId: string): InteractiveState {
  return resolveInteractionState(state, requestId, "cancelled")
}

/** 清理与某个交互关联的排队状态。 */
export function clearPendingInteraction(state: InteractiveState, requestId: string): InteractiveState {
  return resolveInteractionState(state, requestId, "cancelled")
}

/** 标记当前运行为失败。 */
export function markRunFailed(state: InteractiveState, runId: string, message: string, idGenerator: IdGenerator = defaultIdGenerator): InteractiveState {
  const active = state.activeRun
  if (!active || active.runId !== runId) return state
  return {
    ...state,
    activeRun: null,
    activity: { kind: "failed" },
    runProgress: null,
    composeState: null,
    lastRun: { runId, outcome: "failed", composeSummary: composeSummaryOf(state) },
    timeline: freezeReasoning(finishAssistant(settlePendingInteractions(state.timeline, runId), runId, `error: ${message}`, idGenerator), runId),
  }
}

/** 丢弃旧 run、重复帧和乱序帧。 */
export function applyAgentEvent(state: InteractiveState, event: EventEnvelope, idGenerator: IdGenerator = defaultIdGenerator): InteractiveState {
  const active = state.activeRun
  if (!active || active.threadId !== event.thread_id || active.runId !== event.run_id) return state
  const next = acceptSequence(state, event.thread_id, event.run_id, event.sequence)
  if (!next) return state
  const runId = event.run_id

  switch (event.type) {
    case EventType.RUN_STARTED: {
      return { ...next, activity: { kind: "running" }, runProgress: next.runProgress ?? { phase: "preparing", elapsedMs: 0 } }
    }
    case EventType.RUN_PROGRESS: {
      const payload = event.payload
      if ((payload.phase !== "preparing" && payload.phase !== "model") || !Number.isInteger(payload.elapsed_ms) || payload.elapsed_ms < 0) return next
      return {
        ...next,
        activity: { kind: "running" },
        runProgress: { phase: payload.phase, elapsedMs: payload.elapsed_ms },
      }
    }
    case EventType.SKILL_LOADED: {
      const payload = event.payload
      return {
        ...next,
        activity: { kind: "running" },
        timeline: [
          ...next.timeline,
          {
            type: "message",
            message: {
              id: `skill-${runId}-${event.sequence}`,
              role: "system",
              content: `skill-loaded: ${stringValue(payload.skill_id, "unknown")}`,
              runId,
            },
          },
        ],
      }
    }
    case EventType.CONTENT_DELTA: {
      const payload = event.payload
      return typeof payload.text === "string"
        ? { ...next, timeline: freezeReasoning(appendAssistantDelta(next.timeline, runId, payload.text, idGenerator), runId), activity: { kind: "running" } }
        : next
    }
    case EventType.REASONING_DELTA: {
      const payload = event.payload
      const text = payloadText(payload.text)
      if (!text) return next
      return {
        ...next,
        activity: { kind: "running" },
        timeline: appendReasoningDelta(next.timeline, runId, text, idGenerator),
      }
    }
    case EventType.TOOL_STARTED: {
      const payload = event.payload
      return {
        ...next,
        activity: { kind: "running" },
        timeline: freezeReasoning(updateTool(next.timeline, {
          id: stringValue(payload.tool_call_id, `tool-${runId}`),
          runId,
          name: stringValue(payload.name, "tool"),
          arguments: "",
          output: "",
          status: "running",
        }), runId),
      }
    }
    case EventType.TOOL_DELTA:
      return applyToolDelta(next, runId, event.payload)
    case EventType.TOOL_COMPLETED:
      {
        const payload = event.payload
        const result = objectRecord(payload.result)
        const toolId = stringValue(payload.tool_call_id, `tool-${runId}`)
        return {
          ...next,
          timeline: updateTool(next.timeline, {
            id: toolId,
            runId,
            name: toolName(next.timeline, runId, toolId),
            arguments: toolArguments(next.timeline, runId, toolId),
            output: stringValue(result.content, ""),
            status: result.is_error === true ? "failed" : "completed",
          }),
        }
      }
    case EventType.CONTEXT_UPDATED: {
      const payload = event.payload
      return {
        ...next,
        activity: { kind: "running" },
        timeline: appendNotice(next, contextNotice(payload), idGenerator).timeline,
      }
    }
    case EventType.COMPOSE_STATE: {
      return applyComposeState(next, event.payload)
    }
    case EventType.INTERACTION_RESOLVED: {
      const payload = event.payload
      return {
        ...next,
        activity: { kind: "running" },
        timeline: resolveInteraction(next.timeline, runId, stringValue(payload.request_id, "")),
      }
    }
    case EventType.RUN_COMPLETED: {
      const payload = event.payload
      return {
        ...next,
        activeRun: null,
        activity: { kind: "completed" },
        runProgress: null,
        lastRun: {
          runId,
          outcome: "completed",
          durationMs: numberValue(payload.duration_ms),
          usage: usageValue(payload.usage),
          context: contextValue(payload.context),
          ...(composeSummaryOf(next) ? { composeSummary: composeSummaryOf(next) } : {}),
        },
        composeState: null,
        timeline: finishAssistant(settlePendingInteractions(freezeReasoning(next.timeline, runId), runId), runId, "", idGenerator),
      }
    }
    case EventType.RUN_CANCELLED: {
      const payload = event.payload
      return {
        ...next,
        activeRun: null,
        activity: { kind: "cancelled" },
        runProgress: null,
        lastRun: { runId, outcome: "cancelled", composeSummary: composeSummaryOf(next) },
        composeState: null,
        timeline: freezeReasoning(finishAssistant(settlePendingInteractions(next.timeline, runId), runId, `cancelled: ${stringValue(payload.reason, "user cancelled")}`, idGenerator), runId),
      }
    }
    case EventType.RUN_FAILED:
      return markRunFailed(next, runId, stringValue(event.payload.error.message, "run failed"), idGenerator)
    default:
      return next
  }
}

function resolveInteractionState(state: InteractiveState, requestId: string, targetStatus: InteractionCard["status"]): InteractiveState {
  const hasInteraction = state.timeline.some(item => item.type === "interaction" && item.interaction.id === requestId)
  if (!hasInteraction) return state
  return {
    ...state,
    activity: state.activity.kind === "waiting-interaction" ? { kind: "running" } : state.activity,
    timeline: resolveInteraction(state.timeline, state.activeRun?.runId ?? "", requestId, targetStatus),
  }
}

function resolveInteraction(timeline: TimelineItem[], _runId: string, requestId: string, targetStatus: InteractionCard["status"] = "resolved"): TimelineItem[] {
  return timeline.map(item => {
    if (item.type !== "interaction" || item.interaction.id !== requestId) return item
    return {
      ...item,
      interaction: {
        ...item.interaction,
        status: targetStatus,
      },
    }
  })
}

function settlePendingInteractions(timeline: TimelineItem[], runId: string): TimelineItem[] {
  return timeline.map(item => {
    if (item.type !== "interaction" || item.interaction.runId !== runId || item.interaction.status !== "pending") return item
    return {
      ...item,
      interaction: {
        ...item.interaction,
        status: "cancelled",
      },
    }
  })
}

function acceptSequence(state: InteractiveState, threadId: string, runId: string, sequence: number): InteractiveState | null {
  const key = `${threadId}:${runId}`
  const lastSequence = state.sequences[key] ?? 0
  if (sequence <= lastSequence) return null
  const nextSequences = { ...state.sequences, [key]: sequence }
  const nextState = { ...state, sequences: nextSequences }
  if (lastSequence > 0 && sequence > lastSequence + 1) {
    return appendNotice(nextState, `sequence-gap: expected ${lastSequence + 1}, got ${sequence}`)
  }
  return nextState
}

function appendAssistantDelta(timeline: TimelineItem[], runId: string, text: string, idGenerator: IdGenerator = defaultIdGenerator): TimelineItem[] {
  const index = timeline.findLastIndex(item => item.type === "message" && item.message.role === "assistant" && item.message.runId === runId)
  if (index < 0) {
    return [...timeline, { type: "message", message: { id: `assistant-${runId}-${idGenerator.uuid()}`, role: "assistant", content: text, runId, streaming: true } }]
  }
  const item = timeline[index]
  if (item?.type === "message" && index === timeline.length - 1) {
    return timeline.map((entry, itemIndex) => (
      itemIndex === index && entry.type === "message"
        ? { ...entry, message: { ...entry.message, content: entry.message.content + text } }
        : entry
    ))
  }
  return [...timeline, { type: "message", message: { id: `assistant-${runId}-${idGenerator.uuid()}`, role: "assistant", content: text, runId, streaming: true } }]
}

function finishAssistant(timeline: TimelineItem[], runId: string, suffix = "", idGenerator: IdGenerator = defaultIdGenerator): TimelineItem[] {
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
  return [
    ...settled,
    {
      type: "message",
      message: {
        id: `assistant-${runId}-terminal-${idGenerator.uuid()}`,
        role: "assistant",
        content: suffix.trimStart(),
        runId,
        streaming: false,
      },
    },
  ]
}

function updateTool(timeline: TimelineItem[], tool: ToolCard): TimelineItem[] {
  const index = findToolIndex(timeline, tool.runId, tool.id)
  if (index < 0) return [...timeline, { type: "tool", tool }]
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool" ? { ...item, tool: { ...item.tool, ...tool } } : item
  ))
}

function applyToolDelta(state: InteractiveState, runId: string, payload: Record<string, unknown>): InteractiveState {
  const toolId = stringValue(payload.tool_call_id, `tool-${runId}`)
  let timeline = state.timeline
  if (typeof payload.arguments_delta === "string") {
    timeline = updateToolStream(timeline, runId, toolId, "arguments", payload.arguments_delta)
  }
  if (typeof payload.output_delta === "string") {
    timeline = updateToolStream(timeline, runId, toolId, "output", payload.output_delta)
  }
  return { ...state, timeline }
}

function updateToolStream(timeline: TimelineItem[], runId: string, toolId: string, field: "arguments" | "output", delta: string): TimelineItem[] {
  const index = findToolIndex(timeline, runId, toolId)
  if (index < 0) {
    return [
      ...timeline,
      {
        type: "tool",
        tool: {
          id: toolId,
          runId,
          name: "tool",
          arguments: field === "arguments" ? delta : "",
          output: field === "output" ? delta : "",
          status: "running",
        },
      },
    ]
  }
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool"
      ? { ...item, tool: { ...item.tool, [field]: item.tool[field] + delta } }
      : item
  ))
}

function findToolIndex(timeline: TimelineItem[], runId: string, toolId: string): number {
  return timeline.findLastIndex(item => item.type === "tool" && item.tool.runId === runId && item.tool.id === toolId)
}

function toolName(timeline: TimelineItem[], runId: string, toolId: string): string {
  const item = timeline.find(entry => entry.type === "tool" && entry.tool.runId === runId && entry.tool.id === toolId)
  return item?.type === "tool" ? item.tool.name : "tool"
}

function toolArguments(timeline: TimelineItem[], runId: string, toolId: string): string {
  const item = timeline.find(entry => entry.type === "tool" && entry.tool.runId === runId && entry.tool.id === toolId)
  return item?.type === "tool" ? item.tool.arguments : ""
}

function contextNotice(payload: Record<string, unknown>): string {
  const action = stringValue(payload.action, "compact")
  const estimated = numberValue(payload.estimated_tokens)
  const capacity = numberValue(payload.input_cap_tokens)
  if (estimated && capacity) return `context-updated: ${action} (${estimated}/${capacity} tokens)`
  return `context-updated: ${action}`
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback
}

function payloadText(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function appendReasoningDelta(timeline: TimelineItem[], runId: string, text: string, idGenerator: IdGenerator = defaultIdGenerator): TimelineItem[] {
  const index = timeline.findLastIndex(item => item.type === "reasoning" && item.reasoning.runId === runId && item.reasoning.active)
  if (index < 0) {
    return [...timeline, { type: "reasoning", reasoning: { id: `reasoning-${runId}-${idGenerator.uuid()}`, runId, text, active: true } }]
  }
  return timeline.map((entry, itemIndex) => (
    itemIndex === index && entry.type === "reasoning"
      ? { ...entry, reasoning: { ...entry.reasoning, text: entry.reasoning.text + text } }
      : entry
  ))
}

function freezeReasoning(timeline: TimelineItem[], runId: string): TimelineItem[] {
  const index = timeline.findLastIndex(item => item.type === "reasoning" && item.reasoning.runId === runId && item.reasoning.active)
  if (index < 0) return timeline
  return timeline.map((entry, itemIndex) => (
    itemIndex === index && entry.type === "reasoning"
      ? { ...entry, reasoning: { ...entry.reasoning, active: false } }
      : entry
  ))
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && !Number.isNaN(value) ? value : undefined
}

function objectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {}
}

function usageValue(value: unknown): { inputTokens: number; outputTokens: number } | undefined {
  const record = objectRecord(value)
  const inputTokens = numberValue(record.input_tokens)
  const outputTokens = numberValue(record.output_tokens)
  if (inputTokens !== undefined && outputTokens !== undefined) {
    return { inputTokens, outputTokens }
  }
  return undefined
}

function contextValue(value: unknown): { action: string; estimatedTokens?: number; inputCapTokens?: number } | undefined {
  const record = objectRecord(value)
  const action = stringValue(record.action, "")
  if (!action) return undefined
  return {
    action,
    estimatedTokens: numberValue(record.estimated_tokens),
    inputCapTokens: numberValue(record.input_cap_tokens),
  }
}
