/** PresentationCoordinator 状态机测试：生命周期、token 校验、单 renderer 门禁与收敛。 */

import { expect, test } from "bun:test"

import { AsyncQueue } from "../../src/ipc/transport"
import {
  createPresentationCoordinator,
  type GatewayChannel,
  type PresentationCoordinator,
  type PresentationScheduler,
  type PresentationServer,
} from "../../src/presentation-coordinator/coordinator"
import type { WebUiServerMessage } from "../../src/presentation-coordinator/contracts/messages"
import type { PresentationState } from "../../src/presentation-coordinator/state"
import type { InteractiveIntent, IntentOutcome } from "../../src/interactive/types"

// ---- fakes -----------------------------------------------------------------

type TimerEntry = { callback: () => void; at: number; active: boolean }

/** 手动 PresentationScheduler：测试直接推进时钟并触发到期回调。 */
function createManualScheduler(initialNow = 1_000) {
  let now = initialNow
  const timers: TimerEntry[] = []
  const scheduler: PresentationScheduler = {
    set: (callback, ms) => {
      const entry: TimerEntry = { callback, at: now + ms, active: true }
      timers.push(entry)
      return { clear: () => { entry.active = false } }
    },
    now: () => now,
  }
  return {
    scheduler,
    timers,
    advance(ms: number): void {
      now += ms
      for (const entry of [...timers]) {
        if (entry.active && entry.at <= now) {
          entry.active = false
          entry.callback()
        }
      }
    },
  }
}

function createFakeServer() {
  const record = { startCalls: 0, stopCalls: 0 }
  const server: PresentationServer = {
    origin: "http://127.0.0.1:1",
    pathFor: handoffId => `/web/h/${handoffId}`,
    start: async () => { record.startCalls += 1 },
    stop: async () => { record.stopCalls += 1 },
  }
  return { server, record }
}

function createFakeChannel() {
  const queue = new AsyncQueue<unknown>()
  const sent: WebUiServerMessage[] = []
  const closed: Array<{ code: number; reason: string }> = []
  const channel: GatewayChannel = {
    messages: queue,
    send: async message => { sent.push(message) },
    close: async (code, reason) => { closed.push({ code, reason }); queue.end() },
  }
  return { channel, sent, closed }
}

function createCoordinator(options: {
  readyTimeoutMs?: number
  reconnectGraceMs?: number
  uiTokenTtlMs?: number
  dispatch?: (intent: InteractiveIntent) => Promise<IntentOutcome>
} = {}) {
  const manual = createManualScheduler()
  const { server, record } = createFakeServer()
  const openedUrls: string[] = []
  const connectedChannels: GatewayChannel[] = []
  const dispatchCalls: InteractiveIntent[] = []
  const dispatch = options.dispatch ?? (async (intent: InteractiveIntent) => {
    dispatchCalls.push(intent)
    return { status: "accepted" }
  })
  const coordinator = createPresentationCoordinator({
    server,
    openBrowser: async url => { openedUrls.push(url) },
    dispatch,
    onRendererConnected: channel => { connectedChannels.push(channel) },
    readyTimeoutMs: options.readyTimeoutMs ?? 65_000,
    reconnectGraceMs: options.reconnectGraceMs ?? 10_000,
    uiTokenTtlMs: options.uiTokenTtlMs ?? 60_000,
    scheduler: manual.scheduler,
  })
  return { coordinator, manual, server, record, openedUrls, connectedChannels, dispatchCalls }
}

async function openAndExtract(coordinator: PresentationCoordinator, openedUrls: string[]) {
  await coordinator.open()
  const opening = coordinator.getSnapshot()
  if (opening.phase !== "opening-web") throw new Error("expected opening-web")
  const url = new URL(openedUrls[0]!)
  const token = url.hash.startsWith("#ui=") ? url.hash.slice("#ui=".length) : ""
  return { handoffId: opening.handoffId, token, url }
}

