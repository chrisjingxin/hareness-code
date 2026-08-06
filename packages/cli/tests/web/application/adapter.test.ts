/** Web Interactive Adapter：通过 fake WebUiClient 验证视图缓存、语义意图、Context Dock 与工作区联动。 */

import { expect, test } from "bun:test"

import type { InteractiveIntent, InteractiveSnapshot, IntentOutcome } from "../../../src/interactive/types"
import type { WorkspaceIntent, WorkspaceOutcome, WorkspacePreviewState, WorkspaceSnapshot } from "../../../src/workspace/types"
import { buildWebUiState, type PresentationState, type WebUiState } from "../../../src/presentation-coordinator"
import {
  createDefaultFrameScheduler,
  createWebInteractiveAdapter,
  toolKey,
  type ContextDockPanel,
  type WebAdapterSnapshot,
  type WebFrameScheduler,
  type WebIntent,
  type WebInteractiveAdapter,
} from "../../../src/web/application/adapter"
import type { WebUiClient } from "../../../src/web/ui-client"
import { makeApproval, makeConfirmation, makeInteractive, makeMcp, makeModel, makeQuestion, makeSkill, makeThread } from "../presentation/fixtures"
import type { CommandMenuItem } from "../../../src/interactive/commands"

/** 空 workspace 快照：所有表现层测试的默认起点。 */
const emptyWorkspace = (): WorkspaceSnapshot => ({
  tree: { status: "idle", rows: [], selectedPath: null, limited: false },
  preview: { status: "idle" },
})

/** 测试用 frameScheduler：手动驱动；schedule 的任务不会自动执行。 */
function createManualScheduler(): WebFrameScheduler & { runScheduled(): void; pending: () => boolean } {
  let task: (() => void) | null = null
  return {
    schedule(next: () => void): void {
      task = next
    },
    cancel(): void {
      task = null
    },
    flush(): void {
      this.runScheduled()
    },
    runScheduled(): void {
      const next = task
      task = null
      next?.()
    },
    pending: () => task !== null,
  }
}

/** 测试用手动定时器：run 结束自动刷新的 200ms 延迟由测试驱动。 */
function createManualTimer() {
  let callback: (() => void) | null = null
  let handle = 0
  return {
    setTimeoutFn: (cb: () => void) => {
      callback = cb
      handle += 1
      return handle
    },
    clearTimeoutFn: () => {
      callback = null
    },
    run(): void {
      const cb = callback
      callback = null
      cb?.()
    },
    pending: () => callback !== null,
  }
}

/** fake WebUiClient：记录 interactive/workspace intent 与生命周期调用，可注入 outcome 或推送视图。 */
function createFakeClient(initial = makeInteractive()): WebUiClient & {
  intents: InteractiveIntent[]
  workspaceIntents: WorkspaceIntent[]
  nextOutcome: IntentOutcome | null
  nextWorkspaceOutcome: WorkspaceOutcome | null
  readyCalls: number
  returnCalls: number
  exitCalls: number
  closed: boolean
  pushState(updater: (state: WebUiState) => WebUiState): void
  pushInteractive(updater: (snapshot: InteractiveSnapshot) => InteractiveSnapshot): void
  pushWorkspace(updater: (workspace: WorkspaceSnapshot) => WorkspaceSnapshot): void
  pushHandoffState(state: PresentationState): void
} {
  const stateListeners = new Set<(state: WebUiState) => void>()
  const handoffListeners = new Set<(state: PresentationState) => void>()
  const client = {
    state: buildWebUiState(initial, emptyWorkspace()),
    workspace: emptyWorkspace(),
    handoffState: { phase: "opening-web", handoffId: "h1" } as PresentationState,
    intents: [] as InteractiveIntent[],
    workspaceIntents: [] as WorkspaceIntent[],
    nextOutcome: null as IntentOutcome | null,
    nextWorkspaceOutcome: null as WorkspaceOutcome | null,
    readyCalls: 0,
    returnCalls: 0,
    exitCalls: 0,
    closed: false,
    getState() {
      return this.state
    },
    getHandoffState() {
      return this.handoffState
    },
    subscribeState(listener: (state: WebUiState) => void) {
      stateListeners.add(listener)
      return () => stateListeners.delete(listener)
    },
    subscribeHandoff(listener: (state: PresentationState) => void) {
      handoffListeners.add(listener)
      return () => handoffListeners.delete(listener)
    },
    submitIntent(intent: InteractiveIntent): Promise<IntentOutcome> {
      this.intents.push(intent)
      const outcome = this.nextOutcome ?? { status: "accepted" as const }
      return Promise.resolve(outcome)
    },
    workspaceIntent(intent: WorkspaceIntent): Promise<WorkspaceOutcome> {
      this.workspaceIntents.push(intent)
      const outcome = this.nextWorkspaceOutcome ?? { status: "accepted" as const }
      return Promise.resolve(outcome)
    },
    ready() {
      this.readyCalls += 1
    },
    returnToTui() {
      this.returnCalls += 1
    },
    requestExit() {
      this.exitCalls += 1
    },
    close() {
      this.closed = true
    },
    pushState(updater: (state: WebUiState) => WebUiState) {
      this.state = updater(this.state)
      for (const listener of [...stateListeners]) listener(this.state)
    },
    pushInteractive(updater: (snapshot: InteractiveSnapshot) => InteractiveSnapshot) {
      this.pushState(() => buildWebUiState(updater(client.getSnapshotFromState()), client.workspace))
    },
    pushWorkspace(updater: (workspace: WorkspaceSnapshot) => WorkspaceSnapshot) {
      this.workspace = updater(this.workspace)
      this.state = { ...this.state, workspaceTree: this.workspace.tree, workspacePreview: this.workspace.preview }
      for (const listener of [...stateListeners]) listener(this.state)
    },
    pushHandoffState(state: PresentationState) {
      this.handoffState = state
      for (const listener of [...handoffListeners]) listener(state)
    },
    getSnapshotFromState(): InteractiveSnapshot {
      const view = this.state
      return {
        currentThreadId: view.conversation.currentThreadId,
        activity: view.conversation.activity,
        activeRun: view.conversation.activeRun,
        timeline: view.conversation.timeline,
        lastRun: view.conversation.lastRun,
        interaction: view.interaction.interaction,
        confirmation: view.interaction.confirmation,
        catalogs: view.navigation.catalogs,
        commands: view.command.commands,
        runtime: view.runtime.runtime,
        connection: view.runtime.connection,
        selection: view.runtime.selection,
      }
    },
  }
  return client
}

