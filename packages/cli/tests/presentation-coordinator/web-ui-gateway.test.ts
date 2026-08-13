/** WebUiGateway 测试：真实 InteractiveController + 真实 Coordinator 驱动的分片发布与双域意图受理。 */

import { expect, test } from "bun:test"

import { AsyncQueue } from "../../src/ipc/transport"
import { makeHarness } from "../interactive/harness"
import {
  createPresentationCoordinator,
  type GatewayChannel,
  type PresentationCoordinator,
  type PresentationScheduler,
  type PresentationServer,
} from "../../src/presentation-coordinator/coordinator"
import {
  createWebUiGateway,
  type WebUiGateway,
} from "../../src/presentation-coordinator/web-ui-gateway"
import type { WebUiClientMessage, WebUiServerMessage } from "../../src/presentation-coordinator/contracts/messages"
import type { InteractiveController, InteractiveIntent } from "../../src/interactive/types"
import type { WorkspaceExplorer, WorkspaceIntent, WorkspacePreviewState, WorkspaceSnapshot, WorkspaceTreeState } from "../../src/workspace/types"

// ---- 命名形状（避免 ReturnType 发布契约） -------------------------------------

type FakeChannel = {
  channel: GatewayChannel
  queue: AsyncQueue<unknown>
  sent: WebUiServerMessage[]
  closed: Array<{ code: number; reason: string }>
}

type FakeExplorer = WorkspaceExplorer & {
  intents: WorkspaceIntent[]
  setTree(next: WorkspaceTreeState): void
  setPreview(next: WorkspacePreviewState): void
}

type TestSystem = {
  controller: InteractiveController
  coordinator: PresentationCoordinator
  gateway: WebUiGateway
  explorer: FakeExplorer
  server: PresentationServer
  openedUrls: string[]
  timers: Array<{ callback: () => void; at: number; active: boolean }>
}

// ---- fakes -----------------------------------------------------------------

function createFakeChannel(): FakeChannel {
  const queue = new AsyncQueue<unknown>()
  const sent: WebUiServerMessage[] = []
  const closed: Array<{ code: number; reason: string }> = []
  const channel: GatewayChannel = {
    messages: queue,
    send: async message => { sent.push(message) },
    close: async (code, reason) => { closed.push({ code, reason }); queue.end() },
  }
  return { channel, queue, sent, closed }
}

function sendClient(ch: FakeChannel, message: WebUiClientMessage): void {
  ch.queue.push(JSON.stringify(message))
}

/** 内存 fake explorer：记录 workspace intent、可按需发布新树/预览快照。 */
function createFakeExplorer(): FakeExplorer {
  const listeners = new Set<(snapshot: WorkspaceSnapshot) => void>()
  let tree: WorkspaceTreeState = { status: "idle", rows: [], selectedPath: null, limited: false }
  let preview: WorkspacePreviewState = { status: "idle" }
  const explorer: FakeExplorer = {
    intents: [],
    getSnapshot: () => ({ tree, preview }),
    subscribe: listener => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    dispatch: async intent => {
      explorer.intents.push(intent)
      return { status: "accepted" }
    },
    close: async () => {},
    setTree(next) {
      tree = next
      for (const listener of [...listeners]) listener({ tree, preview })
    },
    setPreview(next) {
      preview = next
      for (const listener of [...listeners]) listener({ tree, preview })
    },
  }
  return explorer
}

/** 包装真实 controller 并对 dispatch 计数；用于重放/并发去重断言。 */
function createCountingController(controller: InteractiveController) {
  const counting = { dispatchCalls: 0 }
  const wrapped: InteractiveController = {
    getSnapshot: () => controller.getSnapshot(),
    subscribe: listener => controller.subscribe(listener),
    dispatch: async (intent: InteractiveIntent) => {
      counting.dispatchCalls += 1
      return controller.dispatch(intent)
    },
    close: () => controller.close(),
  }
  return { wrapped, counting }
}

async function flush(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0))
  await new Promise(resolve => setTimeout(resolve, 0))
}

async function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    const { promise, resolve } = Promise.withResolvers<void>()
    setTimeout(resolve, 2)
    await promise
  }
}

// ---- 系统装配 ---------------------------------------------------------------

