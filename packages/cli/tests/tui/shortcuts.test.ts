import { expect, test } from "bun:test"

import { resolveShortcut } from "../../src/tui/shortcuts"

const idle = {
  commandMenuVisible: false,
  commandOptionCount: 0,
  activeRun: false,
  hasDraft: false,
  canScrollConversation: false,
  canNavigatePromptHistory: false,
}

test("Ctrl+C 按输入与运行状态分层处理", () => {
  expect(resolveShortcut({ name: "c", ctrl: true }, { ...idle, hasDraft: true })).toBe("clear-draft")
  expect(resolveShortcut({ name: "c", ctrl: true }, { ...idle, activeRun: true })).toBe("cancel-run")
  expect(resolveShortcut({ name: "c", ctrl: true }, idle)).toBe("exit")
})

test("命令菜单优先消费导航、选择与关闭快捷键", () => {
  const menu = { ...idle, commandMenuVisible: true, commandOptionCount: 2, hasDraft: true }
  expect(resolveShortcut({ name: "down", ctrl: false }, menu)).toBe("command-next")
  expect(resolveShortcut({ name: "p", ctrl: true }, menu)).toBe("command-previous")
  expect(resolveShortcut({ name: "tab", ctrl: false }, menu)).toBe("command-select")
  expect(resolveShortcut({ name: "escape", ctrl: false }, menu)).toBe("close-command-menu")
  expect(resolveShortcut({ name: "return", ctrl: false }, { ...menu, commandOptionCount: 0 })).toBe("command-block")
})

test("Esc、Ctrl+P、Ctrl+O 和 Ctrl+D 保留真实 TUI 行为", () => {
  expect(resolveShortcut({ name: "escape", ctrl: false }, { ...idle, activeRun: true })).toBe("cancel-run")
  expect(resolveShortcut({ name: "p", ctrl: true }, idle)).toBe("command-open")
  expect(resolveShortcut({ name: "o", ctrl: true }, idle)).toBe("toggle-tool-details")
  expect(resolveShortcut({ name: "d", ctrl: true }, idle)).toBe("exit")
})

test("空 composer 的上下键和分页键浏览会话，不抢占编辑中的光标", () => {
  const session = { ...idle, canScrollConversation: true }
  expect(resolveShortcut({ name: "up", ctrl: false }, session)).toBe("scroll-conversation-up")
  expect(resolveShortcut({ name: "down", ctrl: false }, session)).toBe("scroll-conversation-down")
  expect(resolveShortcut({ name: "pageup", ctrl: false }, session)).toBe("scroll-conversation-page-up")
  expect(resolveShortcut({ name: "pagedown", ctrl: false }, session)).toBe("scroll-conversation-page-down")
  expect(resolveShortcut({ name: "up", ctrl: false }, { ...session, hasDraft: true })).toBe("none")
})

test("已有提示词历史时优先回填历史，分页仍用于浏览会话", () => {
  const session = { ...idle, canScrollConversation: true, canNavigatePromptHistory: true }
  expect(resolveShortcut({ name: "up", ctrl: false }, session)).toBe("history-previous")
  expect(resolveShortcut({ name: "down", ctrl: false }, { ...session, hasDraft: true })).toBe("history-next")
  expect(resolveShortcut({ name: "pageup", ctrl: false }, session)).toBe("scroll-conversation-page-up")
})
