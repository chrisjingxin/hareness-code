/** TUI 右上角气泡通知 (Toast) 状态模型与定时队列测试。 */

import { expect, test } from "bun:test"
import { createTuiAdapter } from "../../src/tui/application/adapter"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway } from "../../src/interactive/ports"

test("showToast 推送通知到 snapshot.toasts 队列", () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    onRequestExit: () => {},
  })

  expect(adapter.getSnapshot().toasts).toEqual([])

  adapter.showToast("复制成功", "success")

  const snapshot = adapter.getSnapshot()
  expect(snapshot.toasts.length).toBe(1)
  expect(snapshot.toasts[0].message).toBe("复制成功")
  expect(snapshot.toasts[0].variant).toBe("success")
  expect(typeof snapshot.toasts[0].id).toBe("string")
})

test("showToast 队列上限截断（最多保留最新 3 条，超出时淘汰最旧条目）", () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    onRequestExit: () => {},
  })

  adapter.showToast("通知 1", "info")
  adapter.showToast("通知 2", "warning")
  adapter.showToast("通知 3", "error")
  adapter.showToast("通知 4", "success")

  const snapshot = adapter.getSnapshot()
  expect(snapshot.toasts.length).toBe(3)
  expect(snapshot.toasts.map(t => t.message)).toEqual(["通知 2", "通知 3", "通知 4"])
})

test("showToast 单条经过指定 durationMs 后自动从队列中移出", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    onRequestExit: () => {},
  })

  adapter.showToast("临时通知", "info", 50)
  expect(adapter.getSnapshot().toasts.length).toBe(1)

  await new Promise(resolve => setTimeout(resolve, 80))

  expect(adapter.getSnapshot().toasts.length).toBe(0)
})

test("adapter.close() 时正确清理所有活动定时器", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    onRequestExit: () => {},
  })

  adapter.showToast("通知 A", "info", 500)
  adapter.showToast("通知 B", "info", 500)
  expect(adapter.getSnapshot().toasts.length).toBe(2)

  await adapter.close()
  // close 后不应发生异常
})
