import { expect, test } from "bun:test"
import { EventType } from "@za38/protocol"
import { makeHarness, flush, notices, terminalEvent, approvalRequest, planRequest } from "./harness"
import { PLAN_IMPLEMENT_PROMPT } from "../../src/interactive/features/run-feature"

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

test("approval-mode.set 直接选择审批模式，并传递给下一次 Run", async () => {
  const harness = makeHarness()
  try {
    const outcome = await harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })
    expect(outcome).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")

    await harness.controller.dispatch({ type: "input.submit", value: "使用指定模式" })
    expect(harness.port.lastRunSelection()).toMatchObject({ message: "使用指定模式", approvalMode: "yolo" })
  } finally {
    await harness.controller.close()
  }
})

test("/plan 记下进入前档位，exit 恢复；Shift+Tab 离开 plan 走到 default", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })
    await harness.controller.dispatch({ type: "input.submit", value: "/plan" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")
    expect(harness.calls).not.toContain("run.start")

    await harness.controller.dispatch({ type: "input.submit", value: "/plan exit" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")

    await harness.controller.dispatch({ type: "input.submit", value: "/plan" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("default")
  } finally {
    await harness.controller.close()
  }
})

test("/plan 带目标先切到 plan 再提交该句", async () => {
  const harness = makeHarness()
  try {
    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "/plan 给登录做个方案" })
    expect(outcome).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")
    expect(harness.port.lastRunSelection()).toMatchObject({
      message: "给登录做个方案",
      approvalMode: "plan",
    })
  } finally {
    await harness.controller.close()
  }
})

test("批准计划后恢复进入前档位并自动开实现轮", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })
    await harness.controller.dispatch({ type: "input.submit", value: "/plan 给登录做个方案" })
    const planRun = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(planRequest(planRun.threadId, planRun.runId))
    await flush()
    expect(harness.controller.getSnapshot().interaction?.type).toBe("plan")
    expect(harness.controller.getSnapshot().interaction).toMatchObject({ hasPlan: true })

    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "plan-1",
      response: { kind: "plan", decision: "approved" },
    })
    expect(await responsePromise).toMatchObject({ type: "plan", decision: "approved" })

    harness.port.completeRun(planRun.threadId, planRun.runId)
    await flush()
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")
    expect(harness.port.lastRunSelection()).toMatchObject({
      message: PLAN_IMPLEMENT_PROMPT,
      approvalMode: "yolo",
    })
  } finally {
    await harness.controller.close()
  }
})

test("批准计划时的行批注附在自动实现轮提示中", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "/plan 给登录做个方案" })
    const planRun = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(planRequest(planRun.threadId, planRun.runId))
    await flush()
    const review = "Proposed plan line 2:\n> 保留协议\nComment: 不要改公开接口"
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "plan-1",
      response: { kind: "plan", decision: "approved", feedback: review },
    })
    expect(await responsePromise).toMatchObject({ type: "plan", decision: "approved", feedback: review })

    harness.port.completeRun(planRun.threadId, planRun.runId)
    await flush()
    expect(harness.port.lastRunSelection()?.message).toBe(
      `${PLAN_IMPLEMENT_PROMPT}\n\n批准时的审阅意见：\n${review}`,
    )
  } finally {
    await harness.controller.close()
  }
})

test("Default、AutoEdit、Auto、YOLO 进入计划后都恢复原档位并开始实现", async () => {
  for (const mode of ["default", "auto-edit", "auto", "yolo"] as const) {
    const harness = makeHarness()
    try {
      await harness.controller.dispatch({ type: "approval-mode.set", mode })
      await harness.controller.dispatch({ type: "input.submit", value: `/plan 为 ${mode} 规划` })
      const planRun = harness.runHandles.at(-1)!
      const responsePromise = harness.port.sendInteraction(planRequest(planRun.threadId, planRun.runId))
      await flush()
      await harness.controller.dispatch({
        type: "interaction.respond",
        requestId: "plan-1",
        response: { kind: "plan", decision: "approved" },
      })
      await responsePromise
      harness.port.completeRun(planRun.threadId, planRun.runId)
      await flush()
      expect(harness.controller.getSnapshot().runtime.approvalMode).toBe(mode)
      expect(harness.port.lastRunSelection()).toMatchObject({
        message: PLAN_IMPLEMENT_PROMPT,
        approvalMode: mode,
      })
    } finally {
      await harness.controller.close()
    }
  }
})

