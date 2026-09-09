/** Thread catalog 生命周期：run 终态、thread.open 后自动刷新，以及按更新时间排序。 */

import { expect, test } from "bun:test"

import { EventType, type ThreadSummary } from "@za38/protocol"

import { sortThreadsByRecency } from "../../src/interactive/features/catalog-feature"
import { flush, makeHarness, notices, terminalEvent, threadSummary } from "./harness"

test("Run 完成后自动刷新 Thread catalog", async () => {
  const harness = makeHarness()
  try {
    const before = harness.calls.filter(call => call === "threads.list").length
    await harness.controller.dispatch({ type: "input.submit", value: "你好" })
    const run = harness.runHandles.at(-1)!
    harness.port.emitEvent(terminalEvent(EventType.RUN_STARTED, run.threadId, run.runId, 1, {}))
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    const after = harness.calls.filter(call => call === "threads.list").length
    expect(after).toBeGreaterThan(before)
  } finally {
    await harness.controller.close()
  }
})

test("Run 失败收敛后同样刷新 Thread catalog", async () => {
  const harness = makeHarness()
  try {
    const before = harness.calls.filter(call => call === "threads.list").length
    await harness.controller.dispatch({ type: "input.submit", value: "触发失败" })
    const run = harness.runHandles.at(-1)!
    harness.port.emitEvent(terminalEvent(EventType.RUN_STARTED, run.threadId, run.runId, 1, {}))
    harness.port.failRunWithEvent(run.threadId, run.runId)
    await flush()
    const after = harness.calls.filter(call => call === "threads.list").length
    expect(after).toBeGreaterThan(before)
    expect(harness.controller.getSnapshot().activeRun).toBeNull()
  } finally {
    await harness.controller.close()
  }
})

test("打开 Thread 后自动刷新 Thread catalog", async () => {
  const harness = makeHarness()
  try {
    const before = harness.calls.filter(call => call === "threads.list").length
    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-9" })
    await flush()
    const after = harness.calls.filter(call => call === "threads.list").length
    expect(after).toBeGreaterThan(before)
  } finally {
    await harness.controller.close()
  }
})

test("Run 受理后立刻刷新 Thread catalog", async () => {
  const harness = makeHarness()
  try {
    const before = harness.calls.filter(call => call === "threads.list").length
    await harness.controller.dispatch({ type: "input.submit", value: "你好" })
    await flush()
    const after = harness.calls.filter(call => call === "threads.list").length
    expect(after).toBeGreaterThan(before)
    expect(harness.controller.getSnapshot().activeRun).not.toBeNull()
  } finally {
    await harness.controller.close()
  }
})

test("thread.summary 通知合并 catalog 且不进入时间线", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-1" })
    await flush()
    const before = harness.controller.getSnapshot().timeline.length
    harness.port.emitThreadSummary({
      ...threadSummary("thread-1", "第一条历史"),
      title: "自动短标题",
    })
    await flush()
    const titled = harness.controller.getSnapshot().catalogs.threads.items.find(item => item.thread_id === "thread-1")
    expect(titled?.title).toBe("自动短标题")
    expect(harness.controller.getSnapshot().timeline.length).toBe(before)
  } finally {
    await harness.controller.close()
  }
})

test("/title 成功后立刻合并 catalog 标题", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-1" })
    await flush()
    await harness.controller.dispatch({ type: "input.submit", value: "/title 修索引" })
    await flush()
    expect(harness.calls).toContain("threads.set_title")
    const titled = harness.controller.getSnapshot().catalogs.threads.items.find(item => item.thread_id === "thread-1")
    expect(titled?.title).toBe("修索引")
    expect(notices(harness.controller.getSnapshot())).toContain("已将标题设为「修索引」")
  } finally {
    await harness.controller.close()
  }
})

test("Thread catalog 按 updated_at_ms 降序排列", () => {
  const older: ThreadSummary = { thread_id: "old", created_at_ms: 1, updated_at_ms: 100, first_message: "旧", latest_message: "旧", message_count: 1, title: null }
  const newer: ThreadSummary = { thread_id: "new", created_at_ms: 1, updated_at_ms: 200, first_message: "新", latest_message: "新", message_count: 1, title: null }
  const sorted = sortThreadsByRecency([older, newer])
  expect(sorted.map(item => item.thread_id)).toEqual(["new", "old"])
  // 不修改入参数组。
  expect([older, newer].map(item => item.thread_id)).toEqual(["old", "new"])
})
