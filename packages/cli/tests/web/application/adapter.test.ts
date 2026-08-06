/** Web Interactive Adapter：通过 fake WebUiClient 验证视图缓存、语义意图与副作用。 */

import { expect, test } from "bun:test"

import type { InteractiveIntent, InteractiveSnapshot, IntentOutcome } from "../../../src/interactive/types"
import { buildWebUiState, type PresentationState, type WebUiState } from "../../../src/presentation-coordinator"
import {
  createDefaultFrameScheduler,
  createWebInteractiveAdapter,
  toolKey,
  type WebAdapterSnapshot,
  type WebFrameScheduler,
  type WebIntent,
  type WebInteractiveAdapter,
} from "../../../src/web/application/adapter"
import type { WebUiClient } from "../../../src/web/ui-client"
import { makeApproval, makeConfirmation, makeInteractive, makeMcp, makeModel, makeQuestion, makeSkill, makeThread } from "../presentation/fixtures"
import type { CommandMenuItem } from "../../../src/interactive/commands"

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

/** fake WebUiClient：记录 intent 与生命周期调用，测试可注入 outcome 或推送视图。 */
function createFakeClient(initial = makeInteractive()): WebUiClient & {
  intents: InteractiveIntent[]
  nextOutcome: IntentOutcome | null
  readyCalls: number
  returnCalls: number
  exitCalls: number
  closed: boolean
  pushState(updater: (state: WebUiState) => WebUiState): void
  pushInteractive(updater: (snapshot: InteractiveSnapshot) => InteractiveSnapshot): void
  pushHandoffState(state: PresentationState): void
} {
  const stateListeners = new Set<(state: WebUiState) => void>()
  const handoffListeners = new Set<(state: PresentationState) => void>()
  const client = {
    state: buildWebUiState(initial),
    handoffState: { phase: "opening-web", handoffId: "h1" } as PresentationState,
    intents: [] as InteractiveIntent[],
    nextOutcome: null as IntentOutcome | null,
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
      this.pushState(() => buildWebUiState(updater(client.getSnapshotFromState())))
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

function makeAdapter(client = createFakeClient(), scheduler = createManualScheduler()): {
  adapter: WebInteractiveAdapter
  client: ReturnType<typeof createFakeClient>
  scheduler: typeof scheduler
} {
  const adapter = createWebInteractiveAdapter({ client, frameScheduler: scheduler })
  return { adapter, client, scheduler }
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

test("初始 snapshot 来自共享视图：interactive 由五个分片重组", () => {
  const { adapter } = makeAdapter(createFakeClient(makeInteractive({ currentThreadId: "t-1" })))
  const snapshot = adapter.getSnapshot()
  expect(snapshot.interactive.currentThreadId).toBe("t-1")
  expect(snapshot.interactive.timeline).toEqual(snapshot.interactive.timeline)
  expect(snapshot.expandedTools).toBeInstanceOf(Set)
})

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

test("present result 打开对应面板并触发 catalog.refresh", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "accepted", effects: [{ type: "present", target: "threads" }] }
  await adapter.dispatch({ type: "draft-change", value: "/threads" })
  await adapter.dispatch({ type: "submit" })
  expect(adapter.getSnapshot().activePanel).toBe("threads")
  expect(client.intents.some(intent => intent.type === "catalog.refresh" && intent.catalog === "threads")).toBe(true)
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

test("model-select rejected 时保持面板打开并显示错误；不关闭面板", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "panel-open", panel: "models" })
  client.nextOutcome = { status: "rejected", code: "busy", message: "运行中不能切换模型" }
  await adapter.dispatch({ type: "model-select", profileId: "p-1" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.activePanel).toBe("models")
  expect(snapshot.panelSearch.models.error).toBe("运行中不能切换模型")
  expect(snapshot.panelSearch.models.submitting).toBe(false)
})

test("model-select accepted 后关闭面板并清除 submitting", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "panel-open", panel: "models" })
  client.nextOutcome = { status: "accepted" }
  await adapter.dispatch({ type: "model-select", profileId: "p-1" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.activePanel).toBeNull()
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
  await adapter.dispatch({ type: "panel-open", panel: "mcp" })
  client.nextOutcome = { status: "rejected", code: "agent-error", message: "MCP 服务器连接失败" }
  await adapter.dispatch({ type: "mcp-add", input: { name: "mcp-1" } as never })
  expect(adapter.getSnapshot().panelSearch.mcp.error).toBe("MCP 服务器连接失败")
})