function statesOf(coordinator: PresentationCoordinator): PresentationState[] {
  const states: PresentationState[] = []
  coordinator.subscribe(state => states.push(state))
  return states
}

async function flush(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0))
  await new Promise(resolve => setTimeout(resolve, 0))
}

// ---- 生命周期与 token ------------------------------------------------------

test("open：进入 opening-web，启动 server，URL 携带 UI token；二次 open 拒绝", async () => {
  const c = createCoordinator()
  const { handoffId, token, url } = await openAndExtract(c.coordinator, c.openedUrls)

  expect(c.record.startCalls).toBe(1)
  expect(c.coordinator.getSnapshot()).toEqual({ phase: "opening-web", handoffId })
  expect(token.length).toBeGreaterThan(0)
  expect(c.openedUrls[0]).toBe(`${c.server.origin}${c.server.pathFor(handoffId)}#ui=${token}`)
  expect(url.origin).toBe(c.server.origin)
  expect(c.coordinator.isHandoffActive(handoffId)).toBe(true)
  expect(c.coordinator.isHandoffActive("other-handoff")).toBe(false)
  await expect(c.coordinator.open()).rejects.toThrow("already active")
})

test("token 校验：正确 token/Origin 通过；错误 token、错误 Origin、过期、错 handoffId 拒绝", async () => {
  const c = createCoordinator()
  const { handoffId, token } = await openAndExtract(c.coordinator, c.openedUrls)
  const origin = c.server.origin

  expect(c.coordinator.consumeUiToken(handoffId, token, origin)).toBe(true)
  expect(c.coordinator.consumeUiToken(handoffId, "wrong-token", origin)).toBe(false)
  expect(c.coordinator.consumeUiToken(handoffId, token, "http://evil.example")).toBe(false)
  expect(c.coordinator.consumeUiToken("other-handoff", token, origin)).toBe(false)

  // TTL 60s：过期后即使 token/Origin 正确也拒绝
  c.manual.advance(60_001)
  expect(c.coordinator.consumeUiToken(handoffId, token, origin)).toBe(false)
})

test("token 校验：web-active 阶段有效；收敛回 tui-active 后拒绝", async () => {
  const c = createCoordinator()
  const { handoffId, token } = await openAndExtract(c.coordinator, c.openedUrls)
  const origin = c.server.origin
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()
  expect(c.coordinator.getSnapshot().phase).toBe("web-active")
  expect(c.coordinator.consumeUiToken(handoffId, token, origin)).toBe(true)

  c.coordinator.requestReturn()
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
  expect(c.coordinator.consumeUiToken(handoffId, token, origin)).toBe(false)
})

// ---- renderer 门禁 ---------------------------------------------------------

test("attachRenderer：合法 channel 交接给 onRendererConnected；第二个并发连接拒绝并关闭", async () => {
  const c = createCoordinator()
  const { handoffId, token } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch1 = createFakeChannel()
  const ch2 = createFakeChannel()

  await c.coordinator.attachRenderer(handoffId, ch1.channel)
  expect(c.connectedChannels).toEqual([ch1.channel])

  await c.coordinator.attachRenderer(handoffId, ch2.channel)
  expect(ch2.sent).toEqual([{ type: "handoff.state", state: { phase: "opening-web", handoffId } }])
  expect(ch2.closed).toEqual([{ code: 1008, reason: "already-open" }])
  expect(c.connectedChannels).toHaveLength(1)
  // token 不因已有连接而失效：门禁在 attachRenderer 层，同页重连仍可换 channel
  expect(c.coordinator.consumeUiToken(handoffId, token, c.server.origin)).toBe(true)
})

