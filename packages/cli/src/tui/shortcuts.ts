/** 全局快捷键解析器；方向键留给 textarea 依据光标位置处理。 */

type KeyLike = {
  name: string
  ctrl: boolean
}

export type ShortcutContext = {
  skillPickerVisible?: boolean
  skillOptionCount?: number
  threadPickerVisible?: boolean
  threadOptionCount?: number
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
  | "close-skill-picker"
  | "skill-previous"
  | "skill-next"
  | "skill-select"
  | "skill-block"
  | "close-thread-picker"
  | "thread-previous"
  | "thread-next"
  | "thread-select"
  | "thread-block"
  | "command-open"
  | "clear-draft"
  | "cancel-run"
  | "exit"
  | "clear-selected-skill"
  | "toggle-tool-details"

/** 快捷键先处理临时菜单，再处理运行态，避免输入控件吞掉 Ctrl+C 与 Esc。 */
export function resolveShortcut(key: KeyLike, context: ShortcutContext): ShortcutAction {
  if (context.threadPickerVisible) {
    if (key.name === "escape") return "close-thread-picker"
    if (key.name === "up" || (key.ctrl && key.name === "p")) return "thread-previous"
    if (key.name === "down" || (key.ctrl && key.name === "n")) return "thread-next"
    if (key.name === "return" || key.name === "kpenter" || key.name === "tab") {
      return (context.threadOptionCount ?? 0) > 0 ? "thread-select" : "thread-block"
    }
  }
  if (context.skillPickerVisible) {
    if (key.name === "escape") return "close-skill-picker"
    if (key.name === "up" || (key.ctrl && key.name === "p")) return "skill-previous"
    if (key.name === "down" || (key.ctrl && key.name === "n")) return "skill-next"
    if (key.name === "return" || key.name === "kpenter" || key.name === "tab") {
      return (context.skillOptionCount ?? 0) > 0 ? "skill-select" : "skill-block"
    }
  }
  if (context.commandMenuVisible) {
    if (key.name === "escape") return "close-command-menu"
    if (key.name === "up" || (key.ctrl && key.name === "p")) return "command-previous"
    if (key.name === "down" || (key.ctrl && key.name === "n")) return "command-next"
    if (key.name === "return" || key.name === "kpenter" || key.name === "tab") {
      return context.commandOptionCount > 0 ? "command-select" : "command-block"
    }
  }

  if (key.ctrl && key.name === "p") return "command-open"
  // 方向键必须留给 textarea：它需要依据真实光标边界决定回填历史还是滚动 thread。
  if (key.ctrl && key.name === "c") {
    if (context.hasDraft) return "clear-draft"
    return context.activeRun ? "cancel-run" : "exit"
  }
  if (key.name === "escape" && context.activeRun) return "cancel-run"
  if (key.name === "escape" && !context.hasDraft) return "clear-selected-skill"
  if (key.ctrl && key.name === "o") return "toggle-tool-details"
  if (key.ctrl && key.name === "d" && !context.activeRun && !context.hasDraft) return "exit"
  return "none"
}