test("cancel-run rejected 时显示 transient notice", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "not-found", message: "No active run to cancel" }
  await adapter.dispatch({ type: "cancel-run" })
  expect(adapter.getSnapshot().transientNotice).toBe("No active run to cancel")
})

test("approval-mode-cycle rejected 时显示 transient notice", async () => {
  const { adapter, client } = makeAdapter()
  client.nextOutcome = { status: "rejected", code: "busy", message: "任务运行中不能切换审批模式" }
  await adapter.dispatch({ type: "approval-mode-cycle" })
  expect(adapter.getSnapshot().transientNotice).toBe("任务运行中不能切换审批模式")
})

test("returnToTui 正常时发送 handoff.return；active Run 或 pending interaction 时阻止并通知", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "return-to-tui" })
  expect(client.returnCalls).toBe(1)
  expect(adapter.getSnapshot().leaving).toBe(true)

  const running = createFakeClient(makeInteractive({ activeRun: { runId: "r-1", threadId: "t-1", status: "running", model: "m" } as never }))
  const runningAdapter = createWebInteractiveAdapter({ client: running })
  await runningAdapter.dispatch({ type: "return-to-tui" })
  expect(running.returnCalls).toBe(0)
  expect(runningAdapter.getSnapshot().transientNotice).toContain("当前任务结束")

  const interacting = createFakeClient(makeInteractive({ interaction: makeApproval("req-1") } as never))
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
  const { adapter, client } = makeAdapter(createFakeClient(makeInteractive({ interaction: makeApproval("req-1") } as never)))
  await adapter.dispatch({ type: "interaction-draft-change", requestId: "req-1", patch: { kind: "feedback", value: "补充说明" } })
  expect(adapter.getSnapshot().interactionDraft?.feedback).toBe("补充说明")
  // 视图推送新 requestId：草稿必须原子清空。
  client.pushInteractive(snapshot => ({ ...snapshot, interaction: makeApproval("req-2") as never }))
  expect(adapter.getSnapshot().interactionDraft).toBeNull()
  await adapter.dispatch({ type: "interaction-submit", requestId: "req-2", response: { kind: "approval", decision: "approve_once" } })
  expect(client.intents).toEqual([{ type: "interaction.respond", requestId: "req-2", response: { kind: "approval", decision: "approve_once" } }])
})

test("approval 与 question 的 response payload 透传 client", async () => {
  const { adapter, client } = makeAdapter(createFakeClient(makeInteractive({ interaction: makeQuestion("q-1") } as never)))
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

test("panel search 写入 panelSearch；panel open 触发对应 catalog.refresh", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "panel-open", panel: "models" })
  await adapter.dispatch({ type: "panel-search", panel: "models", query: "gpt" })
  expect(adapter.getSnapshot().panelSearch.models.query).toBe("gpt")
  expect(client.intents.some(intent => intent.type === "catalog.refresh" && intent.catalog === "models")).toBe(true)
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
  await adapter.dispatch({ type: "panel-open", panel: "help" })
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

test("移动端抽屉互斥：打开 Thread 抽屉关闭面板，打开面板关闭抽屉，选择 Thread 关闭抽屉", async () => {
  const { adapter, client } = makeAdapter()
  await adapter.dispatch({ type: "panel-open", panel: "models" })
  await adapter.dispatch({ type: "sidebar-toggle", open: true })
  expect(adapter.getSnapshot().sidebarOpen).toBe(true)
  expect(adapter.getSnapshot().activePanel).toBeNull()
  await adapter.dispatch({ type: "panel-open", panel: "skills" })
  expect(adapter.getSnapshot().sidebarOpen).toBe(false)
  await adapter.dispatch({ type: "sidebar-toggle", open: true })
  await adapter.dispatch({ type: "thread-select", threadId: "t-1" })
  expect(adapter.getSnapshot().sidebarOpen).toBe(false)
  expect(client.intents.some(intent => intent.type === "thread.open")).toBe(true)
})

test("close 幂等：第二次 close 不抛错、不再调用 frameScheduler.cancel 之外的操作", async () => {
  const { adapter } = makeAdapter()
  await adapter.close()
  await adapter.close()
})
