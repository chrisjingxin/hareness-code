/** Compose Timeline 活动分组：纯函数，TUI/Web 共用，不持有 reducer 状态。 */

import type {
  ComposeScopeMeta,
  ComposeStageId,
  ComposeSummaryCard,
  TimelineItem,
} from "../interactive/state"

export const COMPOSE_STAGE_LABELS: Record<string, string> = {
  grill: "需求",
  task: "需求",
  spec: "规格",
  plan: "计划",
  implement: "实现",
  verify: "验证",
  requirement: "需求",
  understand: "理解",
  build: "构建",
  review: "检视",
}

/** 时间线条目上的可选 Compose 归属字段。 */
type ScopedFields = {
  runId?: string
  executionId?: string
  activityId?: string
  agentId?: string
  composeScope?: ComposeScopeMeta
}

/** 一段连续 timeline：要么是无 scope 的扁平项，要么是同一 activity 的分组。 */
export type TimelineSegment =
  | { kind: "flat"; item: TimelineItem }
  | { kind: "group"; group: TimelineActivityGroup }

export type TimelineActivityGroup = {
  /** runId:activityId 稳定键。 */
  key: string
  runId: string
  activityId: string
  executionId?: string
  agentId?: string
  stage?: ComposeStageId
  attempt?: number
  taskId?: string
  taskTitle?: string
  items: TimelineItem[]
  /** 组内 Runtime 摘要（若有）。 */
  summary?: ComposeSummaryCard
  /** 有终态摘要时默认折叠。 */
  terminal: boolean
}

/** 从 TimelineItem 提取归属字段。 */
export function itemScopeFields(item: TimelineItem): ScopedFields {
  if (item.type === "tool") {
    return {
      runId: item.tool.runId,
      executionId: item.tool.executionId,
      activityId: item.tool.activityId,
      agentId: item.tool.agentId,
    }
  }
  if (item.type === "reasoning") {
    return {
      runId: item.reasoning.runId,
      executionId: item.reasoning.executionId,
      activityId: item.reasoning.activityId,
      agentId: item.reasoning.agentId,
    }
  }
  if (item.type === "message") {
    return {
      runId: item.message.runId,
      executionId: item.message.executionId,
      activityId: item.message.activityId,
      agentId: item.message.agentId,
    }
  }
  if (item.type === "interaction") {
    return {
      runId: item.interaction.runId,
      executionId: item.interaction.executionId,
      activityId: item.interaction.activityId,
      agentId: item.interaction.agentId,
      composeScope: item.interaction.composeScope,
    }
  }
  return {
    runId: item.summary.runId,
    executionId: item.summary.executionId,
    activityId: item.summary.activityId,
    agentId: item.summary.agentId,
    composeScope: item.summary.composeScope,
  }
}

/** 无 Compose activity 时返回 null，表示按扁平项渲染（Build 路径）。 */
export function itemActivityKey(item: TimelineItem): string | null {
  const fields = itemScopeFields(item)
  if (!fields.activityId || fields.activityId === "root") return null
  const runId = fields.runId ?? "run"
  return `${runId}:${fields.activityId}`
}

/**
 * 按 Host sequence 顺序把 timeline 切成 flat/group 段。
 * 同一 activity 的连续条目合成一组；中间插入的 root 项打断分组。
 */
export function segmentTimeline(timeline: readonly TimelineItem[]): TimelineSegment[] {
  const segments: TimelineSegment[] = []
  let current: TimelineActivityGroup | null = null

  const flush = () => {
    if (current) {
      segments.push({ kind: "group", group: finalizeGroup(current) })
      current = null
    }
  }

  for (const item of timeline) {
    const key = itemActivityKey(item)
    if (key === null) {
      flush()
      segments.push({ kind: "flat", item })
      continue
    }
    if (!current || current.key !== key) {
      flush()
      current = startGroup(item, key)
    } else {
      appendToGroup(current, item)
    }
  }
  flush()
  return segments
}

/** 默认展开策略：终态分组折叠，进行中展开。 */
export function isGroupExpandedByDefault(group: TimelineActivityGroup): boolean {
  return !group.terminal
}

