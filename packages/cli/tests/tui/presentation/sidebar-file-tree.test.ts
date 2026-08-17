/** TUI 侧边栏文件树与焦点导航单元测试。 */

import { expect, test } from "bun:test"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { createTuiAdapter } from "../../../src/tui/application/adapter"
import { createWorkspaceExplorer } from "../../../src/workspace/explorer"
import type { WorkspaceExplorer, WorkspaceSnapshot, WorkspaceIntent } from "../../../src/workspace/types"

/** 等待条件成立的轻量轮询；超时即失败。 */
async function waitFor(predicate: () => boolean, timeoutMs = 2_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

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

test("回归：TuiAdapter 构造时的首次加载必须把树推进到 ready，不能卡在 loading", async () => {
  // 真实 Explorer 的 refreshTree 在首个 await 之前同步发布 loading；
  // 若 Adapter 在自身 snapshot 初始化前派发 workspace.load，同步 publish 链会
  // 读取未初始化的 this.snapshot 抛 TypeError，树永远停在 loading（/web 文件树空白）。
  const dir = await mkdtemp(join(tmpdir(), "tui-explorer-init-"))
  await writeFile(join(dir, "a.txt"), "a")
  const explorer = await createWorkspaceExplorer(dir)
  try {
    const adapter = createTuiAdapter({
      controller: createDummyController(),
      workspaceExplorer: explorer,
    })
    await waitFor(() => explorer.getSnapshot().tree.status === "ready")
    expect(explorer.getSnapshot().tree.rows.map(row => row.path)).toEqual(["a.txt"])
    expect(adapter.getSnapshot().sidebar.fileTree.status).toBe("ready")
    await adapter.close()
  } finally {
    await explorer.close()
    await rm(dir, { recursive: true, force: true })
  }
})
