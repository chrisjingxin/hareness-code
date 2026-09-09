/** Thread 列表显示名：有短标题用短标题，否则回退到消息预览。 */

export type ThreadTitleSource = {
  title: string | null
  first_message: string
  latest_message: string
}

/** `/resume`、Web 侧栏与 TUI 检查器共用的主标签公式。 */
export function threadDisplayTitle(thread: ThreadTitleSource): string {
  const named = thread.title?.trim()
  if (named) return named
  const fallback = thread.first_message.trim() || thread.latest_message.trim()
  return fallback || "（无标题）"
}

/** 当前打开的 thread 在检查器里显示的名字；没有会话时为「新会话」。 */
export function currentThreadDisplayTitle(input: {
  currentThreadId: string | null
  threads: readonly (ThreadTitleSource & { thread_id: string })[]
}): string {
  if (!input.currentThreadId) return "新会话"
  const thread = input.threads.find(item => item.thread_id === input.currentThreadId)
  if (!thread) return "新会话"
  return threadDisplayTitle(thread)
}

/** 本地搜索：标题、第一条和最近一条消息都可命中。 */
export function threadMatchesQuery(thread: ThreadTitleSource, query: string): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  const haystack = `${thread.title ?? ""}\n${thread.first_message}\n${thread.latest_message}`.toLowerCase()
  return haystack.includes(needle)
}
