import { expect, test } from "bun:test"
import { useTerminalDimensions } from "@opentui/react"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import { resolveShortcut } from "../../../src/tui/application/shortcuts"
import {
  Sidebar,
  computeSidebarLayout,
  computeSidebarVisibility,
  SIDEBAR_BREAKPOINT_WIDTH,
} from "../../../src/tui/presentation/sidebar"
import type { ScrollBoxRenderable } from "@opentui/core"
import type { SidebarState } from "../../../src/tui/application/adapter"

const sidebarInteractive = {
  currentThreadId: "thread-1",
  activity: { kind: "idle" },
  activeRun: null,
  timeline: [],
  runProgress: null,
  interaction: null,
  confirmation: null,
  lastRun: null,
  runtime: {
    workspace: "/workspace/harness-code",
    modelName: "test-model",
    cliVersion: "0.1.0",
    approvalMode: "default",
    modelConfigured: true,
  },
  connection: { status: "open" },
  commands: [],
  catalogs: {
    threads: { status: "ready", items: [] },
    models: { status: "ready", items: [] },
    skills: { status: "ready", items: [] },
    mcp: { status: "ready", items: [] },
    agents: { status: "ready", items: [] },
  },
  selection: { requestedModelProfileId: null, actualModel: null, armedSkill: null },
  workMode: "build",
  composeState: null,
  workItem: null,
  threadMode: null,
} as any

const idle = {
  commandMenuVisible: false,
  commandOptionCount: 0,
  activeRun: false,
  hasDraft: false,
}

function ResponsiveOverlayFixture() {
  const terminal = useTerminalDimensions()
  return createElement("box", {
    width: "100%",
    height: "100%",
    flexDirection: "row",
  },
  createElement("box", { flexGrow: 1, height: "100%" }, createElement("text", null, "CHAT")),
  createElement(Sidebar, {
    sidebar: {
      mode: "show",
      drawerOpen: true,
      focus: "sidebar",
      activeTab: "files",
      fileTree: { status: "ready", rows: [], selectedIndex: 0, selectedPath: null, limited: false },
      preview: null,
    },
    interactive: sidebarInteractive,
    terminalWidth: terminal.width,
    terminalHeight: terminal.height,
    onToggle: () => undefined,
  }))
}

test("Ctrl+B 和 F2 解析为 toggle-sidebar 动作", () => {
  expect(resolveShortcut({ name: "b", ctrl: true }, idle)).toBe("toggle-sidebar")
  expect(resolveShortcut({ name: "f2", ctrl: false }, idle)).toBe("toggle-sidebar")
})

test("computeSidebarLayout: 宽屏停靠、中屏覆盖、窄屏全宽", () => {
  const wide = computeSidebarLayout(160, 40)
  expect(wide.isOverlay).toBe(false)
  expect(wide.sidebarWidth).toBe(80)
  expect(wide.filePaneDirection).toBe("columns")

  const stacked = computeSidebarLayout(140, 40)
  expect(stacked.isOverlay).toBe(false)
  expect(stacked.filePaneDirection).toBe("rows")
  expect(stacked.fileTreeHeight).toBe(12)
  expect((40 - 5 - stacked.fileTreeHeight) / (40 - 5)).toBeGreaterThanOrEqual(0.65)

  const medium = computeSidebarLayout(SIDEBAR_BREAKPOINT_WIDTH - 1, 30)
  expect(medium.isOverlay).toBe(true)
  expect(medium.sidebarWidth).toBeLessThan(SIDEBAR_BREAKPOINT_WIDTH - 1)
  expect(medium.filePaneDirection).toBe("rows")

  const narrow = computeSidebarLayout(68, 22)
  expect(narrow.isOverlay).toBe(true)
  expect(narrow.sidebarWidth).toBe(68)
  expect(narrow.compactHeight).toBe(true)
  expect(narrow.filePaneDirection).toBe("rows")
  expect(narrow.fileTreeHeight).toBeGreaterThanOrEqual(7)
})

