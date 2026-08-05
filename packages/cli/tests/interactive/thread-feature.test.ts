import { expect, test } from "bun:test"
import { makeHarness, flush } from "./harness"

test("Thread 切换：/resume 打开选择器并原子恢复，运行中禁止切换", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "command.execute", commandId: "thread.resume" })
    await flush()
    expect(harness.controller.getSnapshot().catalogs.threads.status).toBe("ready")
    expect(harness.controller.getSnapshot().catalogs.threads.items).toHaveLength(2)

    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-2" })
    await flush()
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.currentThreadId).toBe("thread-2")
    expect(snapshot.timeline).toHaveLength(2)

    await harness.controller.dispatch({ type: "input.submit", value: "运行中" })
    const outcome = await harness.controller.dispatch({ type: "thread.open", threadId: "thread-1" })
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.currentThreadId).toBe("thread-2")
    expect(outcome).toEqual({ status: "rejected", code: "busy", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("/new 确认后取消并清空 Thread；取消失败保留原 Thread", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "保留这个 Thread" })
    const run = harness.runHandles.at(-1)!
    await harness.controller.dispatch({ type: "command.execute", commandId: "thread.new" })
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.confirmation?.confirmationId).toBe("clear-thread")

    await harness.controller.dispatch({ type: "confirmation.resolve", confirmationId: "clear-thread", confirmed: true })
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.currentThreadId).toBeNull()
    expect(snapshot.timeline).toHaveLength(0)
    expect(harness.calls).toContain("run.cancel")
    expect(run.runId).toBeTruthy()
  } finally {
    await harness.controller.close()
  }
})

test("Thread 打开期间重复打开被拒绝，stale 模型结果不覆盖新 Thread", async () => {
  const harness = makeHarness()
  let releaseFirst!: () => void
  const gate = new Promise<void>(resolve => { releaseFirst = resolve })
  let openCount = 0
  const originalOpen = harness.port.openThread.bind(harness.port)
  harness.port.openThread = async (threadId: string) => {
    openCount += 1
    const result = await originalOpen(threadId)
    if (openCount === 1) await gate
    return result
  }
  try {
    const first = harness.controller.dispatch({ type: "thread.open", threadId: "thread-1" })
    await flush()
    // openingThread 保护：第二个 open 不发 RPC。
    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-2" })
    await flush()
    expect(harness.calls.filter(call => call === "threads.open")).toHaveLength(1)
    releaseFirst()
    await first
    await flush()
    expect(harness.controller.getSnapshot().currentThreadId).toBe("thread-1")
  } finally {
    await harness.controller.close()
  }
})

