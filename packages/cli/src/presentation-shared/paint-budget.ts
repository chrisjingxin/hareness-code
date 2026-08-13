/** TUI 有界绘制：只返回窗口内文本，不截 Core 存稿。 */

export type PaintBudgetOptions = {
  maxLines: number
  maxChars: number
  keep: "head" | "tail"
}

export type BoundedVisibleText = {
  text: string
  overflow: boolean
  hiddenLines: number
}

/**
 * 按行窗口再按码点窗口裁剪可见文本。
 * 行窗口决定 hiddenLines；码点裁剪只置 overflow，不把半行算进 hiddenLines。
 */
export function boundVisibleText(text: string, options: PaintBudgetOptions): BoundedVisibleText {
  if (text.length === 0) {
    return { text: "", overflow: false, hiddenLines: 0 }
  }

  const lines = text.split("\n")
  const hiddenLines = Math.max(0, lines.length - options.maxLines)
  const visibleLines = hiddenLines === 0
    ? lines
    : options.keep === "tail"
      ? lines.slice(lines.length - options.maxLines)
      : lines.slice(0, options.maxLines)
  let visible = visibleLines.join("\n")
  const chars = Array.from(visible)
  let overflow = hiddenLines > 0
  if (chars.length > options.maxChars) {
    overflow = true
    visible = options.keep === "tail"
      ? chars.slice(chars.length - options.maxChars).join("")
      : chars.slice(0, options.maxChars).join("")
  }
  return { text: visible, overflow, hiddenLines }
}

export const THINKING_LIVE_BUDGET = { maxLines: 12, maxChars: 4096, keep: "tail" } as const
export const THINKING_EXPANDED_BUDGET = { maxLines: 40, maxChars: 8192, keep: "head" } as const
export const TOOL_PREVIEW_BUDGET = { maxLines: 12, maxChars: 4096, keep: "head" } as const
export const WRITE_FILE_EXPANDED_BUDGET = { maxLines: 100, maxChars: 32_768, keep: "head" } as const

/** write_file 正文窗口：默认 12 行，展开最多 100 行。 */
export function writeFileVisibleBody(text: string, expanded: boolean): BoundedVisibleText {
  return boundVisibleText(text, expanded ? WRITE_FILE_EXPANDED_BUDGET : TOOL_PREVIEW_BUDGET)
}

export type ThinkingPaintState = "live" | "collapsed" | "expanded"

/** 思考正文窗口；折叠时不输出正文，只保留剩余行事实。 */
export function thinkingVisibleBody(text: string, state: ThinkingPaintState): BoundedVisibleText {
  if (state === "collapsed") {
    const lineCount = text.length === 0 ? 0 : text.split("\n").length
    return { text: "", overflow: lineCount > 0, hiddenLines: lineCount }
  }
  return boundVisibleText(text, state === "live" ? THINKING_LIVE_BUDGET : THINKING_EXPANDED_BUDGET)
}

/** 进行中不允许折叠；完成后点击切换展开。 */
export function nextThinkingExpanded(active: boolean, expanded: boolean): boolean {
  if (active) return true
  return !expanded
}