test("computeSidebarVisibility: 首页与显式模式计算", () => {
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
  expect(defaultResult.isOverlay).toBe(false)

  // 2. drawerOpen=true 或 mode=show 时，宽屏作为流内停靠面板可见
  const drawerOpenState: SidebarState = { ...defaultState, drawerOpen: true }
  const drawerResult = computeSidebarVisibility(drawerOpenState, 140)
  expect(drawerResult.visible).toBe(true)
  expect(drawerResult.isOverlay).toBe(false)
  expect(drawerResult.sidebarWidth).toBe(58)

  // 3. 中屏仍可打开，但覆盖主区以保留最小对话宽度
  const mediumResult = computeSidebarVisibility(drawerOpenState, 100)
  expect(mediumResult.visible).toBe(true)
  expect(mediumResult.isOverlay).toBe(true)
  expect(mediumResult.sidebarWidth).toBe(55)

  // 4. mode=hide 时无论宽窄屏均不显示
  const hideState: SidebarState = { ...defaultState, mode: "hide" }
  expect(computeSidebarVisibility(hideState, 140).visible).toBe(false)
  expect(computeSidebarVisibility(hideState, 100).visible).toBe(false)

  // 5. isHome=true（首页）时无论宽窄屏或抽屉状态均不显示
  expect(computeSidebarVisibility(defaultState, 140, true).visible).toBe(false)
  expect(computeSidebarVisibility(defaultState, 100, true).visible).toBe(false)
  expect(computeSidebarVisibility(drawerOpenState, 100, true).visible).toBe(false)
})

