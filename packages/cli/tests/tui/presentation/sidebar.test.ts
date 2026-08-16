import { expect, test } from "bun:test"

import { resolveShortcut } from "../../../src/tui/application/shortcuts"
import { computeSidebarVisibility, DEFAULT_SIDEBAR_WIDTH, SIDEBAR_BREAKPOINT_WIDTH } from "../../../src/tui/presentation/sidebar"
import type { SidebarState } from "../../../src/tui/application/adapter"

const idle = {
  commandMenuVisible: false,
  commandOptionCount: 0,
  activeRun: false,
  hasDraft: false,
}

test("Ctrl+B 和 F2 解析为 toggle-sidebar 动作", () => {
  expect(resolveShortcut({ name: "b", ctrl: true }, idle)).toBe("toggle-sidebar")
  expect(resolveShortcut({ name: "f2", ctrl: false }, idle)).toBe("toggle-sidebar")
})

test("computeSidebarVisibility: 宽屏断点与模式计算", () => {
  const defaultState: SidebarState = {
    mode: "auto",
    drawerOpen: false,
    focus: "chat",
    activeTab: "files",
    fileTree: {
      status: "idle",
      rows: [],
      selectedIndex: 0,
      selectedPath: null,
      limited: false,
    },
    preview: null,
  }

  // 1. 默认 mode=auto 且 drawerOpen=false 时，侧边栏抽屉不显示
  const defaultResult = computeSidebarVisibility(defaultState, 140)
  expect(defaultResult.visible).toBe(false)
  expect(defaultResult.isOverlay).toBe(true)

  // 2. drawerOpen=true 或 mode=show 时，作为右侧抽屉 Overlay 可见
  const drawerOpenState: SidebarState = { ...defaultState, drawerOpen: true }
  const drawerResult = computeSidebarVisibility(drawerOpenState, 140)
  expect(drawerResult.visible).toBe(true)
  expect(drawerResult.isOverlay).toBe(true)
  expect(drawerResult.sidebarWidth).toBe(DEFAULT_SIDEBAR_WIDTH)

  // 3. mode=hide 时无论宽窄屏均不显示
  const hideState: SidebarState = { ...defaultState, mode: "hide" }
  expect(computeSidebarVisibility(hideState, 140).visible).toBe(false)
  expect(computeSidebarVisibility(hideState, 100).visible).toBe(false)

  // 4. isHome=true（首页）时无论宽窄屏或抽屉状态均不显示
  expect(computeSidebarVisibility(defaultState, 140, true).visible).toBe(false)
  expect(computeSidebarVisibility(defaultState, 100, true).visible).toBe(false)
  expect(computeSidebarVisibility(drawerOpenState, 100, true).visible).toBe(false)
})

test("createTuiAdapter: sidebar-toggle 支持显式目标与双向切换", async () => {
  const { createTuiAdapter } = await import("../../../src/tui/application/adapter")
  const { createInteractiveController } = await import("../../../src/interactive/controller")
  const { createFallbackNoopGateway } = await import("../../../src/interactive/ports")

  const gateway = createFallbackNoopGateway()
  const controller = createInteractiveController({ gateway })

  const adapter = createTuiAdapter({
    controller,
    gateway,
    onRequestExit: () => {},
  })

  expect(adapter.getSnapshot().sidebar.mode).toBe("auto")

  // 1. 显式收起
  await adapter.dispatch({ type: "sidebar-toggle", target: "hide" })
  expect(adapter.getSnapshot().sidebar.mode).toBe("hide")
  expect(adapter.getSnapshot().sidebar.drawerOpen).toBe(false)

  // 2. 显式展开
  await adapter.dispatch({ type: "sidebar-toggle", target: "show" })
  expect(adapter.getSnapshot().sidebar.mode).toBe("show")
  expect(adapter.getSnapshot().sidebar.drawerOpen).toBe(true)

  // 4. sidebar-tab-switch 切换 Tab
  expect(adapter.getSnapshot().sidebar.activeTab).toBe("files")
  await adapter.dispatch({ type: "sidebar-tab-switch", tab: "status" })
  expect(adapter.getSnapshot().sidebar.activeTab).toBe("status")
  await adapter.dispatch({ type: "sidebar-tab-switch" })
  expect(adapter.getSnapshot().sidebar.activeTab).toBe("files")

  await adapter.close()
  await controller.close()
})
