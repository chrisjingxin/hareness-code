/** @ 候选窗口导航纯函数测试：固定行数、边界夹取与窗口跟随。 */

import { describe, expect, test } from "bun:test"

import {
  compactMentionRows,
  ensureMentionWindow,
  mentionRowsForTerminal,
  moveMentionSelection,
} from "../../src/presentation-shared/mention-window-policy"

describe("mention window policy", () => {
  test("标准终端显示 8 行，矮终端显示 5 行", () => {
    expect(mentionRowsForTerminal(30)).toBe(8)
    expect(mentionRowsForTerminal(18)).toBe(compactMentionRows)
    expect(compactMentionRows).toBe(5)
  })

  test("上下移动在首尾夹取，不循环", () => {
    expect(moveMentionSelection(0, -1, 20)).toBe(0)
    expect(moveMentionSelection(19, 1, 20)).toBe(19)
    expect(moveMentionSelection(4, 1, 20)).toBe(5)
  })

  test("翻页按可见行数移动，窗口跟随选中项", () => {
    const selectedIndex = moveMentionSelection(0, 8, 30)
    const window = ensureMentionWindow(selectedIndex, 0, 30, 8)
    expect(selectedIndex).toBe(8)
    expect(window).toEqual({ start: 1, end: 9 })
  })

  test("候选减少后同时夹取选中项与窗口", () => {
    expect(ensureMentionWindow(99, 92, 3, 8)).toEqual({ start: 0, end: 3 })
  })
})
