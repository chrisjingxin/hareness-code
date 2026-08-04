/** WebHandoffPort：Browser 侧窄接口的 activate/reportThread/returnToTui/requestExit/close 测试。 */

import { expect, test } from "bun:test"

import {
  createBrowserConnectionSupervisor,
  type BrowserConnectionSupervisor,
  type LifecycleSocket,
} from "../../src/web/connection-supervisor"
import type { LifecycleBrowserMessage } from "../../src/web/handoff-coordinator"
import {
  createWebHandoffPort,
  type ReleaseHostControl,
  type SendLifecycle,
  type WebHandoffPort,
} from "../../src/web/handoff-port"

class FakeSocket implements LifecycleSocket {
  readonly readyState = 1
  readonly sent: string[] = []
  closed = false
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>()

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.closed = true
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const current = this.listeners.get(type) ?? new Set()
    current.add(listener)
    this.listeners.set(type, current)
  }

  removeEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.get(type)?.delete(listener)
  }

  emit(type: string, event: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
}

type Harness = {
  port: WebHandoffPort
  supervisor: BrowserConnectionSupervisor
  sent: LifecycleBrowserMessage[]
  readonly releaseCalls: number
  release: ReleaseHostControl
  failRelease: () => void
}

function createHarness(): Harness {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const sent: LifecycleBrowserMessage[] = []
  const sendLifecycle: SendLifecycle = async message => { sent.push(message) }
  const releaseState = { calls: 0, fail: false }
  const release: ReleaseHostControl = async () => {
    releaseState.calls += 1
    if (releaseState.fail) throw new Error("release failed")
  }
  const port = createWebHandoffPort({ sendLifecycle, supervisor, release })
  return {
    port,
    supervisor,
    sent,
    get releaseCalls() { return releaseState.calls },
    release,
    failRelease: () => { releaseState.fail = true },
  }
}

function accepted(socket: FakeSocket): void {
  socket.emit("message", { data: JSON.stringify({ type: "accepted" }) })
}

function active(socket: FakeSocket): void {
  socket.emit("message", { data: JSON.stringify({ type: "active" }) })
}

async function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 1))
  }
}

test("activate 发送 ready{thread_id} 并等待 active ack", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const sent: LifecycleBrowserMessage[] = []
  const port = createWebHandoffPort({
    sendLifecycle: async message => { sent.push(message) },
    supervisor,
    release: async () => undefined,
  })
  // 模拟 lifecycle 通道：先 accepted 再 active。
  accepted(socket)
  const activation = port.activate("thread-7")
  // activate 同步发送 ready，但 await 还没回来时 supervisor 已经在等 active。
  await waitFor(() => sent.some(message => message.type === "ready"))
  expect(sent[0]).toEqual({ type: "ready", thread_id: "thread-7" })
  active(socket)
  await activation
  expect(sent).toHaveLength(1)
  port.close()
})

test("activate 携带 null thread_id 时仍发送 null 并等到 active", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const sent: LifecycleBrowserMessage[] = []
  const port = createWebHandoffPort({
    sendLifecycle: async message => { sent.push(message) },
    supervisor,
    release: async () => undefined,
  })
  accepted(socket)
  const activation = port.activate(null)
  await waitFor(() => sent.some(message => message.type === "ready"))
  expect(sent[0]).toEqual({ type: "ready", thread_id: null })
  active(socket)
  await activation
  port.close()
})

test("activate 在 close 之后调用抛错", () => {
  const h = createHarness()
  h.port.close()
  expect(() => h.port.activate("t")).toThrow("closed")
})

test("activate 在 abort 后调用抛错（supervisor 已 reject）", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const sent: LifecycleBrowserMessage[] = []
  const port = createWebHandoffPort({
    sendLifecycle: async message => { sent.push(message) },
    supervisor,
    release: async () => undefined,
  })
  accepted(socket)
  supervisor.abort("lifecycle-closed")
  // 同步发送的 ready 帧先到达 sendLifecycle；supervisor 已 abort 之后
  // waitForActive 立刻 reject，activate 抛出但不会发送 active。
  await expect(port.activate("t")).rejects.toThrow("lifecycle-closed")
  expect(sent).toEqual([{ type: "ready", thread_id: "t" }])
  port.close()
})

