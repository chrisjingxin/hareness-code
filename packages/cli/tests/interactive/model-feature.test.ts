import { expect, test } from "bun:test"
import { makeHarness, flush, notices } from "./harness"

test("模型选择先更新当前 Thread，再独立同步默认值", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "catalog.refresh", catalog: "models" })
    await flush()
    await harness.controller.dispatch({ type: "model.select", profileId: "pro" })
    await flush()
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.selection.requestedModelProfileId).toBe("pro")
    expect(notices(snapshot)).toContain("后续新 Thread 默认模型已同步")
    expect(harness.calls).toEqual(["skills.list(true)", "models.list", "config.details", "config.preview", "config.commit", "models.list"])

    await harness.controller.dispatch({ type: "input.submit", value: "使用选择运行" })
    await flush()
    expect(harness.port.lastRunSelection()?.modelSelection).toEqual({ primary_profile: "pro" })
  } finally {
    await harness.controller.close()
  }
})

test("模型默认同步失败保留当前选择并输出稳定原因", async () => {
  const harness = makeHarness()
  harness.port.configDetails = async () => {
    harness.calls.push("config.details")
    throw new Error("managed policy locked")
  }
  try {
    await harness.controller.dispatch({ type: "catalog.refresh", catalog: "models" })
    await flush()
    await harness.controller.dispatch({ type: "model.select", profileId: "pro" })
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.selection.requestedModelProfileId).toBe("pro")
    expect(notices(snapshot)).toContain("未来新 Thread 默认未更新")
  } finally {
    await harness.controller.close()
  }
})