test("/plan-view 读取当前 thread 计划为只读预览，关闭后不产生审批", async () => {
  const harness = makeHarness({
    initialThreadId: "thread-plan-view",
    openThreadImpl: async threadId => ({
      thread: { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: "规划", latest_message: "规划", message_count: 1, title: null },
      messages: [{ kind: "user", content: "规划" }],
      plan: {
        has_plan: true,
        plan_markdown: "# 当前计划\n\n完成停点 4。",
        plan_virtual_path: "/.harness/plan.md",
        plan_display_path: `~/.harness/plans/${threadId}.md`,
      },
    }),
  })
  try {
    await flush()
    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "/plan-view" })
    expect(outcome).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().interaction).toMatchObject({
      type: "plan",
      readOnly: true,
      planMarkdown: "# 当前计划\n\n完成停点 4。",
      decisions: [],
    })
    expect(harness.abandoned).toEqual([])

    expect(await harness.controller.dispatch({ type: "plan-view.close" })).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().interaction).toBeNull()
  } finally {
    await harness.controller.close()
  }
})

test("执行中 /plan-view 无挂起审批时返回 inspect-overlay，不进入 PlanDock", async () => {
  const harness = makeHarness({
    initialThreadId: "thread-plan-view",
    openThreadImpl: async threadId => ({
      thread: { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: "规划", latest_message: "规划", message_count: 1, title: null },
      messages: [{ kind: "user", content: "规划" }],
      plan: {
        has_plan: true,
        plan_markdown: "# 当前计划\n\n完成停点 4。",
        plan_virtual_path: "/.harness/plan.md",
        plan_display_path: `~/.harness/plans/${threadId}.md`,
      },
    }),
  })
  try {
    await flush()
    expect((await harness.controller.dispatch({ type: "input.submit", value: "先做当前任务" })).status).toBe("accepted")
    expect(harness.controller.getSnapshot().activeRun).not.toBeNull()

    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "/plan-view" })
    expect(outcome.status).toBe("accepted")
    expect(outcome.status === "accepted" ? outcome.effects : undefined).toEqual([
      expect.objectContaining({
        type: "inspect-overlay",
        kind: "plan",
        body: expect.stringContaining("完成停点 4。"),
      }),
    ])
    expect(harness.controller.getSnapshot().interaction).toBeNull()
    expect(harness.controller.getSnapshot().activeRun).not.toBeNull()
  } finally {
    await harness.controller.close()
  }
})

test("放弃计划只恢复档位，不自动开跑", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })
    await harness.controller.dispatch({ type: "input.submit", value: "/plan 给登录做个方案" })
    const planRun = harness.runHandles.at(-1)!
    const starts = harness.calls.filter(call => call === "run.start").length
    const responsePromise = harness.port.sendInteraction(planRequest(planRun.threadId, planRun.runId, { has_plan: false, plan_markdown: "" }))
    await flush()
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "plan-1",
      response: { kind: "plan", decision: "abandoned" },
    })
    expect(await responsePromise).toMatchObject({ decision: "abandoned" })
    harness.port.completeRun(planRun.threadId, planRun.runId)
    await flush()
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")
    expect(harness.calls.filter(call => call === "run.start").length).toBe(starts)
  } finally {
    await harness.controller.close()
  }
})

test("active Run 时 /plan 与 Shift+Tab 先提交 Host，再更新当前 snapshot", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const run = harness.runHandles.at(-1)!
    const before = harness.calls.filter(call => call === "run.start").length

    const planOutcome = await harness.controller.dispatch({ type: "input.submit", value: "/plan" })
    expect(planOutcome).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")
    expect(harness.controller.getSnapshot().runtime.approvalModeRevision).toBe(1)
    expect(harness.controller.getSnapshot().activeRun?.runId).toBe(run.runId)
    expect(harness.calls.filter(call => call === "run.start").length).toBe(before)
    expect(harness.calls).toContain("run.set_approval_mode")

    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    await harness.controller.dispatch({ type: "input.submit", value: "规划问题" })
    expect(harness.port.lastRunSelection()).toMatchObject({ message: "规划问题", approvalMode: "plan" })
  } finally {
    await harness.controller.close()
  }
})

test("active Run 审批 RPC 失败时保留旧 mode/revision，不污染 UI", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    harness.port.setApprovalModeImpl(async () => {
      throw new Error("RUN_APPROVAL_MODE_BUSY")
    })

    const outcome = await harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })

    expect(outcome).toEqual({
      status: "rejected",
      code: "agent-error",
      message: "审批模式切换失败：RUN_APPROVAL_MODE_BUSY",
    })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("default")
    expect(harness.controller.getSnapshot().runtime.approvalModeRevision).toBe(0)
  } finally {
    await harness.controller.close()
  }
})

