import { expect, test } from "bun:test"
import { Capability } from "@za38/protocol"
import { makeHarness, flush, notices, runtime } from "./harness"

test("/agents 打开浏览目录，不把长文写进时间线", async () => {
  const harness = makeHarness({
    capabilities: [...(runtime.capabilities ?? []), Capability.AGENTS_READ],
  })
  try {
    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "/agents" })
    expect(outcome).toEqual({ status: "accepted", effects: [{ type: "present", target: "agents" }] })
    expect(notices(harness.controller.getSnapshot())).not.toContain("general-purpose")
    const catalog = harness.controller.getSnapshot().catalogs.agents
    expect(catalog.status).toBe("ready")
    expect(catalog.items.map(agent => agent.id)).toEqual(["general-purpose", "explore"])
    expect(harness.calls).toContain("agents.list")
  } finally {
    await harness.controller.close()
  }
})

test("/agents 查询失败时 catalog 进入 error，浮层仍可打开", async () => {
  const harness = makeHarness({
    capabilities: [...(runtime.capabilities ?? []), Capability.AGENTS_READ],
  })
  harness.port.setListAgentsImpl(async () => {
    throw new Error("catalog unavailable")
  })
  try {
    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "/agents" })
    expect(outcome).toEqual({ status: "accepted", effects: [{ type: "present", target: "agents" }] })
    const catalog = harness.controller.getSnapshot().catalogs.agents
    expect(catalog.status).toBe("error")
    expect(catalog.message).toContain("catalog unavailable")
    expect(notices(harness.controller.getSnapshot())).not.toContain("Agent 查询失败")
  } finally {
    await harness.controller.close()
  }
})


test("compact/status/help/web 命令语义确定", async () => {
  const harness = makeHarness()
  try {
    const exitResult = await harness.controller.dispatch({ type: "command.execute", commandId: "system.quit" })
    expect(exitResult).toEqual({ status: "accepted", effects: [{ type: "request-exit" }] })

    const handoff = await harness.controller.dispatch({ type: "command.execute", commandId: "host.web" })
    expect(handoff).toEqual({ status: "accepted", effects: [{ type: "request-handoff", threadId: null }] })

    await harness.controller.dispatch({ type: "input.submit", value: "需要压缩" })
    const run = harness.runHandles.at(-1)!
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    await harness.controller.dispatch({ type: "command.execute", commandId: "context.compact" })
    expect(notices(harness.controller.getSnapshot())).toContain("上下文已压缩")

    const statusResult = await harness.controller.dispatch({ type: "command.execute", commandId: "system.status" })
    expect(statusResult).toEqual({ status: "accepted", effects: [{ type: "present", target: "status" }] })
    expect(notices(harness.controller.getSnapshot())).not.toContain("工作区")
  } finally {
    await harness.controller.close()
  }
})

test("Compose-only 命令在 Build 模式手输返回本地错误且不进入模型", async () => {
  const harness = makeHarness()
  try {
    const newWork = await harness.controller.dispatch({ type: "input.submit", value: "/new-work" })
    expect(newWork).toEqual({ status: "accepted" })
    expect(notices(harness.controller.getSnapshot())).toContain("COMMAND_MODE_UNAVAILABLE")

    const abandon = await harness.controller.dispatch({ type: "input.submit", value: "/abandon" })
    expect(abandon).toEqual({ status: "accepted" })
    expect(notices(harness.controller.getSnapshot())).toContain("COMMAND_MODE_UNAVAILABLE")

    // 手输命令未触发 run.start，也未提交任何 prompt 给模型。
    expect(harness.calls).not.toContain("run.start")
  } finally {
    await harness.controller.close()
  }
})

test("Compose 下手输 /plan 本地提示仅 Build，不进入模型", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "work-mode.cycle" })
    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "/plan 给登录做个方案" })
    expect(outcome).toEqual({ status: "accepted" })
    expect(notices(harness.controller.getSnapshot())).toContain("`/plan` 仅在 Build 工作模式可用。")
    expect(harness.calls).not.toContain("run.start")
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("default")
  } finally {
    await harness.controller.close()
  }
})

test("已在 plan 时空 /plan 只提示，带目标仍提交", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "/plan" })
    const empty = await harness.controller.dispatch({ type: "input.submit", value: "/plan" })
    expect(empty).toEqual({ status: "accepted" })
    expect(notices(harness.controller.getSnapshot())).toContain("已在计划模式")
    expect(harness.calls).not.toContain("run.start")

    await harness.controller.dispatch({ type: "input.submit", value: "/plan 继续改方案" })
    expect(harness.port.lastRunSelection()).toMatchObject({
      message: "继续改方案",
      approvalMode: "plan",
    })
  } finally {
    await harness.controller.close()
  }
})

test("/compact pending 期间发布忙状态并拒绝新输入和重复压缩", async () => {
  const harness = makeHarness()
  let releaseCompact = () => undefined
  const compactGate = new Promise<void>(resolve => { releaseCompact = resolve })
  harness.port.setCompactContextImpl(async () => {
    await compactGate
    return { compacted: true, context: { action: "manual_summary" } }
  })
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "建立 Thread" })
    const run = harness.runHandles.at(-1)!
    harness.port.completeRun(run.threadId, run.runId)
    await flush()

    const pending = harness.controller.dispatch({ type: "input.submit", value: "/compact" })
    await flush()

    const compacting = harness.controller.getSnapshot()
    expect(compacting.activity.kind).toBe("compacting")
    const compactCommand = compacting.commands.find(item => item.kind === "command" && item.command.id === "context.compact")
    expect(compactCommand?.kind === "command" ? compactCommand.availability.state : undefined).toBe("disabled")

    const message = await harness.controller.dispatch({ type: "input.submit", value: "不能排队" })
    const duplicate = await harness.controller.dispatch({ type: "command.execute", commandId: "context.compact" })
    expect(message).toMatchObject({ status: "rejected", code: "busy" })
    expect(duplicate).toMatchObject({ status: "rejected", code: "busy" })
    expect(harness.calls.filter(call => call === "run.start")).toHaveLength(1)
    expect(harness.calls.filter(call => call === "context.compact")).toHaveLength(1)

    releaseCompact()
    await pending
    expect(harness.controller.getSnapshot().activity.kind).toBe("idle")
    expect(notices(harness.controller.getSnapshot())).toContain("上下文已压缩")
  } finally {
    releaseCompact()
    await harness.controller.close()
  }
})

test("/compact 失败后释放 pending 状态", async () => {
  const harness = makeHarness()
  harness.port.setCompactContextImpl(async () => {
    throw new Error("summary model unavailable")
  })
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "建立 Thread" })
    const run = harness.runHandles.at(-1)!
    harness.port.completeRun(run.threadId, run.runId)
    await flush()

    await harness.controller.dispatch({ type: "command.execute", commandId: "context.compact" })

    expect(harness.controller.getSnapshot().activity.kind).toBe("idle")
    expect(notices(harness.controller.getSnapshot())).toContain("上下文压缩失败：summary model unavailable")
  } finally {
    await harness.controller.close()
  }
})
