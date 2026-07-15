type KeyLike = {
  name: string
  ctrl: boolean
}

export type ShortcutContext = {
  commandMenuVisible: boolean
  commandOptionCount: number
  activeRun: boolean
  hasDraft: boolean
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
  // 方向键必须留给 textarea：它需要依据真实光标边界决定回填历史还是滚动会话。
  if (key.ctrl && key.name === "c") {
    if (context.hasDraft) return "clear-draft"
    return context.activeRun ? "cancel-run" : "exit"
  }
  if (key.name === "escape" && context.activeRun) return "cancel-run"
  if (key.ctrl && key.name === "o") return "toggle-tool-details"
  if (key.ctrl && key.name === "d" && !context.activeRun && !context.hasDraft) return "exit"
  return "none"
}
