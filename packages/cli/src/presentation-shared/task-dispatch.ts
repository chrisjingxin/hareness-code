/** task 派出的用户可读视图：谁接手、做什么，不把 JSON 键铺开。 */

/** 从 task 参数抽出的派出信息。 */
export type TaskDispatchView = {
  agentId: string | null
  description: string | null
}

/** 解析 task 参数；认 subagent_type/agent 与 description/prompt。畸形或空得到空视图。 */
export function parseTaskDispatch(argumentsText: string | undefined): TaskDispatchView {
  if (!argumentsText?.trim()) return { agentId: null, description: null }
  try {
    const parsed: unknown = JSON.parse(argumentsText)
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { agentId: null, description: null }
    }
    const record = parsed as Record<string, unknown>
    return {
      agentId: scalarText(record.subagent_type) ?? scalarText(record.agent),
      description: scalarText(record.description) ?? scalarText(record.prompt),
    }
  } catch {
    return { agentId: null, description: null }
  }
}

/** 有角色或任务描述时才走友好卡片，否则回退通用 JSON 渲染。 */
export function hasTaskDispatchView(view: TaskDispatchView): boolean {
  return Boolean(view.agentId || view.description)
}

/** 卡片分区：任务是派出内容，结论是子代理回报。 */
export const TASK_DISPATCH_TASK_LABEL = "任务"
export const TASK_DISPATCH_RESULT_LABEL = "结论"

/** 时间线标题：有 id 则「派出 {id}」，否则「派出子代理」。 */
export function taskDispatchLabel(view: TaskDispatchView): string {
  return view.agentId ? `派出 ${view.agentId}` : "派出子代理"
}

/** 折叠行主参数：任务描述单行截断；没有描述则不占这一列。 */
export function taskDispatchPrimaryLine(view: TaskDispatchView, maxChars: number = 72): string | null {
  if (!view.description) return null
  return truncateSingleLine(view.description, maxChars)
}

function scalarText(value: unknown): string | null {
  if (typeof value !== "string") return null
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : null
}

function truncateSingleLine(text: string, maxChars: number): string {
  const singleLine = text.replace(/\s+/g, " ").trim()
  if (singleLine.length <= maxChars) return singleLine
  return `${singleLine.slice(0, maxChars - 1)}…`
}
