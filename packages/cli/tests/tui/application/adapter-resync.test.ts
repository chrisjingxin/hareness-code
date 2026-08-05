/** TUI Adapter 的 Handoff 恢复重同步测试：返回 TUI 后按 Web 会话 Thread 重拉，空首页时清空。 */

import { expect, test } from "bun:test"
import { createTuiAdapter } from "../../../src/tui/application/adapter"
import { makeHarness, flush } from "../../interactive/harness"

function createAdapter(controller: ReturnType<typeof makeHarness>["controller"]) {
  return createTuiAdapter({
    controller,
    onRequestExit: () => undefined,
  })
}

test("resyncAfterHandoff：按 Web 会话 Thread 重开并拉取最新内容", async () => {
  const harness = makeHarness()
  try {
    const adapter = createAdapter(harness.controller)
    const openCallsBefore = harness.calls.filter(call => call === "threads.open").length

    await adapter.resyncAfterHandoff("thread-1")
    await flush()

    expect(harness.calls.filter(call => call === "threads.open").length).toBe(openCallsBefore + 1)
    expect(harness.controller.getSnapshot().currentThreadId).toBe("thread-1")
    expect(harness.controller.getSnapshot().timeline.length).toBeGreaterThan(0)
  } finally {
    await harness.controller.close()
  }
})

test("resyncAfterHandoff：Web 会话为空首页时清空回首页，不启动 Run", async () => {
  const harness = makeHarness()
  try {
    const adapter = createAdapter(harness.controller)
    // 先建立已打开的 Thread，再验证 null 时真正发生清空迁移。
    await adapter.resyncAfterHandoff("thread-1")
    await flush()
    expect(harness.controller.getSnapshot().currentThreadId).toBe("thread-1")

    await adapter.resyncAfterHandoff(null)
    await flush()

    expect(harness.controller.getSnapshot().currentThreadId).toBeNull()
    expect(harness.calls).not.toContain("run.start")
  } finally {
    await harness.controller.close()
  }
})

test("resyncAfterHandoff：任务运行中保留当前 Thread 并提示，不重开 Web Thread", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "运行中" })
    const run = harness.runHandles.at(-1)!
    const adapter = createAdapter(harness.controller)
    const openCallsBefore = harness.calls.filter(call => call === "threads.open").length

    await adapter.resyncAfterHandoff("thread-2")
    await flush()

    expect(harness.calls.filter(call => call === "threads.open").length).toBe(openCallsBefore)
    expect(harness.controller.getSnapshot().currentThreadId).toBe(run.threadId)
    expect(adapter.getSnapshot().transientNotice?.message).toContain("任务仍在运行")
  } finally {
    await harness.controller.close()
  }
})

test("resyncAfterHandoff：closed 后为 no-op", async () => {
  const harness = makeHarness()
  try {
    const adapter = createAdapter(harness.controller)
    await adapter.close()
    const openCallsBefore = harness.calls.filter(call => call === "threads.open").length
    await adapter.resyncAfterHandoff("thread-1")
    expect(harness.calls.filter(call => call === "threads.open").length).toBe(openCallsBefore)
  } finally {
    await harness.controller.close()
  }
})
