/** TUI 工作区变更工具完成后的文件树、Git 状态与预览刷新测试。 */

import { expect, test } from "bun:test"

import { createTuiAdapter } from "../../../src/tui/application/adapter"
import type { GitChangedFile } from "../../../src/interactive/runtime"
import type { InteractiveController, InteractiveSnapshot } from "../../../src/interactive/types"
import type { WorkspaceExplorer, WorkspaceIntent, WorkspaceSnapshot, WorkspaceTreeRow } from "../../../src/workspace/types"

const RUN = { threadId: "thread-1", runId: "run-1" }

const baseSnapshot = (): InteractiveSnapshot => ({
  currentThreadId: RUN.threadId,
  activity: { kind: "running" },
  activeRun: RUN,
  timeline: [],
  runProgress: { phase: "model", elapsedMs: 10 },
  interaction: null,
  confirmation: null,
  lastRun: null,
  runtime: {
    workspace: "/workspace",
    cliVersion: "0.1.0",
    modelConfigured: false,
    executionMode: "local",
    approvalMode: "default",
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
  childTimelineExecutionId: null,
  isReverted: false,
  revertedTurnId: null,
})

function tool(
  name: string,
  status: "running" | "completed" | "failed",
  id: string,
): InteractiveSnapshot["timeline"][number] {
  return {
    type: "tool",
    tool: {
      id,
      runId: RUN.runId,
      name,
      arguments: JSON.stringify({ file_path: "src/changed.ts", command: "printf changed" }),
      output: status === "failed" ? "failed" : "ok",
      status,
    },
  }
}

function createControlledController(initial = baseSnapshot()): {
  controller: InteractiveController
  push: (next: InteractiveSnapshot | ((current: InteractiveSnapshot) => InteractiveSnapshot)) => void
} {
  let snapshot = initial
  const listeners = new Set<(next: InteractiveSnapshot) => void>()
  const controller: InteractiveController = {
    getSnapshot: () => snapshot,
    subscribe: listener => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    dispatch: async () => ({ status: "accepted" }),
    close: async () => {
      listeners.clear()
    },
  }
  return {
    controller,
    push: next => {
      snapshot = typeof next === "function" ? next(snapshot) : next
      for (const listener of [...listeners]) listener(snapshot)
    },
  }
}

type WorkspaceHarness = {
  explorer: WorkspaceExplorer
  intents: WorkspaceIntent[]
  setNextRows: (rows: readonly WorkspaceTreeRow[]) => void
  setNextPreviewContent: (content: string) => void
  deferRefresh: boolean
  releaseNextRefresh: () => void
}

function createWorkspaceHarness(
  initialRows: readonly WorkspaceTreeRow[],
  initialPreview?: { path: string; content: string },
): WorkspaceHarness {
  const listeners = new Set<(snapshot: WorkspaceSnapshot) => void>()
  const intents: WorkspaceIntent[] = []
  let rows = [...initialRows]
  let previewContent = initialPreview?.content ?? ""
  let state: WorkspaceSnapshot = {
    tree: {
      status: "ready",
      rows,
      allEntries: rows,
      selectedPath: rows[0]?.path ?? null,
      limited: false,
    },
    preview: initialPreview
      ? {
          status: "ready",
          file: {
            path: initialPreview.path,
            name: initialPreview.path.split("/").at(-1) ?? initialPreview.path,
            content: initialPreview.content,
            language: "typescript",
            sizeBytes: initialPreview.content.length,
            lineCount: initialPreview.content.split("\n").length,
            modifiedAtMs: 1,
            truncated: false,
            version: "v1",
          },
        }
      : { status: "idle" },
  }
  let deferredResolvers: Array<() => void> = []
  let deferRefresh = false

  const publish = () => {
    for (const listener of [...listeners]) listener(state)
  }
  const explorer: WorkspaceExplorer = {
    getSnapshot: () => state,
    subscribe: listener => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    dispatch: async intent => {
      intents.push(intent)
      if (intent.type === "workspace.refresh") {
        const refreshedRows = rows
        state = { ...state, tree: { ...state.tree, status: "loading" } }
        publish()
        if (deferRefresh) await new Promise<void>(resolve => deferredResolvers.push(resolve))
        state = {
          ...state,
          tree: {
            ...state.tree,
            status: "ready",
            rows: refreshedRows,
            allEntries: refreshedRows,
          },
        }
        publish()
      } else if (intent.type === "workspace.refresh-preview") {
        const current = state.preview
        if (current?.status === "ready") {
          const file = current.file
          state = {
            ...state,
            preview: {
              status: "ready",
              file: {
                ...file,
                content: previewContent,
                sizeBytes: previewContent.length,
                lineCount: previewContent.split("\n").length,
                modifiedAtMs: file.modifiedAtMs + 1,
                version: file.version === "v1" ? "v2" : "v3",
              },
            },
          }
          publish()
        }
      }
      return { status: "accepted" }
    },
    close: async () => {
      listeners.clear()
      deferredResolvers = []
    },
  }
  return {
    explorer,
    intents,
    setNextRows: nextRows => { rows = [...nextRows] },
    setNextPreviewContent: content => { previewContent = content },
    get deferRefresh() {
      return deferRefresh
    },
    set deferRefresh(value: boolean) {
      deferRefresh = value
    },
    releaseNextRefresh: () => {
      deferredResolvers.shift()?.()
    },
  }
}

async function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("waitFor 超时")
    await Bun.sleep(2)
  }
}

