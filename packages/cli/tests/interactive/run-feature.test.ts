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

test("Compose 失败前 TUI 保留用户消息与进度投影，失败后显示错误", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "work-mode.cycle" })  // → compose
    await harness.controller.dispatch({ type: "input.submit", value: "实现搜索" })
    const run = harness.runHandles.at(-1)!
    // run.started
    harness.port.emitEvent(terminalEvent(EventType.RUN_STARTED, run.threadId, run.runId, 1, {
      resumed: false, mode: "compose", skills_snapshot_id: null,
    }))
    // 进度帧 rev 0/1/2（understand 两次 schema retry 后失败）
    const frame = (revision: number, status: string) => ({
      revision,
      stage: "understand",
      status,
      stages: [
        { id: "understand", status: revision === 2 ? "failed" : "running", attempts: revision === 0 ? 1 : 2 },
        { id: "plan", status: "pending", attempts: 0 },
        { id: "build", status: "pending", attempts: 0 },
        { id: "verify", status: "pending", attempts: 0 },
        { id: "review", status: "pending", attempts: 0 },
      ],
      tasks: [],
      evidence: [],
      blocked_reason: null,
    })
    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_STATE, run.threadId, run.runId, 3, frame(0, "running")))
    await flush()
    let snapshot = harness.controller.getSnapshot()
    // 错误出现前：用户消息 + 进度投影都在。
    expect(snapshot.timeline.some(item => item.type === "message" && item.message.role === "user")).toBeTrue()
    expect(snapshot.composeState?.revision).toBe(0)
    expect(snapshot.activity.kind).toBe("running")

    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_STATE, run.threadId, run.runId, 4, frame(1, "running")))
    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_STATE, run.threadId, run.runId, 5, frame(2, "failed")))
    harness.port.emitEvent(terminalEvent(EventType.RUN_FAILED, run.threadId, run.runId, 6, {
      error: { code: "COMPOSE_ARTIFACT_INVALID", message: "stage 输出为空：模型没有产出 JSON 对象", retryable: false },
    }))
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.lastRun?.outcome).toBe("failed")
    expect(snapshot.composeState).toBeNull()
    // 失败后仍保留最后一份完整投影：用户能看见哪个阶段失败。
    expect(snapshot.lastRun?.composeSummary?.stage).toBe("understand")
    expect(snapshot.lastRun?.composeSummary?.status).toBe("failed")
    expect(snapshot.lastRun?.composeSummary?.stages[0]).toEqual({ id: "understand", status: "failed", attempts: 2 })
    const text = snapshot.timeline.map(item => item.type === "message" ? item.message.content : "").join("")
    expect(text).toContain("stage 输出为空")
  } finally {
    await harness.controller.close()
  }
})