function makeAdapter(
  client = createFakeClient(),
  scheduler = createManualScheduler(),
  timer = createManualTimer(),
): {
  adapter: WebInteractiveAdapter
  client: ReturnType<typeof createFakeClient>
  scheduler: typeof scheduler
  timer: typeof timer
} {
  const adapter = createWebInteractiveAdapter({
    client,
    frameScheduler: scheduler,
    setTimeoutFn: timer.setTimeoutFn,
    clearTimeoutFn: timer.clearTimeoutFn,
  })
  return { adapter, client, scheduler, timer }
}

function commandItem(commandId: string, name: string, kind: "command" | "skill" = "command"): CommandMenuItem {
  return {
    kind,
    ...(kind === "skill"
      ? { skill: makeSkill(commandId, true) }
      : { command: { id: commandId, name, aliases: [], category: "general", usage: "", description: "" } }),
    availability: { state: "available" },
  }
}

/** 构造一个 ready 预览视图。 */
function readyPreview(path: string, content = "const x = 1\n"): Extract<WorkspacePreviewState, { status: "ready" }> {
  return {
    status: "ready",
    file: { path, name: path.split("/").at(-1)!, content, language: "typescript", sizeBytes: content.length, lineCount: 1, modifiedAtMs: 1, truncated: false, version: "1:12" },
  }
}

test("初始 snapshot：interactive 由五个分片重组，workspace 分片来自视图", () => {
  const { adapter } = makeAdapter(createFakeClient(makeInteractive({ currentThreadId: "t-1" })))
  const snapshot = adapter.getSnapshot()
  expect(snapshot.interactive.currentThreadId).toBe("t-1")
  expect(snapshot.workspaceTree.status).toBe("idle")
  expect(snapshot.workspaceSidebar).toEqual({ threadRatio: 0.38, selectedPath: null })
  expect(snapshot.contextDock).toMatchObject({ open: false, activePanel: "code", widthPx: 560 })
  expect(snapshot.contextDock.code).toEqual({ tabs: [], activePath: null, previews: {}, previewErrors: {} })
  expect(snapshot.expandedTools).toBeInstanceOf(Set)
})

// ---- 输入与命令菜单 ----------------------------------------------------------

test("plain input submit 产生 input.submit 携带原始 draft；accepted 后才清空 draft", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "draft-change", value: "你好" })
  await adapter.dispatch({ type: "submit" })
  expect(client.intents).toEqual([{ type: "input.submit", value: "你好" }])
  expect(adapter.getSnapshot().draft).toBe("")
  expect(adapter.getSnapshot().scrollRequest).toBe("to-bottom")
})

test("rejected submit 保留 draft 并展示错误，不发送滚动意图", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "busy", message: "运行中" }
  await adapter.dispatch({ type: "draft-change", value: "被拒" })
  await adapter.dispatch({ type: "submit" })
  expect(adapter.getSnapshot().draft).toBe("被拒")
  expect(adapter.getSnapshot().composerError).toBe("运行中")
  expect(adapter.getSnapshot().scrollRequest).toBeNull()
})

test("slash input、`//`、未知命令都只转交 client，不在 adapter 解释", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "draft-change", value: "/help" })
  await adapter.dispatch({ type: "submit" })
  expect(client.intents).toEqual([{ type: "input.submit", value: "/help" }])
  await adapter.dispatch({ type: "draft-change", value: "//斜杠" })
  await adapter.dispatch({ type: "submit" })
  expect(client.intents.at(-1)).toEqual({ type: "input.submit", value: "//斜杠" })
})

test("命令菜单 items 由 filterCommandMenuItems 计算；`//` 与缺少 `/` 都不开菜单", () => {
  const interactive = makeInteractive()
  interactive.commands = [commandItem("help", "help")]
  const { adapter } = makeAdapter(createFakeClient(interactive))
  adapter.dispatch({ type: "draft-change", value: "/h" })
  expect(adapter.getSnapshot().commandMenuOpen).toBe(true)
  expect(adapter.getSnapshot().commandOptions.length).toBeGreaterThan(0)
  adapter.dispatch({ type: "draft-change", value: "//h" })
  expect(adapter.getSnapshot().commandMenuOpen).toBe(false)
  adapter.dispatch({ type: "draft-change", value: "普通文本" })
  expect(adapter.getSnapshot().commandMenuOpen).toBe(false)
})