function createSystem(options: { controller?: InteractiveController; explorer?: FakeExplorer } = {}): TestSystem {
  const harness = makeHarness({ initialThreadId: "thread-1" })
  const controller = options.controller ?? harness.controller
  const explorer = options.explorer ?? createFakeExplorer()
  const openedUrls: string[] = []
  const timers: Array<{ callback: () => void; at: number; active: boolean }> = []
  const scheduler: PresentationScheduler = {
    set: (callback, ms) => {
      const entry = { callback, at: Date.now() + ms, active: true }
      timers.push(entry)
      return { clear: () => { entry.active = false } }
    },
    now: () => Date.now(),
  }
  const server: PresentationServer = {
    origin: "http://127.0.0.1:1",
    pathFor: handoffId => `/web/h/${handoffId}`,
    start: async () => {},
    stop: async () => {},
  }
  let gateway!: WebUiGateway
  const coordinator = createPresentationCoordinator({
    server,
    openBrowser: async url => { openedUrls.push(url) },
    dispatch: intent => controller.dispatch(intent),
    onRendererConnected: channel => gateway.connectRenderer(channel),
    readyTimeoutMs: 65_000,
    reconnectGraceMs: 10_000,
    uiTokenTtlMs: 60_000,
    scheduler,
  })
  gateway = createWebUiGateway({ coordinator, controller, workspaceExplorer: explorer })
  return { controller, coordinator, gateway, explorer, server, openedUrls, timers }
}

/** open + attachRenderer（opening-web 阶段，尚未 ready）。 */
async function connectRenderer(system: TestSystem) {
  await system.coordinator.open()
  const opening = system.coordinator.getSnapshot()
  if (opening.phase !== "opening-web") throw new Error("expected opening-web")
  const ch = createFakeChannel()
  await system.coordinator.attachRenderer(opening.handoffId, ch.channel)
  await flush()
  return { handoffId: opening.handoffId, ch }
}

async function connectAndReady(system: TestSystem) {
  const { handoffId, ch } = await connectRenderer(system)
  sendClient(ch, { type: "handoff.ready" })
  await waitFor(() => ch.sent.some(message => message.type === "handoff.state" && message.state.phase === "web-active"))
  return { handoffId, ch }
}

function lastSent(ch: FakeChannel): WebUiServerMessage {
  const last = ch.sent.at(-1)
  if (!last) throw new Error("no messages sent")
  return last
}

// ---- 首帧与分片发布 ---------------------------------------------------------

test("connectRenderer：首帧 state.replace（revision 1、八个分片完整）+ handoff.state(opening-web)", async () => {
  const system = createSystem()
  await flush() // 等构造期的 catalog/thread 恢复发布先落定
  const { ch } = await connectRenderer(system)

  const replace = ch.sent[0]
  expect(replace).toMatchObject({ type: "state.replace", revision: 1 })
  const state = (replace as Extract<WebUiServerMessage, { type: "state.replace" }>).state
  expect(Object.keys(state)).toEqual(["conversation", "interaction", "navigation", "command", "runtime", "workItem", "workspaceTree", "workspacePreview"])
  for (const key of Object.keys(state)) {
    expect(typeof (state as Record<string, unknown>)[key]).toBe("object")
  }
  expect(ch.sent[1]).toMatchObject({ type: "handoff.state", state: { phase: "opening-web" } })
})

test("controller publish → state.patch：只含变化分片且 revision 单调递增", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  await system.controller.dispatch({ type: "approval-mode.cycle" })
  await flush()
  const patch = lastSent(ch)
  expect(patch.type).toBe("state.patch")
  expect(patch.revision).toBe(2)
  expect(Object.keys(patch.patch)).toEqual(["runtime"])
  const runtimeSlice = patch.patch.runtime as { runtime: { approvalMode: string } }
  expect(runtimeSlice.runtime.approvalMode).toBe("auto-edit")

  // 再次发布 → revision 继续递增
  await system.controller.dispatch({ type: "approval-mode.cycle" })
  await flush()
  const patch2 = lastSent(ch)
  expect(patch2.type).toBe("state.patch")
  expect(patch2.revision).toBe(3)
})

test("explorer publish → state.patch 只含 workspaceTree，且不混入 interactive 分片", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  system.explorer.setTree({ status: "ready", rows: [{ path: "src", name: "src", kind: "directory", depth: 0, expanded: false, loading: false, hasChildren: true }], selectedPath: null, limited: false })
  await flush()
  const patch = lastSent(ch)
  expect(patch.type).toBe("state.patch")
  expect(patch.revision).toBe(2)
  expect(Object.keys(patch.patch)).toEqual(["workspaceTree"])

  system.explorer.setPreview({ status: "loading", path: "src/a.ts" })
  await flush()
  const patch2 = lastSent(ch)
  expect(patch2.type).toBe("state.patch")
  expect(patch2.revision).toBe(3)
  expect(Object.keys(patch2.patch)).toEqual(["workspacePreview"])
})

