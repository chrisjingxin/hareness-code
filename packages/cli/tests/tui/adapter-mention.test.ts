/** TUI 提及菜单 (@) 状态机、过滤与补全交互测试。 */

import { expect, test } from "bun:test"
import { createTuiAdapter } from "../../src/tui/application/adapter"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway } from "../../src/interactive/ports"
import type { WorkspaceExplorer, WorkspaceSnapshot, WorkspaceTreeRow } from "../../src/workspace/types"

function createMockWorkspaceExplorer(rows: WorkspaceTreeRow[]): WorkspaceExplorer {
  const snapshot: WorkspaceSnapshot = {
    tree: {
      status: "ready",
      rows,
      selectedPath: rows[0]?.path ?? null,
      limited: false,
    },
    preview: { status: "idle" },
  }

  return {
    getSnapshot: () => snapshot,
    subscribe: () => () => {},
    dispatch: async () => {},
    dispose: () => {},
  }
}

const MOCK_ROWS: WorkspaceTreeRow[] = [
  { path: "src", name: "src", kind: "directory", depth: 0 },
  { path: "src/app.tsx", name: "app.tsx", kind: "file", depth: 1 },
  { path: "src/index.ts", name: "index.ts", kind: "file", depth: 1 },
  { path: "docs", name: "docs", kind: "directory", depth: 0 },
  { path: "docs/my guide.md", name: "my guide.md", kind: "file", depth: 1 },
  { path: "package.json", name: "package.json", kind: "file", depth: 0 },
]

test("键入 @ 时触发 mentionMenu 并展示工作区文件候选", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  expect(adapter.getSnapshot().mentionMenu.visible).toBe(false)
  expect(adapter.getSnapshot().mentionSearch).toEqual({ items: [], totalMatches: 0, truncated: false })

  // 1. 输入 @
  await adapter.dispatch({ type: "draft-input", value: "hello @" })

  const snapshot1 = adapter.getSnapshot()
  expect(snapshot1.mentionMenu.visible).toBe(true)
  expect(snapshot1.mentionMenu.query).toBe("")
  expect(snapshot1.mentionSearch.items.map(o => o.path)).toEqual(["docs", "src", "package.json"])
})

test("输入 @ 搜索词精准过滤候选项", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "check @app" })

  const snapshot = adapter.getSnapshot()
  expect(snapshot.mentionMenu.visible).toBe(true)
  expect(snapshot.mentionMenu.query).toBe("app")
  expect(snapshot.mentionSearch.items.map(o => o.path)).toEqual(["src/app.tsx"])
})

test("快捷键 mention-next / mention-previous 在首尾夹取选中索引", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "@" })
  expect(adapter.getSnapshot().mentionMenu.selectedIndex).toBe(0)

  // 向下移动
  await adapter.dispatch({ type: "shortcut", action: "mention-next" })
  expect(adapter.getSnapshot().mentionMenu.selectedIndex).toBe(1)

  // 向上移动
  await adapter.dispatch({ type: "shortcut", action: "mention-previous" })
  expect(adapter.getSnapshot().mentionMenu.selectedIndex).toBe(0)

  await adapter.dispatch({ type: "shortcut", action: "mention-previous" })
  expect(adapter.getSnapshot().mentionMenu.selectedIndex).toBe(0)
})

test("大文件集保留真实匹配数、截断至 1000 项并支持按页移动", async () => {
  const allEntries: WorkspaceTreeRow[] = Array.from({ length: 1_205 }, (_, index) => ({
    path: `src/file_${String(index).padStart(4, "0")}.ts`,
    name: `file_${String(index).padStart(4, "0")}.ts`,
    kind: "file",
    depth: 1,
    expanded: false,
    loading: false,
    hasChildren: false,
  }))
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: createMockWorkspaceExplorer(allEntries),
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "@file" })
  expect(adapter.getSnapshot().mentionSearch).toMatchObject({ totalMatches: 1_205, truncated: true })
  expect(adapter.getSnapshot().mentionSearch.items).toHaveLength(1_000)

  await adapter.dispatch({ type: "mention-menu-page", direction: "next", pageSize: 8 })
  expect(adapter.getSnapshot().mentionMenu).toMatchObject({ selectedIndex: 8, windowStart: 1 })
})

