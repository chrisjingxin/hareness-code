/** 输入栏执行中仍可聚焦；压缩中与独占浮层才失焦。 */

import { expect, test } from "bun:test"

import { inputBarPlaceholder, inputBarShouldFocus } from "../../../src/tui/presentation/input-bar"

test("执行中输入栏保持焦点；压缩中与 picker 失焦", () => {
  expect(inputBarShouldFocus({ compacting: false, awaitingQuestion: false, pickerVisible: false })).toBe(true)
  expect(inputBarShouldFocus({ compacting: true, awaitingQuestion: false, pickerVisible: false })).toBe(false)
  expect(inputBarShouldFocus({ compacting: true, awaitingQuestion: true, pickerVisible: false })).toBe(true)
  expect(inputBarShouldFocus({ compacting: false, awaitingQuestion: false, pickerVisible: true })).toBe(false)
})

test("执行中 placeholder 提示 Ctrl+C 中断", () => {
  expect(inputBarPlaceholder({
    awaitingQuestion: false,
    compacting: false,
    activeRun: true,
    isShell: false,
  })).toBe("正在执行；Ctrl+C 中断")
  expect(inputBarPlaceholder({
    awaitingQuestion: false,
    compacting: true,
    activeRun: false,
    isShell: false,
  })).toBe("正在压缩上下文…")
})