test("Web 命令菜单隐藏 host.web（TUI 入口，共享 Core 不能嵌套接管）", () => {
  const interactive = makeInteractive()
  interactive.commands = [commandItem("host.web", "web"), commandItem("system.help", "help")]
  const { adapter } = makeAdapter(createFakeClient(interactive))
  adapter.dispatch({ type: "draft-change", value: "/" })
  const ids = adapter.getSnapshot().commandOptions
    .filter(item => item.kind === "command")
    .map(item => (item.kind === "command" ? item.command.id : ""))
  expect(ids).toEqual(["system.help"])
})

// ---- Context Dock 基础 ------------------------------------------------------

test("dock-open：打开并切到指定面板；models 触发 catalog.refresh", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "dock-open", panel: "models" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.contextDock.open).toBe(true)
  expect(snapshot.contextDock.activePanel).toBe("models")
  expect(client.intents.some(intent => intent.type === "catalog.refresh" && intent.catalog === "models")).toBe(true)
})

test("dock-panel-select：已开时仅切面板；skills/mcp 触发 catalog.refresh，code/status/help 不触发", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "dock-open", panel: "status" })
  const before = client.intents.length
  await adapter.dispatch({ type: "dock-panel-select", panel: "code" })
  await adapter.dispatch({ type: "dock-panel-select", panel: "status" })
  await adapter.dispatch({ type: "dock-panel-select", panel: "help" })
  expect(adapter.getSnapshot().contextDock.activePanel).toBe("help")
  expect(client.intents.length).toBe(before) // code/status/help 无 catalog.refresh
  await adapter.dispatch({ type: "dock-panel-select", panel: "mcp" })
  expect(client.intents.at(-1)).toEqual({ type: "catalog.refresh", catalog: "mcp" })
})

test("dock-close：关闭但保留 activePanel，重新打开恢复上次面板", async () => {
  const { adapter } = makeAdapter()
  await adapter.dispatch({ type: "dock-open", panel: "skills" })
  await adapter.dispatch({ type: "dock-close" })
  expect(adapter.getSnapshot().contextDock.open).toBe(false)
  expect(adapter.getSnapshot().contextDock.activePanel).toBe("skills")
  await adapter.dispatch({ type: "dock-open", panel: "code" })
  expect(adapter.getSnapshot().contextDock.activePanel).toBe("code")
})

test("dock-width-change：夹取 400-760", async () => {
  const { adapter } = makeAdapter()
  await adapter.dispatch({ type: "dock-width-change", widthPx: 9999 })
  expect(adapter.getSnapshot().contextDock.widthPx).toBe(760)
  await adapter.dispatch({ type: "dock-width-change", widthPx: 10 })
  expect(adapter.getSnapshot().contextDock.widthPx).toBe(400)
  await adapter.dispatch({ type: "dock-width-change", widthPx: 600 })
  expect(adapter.getSnapshot().contextDock.widthPx).toBe(600)
})

test("sidebar-thread-ratio-change：夹取比例并写入 workspaceSidebar", async () => {
  const { adapter } = makeAdapter()
  await adapter.dispatch({ type: "sidebar-thread-ratio-change", ratio: 0.99 })
  expect(adapter.getSnapshot().workspaceSidebar.threadRatio).toBe(0.8)
  await adapter.dispatch({ type: "sidebar-thread-ratio-change", ratio: 0.01 })
  expect(adapter.getSnapshot().workspaceSidebar.threadRatio).toBe(0.2)
  await adapter.dispatch({ type: "sidebar-thread-ratio-change", ratio: 0.5 })
  expect(adapter.getSnapshot().workspaceSidebar.threadRatio).toBe(0.5)
})

test("panel search 写入 panelSearch；仅本地表现状态，不触发 client", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "panel-search", panel: "models", query: "gpt" })
  expect(adapter.getSnapshot().panelSearch.models.query).toBe("gpt")
  expect(client.intents).toEqual([])
  expect(client.workspaceIntents).toEqual([])
})

// ---- 文件 Tab 与预览 ---------------------------------------------------------

test("workspace-file-open：新 Tab + Dock 自动打开切 Code + 触发 preview-file", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "workspace-file-open", path: "src/a.ts" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.contextDock.open).toBe(true)
  expect(snapshot.contextDock.activePanel).toBe("code")
  expect(snapshot.contextDock.code.activePath).toBe("src/a.ts")
  expect(snapshot.contextDock.code.tabs).toEqual([{ path: "src/a.ts", name: "a.ts", language: "typescript" }])
  expect(snapshot.contextDock.code.previews["src/a.ts"]).toEqual({ status: "loading", path: "src/a.ts" })
  expect(client.workspaceIntents).toContainEqual({ type: "workspace.preview-file", path: "src/a.ts" })
})

