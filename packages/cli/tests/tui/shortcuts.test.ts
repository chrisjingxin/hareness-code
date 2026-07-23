import { expect, test } from "bun:test"

import { resolveShortcut } from "../../src/tui/shortcuts"

const idle = {
  commandMenuVisible: false,
  commandOptionCount: 0,
  activeRun: false,
  hasDraft: false,
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

test("命令确认框优先消费确认和取消快捷键", () => {
  const dialog = { ...idle, commandDialogVisible: true, commandMenuVisible: true, commandOptionCount: 2 }
  expect(resolveShortcut({ name: "return", ctrl: false }, dialog)).toBe("confirm-command-dialog")
  expect(resolveShortcut({ name: "escape", ctrl: false }, dialog)).toBe("cancel-command-dialog")
  expect(resolveShortcut({ name: "down", ctrl: false }, dialog)).toBe("none")
})

test("Skill 选择器优先消费搜索框导航、选择与关闭键", () => {
  const picker = { ...idle, skillPickerVisible: true, skillOptionCount: 2, commandMenuVisible: true }
  expect(resolveShortcut({ name: "down", ctrl: false }, picker)).toBe("skill-next")
  expect(resolveShortcut({ name: "return", ctrl: false }, picker)).toBe("skill-select")
  expect(resolveShortcut({ name: "escape", ctrl: false }, picker)).toBe("close-skill-picker")
})

test("thread 恢复选择器优先消费导航、选择与关闭键", () => {
  const picker = { ...idle, threadPickerVisible: true, threadOptionCount: 2, skillPickerVisible: true }
  expect(resolveShortcut({ name: "down", ctrl: false }, picker)).toBe("thread-next")
  expect(resolveShortcut({ name: "return", ctrl: false }, picker)).toBe("thread-select")
  expect(resolveShortcut({ name: "escape", ctrl: false }, picker)).toBe("close-thread-picker")
})

test("Esc、Ctrl+P、Ctrl+O 和 Ctrl+D 保留真实 TUI 行为", () => {
  expect(resolveShortcut({ name: "escape", ctrl: false }, { ...idle, activeRun: true })).toBe("cancel-run")
  expect(resolveShortcut({ name: "p", ctrl: true }, idle)).toBe("command-open")
  expect(resolveShortcut({ name: "o", ctrl: true }, idle)).toBe("toggle-tool-details")
  expect(resolveShortcut({ name: "d", ctrl: true }, idle)).toBe("exit")
})

test("方向键不再被全局快捷键抢占，交由 textarea 根据真实光标位置处理", () => {
  expect(resolveShortcut({ name: "up", ctrl: false }, idle)).toBe("none")
  expect(resolveShortcut({ name: "down", ctrl: false }, { ...idle, hasDraft: true })).toBe("none")
  expect(resolveShortcut({ name: "pageup", ctrl: false }, idle)).toBe("none")
})
