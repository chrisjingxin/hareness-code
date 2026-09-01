/** Interactive Core 的纯 reducer：把 sidecar 流事件折叠为稳定领域状态。 */

import { EventType, type EventEnvelope, type InteractionRequestEnvelope } from "@za38/protocol"
import type { IdGenerator } from "./ports/id-generator"

export type MessageRole = "user" | "assistant" | "system"

/** 工作模式在 Run 受理时冻结；同一 Thread 相邻 Run 之间可在空闲时切换。 */
export type WorkMode = "build" | "compose"

export type ComposeStageId = "grill" | "task" | "spec" | "plan" | "implement" | "review"
export type ComposeUiStageId = "requirement" | "spec" | "plan" | "implement" | "review"

/** compose.progress 的有界投影；revision 单调递增，迟到帧被拒绝。 */
export type ComposeProjection = {
  threadId: string
  slug: string
  complexity: "simple" | "complex"
  status: "active" | "waiting_user" | "verifying" | "completed" | "abandoned"
  currentStage: ComposeStageId
  waiting: "none" | "task_confirm" | "spec_confirm" | "plan_confirm" | "review_confirm" | "ask_user" | "implement_choice"
  stages: Array<{ id: ComposeUiStageId; state: "pending" | "current" | "confirmed" | "skipped" | "failed" }>
  documents: Array<{ kind: "task" | "spec" | "plan" | "todo" | "review"; path: string; confirmed: boolean }>
  fixRounds: number
  revision: number
}

export type WorkItemStatus = "active" | "waiting_user" | "blocked" | "completed" | "abandoned"

/** compose.work_item / threads.open 的 Work Item 非敏感投影；revision 单调递增。 */
export type WorkItemProjection = {
  workItemId: string
  slug: string
  title: string
  revision: number
  status: WorkItemStatus
  currentActivity: string
  pendingDecision: string | null
  blockedReason: string | null
}

/** Compose activity 归属；Build 无 scope 时 execution/activity 视为 root。 */
export type ComposeScopeMeta = {
  activityId: string
  stage: ComposeStageId
  attempt: number
  taskId?: string
  taskTitle?: string
}

export type ConversationMessage = {
  id: string
  role: MessageRole
  content: string
  runId?: string
  /** 消息首次进入 Timeline 的墙钟时间；历史协议未提供时保持缺省。 */
  createdAtMs?: number
  streaming?: boolean
  /** 该条用户消息所属 Run 的工作模式；缺省由恢复路径补 threadMode 或 build。 */
  workMode?: WorkMode
  /** child/root execution 身份；缺省表示 root。 */
  executionId?: string
  activityId?: string
  agentId?: string
}

export type ToolCard = {
  id: string
  runId: string
  name: string
  arguments: string
  output: string
  status: "running" | "completed" | "failed"
  executionId?: string
  activityId?: string
  agentId?: string
  /** 父 task 派出绑定的 child execution；有值后派出卡可进入子时间线。 */
  childExecutionId?: string
  childAgentId?: string
}

export type InteractionCard = {
  id: string
  runId: string
  type: "approval" | "question" | "directory_trust" | "plan"
  status: "pending" | "approved" | "rejected" | "answered" | "resolved" | "cancelled"
  description?: string
  requests?: unknown
  question?: string
  options?: Array<{ name: string; value: string }>
  executionId?: string
  activityId?: string
  agentId?: string
  composeScope?: ComposeScopeMeta
}

/** Runtime 生成的阶段摘要；非 assistant 消息。 */
export type ComposeSummaryCard = {
  id: string
  runId: string
  status: "passed" | "failed" | "blocked" | "cancelled" | "truncated"
  text: string
  executionId?: string
  activityId?: string
  agentId?: string
  composeScope?: ComposeScopeMeta
}

/**
 * JSON-RPC 的 sequence 是唯一可靠的时间顺序。
 */
export type TimelineItem =
  | { type: "message"; message: ConversationMessage }
  | { type: "tool"; tool: ToolCard }
  | { type: "reasoning"; reasoning: ReasoningCard }
  | { type: "interaction"; interaction: InteractionCard }
  | { type: "compose-summary"; summary: ComposeSummaryCard }

export type ActiveRun = {
  threadId: string
  runId: string
}

export type RunSummary = {
  runId: string
  outcome: "completed" | "cancelled" | "failed"
  durationMs?: number
  usage?: { inputTokens: number; outputTokens: number; cachedTokens?: number }
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
  executionId?: string
  activityId?: string
  agentId?: string
}

