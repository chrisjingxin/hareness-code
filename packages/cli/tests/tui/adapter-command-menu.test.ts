/** TUI Slash 命令菜单窗口跟随上下键导航。 */

import { expect, test } from "bun:test"

import { commandMenuItemLabel } from "../../src/interactive/commands"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway } from "../../src/interactive/ports"
import { createTuiAdapter } from "../../src/tui/application/adapter"

function createAdapter() {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  return createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    onRequestExit: () => {},
  })
}

test("输入 / 打开命令菜单，向下越过可见行后窗口跟随选中项", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "/" })
  const opened = adapter.getSnapshot()
  expect(opened.commandMenu.visible).toBe(true)
  expect(opened.commandMenu.selectedIndex).toBe(0)
  expect(opened.commandMenu.windowStart).toBe(0)
  expect(opened.commandOptions.length).toBeGreaterThan(8)

  for (let step = 0; step < 8; step += 1) {
    await adapter.dispatch({ type: "shortcut", action: "command-next" })
  }

  const scrolled = adapter.getSnapshot()
  expect(scrolled.commandMenu.selectedIndex).toBe(8)
  expect(scrolled.commandMenu.windowStart).toBe(1)
})

test("命令菜单在首尾循环，窗口同步跳到对应一端", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "/" })
  const count = adapter.getSnapshot().commandOptions.length
  expect(count).toBeGreaterThan(8)

  await adapter.dispatch({ type: "shortcut", action: "command-previous" })
  const wrappedUp = adapter.getSnapshot()
  expect(wrappedUp.commandMenu.selectedIndex).toBe(count - 1)
  expect(wrappedUp.commandMenu.windowStart).toBe(count - 8)

  await adapter.dispatch({ type: "shortcut", action: "command-next" })
  const wrappedDown = adapter.getSnapshot()
  expect(wrappedDown.commandMenu.selectedIndex).toBe(0)
  expect(wrappedDown.commandMenu.windowStart).toBe(0)
})

test("Tab 把当前选中命令补全进输入框，不执行该命令", async () => {
  const adapter = createAdapter()
  await adapter.dispatch({ type: "draft-input", value: "/he" })
  expect(commandMenuItemLabel(adapter.getSnapshot().commandOptions[0]!)).toBe("/help")

  await adapter.dispatch({ type: "shortcut", action: "command-complete" })
  const completed = adapter.getSnapshot()
  expect(completed.draft).toBe("/help")
  expect(completed.draftCursor).toBe("end")
  expect(completed.commandMenu.visible).toBe(false)
})

test("前缀本身也是命令时，Tab 仍补全高亮项而不是执行前缀", async () => {
  const adapter = createAdapter()
  await adapter.dispatch({ type: "draft-input", value: "/plan" })
  const options = adapter.getSnapshot().commandOptions.map(commandMenuItemLabel)
  const planViewIndex = options.indexOf("/plan-view")
  expect(planViewIndex).toBeGreaterThan(0)

  for (let step = 0; step < planViewIndex; step += 1) {
    await adapter.dispatch({ type: "shortcut", action: "command-next" })
  }
  expect(commandMenuItemLabel(adapter.getSnapshot().commandOptions[adapter.getSnapshot().commandMenu.selectedIndex]!)).toBe("/plan-view")

  await adapter.dispatch({ type: "shortcut", action: "command-complete" })
  const completed = adapter.getSnapshot()
  expect(completed.draft).toBe("/plan-view")
  expect(completed.commandMenu.visible).toBe(false)
})