test("Sidebar 94x32 覆盖面板贴右，不会收缩到左侧", async () => {
  const width = 94
  const height = 32
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ResponsiveOverlayFixture), { width: 160, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    await act(async () => {
      setup.resize(width, height)
    })
    await act(async () => { await setup.flush() })
    const titleLine = setup.captureCharFrame().split("\n").find(line => line.includes("项目检查器"))
    expect(titleLine).toBeDefined()
    const expectedPanelLeft = width - computeSidebarLayout(width, height).sidebarWidth
    expect(titleLine?.indexOf("项目检查器")).toBeGreaterThan(expectedPanelLeft)
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Sidebar 文件页使用项目检查器结构并避免 Emoji", async () => {
  const renderAt = async (width: number, height: number) => {
    let setup: Awaited<ReturnType<typeof testRender>>
    await act(async () => {
      setup = await testRender(createElement(Sidebar, {
        sidebar: {
          mode: "show",
          drawerOpen: true,
          focus: "sidebar",
          activeTab: "files",
          fileTree: {
            status: "ready",
            rows: [
              { path: "src", name: "src", kind: "directory", depth: 0, expanded: true, loading: false, hasChildren: true },
              { path: "src/sidebar.tsx", name: "sidebar.tsx", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
            ],
            selectedIndex: 1,
            selectedPath: "src/sidebar.tsx",
            limited: false,
          },
          preview: { status: "loading", path: "src/sidebar.tsx" },
        },
        interactive: sidebarInteractive,
        terminalWidth: width,
        terminalHeight: height,
        onToggle: () => undefined,
      }), { width, height })
    })
    try {
      await act(async () => { await setup.flush() })
      return setup.captureCharFrame()
    } finally {
      await act(async () => { setup.renderer.destroy() })
    }
  }

  for (const [width, height] of [[160, 40], [140, 40], [68, 22]] as const) {
    const frame = await renderAt(width, height)
    expect(frame).toContain("项目检查器")
    expect(frame).toContain("文件")
    expect(frame).toContain("状态")
    expect(frame).toContain("sidebar.tsx")
    expect(frame).toContain("TSX")
    expect(frame).toContain("正在读取文件内容")
    expect(frame).not.toContain("┌────┐")
    expect(frame).not.toMatch(/[📁📂📄⚡✕]/u)

    const fileNameRows = frame
      .split("\n")
      .flatMap((line, row) => line.includes("sidebar.tsx") ? [row] : [])
    expect(fileNameRows.length).toBeGreaterThanOrEqual(2)
    const rowDistance = Math.max(...fileNameRows) - Math.min(...fileNameRows)
    if (width >= 150) {
      expect(rowDistance).toBeLessThanOrEqual(3)
    } else {
      expect(rowDistance).toBeGreaterThanOrEqual(5)
    }
  }
})

test("Sidebar 状态页组合工作目录、上下文、MCP 与唯一的工作区变更", async () => {
  const statusScrollRef = { current: null as ScrollBoxRenderable | null }
  const interactive = {
    ...sidebarInteractive,
    lastRun: {
      runId: "run-1",
      outcome: "completed",
      durationMs: 2_000,
      usage: { inputTokens: 2_048, outputTokens: 512 },
      context: { action: "run", inputCapTokens: 128_000 },
    },
    runtime: {
      ...sidebarInteractive.runtime,
      gitWorkspace: { kind: "branch", branch: "master", isClean: false },
    },
    catalogs: {
      ...sidebarInteractive.catalogs,
      mcp: {
        status: "ready",
        items: [
          { name: "filesystem", status: "connected" },
          { name: "github", status: "failed" },
        ],
      },
    },
    timeline: [{
      type: "tool",
      tool: {
        id: "tool-1",
        runId: "run-1",
        name: "write_file",
        arguments: JSON.stringify({ file_path: "src/sidebar.tsx", content: "line 1\nline 2\n" }),
        output: "ok",
        status: "completed",
      },
    }],
  } as any

  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(Sidebar, {
      sidebar: {
        mode: "show",
        drawerOpen: true,
        focus: "sidebar",
        activeTab: "status",
        workspaceChangedFiles: [
          { path: "src/sidebar.tsx", status: "modified", addedLines: 156, removedLines: 0 },
          { path: "src/new-widget.tsx", status: "added", addedLines: 28, removedLines: 12 },
          { path: "docs/old.md", status: "deleted", addedLines: 0, removedLines: 64 },
          { path: "README.md", status: "untracked", addedLines: 3, removedLines: 0 },
        ],
        fileTree: {
          status: "ready",
          rows: [
            { path: "src", name: "src", kind: "directory", depth: 0, expanded: false, loading: false, hasChildren: true },
            { path: "README.md", name: "README.md", kind: "file", depth: 0, expanded: false, loading: false, hasChildren: false },
          ],
          selectedIndex: 0,
          selectedPath: "src",
          limited: false,
        },
        preview: null,
      },
      interactive,
      terminalWidth: 140,
      terminalHeight: 40,
      statusScrollRef,
      onToggle: () => undefined,
    }), { width: 140, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("工作目录")
    expect(frame).toContain("harness-code")
    expect(frame).toContain("上下文")
    expect(frame).toContain("2.6k / 128.0k")
    expect(frame).toContain("2.0%")
    expect(frame).toContain("▮")
    expect(frame).toContain("─")
    expect(frame).toContain("MCP 服务")
    expect(frame).toContain("1/2 已连接")
    expect(frame).toContain("● filesystem 已连接")
    expect(frame).toContain("● github 失败")
    expect(frame).not.toContain("本会话变更")
    expect(frame).toContain("工作区变更")
    expect(frame).toContain("4 个文件")
    expect(frame).toContain("src/sidebar.tsx")
    expect(frame).toContain("+156")
    expect(frame).toContain("-0")
    expect(frame).toContain("src/new-widget.tsx")
    expect(frame).toContain("+28")
    expect(frame).toContain("-12")
    expect(statusScrollRef.current?.verticalScrollBar.visible).toBe(false)
    expect(frame).not.toContain("▸ 文件")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Sidebar 工作区变更列表拥有独立滚动视口", async () => {
  const statusScrollRef = { current: null as ScrollBoxRenderable | null }
  const files = Array.from({ length: 24 }, (_, index) => ({
    path: `src/file-${String(index).padStart(2, "0")}.ts`,
    status: "modified" as const,
    addedLines: index + 1,
    removedLines: index,
  }))
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(Sidebar, {
      sidebar: {
        mode: "show",
        drawerOpen: true,
        focus: "sidebar",
        activeTab: "status",
        workspaceChangedFiles: files,
        fileTree: { status: "ready", rows: [], selectedIndex: 0, selectedPath: null, limited: false },
        preview: null,
      },
      interactive: {
        ...sidebarInteractive,
        runtime: {
          ...sidebarInteractive.runtime,
          gitWorkspace: { kind: "branch", branch: "master", root: "/workspace/harness-code" },
        },
      },
      terminalWidth: 140,
      terminalHeight: 40,
      statusScrollRef,
      onToggle: () => undefined,
    }), { width: 140, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    expect(statusScrollRef.current).not.toBeNull()
    expect(statusScrollRef.current?.verticalScrollBar.y).toBe(statusScrollRef.current?.viewport.y)
    expect(statusScrollRef.current?.verticalScrollBar.x).toBeGreaterThan(statusScrollRef.current?.viewport.x ?? 0)
    expect(setup.captureCharFrame()).toContain("file-00.ts")
    expect(setup.captureCharFrame()).not.toContain("file-23.ts")

    await act(async () => {
      statusScrollRef.current?.scrollTo(statusScrollRef.current.scrollHeight)
      await setup.flush()
    })
    expect(setup.captureCharFrame()).toContain("file-23.ts")
    expect(setup.captureCharFrame()).not.toContain("file-00.ts")
    expect(setup.captureCharFrame()).toContain("工作区变更")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Sidebar 矮屏固定工作区变更标题，只滚动文件", async () => {
  const statusScrollRef = { current: null as ScrollBoxRenderable | null }
  const files = Array.from({ length: 24 }, (_, index) => ({
    path: `src/compact-${String(index).padStart(2, "0")}.ts`,
    status: "modified" as const,
    addedLines: index + 1,
    removedLines: index,
  }))
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(Sidebar, {
      sidebar: {
        mode: "show",
        drawerOpen: true,
        focus: "sidebar",
        activeTab: "status",
        workspaceChangedFiles: files,
        fileTree: { status: "ready", rows: [], selectedIndex: 0, selectedPath: null, limited: false },
        preview: null,
      },
      interactive: {
        ...sidebarInteractive,
        runtime: {
          ...sidebarInteractive.runtime,
          gitWorkspace: { kind: "branch", branch: "master", root: "/workspace/harness-code" },
        },
      },
      terminalWidth: 68,
      terminalHeight: 22,
      statusScrollRef,
      onToggle: () => undefined,
    }), { width: 68, height: 22 })
  })
  try {
    await act(async () => { await setup.flush() })
    const firstFrame = setup.captureCharFrame()
    expect(firstFrame).toContain("工作区变更")
    expect(firstFrame).toContain("compact-00.ts")

    await act(async () => {
      statusScrollRef.current?.scrollTo(statusScrollRef.current.scrollHeight)
      await setup.flush()
    })
    const lastFrame = setup.captureCharFrame()
    expect(lastFrame).toContain("工作区变更")
    expect(lastFrame).toContain("compact-23.ts")
    expect(lastFrame).not.toContain("compact-00.ts")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Sidebar 非 Git 工作区不显示工作区变更入口", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(Sidebar, {
      sidebar: {
        mode: "show",
        drawerOpen: true,
        focus: "sidebar",
        activeTab: "status",
        workspaceChangedFiles: [{ path: "README.md", status: "modified", addedLines: 1, removedLines: 0 }],
        fileTree: { status: "ready", rows: [], selectedIndex: 0, selectedPath: null, limited: false },
        preview: null,
      },
      interactive: {
        ...sidebarInteractive,
        runtime: {
          ...sidebarInteractive.runtime,
          gitWorkspace: { kind: "not-repository" },
        },
      },
      terminalWidth: 140,
      terminalHeight: 40,
      onToggle: () => undefined,
    }), { width: 140, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).not.toContain("工作区变更")
    expect(frame).not.toContain("README.md")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("createTuiAdapter: sidebar-toggle 支持显式目标与双向切换", async () => {
  const { createTuiAdapter } = await import("../../../src/tui/application/adapter")
  const { createInteractiveController } = await import("../../../src/interactive/controller")
  const { createFallbackNoopGateway } = await import("../../../src/interactive/ports")

  const gateway = createFallbackNoopGateway()
  const controller = createInteractiveController({ gateway })

  let workspaceChangedFiles = [{ path: "first.ts", status: "modified" as const, addedLines: 1, removedLines: 0 }]
  const adapter = createTuiAdapter({
    controller,
    gateway,
    workspaceChangeProbe: async () => workspaceChangedFiles,
    onRequestExit: () => {},
  })

  await new Promise(resolve => setImmediate(resolve))
  expect(adapter.getSnapshot().sidebar.workspaceChangedFiles).toEqual(workspaceChangedFiles)

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
  workspaceChangedFiles = [
    { path: "first.ts", status: "modified" as const, addedLines: 1, removedLines: 0 },
    { path: "second.ts", status: "added" as const, addedLines: 2, removedLines: 0 },
  ]
  await adapter.dispatch({ type: "sidebar-tab-switch", tab: "status" })
  await new Promise(resolve => setImmediate(resolve))
  expect(adapter.getSnapshot().sidebar.activeTab).toBe("status")
  expect(adapter.getSnapshot().sidebar.workspaceChangedFiles).toEqual(workspaceChangedFiles)
  await adapter.dispatch({ type: "sidebar-tab-switch" })
  expect(adapter.getSnapshot().sidebar.activeTab).toBe("files")

  await adapter.close()
  await controller.close()
})