/** 事件归组键：run + execution + activity（Build 无 scope 时后两者为 root）。 */
type EventIdentity = {
  runId: string
  executionId: string
  activityId: string
  agentId?: string
  composeScope?: ComposeScopeMeta
}

export type RestoredThreadMessage = {
  kind: "user" | "assistant" | "tool"
  content: string
  toolName?: string
  createdAtMs?: number
}

/** Thread reopen 恢复的有界 Compose activity；不含 Reasoning/原始 Tool 正文。 */
export type RestoredComposeActivity = {
  runId: string
  eventSequence: number
  activityId: string
  stage: ComposeStageId
  attempt: number
  kind: "summary" | "tool_terminal" | "truncation"
  label: string
  status: string
  createdAtMs: number
  taskId?: string
  taskTitle?: string
  executionId?: string
  agentId?: string
  boundedText?: string
}

/**
 * 运行活动仅使用领域 kind 表达语义，Presenter 层负责将 kind 转换为具体语言的展示文案。
 */
export type InteractiveActivity =
  | { kind: "home" }
  | { kind: "idle" }
  | { kind: "compacting" }
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
  pendingOperation: "context.compact" | null
  timeline: TimelineItem[]
  activity: InteractiveActivity
  /** 仅当前 Run 可见的事实进度，不进入 Timeline 或 Thread 历史。 */
  runProgress: RunProgress | null
  lastRun?: RunSummary
  sequences: Record<string, number>
  composeState: ComposeProjection | null
  /** 当前 Thread 持久化的 Work Item 投影；无未终结项或 Build Thread 为 null。 */
  workItem: WorkItemProjection | null
  /** Thread 首条有效消息后冻结的持久工作模式；未冻结为 null。 */
  threadMode: WorkMode | null
  /** 当前 Thread 下一次 Run 的工作模式；Run 受理后冻结。 */
  workMode: WorkMode
  /** 正在查看的 child execution；null 表示父时间线。 */
  childTimelineExecutionId: string | null
  /** 当前 Thread 是否处于暂存回退态（已执行 /undo 且尚未提交新 Prompt）。 */
  isReverted?: boolean
  revertedTurnId?: string | null
}

/** 创建无 thread 内容的初始状态；显式 null 进入空首页。 */
export function createInitialState(threadId: string | null = null, workMode: WorkMode = "build"): InteractiveState {
  return {
    currentThreadId: threadId,
    activeRun: null,
    pendingOperation: null,
    timeline: [],
    activity: { kind: threadId ? "idle" : "home" },
    runProgress: null,
    sequences: {},
    workMode,
    composeState: null,
    workItem: null,
    threadMode: null,
    childTimelineExecutionId: null,
    isReverted: false,
    revertedTurnId: null,
  }
}

/** 进入某一个 child 的只读时间线。未知 id 仍记录，由视图显示空态。 */
export function openChildTimeline(state: InteractiveState, executionId: string): InteractiveState {
  const id = executionId.trim()
  if (!id) return state
  return { ...state, childTimelineExecutionId: id }
}

/** 回到父时间线。 */
export function leaveChildTimeline(state: InteractiveState): InteractiveState {
  if (state.childTimelineExecutionId === null) return state
  return { ...state, childTimelineExecutionId: null }
}

/** 空闲时切换下一次 Run 的工作模式；Thread 已冻结模式时切换被锁定。 */
export function setWorkMode(state: InteractiveState, mode: WorkMode): InteractiveState {
  if (state.workMode === mode) return state
  if (state.threadMode !== null && mode !== state.threadMode) return state
  return { ...state, workMode: mode }
}

/** 折叠一帧 compose.work_item 投影；revision 不递增的迟到帧被拒绝。 */
export function applyWorkItem(state: InteractiveState, payload: unknown): InteractiveState {
  const projection = parseWorkItemProjection(payload)
  if (!projection) return state
  const current = state.workItem
  if (current !== null && projection.revision < current.revision) return state
  return { ...state, workItem: projection }
}

/** 折叠 threads.open 携带的持久 Thread 模式；首条有效消息后不可变。 */
export function applyThreadMode(state: InteractiveState, mode: unknown): InteractiveState {
  if (mode !== "build" && mode !== "compose") return state
  const next: InteractiveState = { ...state, threadMode: mode }
  if (next.workMode !== mode) return { ...next, workMode: mode }
  return next
}

const WORK_ITEM_STATUSES: Record<string, true> = {
  active: true,
  waiting_user: true,
  blocked: true,
  completed: true,
  abandoned: true,
}

