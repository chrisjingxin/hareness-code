/** WebAwareRoot 挂载测试：两轮 handoff 中 Controller/Adapter identity 不变，返回后按 Web Thread 重同步。 */

import { expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import type {
  ControlStatus,
  HostAttachmentCreateResult,
  HostAttachmentRevokeResult,
} from "@za38/protocol"

import { AsyncQueue } from "../../src/ipc/transport"
import { WebAwareRoot } from "../../src/tui/app"
import { createTuiAdapter } from "../../src/tui/application/adapter"
import { makeHarness } from "../interactive/harness"
import {
  createWebHandoffCoordinator,
  type LifecycleChannel,
  type LifecycleServerMessage,
  type WebHandoffCoordinator,
  type WebHandoffServer,
  type WebHostControl,
} from "../../src/web/handoff-coordinator"

class FakeHost implements WebHostControl {
  status: ControlStatus = {
    state: "owner",
    holder: { connection_id: "owner", role: "owner", attachment_id: null },
  }

  async createAttachment(_origin: string): Promise<HostAttachmentCreateResult> {
    return {
      attachment_id: "att-root",
      endpoint: "ws://127.0.0.1:1",
      token: "token-root",
      expires_at_ms: 0,
    }
  }

  async revokeAttachment(id: string): Promise<HostAttachmentRevokeResult> {
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

test("两轮 handoff 往返后 Controller/Adapter identity 不变，返回时按 Web Thread 重同步", async () => {
  const harness = makeHarness()
  const controller = harness.controller
  const closed: string[] = []
  const originalClose = controller.close.bind(controller)
  controller.close = async () => {
    closed.push("controller.close")
    await originalClose()
  }
  const host = new FakeHost()
  const server = new FakeServer()
  const coordinator = createWebHandoffCoordinator({
    host,
    server,
    openBrowser: async () => undefined,
    ownerPollMs: 1,
    ownerWaitMs: 100,
  })
  const adapter = createTuiAdapter({
    controller,
    onRequestExit: () => undefined,
  })
  const adapterClosed: string[] = []
  const originalAdapterClose = adapter.close.bind(adapter)
  adapter.close = async () => {
    adapterClosed.push("adapter.close")
    await originalAdapterClose()
  }
  const historyHome = await mkdtemp(join(tmpdir(), "za38-web-root-"))
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(WebAwareRoot, {
        controller,
        adapter,
        webHandoff: coordinator,
        promptHistoryFile: join(historyHome, "prompt-history.jsonl"),
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })

    // 第一次 handoff：opening 阶段保持正常 TUI。
    await act(async () => {
      await coordinator!.open("thread-1")
      await setup.flush()
    })
    expect(coordinator!.getSnapshot().phase).toBe("opening")
    let frame = setup.captureCharFrame()
    expect(frame).toContain("输入消息")
    expect(frame).not.toContain("已移交 Web")

    // 进入 active：TUI 渲染卸载并显示接管页。
    const channel = new FakeChannel()
    await act(async () => {
      const snapshot = coordinator!.getSnapshot()
      if (snapshot.phase !== "opening") throw new Error("expected opening")
      await coordinator!.attachLifecycle(snapshot.handoffId, channel)
      host.status = {
        state: "attached",
        holder: { connection_id: "web-1", role: "attached", attachment_id: "att-root" },
      }
      channel.messages.push(JSON.stringify({ type: "ready", thread_id: "thread-1" }))
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "active")
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("已移交 Web"))
    expect(frame).toContain("返回 TUI")

    // 归还：idle 后 TUI 复用同一 Controller，按 Web Thread 重同步恢复。
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
      await waitFor(() => harness.calls.filter(call => call === "threads.open").length === 1)
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("恢复的请求"))
    expect(frame).toContain("正在恢复")
    expect(harness.controller.getSnapshot().currentThreadId).toBe("thread-1")

    // 第二次 handoff：Controller/Adapter 均未重建、未被关闭，Thread 状态保留。
    await act(async () => {
      await coordinator!.open("thread-1")
      await setup.flush()
    })
    expect(coordinator!.getSnapshot().handoffVersion).toBe(1)
    expect(closed).toEqual([])
    expect(adapterClosed).toEqual([])
    frame = setup.captureCharFrame()
    expect(frame).toContain("恢复的请求")
    expect(frame).not.toContain("已移交 Web")

    // 第二次 active → 接管页；第二次归还 → 重同步拉取最新内容。
    const second = new FakeChannel()
    await act(async () => {
      const snapshot = coordinator!.getSnapshot()
      if (snapshot.phase !== "opening") throw new Error("expected opening")
      await coordinator!.attachLifecycle(snapshot.handoffId, second)
      host.status = {
        state: "attached",
        holder: { connection_id: "web-2", role: "attached", attachment_id: "att-root" },
      }
      second.messages.push(JSON.stringify({ type: "ready", thread_id: "thread-1" }))
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
    await act(async () => {
      await waitFor(() => harness.calls.filter(call => call === "threads.open").length === 2)
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("恢复的请求"))
    expect(frame).toContain("正在恢复")
    // 两轮往返后 Controller/Adapter 始终是同一个实例且从未被关闭。
    expect(closed).toEqual([])
    expect(adapterClosed).toEqual([])
    expect(harness.controller.getSnapshot().currentThreadId).toBe("thread-1")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    await adapter.close()
    await controller.close()
    await rm(historyHome, { recursive: true, force: true })
  }
})