/** 分组标题文案：阶段 · Task · Agent · attempt。 */
export function activityGroupTitle(group: TimelineActivityGroup): string {
  const stageLabel = group.stage ? (COMPOSE_STAGE_LABELS[group.stage] ?? group.stage) : "活动"
  const parts = [stageLabel]
  if (group.taskTitle) parts.push(group.taskTitle)
  else if (group.taskId) parts.push(group.taskId)
  if (group.agentId) parts.push(group.agentId)
  if (group.attempt && group.attempt > 0) parts.push(`#${group.attempt}`)
  return parts.join(" · ")
}

/** 折叠头副标题：摘要或状态。 */
export function activityGroupSubtitle(group: TimelineActivityGroup): string {
  if (group.summary?.text) return group.summary.text
  if (group.summary?.status) return group.summary.status
  if (group.terminal) return "已结束"
  return "进行中"
}

function startGroup(item: TimelineItem, key: string): TimelineActivityGroup {
  const fields = itemScopeFields(item)
  const scope = fields.composeScope
  const group: TimelineActivityGroup = {
    key,
    runId: fields.runId ?? "run",
    activityId: fields.activityId ?? key,
    executionId: fields.executionId,
    agentId: fields.agentId ?? scope?.stage,
    stage: scope?.stage,
    attempt: scope?.attempt,
    taskId: scope?.taskId,
    taskTitle: scope?.taskTitle,
    items: [item],
    terminal: false,
  }
  if (item.type === "compose-summary") {
    applySummary(group, item.summary)
  }
  return group
}

function appendToGroup(group: TimelineActivityGroup, item: TimelineItem): void {
  group.items.push(item)
  const fields = itemScopeFields(item)
  if (!group.executionId && fields.executionId) group.executionId = fields.executionId
  if (!group.agentId && fields.agentId) group.agentId = fields.agentId
  const scope = fields.composeScope
  if (scope) {
    group.stage = group.stage ?? scope.stage
    group.attempt = group.attempt ?? scope.attempt
    group.taskId = group.taskId ?? scope.taskId
    group.taskTitle = group.taskTitle ?? scope.taskTitle
  }
  if (item.type === "compose-summary") applySummary(group, item.summary)
}

function applySummary(group: TimelineActivityGroup, summary: ComposeSummaryCard): void {
  group.summary = summary
  if (summary.composeScope) {
    group.stage = group.stage ?? summary.composeScope.stage
    group.attempt = group.attempt ?? summary.composeScope.attempt
    group.taskId = group.taskId ?? summary.composeScope.taskId
    group.taskTitle = group.taskTitle ?? summary.composeScope.taskTitle
  }
  group.agentId = group.agentId ?? summary.agentId
  group.executionId = group.executionId ?? summary.executionId
}

function finalizeGroup(group: TimelineActivityGroup): TimelineActivityGroup {
  const hasTerminalSummary = Boolean(
    group.summary
    && (group.summary.status === "passed"
      || group.summary.status === "failed"
      || group.summary.status === "blocked"
      || group.summary.status === "cancelled"
      || group.summary.status === "truncated"),
  )
  const hasLiveWork = group.items.some(item => {
    if (item.type === "tool" && item.tool.status === "running") return true
    if (item.type === "reasoning" && item.reasoning.active) return true
    if (item.type === "interaction" && item.interaction.status === "pending") return true
    if (item.type === "message" && item.message.streaming) return true
    return false
  })
  return {
    ...group,
    terminal: hasTerminalSummary && !hasLiveWork,
  }
}

/** Compose 活动行文案：Compose · 阶段 · task · agent · 相位 · 耗时。 */
export function composeLiveStatusLine(input: {
  stage?: ComposeStageId | string | null
  taskTitle?: string | null
  agentId?: string | null
  phaseLabel: string
  elapsedLabel: string
}): string {
  const parts = ["Compose"]
  if (input.stage) {
    const stage = String(input.stage)
    parts.push(COMPOSE_STAGE_LABELS[stage as ComposeStageId] ?? stage)
  }
  if (input.taskTitle) parts.push(input.taskTitle)
  if (input.agentId) parts.push(input.agentId)
  parts.push(input.phaseLabel)
  parts.push(`已运行 ${input.elapsedLabel}`)
  return parts.join(" · ")
}