test("attachRenderer：web-active 时失效主连接可被同 handoff 新连接替换接管", async () => {
  const c = createCoordinator()
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch1 = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch1.channel)
  c.coordinator.requestReady()
  expect(c.coordinator.getSnapshot().phase).toBe("web-active")

  // 活跃主连接：第二窗口仍被拒绝（单窗口 invariant 不回归）。
  const chLive = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, chLive.channel)
  expect(chLive.closed).toEqual([{ code: 1008, reason: "already-open" }])
  expect(c.connectedChannels).toHaveLength(1)

  // 主连接失效（模拟整页重载重连：旧 socket 关闭事件尚未驱动 consume 清理，
  // primary 仍指向已关闭的连接）：新连接替换接管，旧连接被收敛关闭。
  ;(ch1.channel as GatewayChannel & { isOpen?: () => boolean }).isOpen = () => false
  const ch2 = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch2.channel)
  expect(ch1.closed).toContainEqual({ code: 1001, reason: "superseded" })
  expect(ch2.closed).toHaveLength(0)
  expect(c.connectedChannels).toEqual([ch1.channel, ch2.channel])
})

test("attachRenderer：错误 handoffId 拒绝（invalid-handoff）", async () => {
  const c = createCoordinator()
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const wrong = createFakeChannel()
  await c.coordinator.attachRenderer("nope", wrong.channel)
  expect(wrong.sent).toEqual([{ type: "handoff.state", state: { phase: "opening-web", handoffId } }])
  expect(wrong.closed).toEqual([{ code: 1008, reason: "invalid-handoff" }])
  expect(c.connectedChannels).toHaveLength(0)
})

test("attachRenderer：returning-tui 过渡态拒绝（returning）", async () => {
  const c = createCoordinator()
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()

  const late = createFakeChannel()
  const lateSent: WebUiServerMessage[][] = []
  const lateClosed: Array<{ code: number; reason: string }>[] = []
  const unsub = c.coordinator.subscribe(state => {
    if (state.phase === "returning-tui") {
      void c.coordinator.attachRenderer(handoffId, late.channel).then(() => {
        lateSent.push([...late.sent])
        lateClosed.push([...late.closed])
      })
    }
  })
  c.coordinator.requestReturn()
  await flush()
  unsub()

  expect(lateSent).toEqual([[{ type: "handoff.state", state: { phase: "returning-tui", handoffId, reason: "returned" } }]])
  expect(lateClosed).toEqual([[{ code: 1008, reason: "returning" }]])
})

// ---- 状态迁移 --------------------------------------------------------------

test("requestReady：opening-web → web-active 并通知订阅者", async () => {
  const c = createCoordinator()
  const states = statesOf(c.coordinator)
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)

  c.coordinator.requestReady()
  expect(c.coordinator.getSnapshot()).toEqual({ phase: "web-active", handoffId })
  expect(states.some(state => state.phase === "web-active")).toBe(true)
})

test("requestReady 在非 opening-web 阶段视为协议违规 → fail-closed 收敛", async () => {
  const c = createCoordinator()
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()

  c.coordinator.requestReady()
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
})

test("requestReturn：returning-tui（returned）→ tui-active", async () => {
  const c = createCoordinator()
  const states = statesOf(c.coordinator)
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()

  c.coordinator.requestReturn()
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
  expect(states.some(state => state.phase === "returning-tui" && state.reason === "returned")).toBe(true)
  expect(c.coordinator.isHandoffActive(handoffId)).toBe(false)
})

test("requestExit：returning-tui（exit-requested）→ tui-active 并触发 exit handler", async () => {
  const c = createCoordinator()
  const states = statesOf(c.coordinator)
  let exitCalls = 0
  c.coordinator.registerExitHandler(() => { exitCalls += 1 })
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()

  c.coordinator.requestExit()
  expect(exitCalls).toBe(1)
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
  expect(states.some(state => state.phase === "returning-tui" && state.reason === "exit-requested")).toBe(true)
})

