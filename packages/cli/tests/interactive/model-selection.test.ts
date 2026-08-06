/** /model 切换生效性：显式选择不得被服务端持久化的陈旧 thread_selection 覆盖。 */

import { expect, test } from "bun:test"

import { Capability } from "@za38/protocol"

import { flush, makeHarness } from "./harness"

test("/model 选择 fast 后，陈旧的持久化 thread_selection=pro 不得覆盖显式选择", async () => {
  const harness = makeHarness({
    initialThreadId: "thread-1",
    capabilities: [Capability.CONFIG_WRITE],
  })
  try {
    // 服务端持久化的是上次运行的选择（pro）；配置默认是 fast。
    harness.port.setThreadSelection("pro")
    await harness.controller.dispatch({ type: "catalog.refresh", catalog: "models" })
    await flush()
    // 打开/刷新后 UI 恢复为持久化选择（重连语义）。
    expect(harness.controller.getSnapshot().selection.requestedModelProfileId).toBe("pro")

    // 用户显式切换到 fast。
    const outcome = await harness.controller.dispatch({ type: "model.select", profileId: "fast" })
    expect(outcome.status).toBe("accepted")
    await flush()

    // 显式选择必须保持，不能被 selectModel 内部触发的 catalog 刷新回滚。
    expect(harness.controller.getSnapshot().selection.requestedModelProfileId).toBe("fast")

    // 下一次 Run 必须携带用户选择的 primary_profile。
    await harness.controller.dispatch({ type: "input.submit", value: "用 fast 运行" })
    expect(harness.port.lastRunSelection()?.modelSelection).toEqual({ primary_profile: "fast" })
  } finally {
    await harness.controller.close()
  }
})

test("切换 Thread 时采用该 Thread 持久化的 thread_selection（恢复语义不受影响）", async () => {
  const harness = makeHarness({ initialThreadId: "thread-1" })
  try {
    harness.port.setThreadSelection("pro")
    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-9" })
    await flush()
    expect(harness.controller.getSnapshot().selection.requestedModelProfileId).toBe("pro")
  } finally {
    await harness.controller.close()
  }
})