test("workspace-file-open：已存在 Tab 仅激活（MRU 置顶），不重复创建", async () => {
  const { adapter } = makeAdapter()
  await adapter.dispatch({ type: "workspace-file-open", path: "a.ts" })
  await adapter.dispatch({ type: "workspace-file-open", path: "b.ts" })
  await adapter.dispatch({ type: "workspace-file-open", path: "a.ts" })
  const code = adapter.getSnapshot().contextDock.code
  expect(code.activePath).toBe("a.ts")
  expect(code.tabs.map(tab => tab.path)).toEqual(["a.ts", "b.ts"])
})

test("workspace-file-open：超过 12 个 Tab 淘汰最久未使用（末尾），并清理被淘汰 Tab 的预览缓存", async () => {
  const { adapter } = makeAdapter()
  for (let i = 0; i < 13; i++) {
    await adapter.dispatch({ type: "workspace-file-open", path: `f${i}.ts` })
  }
  const code = adapter.getSnapshot().contextDock.code
  expect(code.tabs.length).toBe(12)
  expect(code.tabs.map(tab => tab.path)).not.toContain("f0.ts")
  expect(code.tabs[0]!.path).toBe("f12.ts")
  // 被淘汰 Tab 的 loading 预览必须同步清理，避免会话内无界累积。
  expect(code.previews["f0.ts"]).toBeUndefined()
})

test("Active Run 期间 workspace-file-open 成功（workspace 不受 busy 门禁）", async () => {
  const client = createFakeClient(makeInteractive({ activeRun: { threadId: "t", runId: "run-1" } }))
  const { adapter, client: recordedClient } = makeAdapter(client)
  await adapter.dispatch({ type: "workspace-file-open", path: "src/a.ts" })
  expect(recordedClient.workspaceIntents).toContainEqual({ type: "workspace.preview-file", path: "src/a.ts" })
})

test("workspace-file-tab-select：激活已有 Tab；无缓存预览时触发 preview-file", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "workspace-file-open", path: "a.ts" })
  await adapter.dispatch({ type: "workspace-file-open", path: "b.ts" })
  client.workspaceIntents.length = 0
  // b.ts 已加载过（loading 已入 previews）→ 仅激活
  await adapter.dispatch({ type: "workspace-file-tab-select", path: "b.ts" })
  expect(adapter.getSnapshot().contextDock.code.activePath).toBe("b.ts")
  expect(client.workspaceIntents).toEqual([])
  // 模拟 b.ts 被关闭后 previews 清空 → 重新激活触发读取
  await adapter.dispatch({ type: "workspace-file-tab-close", path: "b.ts" })
  await adapter.dispatch({ type: "workspace-file-tab-select", path: "a.ts" })
  await adapter.dispatch({ type: "workspace-file-tab-select", path: "a.ts" })
  expect(adapter.getSnapshot().contextDock.code.activePath).toBe("a.ts")
})

test("workspace-file-tab-close：关闭当前 Tab 激活相邻（右侧优先）；关最后一个保留 Dock 打开显示空状态", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "workspace-file-open", path: "a.ts" })
  await adapter.dispatch({ type: "workspace-file-open", path: "b.ts" })
  await adapter.dispatch({ type: "workspace-file-open", path: "c.ts" })
  // 激活 b → 关闭 → 右侧 c
  await adapter.dispatch({ type: "workspace-file-tab-select", path: "b.ts" })
  await adapter.dispatch({ type: "workspace-file-tab-close", path: "b.ts" })
  expect(adapter.getSnapshot().contextDock.code.activePath).toBe("c.ts")
  expect(adapter.getSnapshot().contextDock.code.tabs.map(tab => tab.path)).toEqual(["c.ts", "a.ts"])
  // 关闭 c → 左侧 a
  await adapter.dispatch({ type: "workspace-file-tab-close", path: "c.ts" })
  expect(adapter.getSnapshot().contextDock.code.activePath).toBe("a.ts")
  // 关闭最后一个 → activePath null，Dock 保持打开
  await adapter.dispatch({ type: "workspace-file-tab-close", path: "a.ts" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.contextDock.code.activePath).toBeNull()
  expect(snapshot.contextDock.code.tabs).toEqual([])
  expect(snapshot.contextDock.open).toBe(true)
  expect(snapshot.contextDock.code.previews).toEqual({})
  // 无邻居可加载，不触发多余读取
  expect(client.workspaceIntents.filter(intent => intent.type === "workspace.preview-file")).toHaveLength(3)
})

test("workspace 预览结果合并进 Code 面板；只接受当前 activePath 的结果", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "workspace-file-open", path: "a.ts" })
  await adapter.dispatch({ type: "workspace-file-open", path: "b.ts" })
  // b 是 activePath：b 的 ready 结果写入 previews[b]
  client.pushWorkspace(ws => ({ ...ws, preview: readyPreview("b.ts") }))
  expect(adapter.getSnapshot().contextDock.code.previews["b.ts"]).toMatchObject({ status: "ready" })
  // a 不是 activePath：旧请求结果不覆盖，也不切回旧文件
  client.pushWorkspace(ws => ({ ...ws, preview: readyPreview("a.ts", "old content") }))
  expect(adapter.getSnapshot().contextDock.code.previews["a.ts"]).toEqual({ status: "loading", path: "a.ts" })
  expect(adapter.getSnapshot().contextDock.code.activePath).toBe("b.ts")
})

