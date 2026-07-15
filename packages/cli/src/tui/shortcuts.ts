type KeyLike = {
  name: string
  ctrl: boolean
}

export type ShortcutContext = {
  commandMenuVisible: boolean
  commandOptionCount: number
  activeRun: boolean
  hasDraft: boolean
  canScrollConversation: boolean
  canNavigatePromptHistory: boolean
}

export type ShortcutAction =
  | "none"
  | "close-command-menu"
  | "command-previous"
  | "command-next"
  | "command-select"
  | "command-block"
  | "command-open"
  | "clear-draft"
  | "cancel-run"
  | "exit"
  | "toggle-tool-details"
  | "history-previous"
  | "history-next"
  | "scroll-conversation-up"
  | "scroll-conversation-down"
  | "scroll-conversation-page-up"
  | "scroll-conversation-page-down"

/** 快捷键先处理临时菜单，再处理运行态，避免输入控件吞掉 Ctrl+C 与 Esc。 */
export function resolveShortcut(key: KeyLike, context: ShortcutContext): ShortcutAction {
  if (context.commandMenuVisible) {
    if (key.name === "escape") return "close-command-menu"
    if (key.name === "up" || (key.ctrl && key.name === "p")) return "command-previous"
    if (key.name === "down" || (key.ctrl && key.name === "n")) return "command-next"
    if (key.name === "return" || key.name === "kpenter" || key.name === "tab") {
      return context.commandOptionCount > 0 ? "command-select" : "command-block"
    }
  }

  if (key.ctrl && key.name === "p") return "command-open"
  if (context.canNavigatePromptHistory) {
    if (key.name === "up") return "history-previous"
    if (key.name === "down") return "history-next"
  }
  // 无可回填历史时，空输入才把方向键借给会话时间线；手动编辑仍由 textarea 处理。
  if (context.canScrollConversation && !context.hasDraft) {
    if (key.name === "up") return "scroll-conversation-up"
    if (key.name === "down") return "scroll-conversation-down"
    if (key.name === "pageup") return "scroll-conversation-page-up"
    if (key.name === "pagedown") return "scroll-conversation-page-down"
  }
  if (key.ctrl && key.name === "c") {
    if (context.hasDraft) return "clear-draft"
    return context.activeRun ? "cancel-run" : "exit"
  }
  if (key.name === "escape" && context.activeRun) return "cancel-run"
  if (key.ctrl && key.name === "o") return "toggle-tool-details"
  if (key.ctrl && key.name === "d" && !context.activeRun && !context.hasDraft) return "exit"
  return "none"
}
