/** 全局快捷键解析器；方向键留给 textarea 依据光标位置处理。 */

/** 滚动意图：行/半页/跳转首尾，由全局快捷键与空 composer 方向键共用。 */
export type ScrollIntent = "line-up" | "line-down" | "page-up" | "page-down" | "top" | "bottom"

type KeyLike = {
  name: string
  ctrl: boolean
}

export type ShortcutContext = {
  commandDialogVisible?: boolean
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
  | "confirm-command-dialog"
  | "cancel-command-dialog"
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
  | "scroll-line-up"
  | "scroll-line-down"
  | "scroll-page-up"
  | "scroll-page-down"
  | "scroll-top"
  | "scroll-bottom"

/** 滚动专用快捷键：Ctrl 组合键与 PageUp/PageDown，避免抢占方向键与 Home/End 的文本编辑语义。 */
function resolveScrollShortcut(key: KeyLike): ShortcutAction {
  if (key.ctrl) {
    switch (key.name) {
      case "up": return "scroll-line-up"
      case "down": return "scroll-line-down"
      case "home": return "scroll-top"
      case "end": return "scroll-bottom"
    }
  }
  switch (key.name) {
    case "pageup": return "scroll-page-up"
    case "pagedown": return "scroll-page-down"
  }
  return "none"
}

/** 快捷键先处理临时菜单，再处理运行态，避免输入控件吞掉 Ctrl+C 与 Esc。 */
export function resolveShortcut(key: KeyLike, context: ShortcutContext): ShortcutAction {
  if (context.commandDialogVisible) {
    if (key.name === "escape") return "cancel-command-dialog"
    if (key.name === "return" || key.name === "kpenter") return "confirm-command-dialog"
    return "none"
  }
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

  // 滚动键全局生效（含正在输入或运行中），与 opencode 的 session.global 对齐；
  // 浮层打开时让位给选择器，避免在背后滚动历史。
  if (!context.commandMenuVisible && !context.skillPickerVisible && !context.threadPickerVisible) {
    const scrollAction = resolveScrollShortcut(key)
    if (scrollAction !== "none") return scrollAction
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
