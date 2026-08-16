/** TUI 侧边栏文件树与焦点导航单元测试。 */

import { expect, test } from "bun:test"
import { createTuiAdapter } from "../../../src/tui/application/adapter"
import type { WorkspaceExplorer, WorkspaceSnapshot, WorkspaceIntent } from "../../../src/workspace/types"

function createMockWorkspaceExplorer(initialRows: any[] = []): WorkspaceExplorer {
  const listeners = new Set<(s: WorkspaceSnapshot) => void>()
  let state: WorkspaceSnapshot = {
    tree: {
      status: "ready",
      rows: initialRows,
      selectedPath: initialRows[0]?.path ?? null,
      limited: false,
    },
    preview: { status: "idle" },
  }

  return {
    getSnapshot: () => state,
    subscribe: listener => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    dispatch: async (intent: WorkspaceIntent) => {
      if (intent.type === "workspace.toggle-directory") {
        state = {
          ...state,
          tree: {
            ...state.tree,
            rows: state.tree.rows.map(r => r.path === intent.path ? { ...r, expanded: !r.expanded } : r),
          },
        }
        for (const l of listeners) l(state)
      }
      return { status: "accepted" }
    },
    close: async () => {},
  }
}

function createDummyController(): any {
  return {
    getSnapshot: () => ({
      currentThreadId: "thread-1",
      activity: { kind: "idle" },
      activeRun: null,
      timeline: [],
      runProgress: null,
      interaction: null,
      confirmation: null,
      lastRun: null,
      runtime: { workspace: "/test/workspace", modelName: "test-model", cliVersion: "0.1.0", approvalMode: "default", modelConfigured: true },
      connection: { status: "open" },
      commands: [],
      catalogs: {
        threads: { status: "ready", items: [] },
        models: { status: "ready", items: [] },
        skills: { status: "ready", items: [] },
        mcp: { status: "ready", items: [] },
      },
      selection: { requestedModelProfileId: null, actualModel: null, armedSkill: null },
      workMode: "build",
      composeState: null,
      workItem: null,
      threadMode: null,
    }),
    subscribe: () => () => {},
    dispatch: async () => ({ status: "accepted" }),
  }
}

test("TuiAdapter 与 WorkspaceExplorer 联动：初始加载与行选择", async () => {
  const mockExplorer = createMockWorkspaceExplorer([
    { path: "src", name: "src", kind: "directory", depth: 0, expanded: false, loading: false, hasChildren: true },
    { path: "package.json", name: "package.json", kind: "file", depth: 0, expanded: false, loading: false, hasChildren: false },
  ])

  const adapter = createTuiAdapter({
    controller: createDummyController(),
    workspaceExplorer: mockExplorer,
  })

  const snapshot = adapter.getSnapshot()
  expect(snapshot.sidebar.fileTree.rows.length).toBe(2)
  expect(snapshot.sidebar.fileTree.selectedIndex).toBe(0)

  // 焦点切换
  await adapter.dispatch({ type: "sidebar-focus-switch" })
  expect(adapter.getSnapshot().sidebar.focus).toBe("sidebar")

  // 向下移动光标
  await adapter.dispatch({ type: "file-tree-navigate", direction: "down" })
  expect(adapter.getSnapshot().sidebar.fileTree.selectedIndex).toBe(1)

  // 展开目录
  await adapter.dispatch({ type: "file-tree-toggle-expand", path: "src" })
  expect(adapter.getSnapshot().sidebar.fileTree.rows[0].expanded).toBe(true)

  // 向上移动回 src 目录
  await adapter.dispatch({ type: "file-tree-navigate", direction: "up" })
  expect(adapter.getSnapshot().sidebar.fileTree.selectedIndex).toBe(0)

  // 方向键 parent 导航折叠已展开的目录
  await adapter.dispatch({ type: "file-tree-navigate", direction: "parent" })
  expect(adapter.getSnapshot().sidebar.fileTree.rows[0].expanded).toBe(false)
})

test("TuiAdapter 在空工作区或无 Explorer 时的安全降级", async () => {
  const adapter = createTuiAdapter({
    controller: createDummyController(),
  })

  const snapshot = adapter.getSnapshot()
  expect(snapshot.sidebar.fileTree.rows.length).toBe(0)
  expect(snapshot.sidebar.fileTree.status).toBe("idle")

  // 空状态下导航不崩溃
  await adapter.dispatch({ type: "file-tree-navigate", direction: "down" })
  expect(adapter.getSnapshot().sidebar.fileTree.selectedIndex).toBe(0)
})