/** 解析 wire 形状的 Work Item 投影；非法字段 fail closed 为 null。 */
export function parseWorkItemProjection(value: unknown): WorkItemProjection | null {
  if (!value || typeof value !== "object") return null
  const raw = value as Record<string, unknown>
  if (
    typeof raw.work_item_id !== "string" || !raw.work_item_id
    || typeof raw.slug !== "string" || !raw.slug
    || typeof raw.title !== "string"
    || !Number.isInteger(raw.revision) || (raw.revision as number) < 0
    || typeof raw.status !== "string" || !WORK_ITEM_STATUSES[raw.status]
    || typeof raw.current_activity !== "string"
  ) return null
  return {
    workItemId: raw.work_item_id,
    slug: raw.slug,
    title: raw.title,
    revision: raw.revision as number,
    status: raw.status as WorkItemStatus,
    currentActivity: raw.current_activity,
    pendingDecision: typeof raw.pending_decision === "string" ? raw.pending_decision : null,
    blockedReason: typeof raw.blocked_reason === "string" ? raw.blocked_reason : null,
  }
}

/** 折叠一帧 compose.progress；revision 不递增的迟到帧被拒绝。 */
export function applyComposeState(state: InteractiveState, payload: unknown): InteractiveState {
  const projection = parseComposeProjection(payload)
  if (!projection) return state
  const current = state.composeState
  if (current !== null && projection.revision <= current.revision) return state
  const activity: InteractiveActivity =
    projection.status === "waiting_user" || projection.waiting !== "none"
      ? { kind: "waiting-interaction" }
      : { kind: "running" }
  return { ...state, composeState: projection, activity }
}

const COMPOSE_STAGE_IDS: readonly ComposeStageId[] = ["grill", "task", "spec", "plan", "implement", "review"]
const COMPOSE_UI_STAGE_IDS: readonly ComposeUiStageId[] = ["requirement", "spec", "plan", "implement", "review"]
const COMPOSE_STATUSES = new Set(["active", "waiting_user", "verifying", "completed", "abandoned"])
const COMPOSE_WAITING = new Set(["none", "task_confirm", "spec_confirm", "plan_confirm", "review_confirm", "ask_user", "implement_choice"])
const COMPOSE_UI_STATES = new Set(["pending", "current", "confirmed", "skipped", "failed"])

function parseComposeProjection(value: unknown): ComposeProjection | null {
  if (!value || typeof value !== "object") return null
  const raw = value as Record<string, unknown>
  if (!Number.isInteger(raw.revision) || (raw.revision as number) < 0) return null
  if (typeof raw.thread_id !== "string" || !raw.thread_id) return null
  if (typeof raw.slug !== "string" || !raw.slug) return null
  if (raw.complexity !== "simple" && raw.complexity !== "complex") return null
  if (typeof raw.current_stage !== "string" || !COMPOSE_STAGE_IDS.includes(raw.current_stage as ComposeStageId)) return null
  if (typeof raw.status !== "string" || !COMPOSE_STATUSES.has(raw.status)) return null
  if (typeof raw.waiting !== "string" || !COMPOSE_WAITING.has(raw.waiting)) return null
  if (!Array.isArray(raw.stages) || !Array.isArray(raw.documents)) return null
  if (!Number.isInteger(raw.fix_rounds) || (raw.fix_rounds as number) < 0) return null
  const stages = raw.stages.map(item => {
    const entry = item as Record<string, unknown>
    if (typeof entry.id !== "string" || !COMPOSE_UI_STAGE_IDS.includes(entry.id as ComposeUiStageId)) return null
    if (typeof entry.state !== "string" || !COMPOSE_UI_STATES.has(entry.state)) return null
    return { id: entry.id as ComposeUiStageId, state: entry.state as ComposeProjection["stages"][number]["state"] }
  })
  const documents = raw.documents.map(item => {
    const entry = item as Record<string, unknown>
    if (
      (entry.kind !== "task" && entry.kind !== "spec" && entry.kind !== "plan" && entry.kind !== "todo" && entry.kind !== "review")
      || typeof entry.path !== "string"
      || typeof entry.confirmed !== "boolean"
    ) return null
    return { kind: entry.kind, path: entry.path, confirmed: entry.confirmed }
  })
  if (stages.some(item => item === null) || documents.some(item => item === null)) return null
  return {
    threadId: raw.thread_id,
    slug: raw.slug,
    complexity: raw.complexity,
    status: raw.status as ComposeProjection["status"],
    currentStage: raw.current_stage as ComposeStageId,
    waiting: raw.waiting as ComposeProjection["waiting"],
    stages: stages as ComposeProjection["stages"],
    documents: documents as ComposeProjection["documents"],
    fixRounds: raw.fix_rounds as number,
    revision: raw.revision as number,
  }
}

