/** WebHandoffCoordinator 状态机测试：fake Host、lifecycle channel 与 scheduler。 */

import { expect, test } from "bun:test"

import type {
  ControlStatus,
  HostAttachmentCreateResult,
  HostAttachmentRevokeResult,
} from "@za38/protocol"

import { AsyncQueue } from "../../src/ipc/transport"
import {
  createWebHandoffCoordinator,
  parseLifecycleMessage,
  type LifecycleBrowserMessage,
  type LifecycleChannel,
  type LifecycleServerMessage,
  type WebBrowserOpener,
  type WebHandoffCoordinator,
  type WebHandoffServer,
  type WebHandoffSnapshot,
  type WebHostControl,
  type WebScheduler,
  type WebTimer,
} from "../../src/web/handoff-coordinator"

class FakeHost implements WebHostControl {
  readonly created: HostAttachmentCreateResult[] = []
  readonly revoked: string[] = []
  status: ControlStatus = {
    state: "owner",
    holder: { connection_id: "owner", role: "owner", attachment_id: null },
  }
  failRevoke = false
  statusCalls = 0

  async createAttachment(origin: string): Promise<HostAttachmentCreateResult> {
    const result = {
      attachment_id: `att-${this.created.length + 1}`,
      endpoint: "ws://127.0.0.1:1",
      token: `token-${this.created.length + 1}`,
      expires_at_ms: 0,
    }
    this.created.push(result)
    return result
  }

  async revokeAttachment(id: string): Promise<HostAttachmentRevokeResult> {
    if (this.failRevoke) throw new Error("revoke failed")
    this.revoked.push(id)
    this.status = {
      state: "owner",
      holder: { connection_id: "owner", role: "owner", attachment_id: null },
    }
    return { attachment_id: id, revoked: true, control: this.status }
  }

  async controlStatus(): Promise<ControlStatus> {
    this.statusCalls += 1
    return this.status
  }
}

class FakeServer implements WebHandoffServer {
  readonly origin = "http://127.0.0.1:8123"
  started = 0
  stopped = 0

  async start(): Promise<void> {
    this.started += 1
  }

  async stop(): Promise<void> {
    this.stopped += 1
  }

  pathFor(handoffId: string): string {
    return `/web/h/${handoffId}`
  }
}

class FakeChannel implements LifecycleChannel {
  readonly messages = new AsyncQueue<unknown>()
  readonly sent: LifecycleServerMessage[] = []
  closed: { code: number; reason: string } | undefined

  async send(message: LifecycleServerMessage): Promise<void> {
    this.sent.push(message)
  }

  async close(code: number, reason: string): Promise<void> {
    this.closed = { code, reason }
    this.messages.end()
  }
}

class FakeScheduler implements WebScheduler {
  private readonly timers: Array<{ callback: () => void; cleared: boolean }> = []
  nowValue = 1_000

  set(callback: () => void, _ms: number): WebTimer {
    const timer = { callback, cleared: false }
    this.timers.push(timer)
    return { clear: () => { timer.cleared = true } }
  }

  now(): number {
    // owner 轮询 deadline 依赖真实时间推进；timer 触发仍由 fire() 控制。
    return Date.now()
  }

  fire(): void {
    for (const timer of this.timers) {
      if (!timer.cleared) timer.callback()
    }
  }

  advance(ms: number): void {
    this.nowValue += ms
  }
}

type Harness = {
  coordinator: WebHandoffCoordinator
  host: FakeHost
  server: FakeServer
  scheduler: FakeScheduler
  openedUrls: string[]
  snapshots: WebHandoffSnapshot[]
  unsubscribe: () => void
}

