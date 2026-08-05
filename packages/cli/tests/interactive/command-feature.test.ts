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