/** 空状态不应被欢迎文本污染，/new 后才能可靠地回到沉浸式首页。 */
export function isHomeState(state: { activeRun: InteractiveState["activeRun"]; timeline: readonly TimelineItem[]; activity: InteractiveState["activity"] }): boolean {
  return !state.activeRun
    && state.timeline.length === 0
    && state.activity.kind !== "compacting"
    && state.activity.kind !== "waiting-interaction"
}

/** 手动压缩不是 Agent Run，但会独占当前 Thread 的模型投影。 */
export function startContextCompaction(state: InteractiveState): InteractiveState {
  return {
    ...state,
    pendingOperation: "context.compact",
    activity: { kind: "compacting" },
  }
}

/** 只清理匹配的压缩操作；迟到结果不能覆盖其他生命周期状态。 */
export function finishContextCompaction(state: InteractiveState): InteractiveState {
  if (state.pendingOperation !== "context.compact") return state
  return {
    ...state,
    pendingOperation: null,
    activity: { kind: state.currentThreadId ? "idle" : "home" },
  }
}

/** 在发送 run.start 前先登记 run。 */
export function startRun(state: InteractiveState, run: ActiveRun, prompt: string, createdAtMs?: number): InteractiveState {
  const userMessage: ConversationMessage = { id: `user-${run.runId}`, role: "user", content: prompt, runId: run.runId }
  if (createdAtMs !== undefined) userMessage.createdAtMs = createdAtMs
  return {
    ...state,
    currentThreadId: run.threadId,
    activeRun: run,
    lastRun: undefined,
    activity: { kind: "starting" },
    runProgress: { phase: "preparing", elapsedMs: 0 },
    isReverted: false,
    revertedTurnId: null,
    timeline: [
      ...state.timeline,
      { type: "message", message: { ...userMessage, workMode: state.workMode } },
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
  composeActivities: readonly RestoredComposeActivity[] = [],
  threadMode: WorkMode | null = null,
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
    const restoredMessage: ConversationMessage = {
      id,
      role: message.kind,
      content: message.content,
      runId: restoredRunId,
      streaming: false,
    }
    if (message.createdAtMs !== undefined) restoredMessage.createdAtMs = message.createdAtMs
    if (message.kind === "user") restoredMessage.workMode = threadMode ?? "build"
    return { type: "message", message: restoredMessage }
  })
  // Activity 审计只进入 Timeline，不进入模型 context；按原顺序追加在 transcript 之后。
  for (const [index, activity] of composeActivities.entries()) {
    const runId = activity.runId || restoredRunId
    const scope: ComposeScopeMeta = {
      activityId: activity.activityId,
      stage: activity.stage,
      attempt: activity.attempt,
      ...(activity.taskId ? { taskId: activity.taskId } : {}),
      ...(activity.taskTitle ? { taskTitle: activity.taskTitle } : {}),
    }
    if (activity.kind === "tool_terminal") {
      timeline.push({
        type: "tool",
        tool: {
          id: `restored-activity-tool-${index + 1}`,
          runId,
          name: activity.label || "tool",
          arguments: "",
          output: activity.boundedText || "",
          status: activity.status === "failed" || activity.status === "error" ? "failed" : "completed",
          ...(activity.executionId ? { executionId: activity.executionId } : {}),
          activityId: activity.activityId,
          ...(activity.agentId ? { agentId: activity.agentId } : {}),
        },
      })
      continue
    }
    if (activity.kind === "summary" || activity.kind === "truncation") {
      const status =
        activity.status === "passed"
        || activity.status === "failed"
        || activity.status === "blocked"
        || activity.status === "cancelled"
        || activity.status === "truncated"
          ? activity.status
          : "failed"
      timeline.push({
        type: "compose-summary",
        summary: {
          id: `restored-activity-summary-${index + 1}`,
          runId,
          status,
          text: activity.boundedText || activity.label || activity.kind,
          ...(activity.executionId ? { executionId: activity.executionId } : {}),
          activityId: activity.activityId,
          ...(activity.agentId ? { agentId: activity.agentId } : {}),
          composeScope: scope,
        },
      })
    }
  }
  return {
    currentThreadId: threadId,
    activeRun: null,
    pendingOperation: null,
    timeline,
    // 恢复已完成（timeline 已构建）：活动状态必须是 idle，不得停在 restoring。
    activity: { kind: "idle" },
    runProgress: null,
    sequences: {},
    workMode,
    composeState: null,
    workItem: null,
    threadMode,
    childTimelineExecutionId: null,
  }
}

