/** Infrastructure 生产基础设施单测：Clock、IdGenerator 与 PromptHistoryStore 行为断言。 */

import { expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { resolve } from "node:path"

import { SystemClock } from "../../src/infrastructure/system-clock"
import { CryptoIdGenerator } from "../../src/infrastructure/system-id-generator"
import { FilePromptHistoryStore } from "../../src/infrastructure/prompt-history-file-store"

test("SystemClock 准确获取当前时间戳并计算非负持续时间", () => {
  const clock = new SystemClock()
  const start = clock.now()
  expect(start).toBeGreaterThan(0)
  expect(clock.duration(start, start + 100)).toBe(100)
  expect(clock.duration(start + 100, start)).toBe(0)
})

test("CryptoIdGenerator 生成标准 UUID 字符串", () => {
  const generator = new CryptoIdGenerator()
  const id1 = generator.uuid()
  const id2 = generator.uuid()
  expect(id1).not.toBe(id2)
  expect(id1.length).toBeGreaterThan(10)
})

test("FilePromptHistoryStore 正确加载、追加并去重历史文件", async () => {
  const dir = await mkdtemp(resolve(tmpdir(), "za38-history-test-"))
  const filePath = resolve(dir, "prompt-history.jsonl")
  try {
    const store = new FilePromptHistoryStore(filePath)
    expect(await store.load()).toEqual([])

    await store.append("hello world")
    await store.append("hello world") // 重复项被忽略
    await store.append("second prompt")

    expect(await store.load()).toEqual(["hello world", "second prompt"])

    // 新实例能重新读回磁盘内容
    const reloadStore = new FilePromptHistoryStore(filePath)
    expect(await reloadStore.load()).toEqual(["hello world", "second prompt"])
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})