test("空查询可逐层进入目录、返回上级，目录不回填而文件正常回填", async () => {
  const rows: WorkspaceTreeRow[] = [
    { path: "packages", name: "packages", kind: "directory", depth: 0 },
    { path: "packages/cli", name: "cli", kind: "directory", depth: 1 },
    { path: "packages/cli/src", name: "src", kind: "directory", depth: 2 },
    { path: "packages/cli/src/app.ts", name: "app.ts", kind: "file", depth: 3 },
    { path: "README.md", name: "README.md", kind: "file", depth: 0 },
  ]
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: createMockWorkspaceExplorer(rows),
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "@" })
  expect(adapter.getSnapshot().mentionSearch.items.map(item => item.path)).toEqual(["packages", "README.md"])
  await adapter.dispatch({ type: "shortcut", action: "mention-enter" })
  expect(adapter.getSnapshot().draft).toBe("@")
  expect(adapter.getSnapshot().mentionMenu.browsePath).toBe("packages")
  expect(adapter.getSnapshot().mentionSearch.items.map(item => item.path)).toEqual(["packages/cli"])

  await adapter.dispatch({ type: "shortcut", action: "mention-select" })
  expect(adapter.getSnapshot().mentionMenu.browsePath).toBe("packages/cli")
  await adapter.dispatch({ type: "shortcut", action: "mention-parent" })
  expect(adapter.getSnapshot().mentionMenu.browsePath).toBe("packages")
  expect(adapter.getSnapshot().mentionMenu.selectedIndex).toBe(0)

  await adapter.dispatch({ type: "shortcut", action: "mention-select" })
  await adapter.dispatch({ type: "shortcut", action: "mention-select" })
  await adapter.dispatch({ type: "shortcut", action: "mention-select" })
  expect(adapter.getSnapshot().draft).toBe("@packages/cli/src/app.ts ")
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(false)
})

test("选中普通文件项回填并追加空格，关闭补全菜单", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "read @app" })
  await adapter.dispatch({ type: "shortcut", action: "mention-select" })

  const snapshot = adapter.getSnapshot()
  expect(snapshot.draft).toBe("read @src/app.tsx ")
  expect(snapshot.mentionMenu.visible).toBe(false)
  expect(snapshot.draftCursor).toBe("end")
})

test("光标位于输入中间时仅替换光标处的 @query", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "read @app after", cursorOffset: 9 })
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(true)
  await adapter.dispatch({ type: "shortcut", action: "mention-select" })

  expect(adapter.getSnapshot().draft).toBe("read @src/app.tsx after")
})

test("选中含空格路径自动包裹引号", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "read @guide" })
  await adapter.dispatch({ type: "shortcut", action: "mention-select" })

  const snapshot = adapter.getSnapshot()
  expect(snapshot.draft).toBe('read @"docs/my guide.md" ')
  expect(snapshot.mentionMenu.visible).toBe(false)
})

test("按 Esc (close-mention-menu) 关闭提及菜单并保留输入", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "read @app" })
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(true)

  await adapter.dispatch({ type: "shortcut", action: "close-mention-menu" })
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(false)
  expect(adapter.getSnapshot().draft).toBe("read @app")
})

test("关闭一个 @token 后移动到同一草稿的另一个 token 会重新打开菜单", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })
  const draft = "@app and @index"

  await adapter.dispatch({ type: "draft-input", value: draft, cursorOffset: 4 })
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(true)
  await adapter.dispatch({ type: "shortcut", action: "close-mention-menu" })
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(false)

  await adapter.dispatch({ type: "draft-cursor", cursorOffset: draft.length })
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(true)
  expect(adapter.getSnapshot().mentionMenu.query).toBe("index")
})

test("Slash 命令优先于提及菜单", async () => {
  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const explorer = createMockWorkspaceExplorer(MOCK_ROWS)
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: explorer,
    onRequestExit: () => {},
  })

  await adapter.dispatch({ type: "draft-input", value: "/plan" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.commandMenu.visible).toBe(true)
  expect(snapshot.mentionMenu.visible).toBe(false)
})

test("当工作区文件树刷新新增文件时，活动中的提及菜单即时响应并展示新文件", async () => {
  let subscriber: ((snapshot: WorkspaceSnapshot) => void) | undefined
  const intents: any[] = []
  let currentRows = [...MOCK_ROWS]

  const dynamicExplorer: WorkspaceExplorer = {
    getSnapshot: () => ({
      tree: { status: "ready", rows: currentRows, selectedPath: null, limited: false },
      preview: { status: "idle" },
    }),
    subscribe: fn => {
      subscriber = fn
      return () => { subscriber = undefined }
    },
    dispatch: async intent => {
      intents.push(intent)
    },
    dispose: () => {},
  }

  const controller = createInteractiveController({ gateway: createFallbackNoopGateway() })
  const adapter = createTuiAdapter({
    controller,
    gateway: createFallbackNoopGateway(),
    workspaceExplorer: dynamicExplorer,
    onRequestExit: () => {},
  })

  // 1. 键入 @new_file
  await adapter.dispatch({ type: "draft-input", value: "check @new" })
  expect(adapter.getSnapshot().mentionMenu.visible).toBe(true)
  expect(adapter.getSnapshot().mentionSearch.items).toHaveLength(0)

  // 2. 模拟运行结束或外部文件系统变更，发布了包含新文件的快照
  currentRows = [
    ...MOCK_ROWS,
    { path: "src/new-feature.ts", name: "new-feature.ts", kind: "file", depth: 1 },
  ]
  subscriber?.({
    tree: { status: "ready", rows: currentRows, selectedPath: null, limited: false },
    preview: { status: "idle" },
  })

  // 3. 验证 mentionMenu 自动重算并展示新产生的文件
  const snapshot = adapter.getSnapshot()
  expect(snapshot.mentionMenu.visible).toBe(true)
  expect(snapshot.mentionSearch.items.map(o => o.path)).toContain("src/new-feature.ts")
})