function createHarness(
  overrides: {
    readyTimeoutMs?: number
    ownerPollMs?: number
    ownerWaitMs?: number
  } = {},
): Harness {
  const host = new FakeHost()
  const server = new FakeServer()
  const scheduler = new FakeScheduler()
  const openedUrls: string[] = []
  const openBrowser: WebBrowserOpener = async url => { openedUrls.push(url) }
  const coordinator = createWebHandoffCoordinator({
    host,
    server,
    openBrowser,
    scheduler,
    readyTimeoutMs: overrides.readyTimeoutMs ?? 65_000,
    ownerPollMs: overrides.ownerPollMs ?? 1,
    ownerWaitMs: overrides.ownerWaitMs ?? 200,
  })
  const snapshots: WebHandoffSnapshot[] = []
  const unsubscribe = coordinator.subscribe(snapshot => snapshots.push(snapshot))
  return { coordinator, host, server, scheduler, openedUrls, snapshots, unsubscribe }
}

async function waitFor(
  predicate: () => boolean,
  timeoutMs = 1_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 1))
  }
}

async function openAndAccept(h: Harness): Promise<{ channel: FakeChannel; handoffId: string }> {
  await h.coordinator.open("thread-1")
  const opening = h.coordinator.getSnapshot()
  if (opening.phase !== "opening") throw new Error("expected opening")
  const channel = new FakeChannel()
  await h.coordinator.attachLifecycle(opening.handoffId, channel)
  expect(channel.sent[0]).toEqual({ type: "accepted" })
  return { channel, handoffId: opening.handoffId }
}

