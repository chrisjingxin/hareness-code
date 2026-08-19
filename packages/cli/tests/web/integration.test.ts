/** 真实 loopback 集成测试：createWebServer + createPresentationCoordinator + createWebUiGateway + 内存 controller。 */

import { expect, test } from "bun:test"

import { makeHarness } from "../interactive/harness"
import { createWebServer } from "../../src/web/server"
import {
  createPresentationCoordinator,
} from "../../src/presentation-coordinator/coordinator"
import {
  createWebUiGateway,
  type WebUiGateway,
} from "../../src/presentation-coordinator/web-ui-gateway"
import type { WebUiServerMessage } from "../../src/presentation-coordinator/contracts/messages"
import type { WorkspaceExplorer } from "../../src/workspace/types"

/** 空 explorer fake：网关构造必需，本测试不关心工作区。 */
function createFakeExplorer(): WorkspaceExplorer {
  return {
    getSnapshot: () => ({ tree: { status: "idle", rows: [], selectedPath: null, limited: false }, preview: { status: "idle" } }),
    subscribe: () => () => {},
    dispatch: async () => ({ status: "accepted" }),
    close: async () => {},
  }
}

async function waitFor(predicate: () => boolean, timeoutMs = 2_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 2))
  }
}

test("open 后真实 loopback：页面 200；带 token 的 WS 收到 replace/handoff.state；ready → web-active；patch；intent outcome；return 收敛；错误 token 403", async () => {
  const { controller } = makeHarness({ initialThreadId: "thread-1" })
  let gateway!: WebUiGateway
  let openedUrl = ""
  const server = createWebServer({
    html: "<!doctype html><title>web</title>",
    getAssets: async () => ({ script: "console.log('app')", style: "body{}", syntaxWorkerScript: "" }),
    isActiveHandoff: handoffId => coordinator.isHandoffActive(handoffId),
    validateUiToken: (handoffId, token, origin) => coordinator.validateUiToken(handoffId, token, origin),
    attachRenderer: (handoffId, token, channel) => coordinator.attachRenderer(handoffId, token, channel),
  })
  const coordinator = createPresentationCoordinator({
    server,
    openBrowser: async url => { openedUrl = url },
    dispatch: intent => controller.dispatch(intent),
    onRendererConnected: (channel, reconnectToken) => gateway.connectRenderer(channel, reconnectToken),
  })
  gateway = createWebUiGateway({ coordinator, controller, workspaceExplorer: createFakeExplorer() })

  await coordinator.open()
  const opening = coordinator.getSnapshot()
  if (opening.phase !== "opening-web") throw new Error("expected opening-web")
  const token = new URL(openedUrl).hash.slice("#ui=".length)
  expect(token.length).toBeGreaterThan(0)

  // 页面可达
  const page = await fetch(`${server.origin}${server.pathFor(opening.handoffId)}`)
  expect(page.status).toBe(200)
  expect(await page.text()).toContain("<title>web</title>")

  const uiHttp = `${server.origin}${server.pathFor(opening.handoffId)}/ui`
  const upgradeHeaders = (origin: string) => ({ upgrade: "websocket", connection: "Upgrade", origin })

  // 错误 token / 缺 token / 错误 Origin 的升级一律 403
  expect((await fetch(`${uiHttp}?ui=wrong-token`, { headers: upgradeHeaders(server.origin) })).status).toBe(403)
  expect((await fetch(uiHttp, { headers: upgradeHeaders(server.origin) })).status).toBe(403)
  expect((await fetch(`${uiHttp}?ui=${token}`, { headers: upgradeHeaders("http://evil.example") })).status).toBe(403)

  // 正确 token 的 WebSocket 连接
  const wsBase = `${server.origin.replace(/^http/, "ws")}${server.pathFor(opening.handoffId)}/ui`
  const socket = new WebSocket(`${wsBase}?ui=${token}`, { headers: { origin: server.origin } })
  const messages: WebUiServerMessage[] = []
  const closeCodes: number[] = []
  socket.onmessage = event => messages.push(JSON.parse(String(event.data)))
  socket.onclose = event => { closeCodes.push(event.code) }

  // 首帧：轮换 token + state.replace（revision 1）+ handoff.state(opening-web)
  await waitFor(() => messages.some(message => message.type === "state.replace" && message.revision === 1))
  expect(messages[0]).toMatchObject({ type: "handoff.token" })
  expect(messages[1]).toMatchObject({ type: "state.replace", revision: 1 })
  expect(Object.keys((messages[1] as Extract<WebUiServerMessage, { type: "state.replace" }>).state)).toEqual([
    "conversation",
    "interaction",
    "navigation",
    "command",
    "runtime",
    "workItem",
    "workspaceTree",
    "workspacePreview",
  ])
  expect(messages[2]).toMatchObject({ type: "handoff.state", state: { phase: "opening-web" } })

  // ready → web-active
  socket.send(JSON.stringify({ type: "handoff.ready" }))
  await waitFor(() => messages.some(message => message.type === "handoff.state" && message.state.phase === "web-active"))

  // controller 状态变化 → state.patch（只含变化分片）
  await controller.dispatch({ type: "approval-mode.cycle" })
  await waitFor(() => messages.some(message => message.type === "state.patch"))
  const patch = messages.find(message => message.type === "state.patch") as Extract<WebUiServerMessage, { type: "state.patch" }>
  expect(patch.revision).toBe(2)
  expect(Object.keys(patch.patch)).toEqual(["runtime"])

  // intent 受理 → intent.outcome（domain=interactive）与 requestId 一一对应
  socket.send(JSON.stringify({ type: "interactive.intent", requestId: "it-1", revision: 1, intent: { type: "approval-mode.cycle" } }))
  await waitFor(() => messages.some(message => message.type === "intent.outcome" && message.requestId === "it-1"))
  expect(messages.find(message => message.type === "intent.outcome" && message.requestId === "it-1")).toEqual({
    type: "intent.outcome",
    requestId: "it-1",
    domain: "interactive",
    outcome: { status: "accepted" },
  })

  // handoff.return → returning-tui 帧、coordinator 回 tui-active、连接被网关收敛关闭
  socket.send(JSON.stringify({ type: "handoff.return" }))
  await waitFor(() => messages.some(message => message.type === "handoff.state" && message.state.phase === "returning-tui"))
  await waitFor(() => coordinator.getSnapshot().phase === "tui-active")
  await waitFor(() => socket.readyState === WebSocket.CLOSED)
  expect(closeCodes).toContain(1000)

  socket.close()
  await server.stop()
  await coordinator.close()
})