test("活动 Run 结束后忽略迟到的审批 RPC 响应", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const run = harness.runHandles.at(-1)!
    let resolveResponse!: (value: any) => void
    const response = new Promise<any>(resolve => { resolveResponse = resolve })
    harness.port.setApprovalModeImpl(async () => response)

    const outcomePromise = harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })
    await flush()
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    resolveResponse({ thread_id: run.threadId, run_id: run.runId, approval_mode: "yolo", revision: 1 })

    await expect(outcomePromise).resolves.toEqual({
      status: "rejected",
      code: "agent-error",
      message: "审批模式切换失败：活动 Run 已结束，忽略旧审批模式响应",
    })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("default")
    expect(harness.controller.getSnapshot().runtime.approvalModeRevision).toBe(0)
  } finally {
    await harness.controller.close()
  }
})

test("同一 Run 的旧 revision 响应不能回退已确认的审批状态", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const run = harness.runHandles.at(-1)!
    let callCount = 0
    let resolveResponse!: (value: any) => void
    const staleResponse = new Promise<any>(resolve => { resolveResponse = resolve })
    harness.port.setApprovalModeImpl(async (threadId, runId, mode) => {
      callCount += 1
      if (callCount === 1) return { thread_id: threadId, run_id: runId, approval_mode: mode, revision: 2 }
      return staleResponse
    })

    await harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })
    const outcomePromise = harness.controller.dispatch({ type: "approval-mode.set", mode: "default" })
    await flush()
    resolveResponse({ thread_id: run.threadId, run_id: run.runId, approval_mode: "default", revision: 1 })

    await expect(outcomePromise).resolves.toEqual({
      status: "rejected",
      code: "agent-error",
      message: "审批模式切换失败：收到过期的审批模式 revision",
    })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")
    expect(harness.controller.getSnapshot().runtime.approvalModeRevision).toBe(2)
  } finally {
    await harness.controller.close()
  }
})

test("并发活动切档按服务端确认顺序维护 plan 恢复档位", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const run = harness.runHandles.at(-1)!
    let callCount = 0
    let releaseFirst!: () => void
    const firstResponse = new Promise<void>(resolve => { releaseFirst = resolve })
    harness.port.setApprovalModeImpl(async (threadId, runId, mode) => {
      callCount += 1
      if (callCount === 1) {
        await firstResponse
        return { thread_id: threadId, run_id: runId, approval_mode: "yolo", revision: 1 }
      }
      return { thread_id: threadId, run_id: runId, approval_mode: mode, revision: 2 }
    })

    const yoloPromise = harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })
    const planPromise = harness.controller.dispatch({ type: "approval-mode.set", mode: "plan" })
    await flush()
    releaseFirst()

    await expect(yoloPromise).resolves.toEqual({ status: "accepted" })
    await expect(planPromise).resolves.toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")
    expect(callCount).toBe(2)

    await harness.controller.dispatch({ type: "input.submit", value: "/plan exit" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")
  } finally {
    harness.port.completeRun(harness.runHandles.at(-1)!.threadId, harness.runHandles.at(-1)!.runId)
    await flush()
    await harness.controller.close()
  }
})

test("并发活动循环切档在队列执行时读取最新确认模式", async () => {
  const harness = makeHarness()
  let releaseFirst: (() => void) | undefined
  let firstPromise: Promise<unknown> | undefined
  let secondPromise: Promise<unknown> | undefined
  let unsubscribe: (() => void) | undefined
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const run = harness.runHandles.at(-1)!
    let callCount = 0
    const requestedModes: string[] = []
    const firstResponse = new Promise<void>(resolve => { releaseFirst = resolve })
    harness.port.setApprovalModeImpl(async (threadId, runId, mode) => {
      callCount += 1
      requestedModes.push(mode)
      if (callCount === 1) {
        await firstResponse
        return { thread_id: threadId, run_id: runId, approval_mode: "auto-edit", revision: 1 }
      }
      return { thread_id: threadId, run_id: runId, approval_mode: mode, revision: 2 }
    })

    const revisions = [harness.controller.getSnapshot().runtime.approvalModeRevision ?? 0]
    unsubscribe = harness.controller.subscribe(snapshot => {
      revisions.push(snapshot.runtime.approvalModeRevision ?? 0)
    })

    firstPromise = harness.controller.dispatch({ type: "approval-mode.cycle" })
    secondPromise = harness.controller.dispatch({ type: "approval-mode.cycle" })
    await flush()
    expect(requestedModes).toEqual(["auto-edit"])

    releaseFirst!()
    await expect(firstPromise).resolves.toEqual({ status: "accepted" })
    await expect(secondPromise).resolves.toEqual({ status: "accepted" })
    expect(requestedModes).toEqual(["auto-edit", "auto"])
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("auto")
    expect(harness.controller.getSnapshot().runtime.approvalModeRevision).toBe(2)
    expect(revisions.at(-1)).toBe(2)
    expect(revisions.every((revision, index) => index === 0 || revision >= revisions[index - 1]!)).toBe(true)
  } finally {
    unsubscribe?.()
    releaseFirst?.()
    await Promise.allSettled([firstPromise, secondPromise].filter((promise): promise is Promise<unknown> => promise !== undefined))
    const activeRun = harness.runHandles.at(-1)
    if (activeRun) harness.port.completeRun(activeRun.threadId, activeRun.runId)
    await flush()
    await harness.controller.close()
  }
})