test("预览 error 保留旧 ready 内容：真实序列（ready → loading → error）下旧内容不被清掉", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "workspace-file-open", path: "a.ts" })
  // explorer 每次结果前必先推 loading：loading 不得覆盖旧 ready 内容。
  client.pushWorkspace(ws => ({ ...ws, preview: readyPreview("a.ts") }))
  client.pushWorkspace(ws => ({ ...ws, preview: { status: "loading", path: "a.ts" } }))
  const duringLoading = adapter.getSnapshot()
  expect(duringLoading.contextDock.code.previews["a.ts"]).toMatchObject({ status: "ready" })
  // 刷新失败 → 旧内容保留，错误只进头部
  client.pushWorkspace(ws => ({ ...ws, preview: { status: "error", path: "a.ts", code: "io-error", message: "读取失败" } }))
  const snapshot = adapter.getSnapshot()
  expect(snapshot.contextDock.code.previews["a.ts"]).toMatchObject({ status: "ready" })
  expect(snapshot.contextDock.code.previewErrors["a.ts"]).toBe("读取失败")
  // 无旧内容时 error 直接替换 loading 视图
  await adapter.dispatch({ type: "workspace-file-open", path: "b.ts" })
  client.pushWorkspace(ws => ({ ...ws, preview: { status: "loading", path: "b.ts" } }))
  client.pushWorkspace(ws => ({ ...ws, preview: { status: "error", path: "b.ts", code: "not-found", message: "文件或目录不存在" } }))
  const bSnapshot = adapter.getSnapshot()
  expect(bSnapshot.contextDock.code.previews["b.ts"]).toMatchObject({ status: "error", code: "not-found" })
  // 刷新成功：错误清除（先切回 a.ts，否则 a.ts 结果因非 activePath 被丢弃）
  await adapter.dispatch({ type: "workspace-file-tab-select", path: "a.ts" })
  client.pushWorkspace(ws => ({ ...ws, preview: readyPreview("a.ts", "new") }))
  expect(adapter.getSnapshot().contextDock.code.previewErrors["a.ts"]).toBeUndefined()
})

test("notice-dismiss：清除 transientNotice，不触碰 Dock", async () => {
  // 通过 rejected outcome 产生通知
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "busy", message: "运行中" }
  await adapter.dispatch({ type: "cancel-run" })
  expect(adapter.getSnapshot().transientNotice).toBe("运行中")
  await adapter.dispatch({ type: "dock-open", panel: "code" })
  await adapter.dispatch({ type: "notice-dismiss" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.transientNotice).toBeNull()
  expect(snapshot.contextDock.open).toBe(true) // Dock 不受通知关闭影响
})

test("workspace-directory-toggle：记录选中并转发 toggle-directory", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "workspace-directory-toggle", path: "src" })
  expect(adapter.getSnapshot().workspaceSidebar.selectedPath).toBe("src")
  expect(client.workspaceIntents).toEqual([{ type: "workspace.toggle-directory", path: "src" }])
})

test("workspace-refresh / workspace-preview-refresh：直接转发 workspace intent", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "workspace-refresh" })
  await adapter.dispatch({ type: "workspace-preview-refresh", path: "a.ts" })
  expect(client.workspaceIntents).toEqual([
    { type: "workspace.refresh" },
    { type: "workspace.refresh-preview", path: "a.ts" },
  ])
})

// ---- 交互与生命周期 ----------------------------------------------------------

test("present result：models/skills → dock-open；threads 忽略", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "accepted", effects: [{ type: "present", target: "models" }] }
  await adapter.dispatch({ type: "draft-change", value: "/models" })
  await adapter.dispatch({ type: "submit" })
  expect(adapter.getSnapshot().contextDock.open).toBe(true)
  expect(adapter.getSnapshot().contextDock.activePanel).toBe("models")
  expect(client.intents.some(intent => intent.type === "catalog.refresh" && intent.catalog === "models")).toBe(true)

  client.nextOutcome = { status: "accepted", effects: [{ type: "present", target: "threads" }] }
  await adapter.dispatch({ type: "draft-change", value: "/threads" })
  await adapter.dispatch({ type: "submit" })
  // threads 常驻左侧：不打开 Dock，也不触发 catalog.refresh
  expect(adapter.getSnapshot().contextDock.activePanel).toBe("models")
})

test("request-handoff result 只显示本地通知，不调用 client 任何方法", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "accepted", effects: [{ type: "request-handoff", threadId: "t-1" }] }
  await adapter.dispatch({ type: "draft-change", value: "/web" })
  await adapter.dispatch({ type: "submit" })
  expect(adapter.getSnapshot().transientNotice).toContain("不能再次打开 Web")
  expect(client.returnCalls).toBe(0)
  expect(client.exitCalls).toBe(0)
  expect(client.readyCalls).toBe(0)
})

test("handoff 进入 web-active 时自动刷新 Thread catalog；非 web-active 不刷新", async () => {
  const { adapter, client } = makeAdapter()
  expect(client.intents).toEqual([])
  client.pushHandoffState({ phase: "opening-web", handoffId: "h1" })
  expect(client.intents).toEqual([])
  client.pushHandoffState({ phase: "web-active", handoffId: "h1" })
  expect(client.intents).toContainEqual({ type: "catalog.refresh", catalog: "threads" })
})