// ---- interactive 意图受理 ---------------------------------------------------

test("command.execute 无 argument（Web adapter 真实发射形状）受理且不触发会话收敛", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)
  const before = system.controller.getSnapshot().catalogs.threads.items

  // Web adapter thread-new 发射的精确形状：command.execute 不带 argument。
  sendClient(ch, { type: "interactive.intent", requestId: "new-1", revision: 1, intent: { type: "command.execute", commandId: "thread.new" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "new-1"))

  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "new-1")).toMatchObject({
    type: "intent.outcome",
    requestId: "new-1",
    domain: "interactive",
    outcome: { status: "accepted" },
  })

  // 会话未收敛：channel 未被关闭、阶段仍 web-active、回到沉浸式首页。
  expect(ch.closed).toEqual([])
  expect(system.coordinator.getSnapshot().phase).toBe("web-active")
  expect(system.controller.getSnapshot().currentThreadId).toBeNull()
  // 新建只清 conversation scope，全局 Thread catalog 保留（侧栏历史立即可切回）。
  expect(system.controller.getSnapshot().catalogs.threads.items).toEqual(before)
})

test("interactive.intent 受理：intent.outcome 带 domain=interactive 且与 requestId 一一对应", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  sendClient(ch, { type: "interactive.intent", requestId: "it-1", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "it-1"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "it-1")).toEqual({
    type: "intent.outcome",
    requestId: "it-1",
    domain: "interactive",
    outcome: { status: "accepted" },
  })

  sendClient(ch, { type: "interactive.intent", requestId: "it-2", revision: 1, intent: { type: "run.cancel" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "it-2"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "it-2")).toEqual({
    type: "intent.outcome",
    requestId: "it-2",
    domain: "interactive",
    outcome: { status: "rejected", code: "not-found", message: "No active run to cancel" },
  })
})

test("重放同一 requestId：返回缓存 outcome 且 controller.dispatch 只调用一次", async () => {
  const system = createSystem()
  await flush()
  const { wrapped, counting } = createCountingController(system.controller)
  const countingSystem = createSystem({ controller: wrapped })
  await flush()
  const { ch } = await connectAndReady(countingSystem)

  sendClient(ch, { type: "interactive.intent", requestId: "r-1", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "r-1"))
  expect(counting.dispatchCalls).toBe(1)

  // 重放同一 requestId：缓存命中，不再执行
  sendClient(ch, { type: "interactive.intent", requestId: "r-1", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.filter(message => message.type === "intent.outcome" && message.requestId === "r-1").length === 2)
  expect(counting.dispatchCalls).toBe(1)
  expect(ch.sent.filter(message => message.type === "intent.outcome" && message.requestId === "r-1")[1]).toEqual({
    type: "intent.outcome",
    requestId: "r-1",
    domain: "interactive",
    outcome: { status: "accepted" },
  })
})

test("并发相同 requestId 去重：两次帧只执行一次 dispatch，各自收到 outcome", async () => {
  const system = createSystem()
  await flush()
  const { wrapped, counting } = createCountingController(system.controller)
  const countingSystem = createSystem({ controller: wrapped })
  await flush()
  const { ch } = await connectAndReady(countingSystem)

  sendClient(ch, { type: "interactive.intent", requestId: "dup", revision: 1, intent: { type: "approval-mode.cycle" } })
  sendClient(ch, { type: "interactive.intent", requestId: "dup", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.filter(message => message.type === "intent.outcome" && message.requestId === "dup").length === 2)
  expect(counting.dispatchCalls).toBe(1)
})

test("revision 超前/过旧 → rejected invalid-argument；合法 revision 受理", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  // revision 0（低于 BASE_REVISION=1）
  sendClient(ch, { type: "interactive.intent", requestId: "old", revision: 0, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "old"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "old")).toEqual({
    type: "intent.outcome",
    requestId: "old",
    domain: "interactive",
    outcome: { status: "rejected", code: "invalid-argument", message: "Invalid revision; resync from state.replace" },
  })

  // revision 超前于网关发布序列
  sendClient(ch, { type: "interactive.intent", requestId: "fut", revision: 99, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "fut"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "fut")?.outcome).toMatchObject({
    status: "rejected",
    code: "invalid-argument",
  })

  // 合法 revision
  sendClient(ch, { type: "interactive.intent", requestId: "ok", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "ok"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "ok")).toEqual({
    type: "intent.outcome",
    requestId: "ok",
    domain: "interactive",
    outcome: { status: "accepted" },
  })
})

test("非 web-active 阶段 intent：只回 handoff.state，不发 outcome", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectRenderer(system) // 仍在 opening-web，未 ready
  const before = ch.sent.length

  sendClient(ch, { type: "interactive.intent", requestId: "early", revision: 1, intent: { type: "approval-mode.cycle" } })
  sendClient(ch, { type: "workspace.intent", requestId: "early-ws", revision: 1, intent: { type: "workspace.refresh" } })
  await waitFor(() => ch.sent.length > before)
  expect(lastSent(ch)).toMatchObject({ type: "handoff.state", state: { phase: "opening-web" } })
  expect(ch.sent.some(message => message.type === "intent.outcome")).toBe(false)
  expect(system.explorer.intents).toEqual([]) // workspace 也未受理
})