test("存在 pending Interaction 时本地先拒绝审批模式切换", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "需要审批" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId))
    await flush()

    const outcome = await harness.controller.dispatch({ type: "approval-mode.set", mode: "yolo" })

    expect(outcome).toEqual({ status: "rejected", code: "busy", message: "存在待处理交互，暂不能切换审批模式" })
    expect(harness.calls).not.toContain("run.set_approval_mode")
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    await responsePromise
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

test("上下文压缩收敛期间 work-mode.cycle 被拒绝（busy）", async () => {
  let finishCompact!: () => void
  const compactResult = new Promise<{ compacted: true; context: { action: "manual_summary" } }>(resolve => {
    finishCompact = () => resolve({ compacted: true, context: { action: "manual_summary" } })
  })
  const harness = makeHarness({ compactContextImpl: async () => compactResult })
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "建立可压缩的 thread" })
    const run = harness.runHandles.at(-1)!
    harness.port.completeRun(run.threadId, run.runId)
    await flush()

    const compactDispatch = harness.controller.dispatch({ type: "command.execute", commandId: "context.compact" })
    await flush()
    const outcome = await harness.controller.dispatch({ type: "work-mode.cycle" })

    expect(outcome).toEqual({ status: "rejected", code: "busy", message: expect.any(String) })
    expect(harness.controller.getSnapshot().workMode).toBe("build")

    finishCompact()
    await compactDispatch
    await harness.controller.dispatch({ type: "work-mode.cycle" })
    expect(harness.controller.getSnapshot().workMode).toBe("compose")
  } finally {
    finishCompact?.()
    await harness.controller.close()
  }
})

test("compose.progress 事件经 controller 折叠进 snapshot", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "实现搜索" })
    const run = harness.runHandles.at(-1)!
    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_PROGRESS, run.threadId, run.runId, 1, {
      thread_id: run.threadId,
      slug: "jsondiff",
      complexity: "simple",
      status: "active",
      current_stage: "grill",
      waiting: "ask_user",
      stages: [{ id: "requirement", state: "current" }],
      documents: [],
      fix_rounds: 0,
      revision: 1,
    }))
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.composeState?.currentStage).toBe("grill")
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
    const frame = (revision: number, waiting: string) => ({
      thread_id: run.threadId,
      slug: "jsondiff",
      complexity: "simple",
      status: "active",
      current_stage: "grill",
      waiting,
      stages: [{ id: "requirement", state: revision === 2 ? "failed" : "current" }],
      documents: [],
      fix_rounds: 0,
      revision,
    })
    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_PROGRESS, run.threadId, run.runId, 3, frame(0, "none")))
    await flush()
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.timeline.some(item => item.type === "message" && item.message.role === "user")).toBeTrue()
    expect(snapshot.composeState?.revision).toBe(0)
    expect(snapshot.activity.kind).toBe("running")

    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_PROGRESS, run.threadId, run.runId, 4, frame(1, "none")))
    harness.port.emitEvent(terminalEvent(EventType.COMPOSE_PROGRESS, run.threadId, run.runId, 5, frame(2, "none")))
    harness.port.emitEvent(terminalEvent(EventType.RUN_FAILED, run.threadId, run.runId, 6, {
      error: { code: "COMPOSE_ARTIFACT_INVALID", message: "stage 输出为空：模型没有产出 JSON 对象", retryable: false },
    }))
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.lastRun?.outcome).toBe("failed")
    expect(snapshot.composeState).toBeNull()
    // 失败后仍保留最后一份完整投影：用户能看见哪个阶段失败。
    expect(snapshot.lastRun?.composeSummary?.currentStage).toBe("grill")
    expect(snapshot.lastRun?.composeSummary?.stages[0]).toEqual({ id: "requirement", state: "failed" })
    const text = snapshot.timeline.map(item => item.type === "message" ? item.message.content : "").join("")
    expect(text).toContain("stage 输出为空")
  } finally {
    await harness.controller.close()
  }
})
