import { expect, test } from "bun:test"

import { canNavigatePromptHistory, rememberPrompt, selectPromptHistory } from "../../src/tui/prompt-history"

test("提示词历史去重并保留最近发送顺序", () => {
  expect(rememberPrompt(["第一条", "第二条"], "第一条")).toEqual(["第二条", "第一条"])
  expect(rememberPrompt(["第一条"], "   ")).toEqual(["第一条"])
})

test("上下键在空输入与历史提示词之间回填，并保留手动编辑行为", () => {
  const history = ["第一条", "第二条"]
  expect(canNavigatePromptHistory(history, "")).toBeTrue()
  expect(selectPromptHistory(history, "", "previous")).toBe("第二条")
  expect(selectPromptHistory(history, "第二条", "previous")).toBe("第一条")
  expect(selectPromptHistory(history, "第一条", "next")).toBe("第二条")
  expect(selectPromptHistory(history, "第二条", "next")).toBe("")
  expect(canNavigatePromptHistory(history, "第二条（已手动修改）")).toBeFalse()
  expect(selectPromptHistory(history, "第二条（已手动修改）", "previous")).toBeUndefined()
})