test("连接建立时已处于 web-active（重连）也会刷新 Thread catalog", async () => {
  const client = createFakeClient()
  client.pushHandoffState({ phase: "web-active", handoffId: "h1" })
  const adapter = createWebInteractiveAdapter({ client })
  expect(client.intents).toContainEqual({ type: "catalog.refresh", catalog: "threads" })
  await adapter.close()
})

test("request-exit result 触发 client.requestExit 并设置 leaving", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "accepted", effects: [{ type: "request-exit" }] }
  await adapter.dispatch({ type: "draft-change", value: "/quit" })
  await adapter.dispatch({ type: "submit" })
  expect(client.exitCalls).toBe(1)
  expect(adapter.getSnapshot().leaving).toBe(true)
})

test("Thread/Model/Skill/MCP click 意图只携带稳定 ID 或 typed input", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "thread-select", threadId: "t-9" })
  await adapter.dispatch({ type: "model-select", profileId: "p-1" })
  await adapter.dispatch({ type: "skill-arm", skillId: "s-1" })
  await adapter.dispatch({ type: "skill-clear" })
  await adapter.dispatch({ type: "mcp-add", input: { name: "mcp-1" } as never })
  await adapter.dispatch({ type: "mcp-remove", name: "mcp-1" })
  await adapter.dispatch({ type: "cancel-run" })
  const intents = client.intents.map(intent => intent.type)
  expect(intents).toEqual(["thread.open", "model.select", "skill.arm", "skill.clear", "mcp.add", "mcp.remove", "run.cancel"])
  expect(client.intents[0]).toEqual({ type: "thread.open", threadId: "t-9" })
  expect(client.intents[1]).toEqual({ type: "model.select", profileId: "p-1" })
})

test("model-select rejected 时保持 Dock 打开并显示错误；不关闭", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "dock-open", panel: "models" })
  client.nextOutcome = { status: "rejected", code: "busy", message: "运行中不能切换模型" }
  await adapter.dispatch({ type: "model-select", profileId: "p-1" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.contextDock.open).toBe(true)
  expect(snapshot.contextDock.activePanel).toBe("models")
  expect(snapshot.panelSearch.models.error).toBe("运行中不能切换模型")
  expect(snapshot.panelSearch.models.submitting).toBe(false)
})

test("model-select accepted 后关闭 Dock 并清除 submitting", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "dock-open", panel: "models" })
  client.nextOutcome = { status: "accepted" }
  await adapter.dispatch({ type: "model-select", profileId: "p-1" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.contextDock.open).toBe(false)
  expect(snapshot.panelSearch.models.submitting).toBe(false)
})

test("thread-new rejected 时显示 transient notice", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "busy", message: "存在未完成交互" }
  await adapter.dispatch({ type: "thread-new" })
  expect(adapter.getSnapshot().transientNotice).toBe("存在未完成交互")
})

test("thread-select rejected 时显示 transient notice", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "busy", message: "当前任务执行中" }
  await adapter.dispatch({ type: "thread-select", threadId: "t-9" })
  expect(adapter.getSnapshot().transientNotice).toBe("当前任务执行中")
})

test("skill-arm / skill-clear rejected 时显示 transient notice", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "busy", message: "技能不可用" }
  await adapter.dispatch({ type: "skill-arm", skillId: "s-1" })
  expect(adapter.getSnapshot().transientNotice).toBe("技能不可用")
  client.nextOutcome = { status: "rejected", code: "busy", message: "技能未清除" }
  await adapter.dispatch({ type: "skill-clear" })
  expect(adapter.getSnapshot().transientNotice).toBe("技能未清除")
})

test("mcp-add rejected 时在面板内显示错误", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "dock-open", panel: "mcp" })
  client.nextOutcome = { status: "rejected", code: "agent-error", message: "MCP 服务器连接失败" }
  await adapter.dispatch({ type: "mcp-add", input: { name: "mcp-1" } as never })
  expect(adapter.getSnapshot().panelSearch.mcp.error).toBe("MCP 服务器连接失败")
})

test("cancel-run / approval-mode-cycle rejected 时显示 transient notice", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "not-found", message: "No active run to cancel" }
  await adapter.dispatch({ type: "cancel-run" })
  expect(adapter.getSnapshot().transientNotice).toBe("No active run to cancel")
  client.nextOutcome = { status: "rejected", code: "busy", message: "任务运行中不能切换审批模式" }
  await adapter.dispatch({ type: "approval-mode-cycle" })
  expect(adapter.getSnapshot().transientNotice).toBe("任务运行中不能切换审批模式")
})

test("returnToTui 正常时发送 handoff.return；active Run 或 pending interaction 时阻止并通知", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "return-to-tui" })
  expect(client.returnCalls).toBe(1)
  expect(adapter.getSnapshot().leaving).toBe(true)

  const running = createFakeClient(makeInteractive({ activeRun: { threadId: "t-1", runId: "r-1" } }))
  const runningAdapter = createWebInteractiveAdapter({ client: running })
  await runningAdapter.dispatch({ type: "return-to-tui" })
  expect(running.returnCalls).toBe(0)
  expect(runningAdapter.getSnapshot().transientNotice).toContain("当前任务结束")

  const interacting = createFakeClient(makeInteractive({ interaction: makeApproval("req-1") }))
  const interactingAdapter = createWebInteractiveAdapter({ client: interacting })
  await interactingAdapter.dispatch({ type: "return-to-tui" })
  expect(interacting.returnCalls).toBe(0)
  expect(interactingAdapter.getSnapshot().transientNotice).toContain("请先完成当前审批")
})

