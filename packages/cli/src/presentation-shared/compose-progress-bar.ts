/** Compose 五段进度条的纯展示模型：TUI/Web 共用，不持有 reducer 状态。 */

import type { ComposeProjection } from "../interactive/state"
import { COMPOSE_STAGE_LABELS } from "./timeline-activity-groups"

export type ComposeStepperMark = "done" | "current" | "pending" | "failed" | "skipped"

export type ComposeStepperSegment = {
  id: string
  label: string
  mark: ComposeStepperMark
}

/** 从 live 投影或终态摘要取出要画的进度。 */
export function resolveComposeProgress(
  interactive: { composeState: ComposeProjection | null; lastRun?: { composeSummary?: ComposeProjection | null } | null },
): ComposeProjection | null {
  return interactive.composeState ?? interactive.lastRun?.composeSummary ?? null
}

export function composeStepperMark(state: string): ComposeStepperMark {
  if (state === "confirmed" || state === "passed" || state === "completed") return "done"
  if (state === "failed" || state === "blocked" || state === "cancelled") return "failed"
  if (state === "skipped") return "skipped"
  if (state === "current" || state === "running" || state === "waiting_user") return "current"
  return "pending"
}

/** 步骤名保持固定宽度；进行中/等你/失败只出现在整条栏右侧。 */
export function composeStepperHint(progress: ComposeProjection): string {
  const current = progress.stages.find(stage => stage.state === "current" || stage.state === "failed")
  if (!current) return progress.status === "completed" ? "已完成" : ""
  if (current.state === "failed") return "失败"
  if (progress.waiting.endsWith("_confirm") || progress.waiting === "ask_user") return "等你确认"
  if (progress.status === "waiting_user") return "等你确认"
  return "进行中"
}

export function composeStepperSegments(progress: ComposeProjection): ComposeStepperSegment[] {
  return progress.stages.map(stage => ({
    id: stage.id,
    label: COMPOSE_STAGE_LABELS[stage.id] ?? stage.id,
    mark: composeStepperMark(stage.state),
  }))
}

/** 已走过的步骤之后画实线，当前/失败/未开始之后留空线。 */
export function composeStepperTrackFilled(previous: ComposeStepperMark): boolean {
  return previous === "done" || previous === "skipped"
}
