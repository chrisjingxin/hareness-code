import { expect, test } from "bun:test"
import { makeHarness, flush, notices } from "./harness"

test("compact/status/version/help/web 命令语义确定", async () => {
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

    await harness.controller.dispatch({ type: "command.execute", commandId: "system.status" })
    expect(notices(harness.controller.getSnapshot())).toContain("工作区")
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
