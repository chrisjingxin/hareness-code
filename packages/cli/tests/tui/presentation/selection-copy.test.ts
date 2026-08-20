import { expect, test } from "bun:test"

import {
  copyCurrentSelection,
  shouldAttemptSelectionCopy,
} from "../../../src/tui/presentation/selection-copy"

test("非 Windows 仅在左键 mouse-up 时尝试复制选区", () => {
  expect(shouldAttemptSelectionCopy("darwin", { type: "mouse-up", button: 0 })).toBe(true)
  expect(shouldAttemptSelectionCopy("linux", { type: "mouse-up", button: 0 })).toBe(true)
  expect(shouldAttemptSelectionCopy("darwin", { type: "mouse-up", button: 2 })).toBe(false)
  expect(shouldAttemptSelectionCopy("linux", { type: "key-down", name: "c", ctrl: true })).toBe(false)
})

test("Windows 仅在 Ctrl+C 或右键 mouse-up 时尝试复制选区", () => {
  expect(shouldAttemptSelectionCopy("win32", { type: "key-down", name: "c", ctrl: true })).toBe(true)
  expect(shouldAttemptSelectionCopy("win32", { type: "mouse-up", button: 2 })).toBe(true)
  expect(shouldAttemptSelectionCopy("win32", { type: "key-down", name: "c", ctrl: false })).toBe(false)
  expect(shouldAttemptSelectionCopy("win32", { type: "mouse-up", button: 0 })).toBe(false)
})

test("空选区不清除选区、不写剪贴板且不显示 Toast", () => {
  let cleared = false
  const copied: string[] = []
  const toasts: Array<{ message: string; variant: "success" | "error" }> = []

  const result = copyCurrentSelection({
    getSelectedText: () => "",
    clearSelection: () => { cleared = true },
    writeClipboard: async text => {
      copied.push(text)
      return true
    },
    showToast: (message, variant) => { toasts.push({ message, variant }) },
  })

  expect(result).toBeUndefined()
  expect(cleared).toBe(false)
  expect(copied).toEqual([])
  expect(toasts).toEqual([])
})

test("非空选区先清除再等待剪贴板成功，并在成功后显示一次 Toast", async () => {
  const order: string[] = []
  const toasts: Array<{ message: string; variant: "success" | "error" }> = []
  let resolveClipboard: ((value: boolean) => void) | undefined

  const result = copyCurrentSelection({
    getSelectedText: () => "跨行选中的文本",
    clearSelection: () => { order.push("clear") },
    writeClipboard: text => {
      order.push(`copy:${text}`)
      return new Promise<boolean>(resolve => { resolveClipboard = resolve })
    },
    showToast: (message, variant) => { toasts.push({ message, variant }) },
  })

  if (!result || !resolveClipboard) throw new Error("非空选区应启动异步复制")
  expect(order).toEqual(["clear", "copy:跨行选中的文本"])
  expect(toasts).toEqual([])

  resolveClipboard(true)
  expect(await result).toBe(true)
  expect(toasts).toEqual([{ message: "已复制到剪贴板", variant: "success" }])
})

test("剪贴板返回 false 时显示失败 Toast 并稳定返回 false", async () => {
  const toasts: Array<{ message: string; variant: "success" | "error" }> = []
  const result = copyCurrentSelection({
    getSelectedText: () => "无法复制的文本",
    clearSelection: () => undefined,
    writeClipboard: async () => false,
    showToast: (message, variant) => { toasts.push({ message, variant }) },
  })

  if (!result) throw new Error("非空选区应启动异步复制")
  expect(await result).toBe(false)
  expect(toasts).toEqual([{ message: "复制到系统剪贴板失败", variant: "error" }])
})

test("剪贴板 rejection 转为失败 Toast，不向调用方泄漏 rejection", async () => {
  const toasts: Array<{ message: string; variant: "success" | "error" }> = []
  const result = copyCurrentSelection({
    getSelectedText: () => "异常文本",
    clearSelection: () => undefined,
    writeClipboard: async () => { throw new Error("clipboard unavailable") },
    showToast: (message, variant) => { toasts.push({ message, variant }) },
  })

  if (!result) throw new Error("非空选区应启动异步复制")
  expect(await result).toBe(false)
  expect(toasts).toEqual([{ message: "复制到系统剪贴板失败", variant: "error" }])
})
