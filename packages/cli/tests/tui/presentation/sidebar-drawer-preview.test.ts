import { expect, test } from "bun:test"
import { createTuiAdapter } from "../../../src/tui/application/adapter"
import type { WorkspaceExplorer, WorkspaceSnapshot, WorkspaceIntent } from "../../../src/workspace/types"

function createMockWorkspaceExplorer(): WorkspaceExplorer {
  const listeners = new Set<(s: WorkspaceSnapshot) => void>()
  let state: WorkspaceSnapshot = {
    tree: {
      status: "ready",
      rows: [
        { path: "src/index.ts", name: "index.ts", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
      ],
      selectedPath: "src/index.ts",
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
      if (intent.type === "workspace.preview-file") {
        state = {
          ...state,
          preview: {
            status: "ready",
            file: {
              path: intent.path,
              name: "index.ts",
              content: "console.log('hello world')\nconst a = 1\n",
              language: "typescript",
              sizeBytes: 38,
              lineCount: 2,
              modifiedAtMs: Date.now(),
              truncated: false,
              version: "v1",
            },
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

test("TuiAdapter 处理文件预览生命周期：打开、插入引用、关闭", async () => {
  const mockExplorer = createMockWorkspaceExplorer()
  const adapter = createTuiAdapter({
    controller: createDummyController(),
    workspaceExplorer: mockExplorer,
  })

  // 初始 preview 为 null
  expect(adapter.getSnapshot().sidebar.preview).toBeNull()

  // 派发 file-tree-preview
  await adapter.dispatch({ type: "file-tree-preview", path: "src/index.ts" })
  const snapshotAfterOpen = adapter.getSnapshot()
  expect(snapshotAfterOpen.sidebar.preview).not.toBeNull()
  expect(snapshotAfterOpen.sidebar.preview?.status).toBe("ready")
  if (snapshotAfterOpen.sidebar.preview?.status === "ready") {
    expect(snapshotAfterOpen.sidebar.preview.file.path).toBe("src/index.ts")
    expect(snapshotAfterOpen.sidebar.preview.file.language).toBe("typescript")
  }

  // 插入引用到输入框：@src/index.ts，并自动切回 chat 焦点与关闭预览
  await adapter.dispatch({ type: "file-preview-insert-ref", path: "src/index.ts" })
  const snapshotAfterInsert = adapter.getSnapshot()
  expect(snapshotAfterInsert.draft).toBe("@src/index.ts")
  expect(snapshotAfterInsert.sidebar.preview).toBeNull()
  expect(snapshotAfterInsert.sidebar.focus).toBe("chat")

  // 重新打开并手动关闭
  await adapter.dispatch({ type: "file-tree-preview", path: "src/index.ts" })
  expect(adapter.getSnapshot().sidebar.preview).not.toBeNull()
  await adapter.dispatch({ type: "file-preview-close" })
  expect(adapter.getSnapshot().sidebar.preview).toBeNull()
})

import { CodePreviewPane } from "../../../src/tui/presentation/sidebar"

test("CodePreviewPane 组件不同状态渲染契约", () => {
  // 1. loading 状态渲染
  const loadingElement = CodePreviewPane({
    preview: { status: "loading", path: "src/main.rs" },
    width: 60,
    height: 40,
    onClose: () => {},
    onInsertRef: () => {},
  })
  expect(loadingElement).not.toBeNull()

  // 2. error 状态渲染
  const errorElement = CodePreviewPane({
    preview: { status: "error", path: "src/secret.key", code: "permission-denied", message: "权限不足" },
    width: 60,
    height: 40,
    onClose: () => {},
    onInsertRef: () => {},
  })
  expect(errorElement).not.toBeNull()

  // 3. unsupported 大文件/二进制渲染
  const unsupportedElement = CodePreviewPane({
    preview: { status: "unsupported", path: "data.bin", reason: "二进制文件", sizeBytes: 2048576 },
    width: 60,
    height: 40,
    onClose: () => {},
    onInsertRef: () => {},
  })
  expect(unsupportedElement).not.toBeNull()

  // 4. ready 状态渲染
  const readyElement = CodePreviewPane({
    preview: {
      status: "ready",
      file: {
        path: "src/app.tsx",
        name: "app.tsx",
        content: "export const A = 1\nexport const B = 2",
        language: "typescript",
        sizeBytes: 38,
        lineCount: 2,
        modifiedAtMs: Date.now(),
        truncated: false,
        version: "v1",
      },
    },
    width: 60,
    height: 40,
    onClose: () => {},
    onInsertRef: () => {},
  })
  expect(readyElement).not.toBeNull()
})

import { Sidebar } from "../../../src/tui/presentation/sidebar"
import { mock } from "bun:test"

test("Sidebar 面板区域在 mouse-up 时转发 onSelectionMouseUp 并阻止冒泡到根层", () => {
  const onToggle = mock(() => {})
  const onSelectionMouseUp = mock((_event: { button: number }) => {})
  const stopPropagation = mock(() => {})

  const sidebarElement = Sidebar({
    sidebar: {
      mode: "show",
      drawerOpen: true,
      focus: "sidebar",
      activeTab: "files",
      fileTree: { status: "idle", rows: [], selectedIndex: 0, selectedPath: null, limited: false },
      preview: null,
    },
    terminalWidth: 120,
    terminalHeight: 40,
    onToggle,
    onSelectionMouseUp,
  })

  expect(sidebarElement).not.toBeNull()
  // 120 列使用新版停靠布局，返回值本身就是右侧面板。
  const panelBox = sidebarElement as any
  expect(typeof panelBox.props.onMouseUp).toBe("function")

  const fakeEvent = { button: 0, stopPropagation }
  panelBox.props.onMouseUp(fakeEvent)

  expect(onSelectionMouseUp).toHaveBeenCalledTimes(1)
  expect(onSelectionMouseUp).toHaveBeenCalledWith(fakeEvent)
  expect(stopPropagation).toHaveBeenCalledTimes(1)
  expect(onToggle).not.toHaveBeenCalled()
})
