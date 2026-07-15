export const PROMPT_HISTORY_LIMIT = 100

/** 会话内只保留用户实际发送的提示词；重复发送时移动到末尾，便于再次调用。 */
export function rememberPrompt(history: readonly string[], prompt: string): string[] {
  const normalized = prompt.trim()
  if (!normalized) return [...history]
  return [...history.filter(item => item !== normalized), normalized].slice(-PROMPT_HISTORY_LIMIT)
}

/**
 * 仅在空 composer 或当前内容正好来自历史记录时接管方向键。
 * 用户手动修改后的多行提示词仍交给 textarea 移动光标，避免破坏正常编辑。
 */
export function canNavigatePromptHistory(history: readonly string[], draft: string): boolean {
  return history.length > 0 && (draft.length === 0 || history.lastIndexOf(draft) >= 0)
}

/** 返回应回填的提示词；向下越过最新条目时返回空字符串。 */
export function selectPromptHistory(
  history: readonly string[],
  draft: string,
  direction: "previous" | "next",
): string | undefined {
  if (!canNavigatePromptHistory(history, draft)) return undefined
  const current = draft.length === 0 ? history.length : history.lastIndexOf(draft)
  const target = direction === "previous"
    ? Math.max(0, current - 1)
    : Math.min(history.length, current + 1)
  return target === history.length ? "" : history[target]
}
