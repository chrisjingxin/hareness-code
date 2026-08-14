/** 从 write_todos 参数抽出清单，避免把 JSON 当 UI。 */

export type TodoStatus = "pending" | "in_progress" | "completed" | "cancelled"

export type TodoItem = {
  content: string
  status: TodoStatus
}

const STATUSES = new Set<TodoStatus>(["pending", "in_progress", "completed", "cancelled"])

function asStatus(value: unknown): TodoStatus {
  return typeof value === "string" && STATUSES.has(value as TodoStatus) ? value as TodoStatus : "pending"
}

/** 只认 `{ todos: [{ content, status }] }`；畸形或空列表返回空。 */
export function parseWriteTodos(raw: string): TodoItem[] {
  if (!raw.trim()) return []
  try {
    const parsed = JSON.parse(raw) as { todos?: unknown }
    if (!Array.isArray(parsed.todos)) return []
    return parsed.todos.flatMap(item => {
      if (!item || typeof item !== "object") return []
      const content = (item as { content?: unknown }).content
      if (typeof content !== "string" || !content.trim()) return []
      return [{ content: content.trim(), status: asStatus((item as { status?: unknown }).status) }]
    })
  } catch {
    return []
  }
}

/** ASCII 状态位：终端字体一致，不用 emoji。 */
export function todoMarker(status: TodoStatus): string {
  if (status === "completed") return "[x]"
  if (status === "in_progress") return "[>]"
  if (status === "cancelled") return "[-]"
  return "[ ]"
}

/** 已完成条数，供标题和摘要。 */
export function todoProgressLabel(items: readonly TodoItem[]): string {
  const done = items.filter(item => item.status === "completed").length
  return `${done}/${items.length}`
}

/** 当前应关注的那一条：进行中优先，否则第一条未完成。 */
export function currentTodo(items: readonly TodoItem[]): TodoItem | undefined {
  return items.find(item => item.status === "in_progress") ?? items.find(item => item.status === "pending")
}
