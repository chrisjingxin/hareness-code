/** WebAwareRoot 挂载测试：两轮 handoff 中 opening 保留 Controller、active 卸载、idle 重建。 */

import { expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { PassThrough } from "node:stream"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import type {
  ControlStatus,
  HostAttachmentCreateResult,
  HostAttachmentRevokeResult,
} from "@za38/protocol"

import { AgentClient } from "../../src/ipc/client"
import { StdioRpcTransport } from "../../src/ipc/stdio-transport"
import { AsyncQueue } from "../../src/ipc/transport"
import { WebAwareRoot } from "../../src/tui/app"
import type { InteractiveRuntime } from "../../src/interactive/runtime"
import {
  createWebHandoffCoordinator,
  type LifecycleChannel,
  type LifecycleServerMessage,
  type WebHandoffCoordinator,
  type WebHandoffServer,
  type WebHostControl,
} from "../../src/web/handoff-coordinator"

const runtime: InteractiveRuntime = {
  workspace: "/workspace/harness-code",
  cliVersion: "0.1.0",
  modelConfigured: true,
  modelName: "enterprise-model",
  executionMode: "local",
  approvalMode: "default",
  capabilities: ["threads.read", "models.read", "host.attach", "host.control"],
}

class FakeHost implements WebHostControl {
  status: ControlStatus = {
    state: "owner",
    holder: { connection_id: "owner", role: "owner", attachment_id: null },
  }
  readonly revoked: string[] = []

  async createAttachment(_origin: string): Promise<HostAttachmentCreateResult> {
    return {
      attachment_id: "att-root",
      endpoint: "ws://127.0.0.1:1",
      token: "token-root",
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

class FakeServer implements WebHandoffServer {
  readonly origin = "http://127.0.0.1:8123"

  async start(): Promise<void> {}
  async stop(): Promise<void> {}
  pathFor(handoffId: string): string {
    return `/web/h/${handoffId}`
  }
}

class FakeChannel implements LifecycleChannel {
  readonly messages = new AsyncQueue<unknown>()
  readonly sent: LifecycleServerMessage[] = []

  async send(message: LifecycleServerMessage): Promise<void> {
    this.sent.push(message)
  }

  async close(): Promise<void> {
    this.messages.end()
  }
}

async function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 2))
  }
}

function createMockClient() {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const client = new AgentClient(new StdioRpcTransport(stdin, stdout))
  const opened: string[] = []

  stdin.on("data", data => {
    for (const line of data.toString("utf8").split("\n")) {
      if (!line.trim()) continue
      const request = JSON.parse(line) as { id?: string; method?: string; params?: Record<string, unknown> }
      if (typeof request.id !== "string") continue
      const respond = (result: unknown) => {
        stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`)
      }
      if (request.method === "skills.list") {
        respond({ snapshot: { id: "snapshot", count: 0 }, skills: [], diagnostics: [] })
        continue
      }
      if (request.method === "threads.open") {
        const threadId = String(request.params?.thread_id)
        opened.push(threadId)
        respond({
          thread: { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: "恢复的请求", latest_message: "恢复的回答", message_count: 2 },
          messages: [
            { kind: "user", content: "恢复的请求" },
            { kind: "assistant", content: "恢复的回答" },
          ],
        })
        continue
      }
      if (request.method === "models.list") {
        respond({ profiles: [] })
        continue
      }
      respond({})
    }
  })
  return { client, opened }
}

test("WebAwareRoot 挂载不抛 this 异常，两轮 handoff 中 opening 保留 Controller、active 卸载、idle 重建", async () => {
  const { client, opened } = createMockClient()
  const host = new FakeHost()
  let coordinator: WebHandoffCoordinator | undefined
  const server = new FakeServer()
  coordinator = createWebHandoffCoordinator({
    host,
    server,
    openBrowser: async () => undefined,
    ownerPollMs: 1,
    ownerWaitMs: 100,
  })
  const historyHome = await mkdtemp(join(tmpdir(), "za38-web-root-"))
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(WebAwareRoot, {
        client,
        runtime,
        webHandoff: coordinator,
        promptHistoryFile: join(historyHome, "prompt-history.jsonl"),
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })

    // 第一次 handoff：opening 阶段保持正常 TUI，不抛 TypeError。
    await act(async () => {
      await coordinator!.open("thread-1")
      await setup.flush()
    })
    expect(coordinator!.getSnapshot().phase).toBe("opening")
    let frame = setup.captureCharFrame()
    expect(frame).toContain("输入消息")
    expect(frame).not.toContain("已移交 Web")

    // 进入 active：TUI 卸载并显示接管页。
    const channel = new FakeChannel()
    await act(async () => {
      const snapshot = coordinator!.getSnapshot()
      if (snapshot.phase !== "opening") throw new Error("expected opening")
      await coordinator!.attachLifecycle(snapshot.handoffId, channel)
      host.status = {
        state: "attached",
        holder: { connection_id: "web-1", role: "attached", attachment_id: "att-root" },
      }
      channel.messages.push(JSON.stringify({ type: "ready" }))
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "active")
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("已移交 Web"))
    expect(frame).toContain("返回 TUI")

    // 归还：owner 确认后 idle，TUI 以 key=1 重建并恢复 thread-1。
    await act(async () => {
      channel.messages.push(JSON.stringify({ type: "released" }))
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "idle"
        && coordinator!.getSnapshot().handoffVersion === 1)
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => opened.includes("thread-1"))
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("恢复的请求"))
    expect(frame).toContain("已恢复")

    // 第二次 handoff：opening 阶段 key 保持 1，Controller 不重建，Thread 状态保留。
    await act(async () => {
      await coordinator!.open("thread-1")
      await setup.flush()
    })
    expect(coordinator!.getSnapshot().handoffVersion).toBe(1)
    frame = setup.captureCharFrame()
    expect(frame).toContain("恢复的请求")
    expect(frame).not.toContain("已移交 Web")

    // 第二次 active → 接管页；第二次归还 → key=2 重建并再次恢复。
    const second = new FakeChannel()
    await act(async () => {
      const snapshot = coordinator!.getSnapshot()
      if (snapshot.phase !== "opening") throw new Error("expected opening")
      await coordinator!.attachLifecycle(snapshot.handoffId, second)
      host.status = {
        state: "attached",
        holder: { connection_id: "web-2", role: "attached", attachment_id: "att-root" },
      }
      second.messages.push(JSON.stringify({ type: "ready" }))
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "active")
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("已移交 Web"))
    await act(async () => {
      second.messages.push(JSON.stringify({ type: "released" }))
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "idle"
        && coordinator!.getSnapshot().handoffVersion === 2)
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("恢复的请求"))
    expect(frame).toContain("已恢复")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await rm(historyHome, { recursive: true, force: true })
  }
})