async function activate(h: Harness, channel: FakeChannel, attachmentId: string): Promise<void> {
  h.host.status = {
    state: "attached",
    holder: { connection_id: "web-1", role: "attached", attachment_id: attachmentId },
  }
  channel.messages.push(JSON.stringify({ type: "ready" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "active")
}

test("初始 snapshot 是 idle，且不携带凭据", () => {
  const h = createHarness()
  const snapshot = h.coordinator.getSnapshot()
  expect(snapshot).toEqual({
    phase: "idle",
    tuiLocked: false,
    restoreThreadId: null,
    handoffVersion: 0,
  })
  h.unsubscribe()
})

test("open 进入 opening；重复 open 被拒绝且不创建第二个 attachment", async () => {
  const h = createHarness()
  await h.coordinator.open(null)
  expect(h.coordinator.getSnapshot().phase).toBe("opening")
  await expect(h.coordinator.open("thread-2")).rejects.toThrow("already active")
  expect(h.host.created).toHaveLength(1)
  expect(h.server.started).toBe(1)
  expect(h.openedUrls).toHaveLength(1)
  expect(h.openedUrls[0]).toContain(`/web/h/${h.coordinator.getSnapshot().handoffId}`)
  expect(h.openedUrls[0]).toContain("token=")
  expect(h.openedUrls[0]).toContain("attachment=")
  h.unsubscribe()
})

test("第一个 lifecycle 被 accepted，第二个收到 already-open 且不影响 primary", async () => {
  const h = createHarness()
  const { channel, handoffId } = await openAndAccept(h)
  const second = new FakeChannel()
  await h.coordinator.attachLifecycle(handoffId, second)
  expect(second.sent[0]).toEqual({ type: "shutdown", reason: "already-open" })
  expect(second.closed?.code).toBe(1008)

  // primary 仍可完成 ready → active。
  await activate(h, channel, h.host.created[0].attachment_id)
  expect(h.coordinator.getSnapshot().phase).toBe("active")
  h.unsubscribe()
})

test("ready 但 Host status 不是 matching attachment 时进入 cleanup", async () => {
  const h = createHarness()
  const { channel } = await openAndAccept(h)
  channel.messages.push(JSON.stringify({ type: "ready" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  expect(h.host.revoked).toHaveLength(1)
  const snapshot = h.coordinator.getSnapshot()
  if (snapshot.phase !== "idle") throw new Error("expected idle")
  expect(snapshot.handoffVersion).toBe(1)
  expect(snapshot.restoreThreadId).toBe("thread-1")
  h.unsubscribe()
})

test("matching holder 后进入 active/tuiLocked，thread.changed 可切换 null", async () => {
  const h = createHarness()
  const { channel } = await openAndAccept(h)
  await activate(h, channel, h.host.created[0].attachment_id)
  const active = h.coordinator.getSnapshot()
  if (active.phase !== "active") throw new Error("expected active")
  expect(active.tuiLocked).toBe(true)
  expect(active.threadId).toBe("thread-1")

  channel.messages.push(JSON.stringify({ type: "thread.changed", thread_id: "thread-9" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "active"
    && h.coordinator.getSnapshot().threadId === "thread-9")
  channel.messages.push(JSON.stringify({ type: "thread.changed", thread_id: null }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "active"
    && h.coordinator.getSnapshot().threadId === null)

  channel.messages.push(JSON.stringify({ type: "released" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  const idle = h.coordinator.getSnapshot()
  if (idle.phase !== "idle") throw new Error("expected idle")
  expect(idle.restoreThreadId).toBeNull()
  expect(idle.handoffVersion).toBe(1)
  h.unsubscribe()
})

test("重复 ready 幂等忽略，不会触发第二次状态转换", async () => {
  const h = createHarness()
  const { channel } = await openAndAccept(h)
  await activate(h, channel, h.host.created[0].attachment_id)
  channel.messages.push(JSON.stringify({ type: "ready" }))
  await new Promise(resolve => setTimeout(resolve, 5))
  const snapshot = h.coordinator.getSnapshot()
  expect(snapshot.phase).toBe("active")
  expect(h.host.revoked).toHaveLength(0)
  h.unsubscribe()
})

test("release 后 restoreThreadId 保留最终值且 handoffVersion 只递增一次", async () => {
  const h = createHarness()
  const { channel } = await openAndAccept(h)
  await activate(h, channel, h.host.created[0].attachment_id)
  channel.messages.push(JSON.stringify({ type: "thread.changed", thread_id: "thread-2" }))
  await waitFor(() => h.coordinator.getSnapshot().threadId === "thread-2")
  channel.messages.push(JSON.stringify({ type: "released" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  const idle = h.coordinator.getSnapshot()
  if (idle.phase !== "idle") throw new Error("expected idle")
  expect(idle.restoreThreadId).toBe("thread-2")
  expect(idle.handoffVersion).toBe(1)
  expect(h.host.revoked).toEqual([h.host.created[0].attachment_id])
  h.unsubscribe()
})

test("ready timeout 进入统一 cleanup，attachment 只撤销一次", async () => {
  const h = createHarness({ readyTimeoutMs: 5_000 })
  await h.coordinator.open(null)
  h.scheduler.fire()
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  expect(h.host.revoked).toHaveLength(1)
  expect(h.coordinator.getSnapshot().handoffVersion).toBe(1)
  h.unsubscribe()
})

test("畸形 lifecycle 帧进入 invalid-message cleanup", async () => {
  const h = createHarness()
  const { channel } = await openAndAccept(h)
  channel.messages.push(JSON.stringify({ type: "bogus", extra: true }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  expect(h.host.revoked).toHaveLength(1)
  const shutdown = channel.sent.find(message => message.type === "shutdown")
  expect(shutdown).toEqual({ type: "shutdown", reason: "invalid-message" })
  h.unsubscribe()
})

test("primary 在 opening 阶段断开进入 browser-close cleanup", async () => {
  const h = createHarness()
  const { channel } = await openAndAccept(h)
  channel.messages.end()
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  expect(h.host.revoked).toHaveLength(1)
  h.unsubscribe()
})

test("CLI close 走 cli-exit cleanup，重复 close 幂等且停止 server", async () => {
  const h = createHarness()
  await h.coordinator.open("thread-1")
  await h.coordinator.close()
  expect(h.server.stopped).toBe(1)
  expect(h.host.revoked).toHaveLength(1)
  expect(h.coordinator.getSnapshot().phase).toBe("idle")
  await h.coordinator.close()
  expect(h.server.stopped).toBe(1)
  await expect(h.coordinator.open("thread-2")).rejects.toThrow("closed")
  h.unsubscribe()
})

test("owner status 未确认时保持 returning 锁定，确认后才 idle", async () => {
  const h = createHarness({ ownerPollMs: 1, ownerWaitMs: 20 })
  const { channel, handoffId } = await openAndAccept(h)
  await activate(h, channel, h.host.created[0].attachment_id)
  // revoke 后仍保持 attached：cleanup 轮询也无法确认 owner。
  h.host.revokeAttachment = async (id: string) => {
    h.host.revoked.push(id)
    return {
      attachment_id: id,
      revoked: true,
      control: {
        state: "attached",
        holder: { connection_id: "web-1", role: "attached", attachment_id: id },
      },
    }
  }
  channel.messages.push(JSON.stringify({ type: "released" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "returning"
    && h.host.statusCalls > 3)
  // returning 期间新的 lifecycle channel 被拒绝，不影响正在进行的 cleanup。
  const late = new FakeChannel()
  await h.coordinator.attachLifecycle(handoffId, late)
  expect(late.sent[0]).toEqual({ type: "shutdown", reason: "returning" })
  await new Promise(resolve => setTimeout(resolve, 30))
  const snapshot = h.coordinator.getSnapshot()
  expect(snapshot.phase).toBe("returning")
  if (snapshot.phase === "returning") {
    expect(snapshot.tuiLocked).toBe(true)
    expect(snapshot.reason).toBe("released")
  }
  h.unsubscribe()
})

test("owner 在轮询期间恢复后进入 idle 并递增 handoffVersion", async () => {
  const h = createHarness({ ownerPollMs: 1, ownerWaitMs: 200 })
  const { channel } = await openAndAccept(h)
  await activate(h, channel, h.host.created[0].attachment_id)
  // revoke 后仍保持 attached，前几次 status 查询无法确认 owner。
  h.host.revokeAttachment = async (id: string) => {
    h.host.revoked.push(id)
    return {
      attachment_id: id,
      revoked: true,
      control: {
        state: "attached",
        holder: { connection_id: "web-1", role: "attached", attachment_id: id },
      },
    }
  }
  let statusCalls = 0
  h.host.controlStatus = async () => {
    statusCalls += 1
    return statusCalls >= 2
      ? {
          state: "owner",
          holder: { connection_id: "owner", role: "owner", attachment_id: null },
        }
      : h.host.status
  }
  channel.messages.push(JSON.stringify({ type: "released" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  expect(statusCalls).toBeGreaterThan(1)
  expect(h.coordinator.getSnapshot().handoffVersion).toBe(1)
  h.unsubscribe()
})

test("未知 handoff 的 channel 收到 invalid-handoff", async () => {
  const h = createHarness()
  await h.coordinator.open("thread-1")
  const unknown = new FakeChannel()
  await h.coordinator.attachLifecycle("not-a-handoff", unknown)
  expect(unknown.sent[0]).toEqual({ type: "shutdown", reason: "invalid-handoff" })
  expect(unknown.closed?.code).toBe(1008)
  h.unsubscribe()
})

test("parseLifecycleMessage 只接受精确的合法消息", () => {
  expect(parseLifecycleMessage({ type: "ready" })).toBeUndefined()
  expect(parseLifecycleMessage("not json")).toBeUndefined()
  expect(parseLifecycleMessage(JSON.stringify({ type: "ready" }))).toEqual({ type: "ready" })
  expect(parseLifecycleMessage(JSON.stringify({ type: "ready", extra: 1 }))).toBeUndefined()
  expect(parseLifecycleMessage(JSON.stringify({ type: "released" }))).toEqual({ type: "released" })
  expect(parseLifecycleMessage(JSON.stringify({ type: "thread.changed", thread_id: null })))
    .toEqual({ type: "thread.changed", thread_id: null })
  expect(parseLifecycleMessage(JSON.stringify({ type: "thread.changed", thread_id: "t" })))
    .toEqual({ type: "thread.changed", thread_id: "t" })
  expect(parseLifecycleMessage(JSON.stringify({ type: "thread.changed", thread_id: "" }))).toBeUndefined()
  expect(parseLifecycleMessage(JSON.stringify({ type: "thread.changed", thread_id: "x".repeat(257) }))).toBeUndefined()
  expect(parseLifecycleMessage(JSON.stringify({ type: "thread.changed" }))).toBeUndefined()
  expect(parseLifecycleMessage(JSON.stringify({ type: "unknown" }))).toBeUndefined()
  expect(parseLifecycleMessage(JSON.stringify({ type: "ready" }).repeat(100))).toBeUndefined()
  const oversized = "x".repeat(17 * 1024)
  expect(parseLifecycleMessage(oversized)).toBeUndefined()
})

test("opener 失败会撤销 attachment 并允许下一次 open", async () => {
  const host = new FakeHost()
  const server = new FakeServer()
  let fail = true
  const coordinator = createWebHandoffCoordinator({
    host,
    server,
    openBrowser: async () => {
      if (fail) throw new Error("browser launch failed")
    },
    ownerPollMs: 1,
    ownerWaitMs: 100,
  })
  await expect(coordinator.open("thread-1")).rejects.toThrow("browser launch failed")
  await waitFor(() => coordinator.getSnapshot().phase === "idle")
  expect(host.revoked).toHaveLength(1)
  fail = false
  await coordinator.open("thread-1")
  expect(coordinator.getSnapshot().phase).toBe("opening")
  await coordinator.close()
})

test("open 与 close 并发时，close 后创建的 attachment 会被撤销且不启动浏览器", async () => {
  const host = new FakeHost()
  const server = new FakeServer()
  let releaseCreate: () => void = () => undefined
  const createGate = new Promise<void>(resolve => { releaseCreate = resolve })
  const originalCreate = host.createAttachment.bind(host)
  host.createAttachment = async (origin: string) => {
    await createGate
    return originalCreate(origin)
  }
  let opened = false
  const coordinator = createWebHandoffCoordinator({
    host,
    server,
    openBrowser: async () => { opened = true },
    ownerPollMs: 1,
    ownerWaitMs: 50,
  })
  const opening = coordinator.open("thread-1")
  await waitFor(() => coordinator.getSnapshot().phase === "opening")
  await coordinator.close()
  releaseCreate()
  await opening
  expect(opened).toBe(false)
  expect(host.revoked).toEqual(["att-1"])
  // close 是 CLI 终态：不执行局部 owner 恢复，phase 停留在 returning。
  expect(coordinator.getSnapshot().phase).toBe("returning")
})

test("openBrowser 挂起期间完成 active 时，返回后不再 arm ready timer", async () => {
  const h = createHarness()
  let releaseOpen: () => void = () => undefined
  const openGate = new Promise<void>(resolve => { releaseOpen = resolve })
  const originalOpen = h.coordinator.open.bind(h.coordinator)
  const coordinator = createWebHandoffCoordinator({
    host: h.host,
    server: h.server,
    openBrowser: async () => { await openGate },
    scheduler: h.scheduler,
    ownerPollMs: 1,
    ownerWaitMs: 50,
  })
  const opening = coordinator.open("thread-1")
  await waitFor(() => coordinator.getSnapshot().phase === "opening")
  const channel = new FakeChannel()
  const snapshot = coordinator.getSnapshot()
  if (snapshot.phase !== "opening") throw new Error("expected opening")
  await coordinator.attachLifecycle(snapshot.handoffId, channel)
  h.host.status = {
    state: "attached",
    holder: { connection_id: "web-1", role: "attached", attachment_id: "att-1" },
  }
  channel.messages.push(JSON.stringify({ type: "ready" }))
  await waitFor(() => coordinator.getSnapshot().phase === "active")
  releaseOpen()
  await opening
  expect(coordinator.getSnapshot().phase).toBe("active")
  h.scheduler.fire()
  expect(coordinator.getSnapshot().phase).toBe("active")
  h.unsubscribe()
})

test("attachLifecycle 的 accepted send 失败进入 cleanup，不残留 primary", async () => {
  const h = createHarness()
  await h.coordinator.open("thread-1")
  const snapshot = h.coordinator.getSnapshot()
  if (snapshot.phase !== "opening") throw new Error("expected opening")
  const broken = new FakeChannel()
  broken.send = async () => { throw new Error("socket died") }
  await h.coordinator.attachLifecycle(snapshot.handoffId, broken)
  await waitFor(() => h.coordinator.getSnapshot().phase === "idle")
  expect(h.host.revoked).toHaveLength(1)
  h.unsubscribe()
})

test("owner 超时后仍保持 returning，后台轮询在 owner 恢复后回到 idle", async () => {
  // 后台轮询依赖真实 timer，这里不注入 fake scheduler。
  const host = new FakeHost()
  const server = new FakeServer()
  const coordinator = createWebHandoffCoordinator({
    host,
    server,
    openBrowser: async () => undefined,
    ownerPollMs: 1,
    ownerWaitMs: 5,
  })
  await coordinator.open("thread-1")
  const opening = coordinator.getSnapshot()
  if (opening.phase !== "opening") throw new Error("expected opening")
  const channel = new FakeChannel()
  await coordinator.attachLifecycle(opening.handoffId, channel)
  host.status = {
    state: "attached",
    holder: { connection_id: "web-1", role: "attached", attachment_id: "att-1" },
  }
  channel.messages.push(JSON.stringify({ type: "ready" }))
  await waitFor(() => coordinator.getSnapshot().phase === "active")
  host.revokeAttachment = async (id: string) => {
    host.revoked.push(id)
    return {
      attachment_id: id,
      revoked: true,
      control: {
        state: "attached",
        holder: { connection_id: "web-1", role: "attached", attachment_id: id },
      },
    }
  }
  channel.messages.push(JSON.stringify({ type: "released" }))
  await waitFor(() => coordinator.getSnapshot().phase === "returning")
  await new Promise(resolve => setTimeout(resolve, 20))
  expect(coordinator.getSnapshot().phase).toBe("returning")
  host.status = {
    state: "owner",
    holder: { connection_id: "owner", role: "owner", attachment_id: null },
  }
  await waitFor(() => coordinator.getSnapshot().phase === "idle", 2_000)
  expect(coordinator.getSnapshot().handoffVersion).toBe(1)
  await coordinator.close()
})

test("close 在 owner 轮询期间立即收敛，不等待 ownerWaitMs 上限", async () => {
  const h = createHarness({ ownerPollMs: 5, ownerWaitMs: 60_000 })
  const { channel } = await openAndAccept(h)
  await activate(h, channel, h.host.created[0].attachment_id)
  h.host.revokeAttachment = async (id: string) => {
    h.host.revoked.push(id)
    return {
      attachment_id: id,
      revoked: true,
      control: {
        state: "attached",
        holder: { connection_id: "web-1", role: "attached", attachment_id: id },
      },
    }
  }
  channel.messages.push(JSON.stringify({ type: "released" }))
  await waitFor(() => h.coordinator.getSnapshot().phase === "returning")
  const started = Date.now()
  await h.coordinator.close()
  expect(Date.now() - started).toBeLessThan(2_000)
  expect(h.server.stopped).toBe(1)
  expect(h.host.revoked).toHaveLength(1)
  h.unsubscribe()
})