test("reportThread 在 active 前被忽略；激活后发送 thread.changed", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const sent: LifecycleBrowserMessage[] = []
  const port = createWebHandoffPort({
    sendLifecycle: async message => { sent.push(message) },
    supervisor,
    release: async () => undefined,
  })
  port.reportThread("thread-x")
  expect(sent).toEqual([])
  accepted(socket)
  const activation = port.activate(null)
  await waitFor(() => sent.some(message => message.type === "ready"))
  active(socket)
  await activation
  // activate 的 ready 是 null，port 内部初始化 lastReportedThreadId=null。
  sent.length = 0
  port.reportThread("thread-1")
  await waitFor(() => sent.some(message => message.type === "thread.changed"))
  expect(sent[0]).toEqual({ type: "thread.changed", thread_id: "thread-1" })
  port.close()
})

test("reportThread 连续相同值去重 null→id→null", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const sent: LifecycleBrowserMessage[] = []
  const port = createWebHandoffPort({
    sendLifecycle: async message => { sent.push(message) },
    supervisor,
    release: async () => undefined,
  })
  accepted(socket)
  const activation = port.activate(null)
  await waitFor(() => sent.some(message => message.type === "ready"))
  active(socket)
  await activation
  sent.length = 0

  port.reportThread("thread-a")
  await waitFor(() => sent.some(message => message.type === "thread.changed"))
  expect(sent).toHaveLength(1)
  expect(sent[0]).toEqual({ type: "thread.changed", thread_id: "thread-a" })

  // 相同值连续调用：去重，不发送。
  port.reportThread("thread-a")
  port.reportThread("thread-a")
  await new Promise(resolve => setTimeout(resolve, 2))
  expect(sent).toHaveLength(1)

  // 切换到 null：null 是合法状态，不是空缺默认值。
  port.reportThread(null)
  await waitFor(() => sent.length === 2)
  expect(sent[1]).toEqual({ type: "thread.changed", thread_id: null })

  // 再次 null：去重。
  port.reportThread(null)
  await new Promise(resolve => setTimeout(resolve, 2))
  expect(sent).toHaveLength(2)

  // 再切到 id：重新发送。
  port.reportThread("thread-b")
  await waitFor(() => sent.length === 3)
  expect(sent[2]).toEqual({ type: "thread.changed", thread_id: "thread-b" })
  port.close()
})

test("reportThread 在 close 之后被忽略", () => {
  const h = createHarness()
  h.port.close()
  h.port.reportThread("thread-1")
  expect(h.sent).toEqual([])
})

test("returnToTui 成功后调用 release 并发送 released", async () => {
  const h = createHarness()
  await h.port.returnToTui()
  expect(h.releaseCalls).toBe(1)
  expect(h.sent).toEqual([{ type: "released" }])
})

test("returnToTui 在 release 失败时抛错，且不发送 released", async () => {
  const h = createHarness()
  h.failRelease()
  await expect(h.port.returnToTui()).rejects.toThrow("release failed")
  expect(h.sent).toEqual([])
  expect(h.releaseCalls).toBe(1)
})

test("returnToTui 在 close 之后调用抛错", async () => {
  const h = createHarness()
  h.port.close()
  await expect(h.port.returnToTui()).rejects.toThrow("closed")
  expect(h.sent).toEqual([])
  expect(h.releaseCalls).toBe(0)
})

test("requestExit 发送 exit.requested 帧", async () => {
  const h = createHarness()
  await h.port.requestExit()
  expect(h.sent).toEqual([{ type: "exit.requested" }])
})

test("requestExit 在 close 之后调用抛错", async () => {
  const h = createHarness()
  h.port.close()
  await expect(h.port.requestExit()).rejects.toThrow("closed")
  expect(h.sent).toEqual([])
})

test("close 幂等且不发送 released 或其它 lifecycle 帧", () => {
  const h = createHarness()
  h.port.close()
  h.port.close()
  expect(h.sent).toEqual([])
  expect(h.releaseCalls).toBe(0)
})