test("exit-harness 发送 handoff.exit 并设置 leaving", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "exit-harness" })
  expect(client.exitCalls).toBe(1)
  expect(adapter.getSnapshot().leaving).toBe(true)
})

test("Interaction 草稿随 requestId 变化原子重置；stale 草稿不会发给新 requestId", async () => {
  const { adapter, client } = makeAdapter(createFakeClient(makeInteractive({ interaction: makeApproval("req-1") })))
  await adapter.dispatch({ type: "interaction-draft-change", requestId: "req-1", patch: { kind: "feedback", value: "补充说明" } })
  expect(adapter.getSnapshot().interactionDraft?.feedback).toBe("补充说明")
  // 视图推送新 requestId：草稿必须原子清空。
  client.pushInteractive(snapshot => ({ ...snapshot, interaction: makeApproval("req-2") }))
  expect(adapter.getSnapshot().interactionDraft).toBeNull()
  await adapter.dispatch({ type: "interaction-submit", requestId: "req-2", response: { kind: "approval", decision: "approve_once" } })
  expect(client.intents).toEqual([{ type: "interaction.respond", requestId: "req-2", response: { kind: "approval", decision: "approve_once" } }])
})

test("approval 与 question 的 response payload 透传 client", async () => {
  const { adapter, client } = makeAdapter(createFakeClient(makeInteractive({ interaction: makeQuestion("q-1") })))
  await adapter.dispatch({ type: "interaction-submit", requestId: "q-1", response: { kind: "question", answers: { field: ["a"] } } })
  expect(client.intents).toEqual([{ type: "interaction.respond", requestId: "q-1", response: { kind: "question", answers: { field: ["a"] } } }])
})

test("confirmation-resolve 把 confirmationId/confirmed 透传给 client", async () => {
  const { adapter, client } = makeAdapter(createFakeClient(makeInteractive({ confirmation: makeConfirmation({ confirmationId: "c-1" }) })))
  await adapter.dispatch({ type: "confirmation-resolve", confirmationId: "c-1", confirmed: true })
  expect(client.intents).toEqual([{ type: "confirmation.resolve", confirmationId: "c-1", confirmed: true }])
})

test("tool-toggle 使用 runId:toolId 复合键，跨 Run 相同 toolId 不冲突", async () => {
  const { adapter } = makeAdapter()
  await adapter.dispatch({ type: "tool-toggle", runId: "run-a", toolId: "t1" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.expandedTools.has(toolKey("run-a", "t1"))).toBe(true)
  expect(snapshot.expandedTools.has(toolKey("run-b", "t1"))).toBe(false)
  await adapter.dispatch({ type: "tool-toggle", runId: "run-b", toolId: "t1" })
  expect(adapter.getSnapshot().expandedTools.has(toolKey("run-b", "t1"))).toBe(true)
  expect(adapter.getSnapshot().expandedTools.has(toolKey("run-a", "t1"))).toBe(true)
  await adapter.dispatch({ type: "tool-toggle", runId: "run-a", toolId: "t1" })
  expect(adapter.getSnapshot().expandedTools.has(toolKey("run-a", "t1"))).toBe(false)
  expect(adapter.getSnapshot().expandedTools.has(toolKey("run-b", "t1"))).toBe(true)
})

test("快速视图发布合并为每帧最多一次 presentation publish；close 后无 publish", async () => {
  const { adapter, client, scheduler } = makeAdapter()
  const publishes: WebAdapterSnapshot[] = []
  adapter.subscribe(snapshot => publishes.push(snapshot))
  client.pushInteractive(snapshot => ({ ...snapshot, currentThreadId: "t-1" }))
  client.pushInteractive(snapshot => ({ ...snapshot, currentThreadId: "t-2" }))
  expect(publishes).toHaveLength(0)
  scheduler.runScheduled()
  expect(publishes).toHaveLength(1)
  await adapter.close()
  client.pushInteractive(snapshot => ({ ...snapshot, currentThreadId: "t-3" }))
  scheduler.runScheduled()
  expect(publishes).toHaveLength(1)
})

test("subscribe 返回的 unsubscribe 真的能取消订阅", async () => {
  const { adapter, client, scheduler } = makeAdapter()
  let count = 0
  const unsubscribe = adapter.subscribe(() => { count += 1 })
  client.pushInteractive(snapshot => ({ ...snapshot, currentThreadId: "t-1" }))
  scheduler.runScheduled()
  expect(count).toBe(1)
  unsubscribe()
  client.pushInteractive(snapshot => ({ ...snapshot, currentThreadId: "t-2" }))
  scheduler.runScheduled()
  expect(count).toBe(1)
})

test("主题初始固定 light，与外部 matchMedia / client 状态无关", () => {
  const { adapter } = makeAdapter()
  expect(adapter.getSnapshot().theme).toBe("light")
})

test("theme-set 更新主题并发布一次；重复设置当前值不重复发布", async () => {
  const { adapter, scheduler } = makeAdapter()
  const publishes: WebAdapterSnapshot[] = []
  adapter.subscribe(snapshot => publishes.push(snapshot))
  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  scheduler.runScheduled()
  expect(publishes.at(-1)?.theme).toBe("dark")
  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  scheduler.runScheduled()
  expect(publishes.length).toBe(1)
})

test("approval-mode-cycle 转发共享 Core 的 approval-mode.cycle", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "approval-mode-cycle" })
  expect(client.intents).toContainEqual({ type: "approval-mode.cycle" })
})

