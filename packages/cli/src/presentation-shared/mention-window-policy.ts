/**
 * @ 候选窗口策略：TUI 与 Web 共用选中夹取、翻页和窗口跟随规则。
 *
 * 纯函数，零平台与组件依赖。
 */

export const standardMentionRows = 8
export const compactMentionRows = 5
const compactTerminalHeight = 19

/** 矮终端减少三行候选，给输入区和对话区保留空间。 */
export function mentionRowsForTerminal(terminalHeight: number): number {
  return terminalHeight < compactTerminalHeight ? compactMentionRows : standardMentionRows
}

/** 以 delta 移动全局候选索引；到达首尾后保持，不循环。 */
export function moveMentionSelection(selectedIndex: number, delta: number, itemCount: number): number {
  if (itemCount <= 0) return 0
  return Math.max(0, Math.min(itemCount - 1, selectedIndex + delta))
}

/** 让选中项保持在当前窗口内，并把起止位置夹取到候选范围。 */
export function ensureMentionWindow(
  selectedIndex: number,
  currentStart: number,
  itemCount: number,
  visibleRows: number,
): { readonly start: number; readonly end: number } {
  if (itemCount <= 0) return { start: 0, end: 0 }
  const rows = Math.max(1, visibleRows)
  const selected = Math.max(0, Math.min(itemCount - 1, selectedIndex))
  const maxStart = Math.max(0, itemCount - rows)
  let start = Math.max(0, Math.min(maxStart, currentStart))
  if (selected < start) start = selected
  else if (selected >= start + rows) start = selected - rows + 1
  start = Math.max(0, Math.min(maxStart, start))
  return { start, end: Math.min(itemCount, start + rows) }
}
