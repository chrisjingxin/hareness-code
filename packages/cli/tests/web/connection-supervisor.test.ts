/** BrowserConnectionSupervisor：lifecycle 持续监督与 Agent socket 关闭收敛测试。 */

import { expect, test } from "bun:test"

import {
  createBrowserConnectionSupervisor,
  type LifecycleSocket,
} from "../../src/web/connection-supervisor"

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

function accepted(socket: FakeSocket): void {
  socket.emit("message", { data: JSON.stringify({ type: "accepted" }) })
}

function active(socket: FakeSocket): void {
  socket.emit("message", { data: JSON.stringify({ type: "active" }) })
}

test("accepted 前 close/error 拒绝 waitForAccepted 并关闭 socket", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const waiting = supervisor.waitForAccepted()
  socket.emit("close", {})
  await expect(waiting).rejects.toThrow("lifecycle-closed")
  expect(socket.closed).toBe(true)
})

test("accepted 后仍处理 shutdown，并只关闭一次已绑定的 Agent", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const waiting = supervisor.waitForAccepted()
  accepted(socket)
  await waiting
  let agentCloses = 0
  supervisor.bindAgent(() => { agentCloses += 1 })

  socket.emit("message", { data: JSON.stringify({ type: "shutdown", reason: "returning" }) })
  expect(socket.closed).toBe(true)
  expect(agentCloses).toBe(1)
  expect(supervisor.signal.aborted).toBe(true)
  // 重复 close/error 不产生第二次关闭。
  socket.emit("close", {})
  socket.emit("error", new Error("boom"))
  expect(agentCloses).toBe(1)
})

test("accepted 后直接 close 也进入 abort，且绑定晚于 abort 的 Agent 立即关闭", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const waiting = supervisor.waitForAccepted()
  accepted(socket)
  await waiting
  socket.emit("close", {})
  expect(supervisor.signal.aborted).toBe(true)
  let agentCloses = 0
  supervisor.bindAgent(() => { agentCloses += 1 })
  expect(agentCloses).toBe(1)
})

test("abort 幂等：多次 abort 只关闭一次 socket 与 Agent", () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  let agentCloses = 0
  supervisor.bindAgent(() => { agentCloses += 1 })
  supervisor.abort("pagehide")
  supervisor.abort("again")
  expect(socket.closed).toBe(true)
  expect(agentCloses).toBe(1)
  expect(supervisor.signal.aborted).toBe(true)
})

test("dispose 移除监听后不再处理消息或关闭", () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  supervisor.dispose()
  socket.emit("message", { data: JSON.stringify({ type: "shutdown", reason: "returning" }) })
  socket.emit("close", {})
  expect(socket.closed).toBe(false)
  expect(supervisor.signal.aborted).toBe(false)
})

test("accepted 前的 shutdown 拒绝等待者，随后绑定 Agent 立即关闭", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const waiting = supervisor.waitForAccepted()
  socket.emit("message", { data: JSON.stringify({ type: "shutdown", reason: "ready-timeout" }) })
  await expect(waiting).rejects.toThrow("ready-timeout")
  let agentCloses = 0
  supervisor.bindAgent(() => { agentCloses += 1 })
  expect(agentCloses).toBe(1)
})

test("waitForAccepted 在 accepted 后重复调用直接成功", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  accepted(socket)
  await supervisor.waitForAccepted()
  await supervisor.waitForAccepted()
})

test("active 在 accepted 之前到达按协议错误收敛", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const waiting = supervisor.waitForActive()
  active(socket)
  await expect(waiting).rejects.toThrow("lifecycle-invalid")
  expect(supervisor.signal.aborted).toBe(true)
})

test("active 在 accepted 之后到达，waitForActive 完成", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  accepted(socket)
  await supervisor.waitForAccepted()
  const waiting = supervisor.waitForActive()
  active(socket)
  await waiting
  // 重复 active 不影响：已经 active，后续帧幂等忽略。
  active(socket)
  await new Promise(resolve => setTimeout(resolve, 2))
  expect(supervisor.signal.aborted).toBe(false)
})

test("未知或带额外字段的 lifecycle server 帧按协议错误收敛", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const waiting = supervisor.waitForAccepted()
  socket.emit("message", { data: JSON.stringify({ type: "accepted", extra: true }) })
  await expect(waiting).rejects.toThrow("lifecycle-invalid")
  expect(supervisor.signal.aborted).toBe(true)
})

test("abort 同时拒绝 waitForAccepted 与 waitForActive", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  const acceptedWait = supervisor.waitForAccepted()
  const activeWait = supervisor.waitForActive()
  supervisor.abort("shutdown:invalid-handoff")
  await expect(acceptedWait).rejects.toThrow("invalid-handoff")
  await expect(activeWait).rejects.toThrow("invalid-handoff")
  accepted(socket)
  active(socket)
  await new Promise(resolve => setTimeout(resolve, 2))
  expect(supervisor.signal.aborted).toBe(true)
})

test("waitForActive 在 abort 之后调用立即 reject", async () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  accepted(socket)
  await supervisor.waitForAccepted()
  supervisor.abort("lifecycle-closed")
  await expect(supervisor.waitForActive()).rejects.toThrow("lifecycle-closed")
})

test("dispose 后 active 帧不再被处理", () => {
  const socket = new FakeSocket()
  const supervisor = createBrowserConnectionSupervisor(socket)
  accepted(socket)
  supervisor.dispose()
  active(socket)
  // 仍然没有 abort 路径触发，waitForActive 永久挂起。
  let resolved = false
  void supervisor.waitForActive().then(() => { resolved = true })
  return new Promise<void>(resolve => setTimeout(resolve, 5)).then(() => {
    expect(resolved).toBe(false)
  })
})
