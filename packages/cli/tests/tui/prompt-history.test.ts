import { expect, test } from "bun:test"
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import {
  canNavigatePromptHistory,
  loadPromptHistory,
  movePromptHistory,
  persistPromptHistory,
  promptHistoryPath,
  rememberPrompt,
  selectPromptHistory,
} from "../../src/tui/prompt-history"

test("提示词历史连续去重并保留最近发送顺序", () => {
  expect(rememberPrompt(["第一条", "第二条"], "第一条")).toEqual(["第一条", "第二条", "第一条"])
  expect(rememberPrompt(["第一条"], "第一条")).toEqual(["第一条"])
  expect(rememberPrompt(["第一条"], "   ")).toEqual(["第一条"])
})

test("独立历史游标在回到空草稿后不重复截获向下方向键", () => {
  const history = ["第一条", "第二条"]
  const previous = movePromptHistory(history, "", undefined, "previous")
  expect(previous).toEqual({ value: "第二条", cursor: { index: 1 } })

  const next = movePromptHistory(history, previous?.value ?? "", previous?.cursor, "next")
  expect(next).toEqual({ value: "", cursor: { index: 2 } })
  expect(movePromptHistory(history, "", next?.cursor, "next")).toBeUndefined()
})

test("历史 JSONL 跳过损坏行并自愈为受限的规范格式", async () => {
  const home = await mkdtemp(join(tmpdir(), "za38-history-"))
  const path = promptHistoryPath(home)
  try {
    await mkdir(join(home, ".harness"), { recursive: true })
    await writeFile(join(home, ".harness", "prompt-history.jsonl"), "{bad}\n{\"input\":\"第一条\"}\n\"第二条\"\n{\"input\":\"第二条\"}\n", "utf8")
    const history = await loadPromptHistory(path)
    expect(history).toEqual(["第一条", "第二条"])
    expect(await readFile(path, "utf8")).toBe("{\"input\":\"第一条\"}\n{\"input\":\"第二条\"}\n")
  } finally {
    await rm(home, { recursive: true, force: true })
  }
})

test("新增一条提示词追加到 JSONL，历史裁剪时重写保留集合", async () => {
  const home = await mkdtemp(join(tmpdir(), "za38-history-"))
  const path = promptHistoryPath(home)
  try {
    await persistPromptHistory([], ["第一条"], path)
    await persistPromptHistory(["第一条"], ["第一条", "第二条"], path)
    expect(await loadPromptHistory(path)).toEqual(["第一条", "第二条"])
  } finally {
    await rm(home, { recursive: true, force: true })
  }
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
