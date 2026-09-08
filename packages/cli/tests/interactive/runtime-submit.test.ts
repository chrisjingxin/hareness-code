/** 执行中提交路由：运行时命令放行，普通文字与禁用命令 rejected。 */

import { expect, test } from "bun:test"

import { makeHarness, notices } from "./harness"

test("执行中 /status 可打开；普通文字、转义斜杠与禁用命令 rejected 且不新开 Run", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "开始干活" })
    expect(harness.controller.getSnapshot().activeRun).not.toBeNull()
    expect(harness.calls.filter(call => call === "run.start")).toHaveLength(1)

    const status = await harness.controller.dispatch({ type: "input.submit", value: "/status" })
    expect(status).toEqual({ status: "accepted", effects: [{ type: "present", target: "status" }] })

    const compact = await harness.controller.dispatch({ type: "input.submit", value: "/compact" })
    expect(compact).toMatchObject({
      status: "rejected",
      code: "busy",
      message: "/compact 暂不可用：当前任务结束后可用。",
    })

    const resume = await harness.controller.dispatch({ type: "input.submit", value: "/resume" })
    expect(resume).toMatchObject({ status: "rejected", code: "busy", message: expect.stringContaining("/resume 暂不可用") })

    const text = await harness.controller.dispatch({ type: "input.submit", value: "继续改测试" })
    expect(text).toMatchObject({
      status: "rejected",
      code: "busy",
      message: "当前任务进行中，完成后发送，或用 Ctrl+C 中断后再发送。",
    })

    const escaped = await harness.controller.dispatch({ type: "input.submit", value: "//发给模型" })
    expect(escaped).toMatchObject({ status: "rejected", code: "busy" })
    expect(harness.calls.filter(call => call === "run.start")).toHaveLength(1)

    const unknown = await harness.controller.dispatch({ type: "input.submit", value: "/not-a-cmd" })
    expect(unknown).toEqual({ status: "accepted" })
    expect(notices(harness.controller.getSnapshot())).toContain("未知命令：/not-a-cmd")

    const models = await harness.controller.dispatch({ type: "input.submit", value: "/model" })
    expect(models).toMatchObject({ status: "rejected", code: "busy" })
  } finally {
    await harness.controller.close()
  }
})

test("执行中 /agents 可打开只读 picker；/help 仍是本地 notice", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "开始干活" })
    const agents = await harness.controller.dispatch({ type: "input.submit", value: "/agents" })
    expect(agents).toEqual({ status: "accepted", effects: [{ type: "present", target: "agents" }] })

    const help = await harness.controller.dispatch({ type: "input.submit", value: "/help" })
    expect(help).toEqual({ status: "accepted" })
    expect(notices(harness.controller.getSnapshot())).toContain("/status")
  } finally {
    await harness.controller.close()
  }
})

test("执行中 /plan 带目标只切档；/mcp 只读；/teams generate 拒绝留语义", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "开始干活" })
    const starts = harness.calls.filter(call => call === "run.start").length

    const plan = await harness.controller.dispatch({ type: "input.submit", value: "/plan 给登录做个方案" })
    expect(plan).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")
    expect(harness.calls.filter(call => call === "run.start")).toHaveLength(starts)

    const mcp = await harness.controller.dispatch({ type: "input.submit", value: "/mcp" })
    expect(mcp).toMatchObject({
      status: "accepted",
      effects: [{ type: "inspect-overlay", kind: "mcp", title: "MCP 状态" }],
    })
    const mcpAdd = await harness.controller.dispatch({ type: "input.submit", value: "/mcp add filesystem npx -y @mcp/server /tmp" })
    expect(mcpAdd).toMatchObject({
      status: "rejected",
      code: "busy",
      message: "/mcp 暂不可用：当前任务结束后可用。",
    })

    const generate = await harness.controller.dispatch({ type: "input.submit", value: "/teams generate review lead worker-a,worker-b 2" })
    expect(generate).toMatchObject({
      status: "rejected",
      code: "busy",
      message: "/teams 暂不可用：当前任务结束后可用。",
    })
    expect(harness.calls.filter(call => call === "run.start")).toHaveLength(starts)
    expect(harness.controller.getSnapshot().activeRun).not.toBeNull()
  } finally {
    await harness.controller.close()
  }
})

test("执行中 /quit 先确认；取消则继续跑，确认才 request-exit", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "开始干活" })
    const runId = harness.controller.getSnapshot().activeRun?.runId
    const quit = await harness.controller.dispatch({ type: "input.submit", value: "/quit" })
    expect(quit).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().confirmation).toMatchObject({
      confirmationId: "quit-while-running",
      message: expect.stringContaining("当前任务将被中止"),
    })

    const cancelled = await harness.controller.dispatch({
      type: "confirmation.resolve",
      confirmationId: "quit-while-running",
      confirmed: false,
    })
    expect(cancelled).toEqual({ status: "accepted" })
    expect(harness.controller.getSnapshot().confirmation).toBeNull()
    expect(harness.controller.getSnapshot().activeRun?.runId).toBe(runId)

    await harness.controller.dispatch({ type: "input.submit", value: "/q" })
    const confirmed = await harness.controller.dispatch({
      type: "confirmation.resolve",
      confirmationId: "quit-while-running",
      confirmed: true,
    })
    expect(confirmed).toEqual({ status: "accepted", effects: [{ type: "request-exit" }] })
    expect(harness.controller.getSnapshot().activeRun?.runId).toBe(runId)
  } finally {
    await harness.controller.close()
  }
})