// ---- workspace 意图受理 -----------------------------------------------------

test("workspace.intent 路由到 explorer；outcome 带 domain=workspace 回传", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  sendClient(ch, { type: "workspace.intent", requestId: "ws-1", revision: 1, intent: { type: "workspace.refresh" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "ws-1"))
  expect(system.explorer.intents).toEqual([{ type: "workspace.refresh" }])
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "ws-1")).toEqual({
    type: "intent.outcome",
    requestId: "ws-1",
    domain: "workspace",
    outcome: { status: "accepted" },
  })

  sendClient(ch, { type: "workspace.intent", requestId: "ws-2", revision: 1, intent: { type: "workspace.preview-file", path: "src/a.ts" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "ws-2"))
  expect(system.explorer.intents.at(-1)).toEqual({ type: "workspace.preview-file", path: "src/a.ts" })
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "ws-2")).toEqual({
    type: "intent.outcome",
    requestId: "ws-2",
    domain: "workspace",
    outcome: { status: "accepted" },
  })
})

test("workspace.intent 复用 revision 门禁：超前 revision → rejected", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  sendClient(ch, { type: "workspace.intent", requestId: "ws-fut", revision: 99, intent: { type: "workspace.refresh" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "ws-fut"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "ws-fut")).toEqual({
    type: "intent.outcome",
    requestId: "ws-fut",
    domain: "workspace",
    outcome: { status: "rejected", code: "invalid-argument", message: "Invalid revision; resync from state.replace" },
  })
  expect(system.explorer.intents).toEqual([])
})

// ---- 生命周期转发 -----------------------------------------------------------

test("handoff.state 变化转发给 renderer；returning-tui 时 channel.close 被调用", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectRenderer(system)

  // opening-web → web-active 的过渡帧
  sendClient(ch, { type: "handoff.ready" })
  await waitFor(() => ch.sent.some(message => message.type === "handoff.state" && message.state.phase === "web-active"))

  // 主动归还：returning-tui 转发 + channel 收敛关闭
  sendClient(ch, { type: "handoff.return" })
  await waitFor(() => ch.sent.some(message => message.type === "handoff.state" && message.state.phase === "returning-tui"))
  expect(ch.closed).toEqual([{ code: 1000, reason: "handoff-converged" }])
  expect(system.coordinator.getSnapshot().phase).toBe("tui-active")
})

test("gateway close()：停止 controller/explorer 订阅与帧受理", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)
  await system.gateway.close()
  const before = ch.sent.length

  // 客户端帧不再受理
  sendClient(ch, { type: "interactive.intent", requestId: "late", revision: 1, intent: { type: "approval-mode.cycle" } })
  sendClient(ch, { type: "workspace.intent", requestId: "late-ws", revision: 1, intent: { type: "workspace.refresh" } })
  await flush()
  expect(ch.sent.length).toBe(before)

  // controller / explorer 发布不再产生 patch
  await system.controller.dispatch({ type: "approval-mode.cycle" })
  system.explorer.setTree({ status: "ready", rows: [], selectedPath: null, limited: false })
  await flush()
  expect(ch.sent.length).toBe(before)
})