test("ready-timeout：ready 定时器到期 → returning-tui（ready-timeout）→ tui-active", async () => {
  const c = createCoordinator()
  const states = statesOf(c.coordinator)
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  expect(c.manual.timers.length).toBe(1) // open() 已 arm ready 定时器

  c.manual.advance(65_000)
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
  expect(c.coordinator.isHandoffActive(handoffId)).toBe(false)
  expect(states.some(state => state.phase === "returning-tui" && state.reason === "ready-timeout")).toBe(true)
  // 收敛不停止 server；停止只发生在 close()
  expect(c.record.stopCalls).toBe(0)
})

test("web-active 断开：宽限期内重连保持 web-active；宽限期到期才收敛", async () => {
  const c = createCoordinator()
  const states = statesOf(c.coordinator)
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch1 = createFakeChannel()
  const ch2 = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch1.channel)
  c.coordinator.requestReady()

  c.coordinator.notifyRendererDisconnected()
  expect(c.coordinator.getSnapshot().phase).toBe("web-active")
  expect(c.manual.timers.some(timer => timer.active)).toBe(true) // 宽限定时器已 arm

  // 宽限期内同页重连：换 primary 并清除宽限定时器
  await c.coordinator.attachRenderer(handoffId, ch2.channel)
  expect(c.connectedChannels).toHaveLength(2)
  expect(c.coordinator.getSnapshot().phase).toBe("web-active")
  expect(c.manual.timers.every(timer => !timer.active)).toBe(true)

  // 再次断开，宽限期到期后收敛
  c.coordinator.notifyRendererDisconnected()
  c.manual.advance(10_000)
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
  expect(states.some(state => state.phase === "returning-tui" && state.reason === "browser-close")).toBe(true)
})

test("opening-web 阶段断开 → 立即收敛（无宽限期）", async () => {
  const c = createCoordinator()
  await openAndExtract(c.coordinator, c.openedUrls)
  c.coordinator.notifyRendererDisconnected()
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
})

test("notifyInvalidMessage：畸形帧协议违规 → fail-closed 收敛", async () => {
  const c = createCoordinator()
  const states = statesOf(c.coordinator)
  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()

  c.coordinator.notifyInvalidMessage()
  expect(c.coordinator.getSnapshot().phase).toBe("tui-active")
  expect(states.some(state => state.phase === "returning-tui" && state.reason === "invalid-message")).toBe(true)
})

// ---- 输入租约与关闭 --------------------------------------------------------

test("tuiDispatch：tui-active 受理；web-active 拒绝 busy；不进入 dispatch", async () => {
  const c = createCoordinator()
  const outcome = await c.coordinator.tuiDispatch({ type: "run.cancel" })
  expect(outcome).toEqual({ status: "accepted" })
  expect(c.dispatchCalls).toEqual([{ type: "run.cancel" }])

  const { handoffId } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()

  const busy = await c.coordinator.tuiDispatch({ type: "run.cancel" })
  expect(busy.status).toBe("rejected")
  expect(busy.code).toBe("busy")
  expect(c.dispatchCalls).toHaveLength(1)
})

test("close()：停止 server、幂等、不触发 exit handler、token 失效", async () => {
  const c = createCoordinator()
  let exitCalls = 0
  c.coordinator.registerExitHandler(() => { exitCalls += 1 })
  const { handoffId, token } = await openAndExtract(c.coordinator, c.openedUrls)
  const ch = createFakeChannel()
  await c.coordinator.attachRenderer(handoffId, ch.channel)
  c.coordinator.requestReady()

  await c.coordinator.close()
  expect(c.record.stopCalls).toBe(1)
  expect(exitCalls).toBe(0)
  expect(c.coordinator.consumeUiToken(handoffId, token, c.server.origin)).toBe(false)

  await c.coordinator.close()
  expect(c.record.stopCalls).toBe(1)
})

test("close()：无会话时也能收敛且不抛错", async () => {
  const c = createCoordinator()
  await c.coordinator.close()
  expect(c.record.stopCalls).toBe(1)
})
