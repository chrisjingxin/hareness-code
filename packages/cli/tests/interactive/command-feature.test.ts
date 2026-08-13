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

