/** WebUiGateway 测试：真实 InteractiveController + 真实 Coordinator 驱动的分片发布与意图受理。 */

import { expect, test } from "bun:test"

import { AsyncQueue } from "../../src/ipc/transport"
import { makeHarness } from "../interactive/harness"
import {
  createPresentationCoordinator,
  type GatewayChannel,
  type PresentationScheduler,
  type PresentationServer,
} from "../../src/presentation-coordinator/coordinator"
import {
  createWebUiGateway,
  type WebUiGateway,
} from "../../src/presentation-coordinator/web-ui-gateway"
import type { WebUiClientMessage, WebUiServerMessage } from "../../src/presentation-coordinator/contracts/messages"
import type { InteractiveController, InteractiveIntent } from "../../src/interactive/types"

// ---- fakes -----------------------------------------------------------------

function createFakeChannel() {
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

function sendClient(ch: ReturnType<typeof createFakeChannel>, message: WebUiClientMessage): void {
  ch.queue.push(JSON.stringify(message))
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
    await new Promise(resolve => setTimeout(resolve, 2))
  }
}

// ---- 系统装配 ---------------------------------------------------------------

function createSystem(options: { controller?: InteractiveController } = {}) {
  const harness = makeHarness({ initialThreadId: "thread-1" })
  const controller = options.controller ?? harness.controller
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
  gateway = createWebUiGateway({ coordinator, controller })
  return { controller, harness, coordinator, gateway, server, openedUrls, timers }
}

/** open + attachRenderer（opening-web 阶段，尚未 ready）。 */
async function connectRenderer(system: ReturnType<typeof createSystem>) {
  await system.coordinator.open()
  const opening = system.coordinator.getSnapshot()
  if (opening.phase !== "opening-web") throw new Error("expected opening-web")
  const ch = createFakeChannel()
  await system.coordinator.attachRenderer(opening.handoffId, ch.channel)
  await flush()
  return { handoffId: opening.handoffId, ch }
}

async function connectAndReady(system: ReturnType<typeof createSystem>) {
  const { handoffId, ch } = await connectRenderer(system)
  sendClient(ch, { type: "handoff.ready" })
  await waitFor(() => ch.sent.some(message => message.type === "handoff.state" && message.state.phase === "web-active"))
  return { handoffId, ch }
}

function lastSent(ch: ReturnType<typeof createFakeChannel>): WebUiServerMessage {
  const last = ch.sent.at(-1)
  if (!last) throw new Error("no messages sent")
  return last
}

// ---- 首帧与分片发布 ---------------------------------------------------------

test("connectRenderer：首帧 state.replace（revision 1、五个分片完整）+ handoff.state(opening-web)", async () => {
  const system = createSystem()
  await flush() // 等构造期的 catalog/thread 恢复发布先落定
  const { ch } = await connectRenderer(system)

  const replace = ch.sent[0]
  expect(replace).toMatchObject({ type: "state.replace", revision: 1 })
  const state = (replace as Extract<WebUiServerMessage, { type: "state.replace" }>).state
  expect(Object.keys(state)).toEqual(["conversation", "interaction", "navigation", "command", "runtime"])
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

// ---- 意图受理 ---------------------------------------------------------------

test("intent 受理：intent.outcome 与 requestId 一一对应", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  sendClient(ch, { type: "intent", requestId: "it-1", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "it-1"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "it-1")).toEqual({
    type: "intent.outcome",
    requestId: "it-1",
    outcome: { status: "accepted" },
  })

  sendClient(ch, { type: "intent", requestId: "it-2", revision: 1, intent: { type: "run.cancel" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "it-2"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "it-2")).toEqual({
    type: "intent.outcome",
    requestId: "it-2",
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

  sendClient(ch, { type: "intent", requestId: "r-1", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "r-1"))
  expect(counting.dispatchCalls).toBe(1)

  // 重放同一 requestId：缓存命中，不再执行
  sendClient(ch, { type: "intent", requestId: "r-1", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.filter(message => message.type === "intent.outcome" && message.requestId === "r-1").length === 2)
  expect(counting.dispatchCalls).toBe(1)
  expect(ch.sent.filter(message => message.type === "intent.outcome" && message.requestId === "r-1")[1]).toEqual({
    type: "intent.outcome",
    requestId: "r-1",
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

  sendClient(ch, { type: "intent", requestId: "dup", revision: 1, intent: { type: "approval-mode.cycle" } })
  sendClient(ch, { type: "intent", requestId: "dup", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.filter(message => message.type === "intent.outcome" && message.requestId === "dup").length === 2)
  expect(counting.dispatchCalls).toBe(1)
})

test("revision 超前/过旧 → rejected invalid-argument；合法 revision 受理", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)

  // revision 0（低于 BASE_REVISION=1）
  sendClient(ch, { type: "intent", requestId: "old", revision: 0, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "old"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "old")).toEqual({
    type: "intent.outcome",
    requestId: "old",
    outcome: { status: "rejected", code: "invalid-argument", message: "Invalid revision; resync from state.replace" },
  })

  // revision 超前于网关发布序列
  sendClient(ch, { type: "intent", requestId: "fut", revision: 99, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "fut"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "fut")?.outcome).toMatchObject({
    status: "rejected",
    code: "invalid-argument",
  })

  // 合法 revision
  sendClient(ch, { type: "intent", requestId: "ok", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.some(message => message.type === "intent.outcome" && message.requestId === "ok"))
  expect(ch.sent.find(message => message.type === "intent.outcome" && message.requestId === "ok")).toEqual({
    type: "intent.outcome",
    requestId: "ok",
    outcome: { status: "accepted" },
  })
})

test("非 web-active 阶段 intent：只回 handoff.state，不发 outcome", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectRenderer(system) // 仍在 opening-web，未 ready
  const before = ch.sent.length

  sendClient(ch, { type: "intent", requestId: "early", revision: 1, intent: { type: "approval-mode.cycle" } })
  await waitFor(() => ch.sent.length > before)
  expect(lastSent(ch)).toMatchObject({ type: "handoff.state", state: { phase: "opening-web" } })
  expect(ch.sent.some(message => message.type === "intent.outcome")).toBe(false)
})

test("presentation-intent：无害 no-op，不产生任何帧", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)
  const before = ch.sent.length

  sendClient(ch, { type: "presentation-intent", intent: { type: "theme.set", theme: "dark" } })
  sendClient(ch, { type: "presentation-intent", intent: { type: "panel.open", panel: "threads" } })
  await flush()
  expect(ch.sent.length).toBe(before)
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

test("gateway close()：停止 controller 订阅与帧受理", async () => {
  const system = createSystem()
  await flush()
  const { ch } = await connectAndReady(system)
  await system.gateway.close()
  const before = ch.sent.length

  // 客户端帧不再受理
  sendClient(ch, { type: "intent", requestId: "late", revision: 1, intent: { type: "approval-mode.cycle" } })
  await flush()
  expect(ch.sent.length).toBe(before)

  // controller 发布不再产生 patch
  await system.controller.dispatch({ type: "approval-mode.cycle" })
  await flush()
  expect(ch.sent.length).toBe(before)
})
