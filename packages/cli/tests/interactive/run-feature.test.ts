import { expect, test } from "bun:test"
import { EventType } from "@za38/protocol"
import { makeHarness, flush, notices, terminalEvent, approvalRequest } from "./harness"

test("approval-mode.cycle 循环切换审批模式，并传递给下一次 Run", async () => {
  const harness = makeHarness()
  try {
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("default")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("auto-edit")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("auto")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")

    await harness.controller.dispatch({ type: "input.submit", value: "使用覆盖模式" })
    const selection = harness.port.lastRunSelection()
    expect(selection).toMatchObject({ message: "使用覆盖模式", approvalMode: "plan" })
  } finally {
    await harness.controller.close()
  }
})

test("普通输入启动 Run，流式内容与终态更新 Timeline", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "你好" })
    const run = harness.runHandles.at(-1)!
    expect(run.threadId).toBe("thread-1")
    expect(harness.controller.getSnapshot().activeRun).toEqual({ threadId: run.threadId, runId: run.runId })

    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 1, { text: "正在" }))
    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 2, { text: "思考" }))
    await flush()
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.timeline.at(-1)).toMatchObject({ type: "message", message: { role: "assistant", content: "正在思考", streaming: true } })

    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.activeRun).toBeNull()
    expect(snapshot.lastRun?.outcome).toBe("completed")
    expect(snapshot.activity.kind).toBe("completed")
  } finally {
    await harness.controller.close()
  }
})

test("active Run 时重复提交不调用 startRun", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const before = harness.calls.filter(call => call === "run.start").length
    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "第二次" })
    expect(harness.calls.filter(call => call === "run.start").length).toBe(before)
    expect(outcome).toEqual({ status: "rejected", code: "busy", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("旧 Run 的迟到终态不能结束新 Run", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一个" })
    const first = harness.runHandles.at(-1)!
    harness.port.completeRun(first.threadId, first.runId)
    await flush()

    await harness.controller.dispatch({ type: "input.submit", value: "第二个" })
    const second = harness.runHandles.at(-1)!
    expect(harness.controller.getSnapshot().activeRun?.runId).toBe(second.runId)
    harness.port.failRunWithEvent(first.threadId, first.runId)
    await flush()
    expect(harness.controller.getSnapshot().activeRun?.runId).toBe(second.runId)
  } finally {
    await harness.controller.close()
  }
})

test("Run 终态与 Controller close 都会 abandon 未完成 Interaction", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "终态清理" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId))
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    expect(harness.abandoned).toContain("approval-1")
    expect(await responsePromise).toMatchObject({ decision: "reject" })
  } finally {
    await harness.controller.close()
  }
})

test("connection close 禁止新 Run 并收敛 Interaction", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "断连前" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId))
    harness.port.closeConnection("transport closed")
    await flush()
    expect(harness.controller.getSnapshot().connection.status).toBe("closed")
    expect(harness.abandoned).toContain("approval-1")
    expect(await responsePromise).toMatchObject({ decision: "reject" })

    await harness.controller.dispatch({ type: "input.submit", value: "断连后" })
    expect(harness.calls.filter(call => call === "run.start").length).toBe(1)
  } finally {
    await harness.controller.close()
  }
})



test("work-mode.cycle 空闲时切换 Build/Compose 并传给下一次 Run", async () => {
  const harness = makeHarness()
  try {
    expect(harness.controller.getSnapshot().workMode).toBe("build")
    await harness.controller.dispatch({ type: "work-mode.cycle" })
    expect(harness.controller.getSnapshot().workMode).toBe("compose")
    await harness.controller.dispatch({ type: "work-mode.cycle" })
    expect(harness.controller.getSnapshot().workMode).toBe("build")

    await harness.controller.dispatch({ type: "work-mode.cycle" })
    await harness.controller.dispatch({ type: "input.submit", value: "实现搜索" })
    const selection = harness.port.lastRunSelection()
    expect(selection).toMatchObject({ message: "实现搜索", mode: "compose" })
  } finally {
    await harness.controller.close()
  }
})

test("active Run 时 work-mode.cycle 被拒绝（busy）", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const outcome = await harness.controller.dispatch({ type: "work-mode.cycle" })
    expect(outcome).toEqual({ status: "rejected", code: "busy", message: expect.any(String) })
    expect(harness.controller.getSnapshot().workMode).toBe("build")
  } finally {
    await harness.controller.close()
  }
})

test("compose.state 事件经 controller 折叠进 snapshot", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "实现搜索" })
    const run = harness.runHandles.at(-1)!
    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_STATE, run.threadId, run.runId, 1, {
      revision: 1,
      stage: "understand",
      status: "running",
      stages: [
        { id: "understand", status: "running", attempts: 1 },
        { id: "plan", status: "pending", attempts: 0 },
        { id: "build", status: "pending", attempts: 0 },
        { id: "verify", status: "pending", attempts: 0 },
        { id: "review", status: "pending", attempts: 0 },
      ],
      tasks: [],
      evidence: [],
      blocked_reason: null,
    }))
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.composeState?.stage).toBe("understand")
    expect(snapshot.composeState?.revision).toBe(1)
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    expect(harness.controller.getSnapshot().composeState).toBeNull()
  } finally {
    await harness.controller.close()
  }
})
