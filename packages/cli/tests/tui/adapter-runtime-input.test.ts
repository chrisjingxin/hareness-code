/** 执行中取消保留草稿；无浮层 Esc 只提示中断键。 */

import { expect, test } from "bun:test"

import { createTuiAdapter } from "../../src/tui/application/adapter"
import { makeHarness } from "../interactive/harness"

test("执行中 cancel-run 保留草稿", async () => {
  const harness = makeHarness()
  const adapter = createTuiAdapter({
    controller: harness.controller,
    onRequestExit: () => {},
  })
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "开始干活" })
    expect(harness.controller.getSnapshot().activeRun).not.toBeNull()
    await adapter.dispatch({ type: "draft-input", value: "还没发出去" })
    await adapter.dispatch({ type: "shortcut", action: "cancel-run" })
    expect(adapter.getSnapshot().draft).toBe("还没发出去")
  } finally {
    await adapter.close()
    await harness.controller.close()
  }
})

test("执行中 /quit 弹出确认；取消后任务继续", async () => {
  const harness = makeHarness()
  const adapter = createTuiAdapter({
    controller: harness.controller,
    onRequestExit: () => {},
  })
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "开始干活" })
    await adapter.dispatch({ type: "submit", value: "/quit" })
    expect(adapter.getSnapshot().commandDialog).toMatchObject({
      kind: "confirm-quit",
      message: expect.stringContaining("当前任务将被中止"),
    })
    await adapter.dispatch({ type: "dialog-resolve", kind: "command", confirmed: false })
    expect(adapter.getSnapshot().commandDialog).toBeUndefined()
    expect(adapter.getSnapshot().interactive.activeRun).not.toBeNull()
  } finally {
    await adapter.close()
    await harness.controller.close()
  }
})

test("hint-interrupt 弹出 Ctrl+C 提示", async () => {
  const harness = makeHarness()
  const adapter = createTuiAdapter({
    controller: harness.controller,
    onRequestExit: () => {},
  })
  try {
    await adapter.dispatch({ type: "shortcut", action: "hint-interrupt" })
    expect(adapter.getSnapshot().toasts.some(item => item.message === "中断请用 Ctrl+C")).toBe(true)
  } finally {
    await adapter.close()
    await harness.controller.close()
  }
})