function clearInitialRefresh(harness: WorkspaceHarness, probeCalls: { value: number }): void {
  harness.intents.length = 0
  probeCalls.value = 0
}

const rows = [
  { path: "src", name: "src", kind: "directory", depth: 0, expanded: true, loading: false, hasChildren: true },
  { path: "src/old.ts", name: "old.ts", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
] satisfies readonly WorkspaceTreeRow[]

test("运行中 write_file 成功完成后立即刷新文件树与 Git，新增文件无需等 Run 结束", async () => {
  const harness = createWorkspaceHarness(rows)
  const controlled = createControlledController()
  const nextRows = [...rows, { path: "src/new.ts", name: "new.ts", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false } satisfies WorkspaceTreeRow]
  const gitFiles: readonly GitChangedFile[] = [{ path: "src/new.ts", status: "untracked", addedLines: 1, removedLines: 0 }]
  const probeCalls = { value: 0 }
  const adapter = createTuiAdapter({
    controller: controlled.controller,
    workspaceExplorer: harness.explorer,
    workspaceChangeProbe: async () => {
      probeCalls.value += 1
      return gitFiles
    },
    onRequestExit: () => {},
  })
  try {
    await waitFor(() => probeCalls.value >= 1)
    clearInitialRefresh(harness, probeCalls)
    harness.setNextRows(nextRows)

    controlled.push(current => ({ ...current, timeline: [tool("write_file", "running", "write-1")] }))
    controlled.push(current => ({ ...current, timeline: [tool("write_file", "completed", "write-1")] }))

    await waitFor(() => (
      harness.intents.some(intent => intent.type === "workspace.refresh")
      && adapter.getSnapshot().sidebar.fileTree.rows.some(row => row.path === "src/new.ts")
      && adapter.getSnapshot().sidebar.workspaceChangedFiles?.some(file => file.path === "src/new.ts") === true
    ))
    expect(controlled.controller.getSnapshot().activeRun).toEqual(RUN)
    expect(harness.intents.filter(intent => intent.type === "workspace.refresh")).toHaveLength(1)
  } finally {
    await adapter.close()
    await harness.explorer.close()
    await controlled.controller.close()
  }
})

test("成功 edit_file 会强制刷新当前已打开文件预览", async () => {
  const harness = createWorkspaceHarness(rows, { path: "src/changed.ts", content: "old content" })
  const controlled = createControlledController()
  const probeCalls = { value: 0 }
  const adapter = createTuiAdapter({
    controller: controlled.controller,
    workspaceExplorer: harness.explorer,
    workspaceChangeProbe: async () => {
      probeCalls.value += 1
      return []
    },
    onRequestExit: () => {},
  })
  try {
    await waitFor(() => probeCalls.value >= 1)
    clearInitialRefresh(harness, probeCalls)
    harness.setNextPreviewContent("new content")
    controlled.push(current => ({ ...current, timeline: [tool("edit_file", "completed", "edit-1")] }))

    await waitFor(() => (
      adapter.getSnapshot().sidebar.preview?.status === "ready"
      && adapter.getSnapshot().sidebar.preview.file.content === "new content"
    ))
    expect(harness.intents).toContainEqual({ type: "workspace.refresh-preview", path: "src/changed.ts" })
    expect(harness.intents.filter(intent => intent.type === "workspace.refresh")).toHaveLength(1)
  } finally {
    await adapter.close()
    await harness.explorer.close()
    await controlled.controller.close()
  }
})

test("同一完成工具的重复 snapshot 只刷新一次，失败工具不触发刷新", async () => {
  const harness = createWorkspaceHarness(rows)
  const controlled = createControlledController()
  const probeCalls = { value: 0 }
  const adapter = createTuiAdapter({
    controller: controlled.controller,
    workspaceExplorer: harness.explorer,
    workspaceChangeProbe: async () => {
      probeCalls.value += 1
      return []
    },
    onRequestExit: () => {},
  })
  try {
    await waitFor(() => probeCalls.value >= 1)
    clearInitialRefresh(harness, probeCalls)
    const completed = (current: InteractiveSnapshot): InteractiveSnapshot => ({ ...current, timeline: [tool("delete_file", "completed", "delete-1")] })
    controlled.push(completed)
    await waitFor(() => harness.intents.filter(intent => intent.type === "workspace.refresh").length === 1)
    controlled.push(completed)
    controlled.push(current => ({ ...current, timeline: [tool("delete_file", "failed", "delete-failed")] }))
    await Bun.sleep(20)

    expect(harness.intents.filter(intent => intent.type === "workspace.refresh")).toHaveLength(1)
    expect(probeCalls.value).toBe(1)
    expect(harness.intents.filter(intent => intent.type === "workspace.refresh-preview")).toHaveLength(0)
  } finally {
    await adapter.close()
    await harness.explorer.close()
    await controlled.controller.close()
  }
})

test("未知工具不触发运行中刷新，但任何 Run 结束都保留完整刷新兜底", async () => {
  const harness = createWorkspaceHarness(rows)
  const controlled = createControlledController()
  const probeCalls = { value: 0 }
  const adapter = createTuiAdapter({
    controller: controlled.controller,
    workspaceExplorer: harness.explorer,
    workspaceChangeProbe: async () => {
      probeCalls.value += 1
      return []
    },
    onRequestExit: () => {},
  })
  try {
    await waitFor(() => probeCalls.value >= 1)
    clearInitialRefresh(harness, probeCalls)
    controlled.push(current => ({ ...current, timeline: [tool("mcp_workspace_mutation", "completed", "mcp-1")] }))
    await Bun.sleep(10)
    expect(harness.intents.filter(intent => intent.type === "workspace.refresh")).toHaveLength(0)

    controlled.push(current => ({ ...current, activeRun: null, timeline: [tool("mcp_workspace_mutation", "completed", "mcp-1")] }))
    await waitFor(() => harness.intents.filter(intent => intent.type === "workspace.refresh").length === 1)

    clearInitialRefresh(harness, probeCalls)
    controlled.push(current => ({ ...current, activeRun: RUN, timeline: [tool("execute", "failed", "exec-2")] }))
    await Bun.sleep(20)
    expect(harness.intents.filter(intent => intent.type === "workspace.refresh")).toHaveLength(0)
    controlled.push(current => ({ ...current, activeRun: null, timeline: [tool("execute", "failed", "exec-2")] }))
    await waitFor(() => harness.intents.filter(intent => intent.type === "workspace.refresh").length === 1)
    await Bun.sleep(20)
    expect(harness.intents.filter(intent => intent.type === "workspace.refresh")).toHaveLength(1)
    expect(probeCalls.value).toBe(1)
  } finally {
    await adapter.close()
    await harness.explorer.close()
    await controlled.controller.close()
  }
})

test("连续完成的写操作在刷新进行中合并排队，晚到的旧刷新不会覆盖最后一次结果", async () => {
  const harness = createWorkspaceHarness(rows)
  const controlled = createControlledController()
  const adapter = createTuiAdapter({
    controller: controlled.controller,
    workspaceExplorer: harness.explorer,
    onRequestExit: () => {},
  })
  try {
    harness.deferRefresh = true
    const firstRows = [...rows, { path: "src/first.ts", name: "first.ts", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false } satisfies WorkspaceTreeRow]
    const secondRows = [...firstRows, { path: "src/second.ts", name: "second.ts", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false } satisfies WorkspaceTreeRow]
    harness.setNextRows(firstRows)
    controlled.push(current => ({ ...current, timeline: [tool("write_file", "completed", "write-1")] }))
    await waitFor(() => harness.intents.filter(intent => intent.type === "workspace.refresh").length === 1)

    harness.setNextRows(secondRows)
    controlled.push(current => ({ ...current, timeline: [tool("write_file", "completed", "write-1"), tool("edit_file", "completed", "edit-2")] }))
    await Bun.sleep(10)
    expect(harness.intents.filter(intent => intent.type === "workspace.refresh")).toHaveLength(1)

    harness.releaseNextRefresh()
    await waitFor(() => harness.intents.filter(intent => intent.type === "workspace.refresh").length === 2)
    harness.releaseNextRefresh()
    await waitFor(() => adapter.getSnapshot().sidebar.fileTree.rows.some(row => row.path === "src/second.ts"))
    expect(adapter.getSnapshot().sidebar.fileTree.rows.some(row => row.path === "src/first.ts")).toBe(true)
  } finally {
    harness.releaseNextRefresh()
    harness.releaseNextRefresh()
    await adapter.close()
    await harness.explorer.close()
    await controlled.controller.close()
  }
})
