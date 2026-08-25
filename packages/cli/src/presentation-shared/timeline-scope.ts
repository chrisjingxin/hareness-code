/** 按 execution 切时间线：父视图丢掉 child 项，子视图只留该 execution。 */

import type { TimelineItem } from "../interactive/state"

export const ROOT_EXECUTION_ID = "root"

export function isRootExecutionId(executionId: string | undefined | null): boolean {
  if (!executionId) return true
  if (executionId === ROOT_EXECUTION_ID) return true
  if (executionId.startsWith("root-")) return true
  if (executionId.startsWith("team-root-")) return true
  return false
}

/** 一条时间线项所属的 execution；缺省视为父 root。 */
export function timelineItemExecutionId(item: TimelineItem): string {
  let executionId: string | undefined
  if (item.type === "tool") executionId = item.tool.executionId
  else if (item.type === "message") executionId = item.message.executionId
  else if (item.type === "reasoning") executionId = item.reasoning.executionId
  else if (item.type === "interaction") executionId = item.interaction.executionId
  else if (item.type === "compose-summary") executionId = item.summary.executionId

  if (isRootExecutionId(executionId)) return ROOT_EXECUTION_ID
  return executionId ?? ROOT_EXECUTION_ID
}

/** 父视图传 root；子视图传 child execution id。 */
export function scopeTimeline(
  items: readonly TimelineItem[],
  executionId: string,
): readonly TimelineItem[] {
  if (isRootExecutionId(executionId)) {
    return items.filter(item => isRootExecutionId(timelineItemExecutionId(item)))
  }
  return items.filter(item => timelineItemExecutionId(item) === executionId)
}