test("createDefaultFrameScheduler 包装宿主 rAF，避免 detached 调用 Illegal invocation", async () => {
    // 模拟 window 上的宿主方法：detached 调用（this !== globalThis）必须抛 Illegal invocation。
    const frames: FrameRequestCallback[] = []
    function hostRequestAnimationFrame(this: unknown, callback: FrameRequestCallback): number {
      if (this !== globalThis) throw new TypeError("Illegal invocation")
      frames.push(callback)
      return 1
    }
    function hostCancelAnimationFrame(this: unknown, _handle: number): void {
      if (this !== globalThis) throw new TypeError("Illegal invocation")
    }
    const originalRaf = globalThis.requestAnimationFrame
    const originalCaf = globalThis.cancelAnimationFrame
    Object.defineProperty(globalThis, "requestAnimationFrame", { value: hostRequestAnimationFrame, configurable: true, writable: true })
    Object.defineProperty(globalThis, "cancelAnimationFrame", { value: hostCancelAnimationFrame, configurable: true, writable: true })
    try {
      const scheduler = createDefaultFrameScheduler()
      let fired = 0
      scheduler.schedule(() => { fired += 1 })
      scheduler.schedule(() => { fired += 10 })
      expect(frames.length).toBe(1)
      frames[0]!(0)
      expect(fired).toBe(10)
      scheduler.cancel()
      expect(frames.length).toBe(1)
    } finally {
      Object.defineProperty(globalThis, "requestAnimationFrame", { value: originalRaf, configurable: true, writable: true })
      Object.defineProperty(globalThis, "cancelAnimationFrame", { value: originalCaf, configurable: true, writable: true })
    }
  })

test("theme/header menu 意图是纯表现动作：不调用 client 业务方法", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  expect(client.intents).toEqual([])
  expect(client.readyCalls + client.returnCalls + client.exitCalls).toBe(0)
})

test("header menu 关闭规则：选择主题/打开 Help/返回 TUI/退出都先关闭菜单", async () => {
  const { adapter } = makeAdapter()
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(true)
  await adapter.dispatch({ type: "dock-open", panel: "help" })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
})

test("连接变为只读时关闭 header menu 并立即发布", async () => {
  const { adapter, client } = makeAdapter()
  const publishes: WebAdapterSnapshot[] = []
  adapter.subscribe(snapshot => publishes.push(snapshot))
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  client.pushInteractive(snapshot => ({ ...snapshot, connection: { status: "closed", message: "断开" } }))
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
  expect(publishes.length).toBeGreaterThan(0)
})

test("close 之后 theme/header menu intent 安全 no-op", async () => {
  const { adapter } = makeAdapter()
  await adapter.close()
  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  expect(adapter.getSnapshot().theme).toBe("light")
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
})

// ---- run 结束自动刷新 --------------------------------------------------------

test("run 结束（activeRun 非空 → null）延迟触发 workspace.refresh + 当前预览 refresh", async () => {
  const { adapter, client, timer } = makeAdapter()
  await adapter.dispatch({ type: "workspace-file-open", path: "src/a.ts" })
  client.workspaceIntents.length = 0
  // run 开始
  client.pushInteractive(snapshot => ({ ...snapshot, activeRun: { threadId: "t", runId: "run-1" } }))
  expect(timer.pending()).toBe(false)
  // run 结束 → 200ms 延迟定时器挂起
  client.pushInteractive(snapshot => ({ ...snapshot, activeRun: null }))
  expect(timer.pending()).toBe(true)
  timer.run()
  expect(client.workspaceIntents).toContainEqual({ type: "workspace.refresh" })
  expect(client.workspaceIntents).toContainEqual({ type: "workspace.refresh-preview", path: "src/a.ts" })
})

test("run 结束刷新：无打开文件时只刷新树；close 后定时器不再触发", async () => {
  const { adapter, client, timer } = makeAdapter()
  client.pushInteractive(snapshot => ({ ...snapshot, activeRun: { threadId: "t", runId: "run-1" } }))
  client.pushInteractive(snapshot => ({ ...snapshot, activeRun: null }))
  timer.run()
  expect(client.workspaceIntents).toContainEqual({ type: "workspace.refresh" })
  expect(client.workspaceIntents.filter(intent => intent.type === "workspace.refresh-preview")).toEqual([])

  // 关闭后 run 结束不触发
  client.workspaceIntents.length = 0
  client.pushInteractive(snapshot => ({ ...snapshot, activeRun: { threadId: "t", runId: "run-2" } }))
  await adapter.close()
  client.pushInteractive(snapshot => ({ ...snapshot, activeRun: null }))
  expect(timer.pending()).toBe(false)
})

test("close 幂等：第二次 close 不抛错、不再调用 frameScheduler.cancel 之外的操作", async () => {
  const { adapter } = makeAdapter()
  await adapter.close()
  await adapter.close()
})