/** 根据后端 RPC 注册反向 Interaction。 */
export function applyInteractionRequest(state: InteractiveState, envelope: InteractionRequestEnvelope): InteractiveState {
  const active = state.activeRun
  if (!active || active.threadId !== envelope.thread_id || active.runId !== envelope.run_id) return state

  const existingIndex = state.timeline.findIndex(item => item.type === "interaction" && item.interaction.id === envelope.request_id)

  const req = (envelope as unknown as { request?: Record<string, unknown> }).request ?? (envelope as unknown as Record<string, unknown>)
  const kind = (req.type ?? req.kind) as string | undefined
  const interactionIdentity = resolveInteractionIdentity(envelope)

  if (kind === "approval") {
    const payload = req.payload && typeof req.payload === "object" ? req.payload as Record<string, unknown> : {}
    const description = typeof payload.description === "string"
      ? payload.description
      : (req.prompt ?? req.reason ?? "") as string
    const card: InteractionCard = withScopeFields({
      id: envelope.request_id,
      runId: envelope.run_id,
      type: "approval",
      status: "pending",
      description,
      requests: req,
    }, interactionIdentity)
    if (interactionIdentity.composeScope) card.composeScope = interactionIdentity.composeScope
    const timeline = existingIndex >= 0
      ? state.timeline.map((item, index) => index === existingIndex ? { type: "interaction" as const, interaction: card } : item)
      : [...state.timeline, { type: "interaction" as const, interaction: card }]
    return {
      ...state,
      activity: { kind: "waiting-interaction" },
      timeline,
    }
  }

  if (kind === "directory_trust") {
    const payload = req.payload && typeof req.payload === "object" ? req.payload as Record<string, unknown> : {}
    const directory = typeof payload.directory === "string" ? payload.directory : ""
    const card: InteractionCard = {
      id: envelope.request_id,
      runId: envelope.run_id,
      type: "directory_trust",
      status: "pending",
      description: directory ? `是否将此目录加入白名单？${directory}` : "是否将此目录加入白名单？",
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

  if (kind === "plan") {
    const payload = req.payload && typeof req.payload === "object" ? req.payload as Record<string, unknown> : {}
    const card: InteractionCard = {
      id: envelope.request_id,
      runId: envelope.run_id,
      type: "plan",
      status: "pending",
      description: payload.has_plan === true ? "审阅计划" : "还没有写出计划",
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
    const questions = (
      (req.payload && typeof req.payload === "object"
        ? (req.payload as Record<string, unknown>).questions
        : req.questions) as Array<{ header?: string; question?: string; options?: Array<{ label: string; value: string }> }> | undefined
    )
    const firstQuestion = questions?.[0]
    const card: InteractionCard = withScopeFields({
      id: envelope.request_id,
      runId: envelope.run_id,
      type: "question",
      status: "pending",
      question: firstQuestion?.question || firstQuestion?.header || "",
      options: firstQuestion?.options?.map(opt => ({ name: opt.label, value: opt.value })),
    }, interactionIdentity)
    if (interactionIdentity.composeScope) card.composeScope = interactionIdentity.composeScope
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

function resolveInteractionIdentity(envelope: InteractionRequestEnvelope): EventIdentity {
  const raw = envelope as unknown as Record<string, unknown>
  const rawScope = raw.compose_scope
  if (rawScope && typeof rawScope === "object") {
    const scope = rawScope as Record<string, unknown>
    const activityId = typeof scope.activity_id === "string" ? scope.activity_id : ""
    const stage = typeof scope.stage === "string" ? scope.stage : ""
    const attempt = typeof scope.attempt === "number" ? scope.attempt : 0
    if (activityId && COMPOSE_STAGES.has(stage) && Number.isInteger(attempt) && attempt >= 1) {
      const composeScope: ComposeScopeMeta = {
        activityId,
        stage: stage as ComposeStageId,
        attempt,
      }
      if (typeof scope.task_id === "string" && scope.task_id) composeScope.taskId = scope.task_id
      if (typeof scope.task_title === "string" && scope.task_title) composeScope.taskTitle = scope.task_title
      return {
        runId: envelope.run_id,
        executionId: typeof raw.execution_id === "string" && raw.execution_id ? raw.execution_id : "root",
        activityId,
        agentId: typeof raw.agent_id === "string" && raw.agent_id ? raw.agent_id : undefined,
        composeScope,
      }
    }
  }
  return {
    runId: envelope.run_id,
    executionId: typeof raw.execution_id === "string" && raw.execution_id ? raw.execution_id : "root",
    activityId: "root",
    agentId: typeof raw.agent_id === "string" && raw.agent_id ? raw.agent_id : undefined,
  }
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

/** 记录用户已完成的交互，避免在远端继续运行时把成功响应误显示为超时。 */
export function markInteractionResponded(
  state: InteractiveState,
  requestId: string,
  status: "approved" | "rejected" | "answered",
): InteractiveState {
  return resolveInteractionState(state, requestId, status)
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
  // 非法 compose_scope：sequence 已前进，丢弃内容且不污染其他 activity。
  const identity = resolveEventIdentity(event)
  if (identity === null) return next

  switch (event.type) {
    case EventType.RUN_STARTED: {
      const startedMode = event.payload.mode
      const mode: WorkMode | undefined = startedMode === "build" || startedMode === "compose" ? startedMode : undefined
      return {
        ...next,
        activity: { kind: "running" },
        runProgress: next.runProgress ?? { phase: "preparing", elapsedMs: 0 },
        timeline: mode
          ? next.timeline.map(item => {
              if (item.type !== "message" || item.message.role !== "user" || item.message.runId !== runId || item.message.workMode) {
                return item
              }
              return { ...item, message: { ...item.message, workMode: mode } }
            })
          : next.timeline,
      }
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
        ? { ...next, timeline: freezeReasoning(appendAssistantDelta(next.timeline, identity, payload.text, idGenerator, event.timestamp_ms), identity), activity: { kind: "running" } }
        : next
    }
    case EventType.REASONING_DELTA: {
      const payload = event.payload
      const text = payloadText(payload.text)
      if (!text) return next
      return {
        ...next,
        activity: { kind: "running" },
        timeline: appendReasoningDelta(next.timeline, identity, text, idGenerator),
      }
    }
    case EventType.TOOL_STARTED: {
      const payload = event.payload
      return {
        ...next,
        activity: { kind: "running" },
        timeline: freezeReasoning(updateTool(next.timeline, withScopeFields({
          id: stringValue(payload.tool_call_id, `tool-${runId}`),
          runId,
          name: stringValue(payload.name, "tool"),
          arguments: "",
          output: "",
          status: "running",
        }, identity)), identity),
      }
    }
    case EventType.TOOL_DELTA:
      return applyToolDelta(next, identity, event.payload)
    case EventType.TOOL_COMPLETED:
      {
        const payload = event.payload
        const result = objectRecord(payload.result)
        const toolId = stringValue(payload.tool_call_id, `tool-${runId}`)
        return {
          ...next,
          timeline: updateTool(next.timeline, withScopeFields({
            id: toolId,
            runId,
            name: toolName(next.timeline, identity, toolId),
            arguments: toolArguments(next.timeline, identity, toolId),
            output: stringValue(result.content, ""),
            status: result.is_error === true ? "failed" : "completed",
          }, identity)),
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
    case EventType.COMPOSE_PROGRESS: {
      return applyComposeState(next, event.payload)
    }
    case EventType.COMPOSE_SUMMARY: {
      const payload = event.payload
      const status = payload.status
      const text = typeof payload.text === "string" ? payload.text : ""
      if (
        (status !== "passed" && status !== "failed" && status !== "blocked" && status !== "cancelled")
        || !text
      ) {
        return next
      }
      const summary = withScopeFields({
        id: `compose-summary-${runId}-${event.sequence}`,
        runId,
        status,
        text: text.slice(0, 1000),
      }, identity) as ComposeSummaryCard
      if (identity.composeScope) summary.composeScope = identity.composeScope
      return {
        ...next,
        activity: { kind: "running" },
        timeline: freezeReasoning([
          ...next.timeline,
          { type: "compose-summary", summary },
        ], identity),
      }
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

function appendAssistantDelta(
  timeline: TimelineItem[],
  identity: EventIdentity,
  text: string,
  idGenerator: IdGenerator = defaultIdGenerator,
  createdAtMs?: number,
): TimelineItem[] {
  const index = timeline.findLastIndex(item =>
    item.type === "message"
    && item.message.role === "assistant"
    && item.message.runId === identity.runId
    && scopeEquals(item.message, identity)
  )
  if (index < 0) {
    const message: ConversationMessage = withScopeFields({
      id: `assistant-${identity.runId}-${idGenerator.uuid()}`,
      role: "assistant",
      content: text,
      runId: identity.runId,
      streaming: true,
    }, identity)
    if (createdAtMs !== undefined) message.createdAtMs = createdAtMs
    return [...timeline, { type: "message", message }]
  }
  const item = timeline[index]
  if (item?.type === "message" && index === timeline.length - 1) {
    return timeline.map((entry, itemIndex) => (
      itemIndex === index && entry.type === "message"
        ? { ...entry, message: { ...entry.message, content: entry.message.content + text } }
        : entry
    ))
  }
  const message: ConversationMessage = withScopeFields({
    id: `assistant-${identity.runId}-${idGenerator.uuid()}`,
    role: "assistant",
    content: text,
    runId: identity.runId,
    streaming: true,
  }, identity)
  if (createdAtMs !== undefined) message.createdAtMs = createdAtMs
  return [...timeline, { type: "message", message }]
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
  const identity: EventIdentity = {
    runId: tool.runId,
    executionId: tool.executionId ?? "root",
    activityId: tool.activityId ?? "root",
    agentId: tool.agentId,
  }
  const index = findToolIndex(timeline, identity, tool.id)
  if (index < 0) return [...timeline, { type: "tool", tool }]
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool" ? { ...item, tool: { ...item.tool, ...tool } } : item
  ))
}

function applyToolDelta(
  state: InteractiveState,
  identity: EventIdentity,
  payload: Record<string, unknown>,
): InteractiveState {
  const toolId = stringValue(payload.tool_call_id, `tool-${identity.runId}`)
  let timeline = state.timeline
  if (typeof payload.arguments_delta === "string") {
    timeline = updateToolStream(timeline, identity, toolId, "arguments", payload.arguments_delta)
  }
  if (typeof payload.output_delta === "string") {
    timeline = updateToolStream(timeline, identity, toolId, "output", payload.output_delta)
  }
  const childExecutionId = typeof payload.child_execution_id === "string" ? payload.child_execution_id.trim() : ""
  const childAgentId = typeof payload.child_agent_id === "string" ? payload.child_agent_id.trim() : ""
  if (childExecutionId) {
    timeline = bindChildExecution(timeline, identity, toolId, childExecutionId, childAgentId || undefined)
  }
  return { ...state, timeline }
}

function bindChildExecution(
  timeline: TimelineItem[],
  identity: EventIdentity,
  toolId: string,
  childExecutionId: string,
  childAgentId: string | undefined,
): TimelineItem[] {
  const index = findToolIndex(timeline, identity, toolId)
  if (index < 0) return timeline
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool"
      ? { ...item, tool: { ...item.tool, childExecutionId, ...(childAgentId ? { childAgentId } : {}) } }
      : item
  ))
}

function updateToolStream(
  timeline: TimelineItem[],
  identity: EventIdentity,
  toolId: string,
  field: "arguments" | "output",
  delta: string,
): TimelineItem[] {
  const index = findToolIndex(timeline, identity, toolId)
  if (index < 0) {
    return [
      ...timeline,
      {
        type: "tool",
        tool: withScopeFields({
          id: toolId,
          runId: identity.runId,
          name: "tool",
          arguments: field === "arguments" ? delta : "",
          output: field === "output" ? delta : "",
          status: "running",
        }, identity),
      },
    ]
  }
  return timeline.map((item, itemIndex) => (
    itemIndex === index && item.type === "tool"
      ? { ...item, tool: { ...item.tool, [field]: item.tool[field] + delta } }
      : item
  ))
}

function findToolIndex(timeline: TimelineItem[], identity: EventIdentity, toolId: string): number {
  return timeline.findLastIndex(item =>
    item.type === "tool"
    && item.tool.runId === identity.runId
    && item.tool.id === toolId
    && scopeEquals(item.tool, identity)
  )
}

function toolName(timeline: TimelineItem[], identity: EventIdentity, toolId: string): string {
  const item = timeline.find(entry =>
    entry.type === "tool"
    && entry.tool.runId === identity.runId
    && entry.tool.id === toolId
    && scopeEquals(entry.tool, identity)
  )
  return item?.type === "tool" ? item.tool.name : "tool"
}

function toolArguments(timeline: TimelineItem[], identity: EventIdentity, toolId: string): string {
  const item = timeline.find(entry =>
    entry.type === "tool"
    && entry.tool.runId === identity.runId
    && entry.tool.id === toolId
    && scopeEquals(entry.tool, identity)
  )
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

function appendReasoningDelta(
  timeline: TimelineItem[],
  identity: EventIdentity,
  text: string,
  idGenerator: IdGenerator = defaultIdGenerator,
): TimelineItem[] {
  const index = timeline.findLastIndex(item =>
    item.type === "reasoning"
    && item.reasoning.runId === identity.runId
    && item.reasoning.active
    && scopeEquals(item.reasoning, identity)
  )
  if (index < 0) {
    return [...timeline, {
      type: "reasoning",
      reasoning: withScopeFields({
        id: `reasoning-${identity.runId}-${idGenerator.uuid()}`,
        runId: identity.runId,
        text,
        active: true,
      }, identity),
    }]
  }
  return timeline.map((entry, itemIndex) => (
    itemIndex === index && entry.type === "reasoning"
      ? { ...entry, reasoning: { ...entry.reasoning, text: entry.reasoning.text + text } }
      : entry
  ))
}

function freezeReasoning(timeline: TimelineItem[], identityOrRunId: EventIdentity | string): TimelineItem[] {
  if (typeof identityOrRunId === "string") {
    const runId = identityOrRunId
    const index = timeline.findLastIndex(item => item.type === "reasoning" && item.reasoning.runId === runId && item.reasoning.active)
    if (index < 0) return timeline
    return timeline.map((entry, itemIndex) => (
      itemIndex === index && entry.type === "reasoning"
        ? { ...entry, reasoning: { ...entry.reasoning, active: false } }
        : entry
    ))
  }
  const identity = identityOrRunId
  const index = timeline.findLastIndex(item =>
    item.type === "reasoning"
    && item.reasoning.runId === identity.runId
    && item.reasoning.active
    && scopeEquals(item.reasoning, identity)
  )
  if (index < 0) return timeline
  return timeline.map((entry, itemIndex) => (
    itemIndex === index && entry.type === "reasoning"
      ? { ...entry, reasoning: { ...entry.reasoning, active: false } }
      : entry
  ))
}

const COMPOSE_STAGES = new Set(["understand", "plan", "build", "verify", "review"])

/**
 * 解析事件归属；compose_scope 非法时返回 null（调用方丢弃内容、保留 sequence）。
 * Build 无 scope 时 execution/activity 归一为 root。
 */
function resolveEventIdentity(event: EventEnvelope): EventIdentity | null {
  const rawScope = (event as { compose_scope?: unknown }).compose_scope
  if (rawScope !== undefined && rawScope !== null) {
    if (!rawScope || typeof rawScope !== "object") return null
    const scope = rawScope as Record<string, unknown>
    const activityId = typeof scope.activity_id === "string" ? scope.activity_id : ""
    const stage = typeof scope.stage === "string" ? scope.stage : ""
    const attempt = typeof scope.attempt === "number" ? scope.attempt : 0
    if (!activityId || !COMPOSE_STAGES.has(stage) || !Number.isInteger(attempt) || attempt < 1) {
      return null
    }
    const composeScope: ComposeScopeMeta = {
      activityId,
      stage: stage as ComposeStageId,
      attempt,
    }
    if (typeof scope.task_id === "string" && scope.task_id) composeScope.taskId = scope.task_id
    if (typeof scope.task_title === "string" && scope.task_title) composeScope.taskTitle = scope.task_title
    return {
      runId: event.run_id,
      executionId: typeof event.execution_id === "string" && event.execution_id ? event.execution_id : "root",
      activityId,
      agentId: typeof event.agent_id === "string" && event.agent_id ? event.agent_id : undefined,
      composeScope,
    }
  }
  return {
    runId: event.run_id,
    executionId: typeof event.execution_id === "string" && event.execution_id ? event.execution_id : "root",
    activityId: "root",
    agentId: typeof event.agent_id === "string" && event.agent_id ? event.agent_id : undefined,
  }
}

function scopeEquals(
  card: { executionId?: string; activityId?: string },
  identity: EventIdentity,
): boolean {
  return (card.executionId ?? "root") === identity.executionId
    && (card.activityId ?? "root") === identity.activityId
}

/** 仅在非 root 归属时写入字段，保持 Build 无 scope 快照形状不变。 */
function withScopeFields<T extends object>(base: T, identity: EventIdentity): T {
  const extra: Record<string, string> = {}
  if (identity.executionId !== "root") extra.executionId = identity.executionId
  if (identity.activityId !== "root") extra.activityId = identity.activityId
  if (identity.agentId) extra.agentId = identity.agentId
  return Object.keys(extra).length ? { ...base, ...extra } : base
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && !Number.isNaN(value) ? value : undefined
}

function objectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {}
}

function usageValue(value: unknown): { inputTokens: number; outputTokens: number; cachedTokens?: number } | undefined {
  const record = objectRecord(value)
  const inputTokens = numberValue(record.input_tokens)
  const outputTokens = numberValue(record.output_tokens)
  const cachedTokens = numberValue(record.cached_tokens)
  if (inputTokens !== undefined && outputTokens !== undefined) {
    return { inputTokens, outputTokens, ...(cachedTokens !== undefined ? { cachedTokens } : {}) }
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
