/** Coordinator 与真实 loopback server 的端到端接线测试。 */

import { expect, test } from "bun:test"

import type {
  ControlStatus,
  HostAttachmentCreateResult,
  HostAttachmentRevokeResult,
} from "@za38/protocol"

import {
  createWebHandoffCoordinator,
  type WebHostControl,
} from "../../src/web/handoff-coordinator"
import { createWebServer } from "../../src/web/server"

class FakeHost implements WebHostControl {
  status: ControlStatus = {
    state: "owner",
    holder: { connection_id: "owner", role: "owner", attachment_id: null },
  }
  revoked: string[] = []

  async createAttachment(origin: string): Promise<HostAttachmentCreateResult> {
    return {
      attachment_id: "att-integration",
      endpoint: "ws://127.0.0.1:1",
      token: "token-integration",
      expires_at_ms: 0,
    }
  }

  async revokeAttachment(id: string): Promise<HostAttachmentRevokeResult> {
    this.revoked.push(id)
    this.status = {
      state: "owner",
      holder: { connection_id: "owner", role: "owner", attachment_id: null },
    }
    return { attachment_id: id, revoked: true, control: this.status }
  }

  async controlStatus(): Promise<ControlStatus> {
    return this.status
  }
}

async function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 2))
  }
}

test("open 后真实 server 提供 handoff 页面，lifecycle 走 accepted → ready → active → released", async () => {
  const host = new FakeHost()
  let coordinator: ReturnType<typeof createWebHandoffCoordinator> | undefined
  const server = createWebServer({
    html: "<!doctype html><title>web</title>",
    getScript: async () => "console.log('app')",
    isActiveHandoff: handoffId =>
      coordinator !== undefined
      && coordinator.getSnapshot().phase !== "idle"
      && coordinator.getSnapshot().handoffId === handoffId,
    attachLifecycle: (handoffId, channel) => coordinator!.attachLifecycle(handoffId, channel),
  })
  let openedUrl = ""
  coordinator = createWebHandoffCoordinator({
    host,
    server,
    openBrowser: async url => { openedUrl = url },
    ownerPollMs: 1,
    ownerWaitMs: 100,
  })
  await coordinator.open("thread-1")
  const opening = coordinator.getSnapshot()
  if (opening.phase !== "opening") throw new Error("expected opening")
  expect(openedUrl).toContain(server.pathFor(opening.handoffId))
  // Bun 页面与 Python attachment 是两个独立随机端口；打开的 URL 端口不同也合法。
  const pageUrl = new URL(openedUrl)
  expect(pageUrl.port).toBe(new URL(server.origin).port)
  expect(new URL("ws://127.0.0.1:1").port).not.toBe(pageUrl.port)

  const page = await fetch(`${server.origin}${server.pathFor(opening.handoffId)}`)
  expect(page.status).toBe(200)

  const socket = new WebSocket(
    `${server.origin.replace(/^http/, "ws")}${server.pathFor(opening.handoffId)}/lifecycle`,
    { headers: { origin: server.origin } },
  )
  const messages: unknown[] = []
  socket.onmessage = event => messages.push(JSON.parse(String(event.data)))
  await waitFor(() => messages.some(message => (message as { type?: string }).type === "accepted"))

  host.status = {
    state: "attached",
    holder: {
      connection_id: "web-1",
      role: "attached",
      attachment_id: "att-integration",
    },
  }
  socket.send(JSON.stringify({ type: "ready" }))
  await waitFor(() => coordinator!.getSnapshot().phase === "active")
  const active = coordinator!.getSnapshot()
  if (active.phase !== "active") throw new Error("expected active")
  expect(active.tuiLocked).toBe(true)
  expect(active.threadId).toBe("thread-1")

  socket.send(JSON.stringify({ type: "released" }))
  await waitFor(() => coordinator!.getSnapshot().phase === "idle")
  expect(host.revoked).toEqual(["att-integration"])
  const idle = coordinator!.getSnapshot()
  if (idle.phase !== "idle") throw new Error("expected idle")
  expect(idle.restoreThreadId).toBe("thread-1")
  expect(idle.handoffVersion).toBe(1)

  socket.close()
  await coordinator.close()
})
