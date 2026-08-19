/** WebAwareRoot 挂载测试：两轮接管往返中 Controller/Adapter identity 不变，输入租约按阶段收敛。 */

import { expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import { AsyncQueue } from "../../src/ipc/transport"
import { WebAwareRoot } from "../../src/tui/app"
import { createTuiAdapter } from "../../src/tui/application/adapter"
import { makeHarness } from "../interactive/harness"
import {
  createPresentationCoordinator,
  type GatewayChannel,
  type PresentationCoordinator,
  type PresentationServer,
  type WebUiServerMessage,
} from "../../src/presentation-coordinator"

class FakeServer implements PresentationServer {
  readonly origin = "http://127.0.0.1:8123"

  async start(): Promise<void> {}
  async stop(): Promise<void> {}
  pathFor(handoffId: string): string {
    return `/web/h/${handoffId}`
  }
}

class FakeChannel implements GatewayChannel {
  readonly messages = new AsyncQueue<unknown>()
  readonly sent: WebUiServerMessage[] = []
  closed = false

  async send(message: WebUiServerMessage): Promise<void> {
    this.sent.push(message)
  }

  async close(): Promise<void> {
    this.closed = true
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

test("两轮接管往返后 Controller/Adapter identity 不变，TUI 按阶段切换且不重同步", async () => {
  const harness = makeHarness()
  const controller = harness.controller
  const closed: string[] = []
  const originalClose = controller.close.bind(controller)
  controller.close = async () => {
    closed.push("controller.close")
    await originalClose()
  }
  const server = new FakeServer()
  let attached: GatewayChannel | undefined
  let openedUrl = ""
  const coordinator: PresentationCoordinator = createPresentationCoordinator({
    server,
    openBrowser: async url => { openedUrl = url },
    dispatch: intent => controller.dispatch(intent),
    onRendererConnected: channel => { attached = channel },
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

    // 第一次接管：opening-web 阶段 TUI 保持输入。
    await act(async () => {
      await coordinator!.open()
      await setup.flush()
    })
    expect(coordinator!.getSnapshot().phase).toBe("opening-web")
    let frame = setup.captureCharFrame()
    expect(frame).toContain("输入消息")
    expect(frame).not.toContain("已移交 Web")

    // web-active：TUI 渲染卸载并显示接管页；Controller/Adapter 未被关闭。
    const channel = new FakeChannel()
    await act(async () => {
      const snapshot = coordinator!.getSnapshot()
      if (snapshot.phase !== "opening-web") throw new Error("expected opening-web")
      const token = new URL(openedUrl).hash.slice("#ui=".length)
      await coordinator!.attachRenderer(snapshot.handoffId, token, channel)
      coordinator!.requestReady()
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "web-active")
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("已移交 Web"))
    expect(frame).toContain("返回 TUI")
    expect(closed).toEqual([])
    expect(adapterClosed).toEqual([])

    // 归还：returning-tui 后回 tui-active，TUI 复用同一 Controller，不触发任何恢复调用。
    await act(async () => {
      coordinator!.requestReturn()
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "tui-active")
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("输入消息"))
    expect(frame).toContain("输入消息")
    expect(harness.controller.getSnapshot().currentThreadId).toBeNull()
    expect(harness.calls.filter(call => call === "threads.open")).toEqual([])

    // 第二次接管：同一 Controller/Adapter，未重建未关闭。
    await act(async () => {
      await coordinator!.open()
      await setup.flush()
    })
    const second = new FakeChannel()
    await act(async () => {
      const snapshot = coordinator!.getSnapshot()
      if (snapshot.phase !== "opening-web") throw new Error("expected opening-web")
      const token = new URL(openedUrl).hash.slice("#ui=".length)
      await coordinator!.attachRenderer(snapshot.handoffId, token, second)
      coordinator!.requestReady()
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "web-active")
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("已移交 Web"))
    expect(frame).toContain("返回 TUI")
    expect(closed).toEqual([])
    expect(adapterClosed).toEqual([])

    // web-active 期间 TUI 输入经租约被拒（Controller 不执行任何 intent）。
    const before = harness.calls.length
    const outcome = await coordinator!.tuiDispatch({ type: "input.submit", value: "不该被受理" })
    expect(outcome.status).toBe("rejected")
    expect(harness.calls.length).toBe(before)

    await act(async () => {
      coordinator!.requestReturn()
      await setup.flush()
    })
    await act(async () => {
      await waitFor(() => coordinator!.getSnapshot().phase === "tui-active")
      await setup.flush()
    })
    expect(closed).toEqual([])
    expect(adapterClosed).toEqual([])
  } finally {
    await coordinator.close()
    await rm(historyHome, { recursive: true, force: true })
  }
})
