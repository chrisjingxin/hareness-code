/** Bun 静态 server adapter：路由白名单、安全 headers 与 /ui 升级门禁测试。 */

import { expect, test } from "bun:test"

import { createWebServer, type WebServer } from "../../src/web/server"
import { MAX_UI_FRAME_BYTES } from "../../src/presentation-coordinator"
import type { GatewayChannel } from "../../src/presentation-coordinator"

type Harness = {
  server: WebServer
  attachCalls: Array<{ handoffId: string; presentedToken: string; channel: GatewayChannel }>
  activeHandoffs: Set<string>
  validTokens: Set<string>
}

function createHarness(): Harness {
  const attachCalls: Array<{ handoffId: string; presentedToken: string; channel: GatewayChannel }> = []
  const activeHandoffs = new Set<string>()
  const validTokens = new Set<string>()
  const server = createWebServer({
    html: "<!doctype html><title>shell</title>",
    getAssets: async () => ({
      script: "console.log('app')",
      style: "body{}",
      syntaxWorkerScript: "console.log('syntax worker')",
    }),
    isActiveHandoff: handoffId => activeHandoffs.has(handoffId),
    validateUiToken: (_handoffId, token, _origin) => validTokens.has(token),
    attachRenderer: async (handoffId, presentedToken, channel) => {
      attachCalls.push({ handoffId, presentedToken, channel })
      await channel.send({ type: "handoff.state", state: { phase: "opening-web", handoffId } })
    },
  })
  return { server, attachCalls, activeHandoffs, validTokens }
}

async function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 2))
  }
}

test("白名单路由与安全 headers 精确存在；已知 WASM 路由删除后返回 404；未知路由返回 404/405", async () => {
  const { server, activeHandoffs } = createHarness()
  await server.start()
  activeHandoffs.add("handoff-1")
  const origin = server.origin

  const page = await fetch(`${origin}/web/h/handoff-1`)
  expect(page.status).toBe(200)
  expect(await page.text()).toContain("<title>shell</title>")
  expect(page.headers.get("content-security-policy")).toContain("default-src 'none'")
  expect(page.headers.get("content-security-policy")).toContain("connect-src ws://127.0.0.1:*")
  expect(page.headers.get("content-security-policy")).not.toContain("wasm-unsafe-eval")
  expect(page.headers.get("cache-control")).toBe("no-store")
  expect(page.headers.get("referrer-policy")).toBe("no-referrer")
  expect(page.headers.get("x-content-type-options")).toBe("nosniff")
  expect(page.headers.get("cross-origin-resource-policy")).toBe("same-origin")
  expect(page.headers.get("cross-origin-opener-policy")).toBe("same-origin")

  const script = await fetch(`${origin}/web/app.js`)
  expect(script.status).toBe(200)
  expect(script.headers.get("content-type")).toContain("javascript")
  expect(script.headers.get("cache-control")).toBe("no-store")
  expect(script.headers.get("cross-origin-opener-policy")).toBeNull()
  const style = await fetch(`${origin}/web/app.css`)
  expect(style.status).toBe(200)
  expect(style.headers.get("content-type")).toContain("text/css")
  const syntaxWorker = await fetch(`${origin}/web/syntax-worker.js`)
  expect(syntaxWorker.status).toBe(200)
  expect(syntaxWorker.headers.get("content-type")).toContain("javascript")
  expect(await syntaxWorker.text()).toContain("syntax worker")

  // WASM 路由被全量删除，已返回 404
  const treeSitter = await fetch(`${origin}/web/syntax/tree-sitter.wasm`)
  expect(treeSitter.status).toBe(404)
  const language = await fetch(`${origin}/web/syntax/lang/python.wasm`)
  expect(language.status).toBe(404)

  expect((await fetch(`${origin}/`)).status).toBe(404)
  expect((await fetch(`${origin}/index.html`)).status).toBe(404)
  expect((await fetch(`${origin}/web/syntax/lang/not-in-catalog.wasm`)).status).toBe(404)
  expect((await fetch(`${origin}/web/h/unknown-handoff`)).status).toBe(404)
  expect((await fetch(`${origin}/web/h/handoff-1/../app.js`)).status).toBe(404)
  expect((await fetch(`${origin}/web/h/handoff-1`, { method: "POST" })).status).toBe(405)
  // 旧 lifecycle 路由已删除 → 404；/ui 是升级专用路由，普通 GET 返回 405
  expect((await fetch(`${origin}/web/h/handoff-1/lifecycle`)).status).toBe(404)
  expect((await fetch(`${origin}/web/h/handoff-1/ui`)).status).toBe(405)
  expect((await fetch(`${origin}/web/h/handoff-1/ui`, { method: "POST" })).status).toBe(405)

  activeHandoffs.delete("handoff-1")
  expect((await fetch(`${origin}/web/h/handoff-1`)).status).toBe(404)

  await server.stop()
})

test("错误 Host 被拒绝；/ui upgrade 校验 Origin 与 UI token 并投递 handoff.state", async () => {
  const { server, attachCalls, activeHandoffs, validTokens } = createHarness()
  await server.start()
  activeHandoffs.add("handoff-1")
  validTokens.add("token-1")
  const origin = server.origin

  const forged = await fetch(`${origin}/web/h/handoff-1`, {
    headers: { host: "evil.example:9999" },
  })
  expect(forged.status).toBe(403)

  // 错误 Origin 的升级被拒
  let wrongOriginFailed = false
  const wrongWs = new WebSocket(`${origin.replace(/^http/, "ws")}/web/h/handoff-1/ui?ui=token-1`, {
    headers: { origin: "http://evil.example" },
  })
  wrongWs.onerror = () => { wrongOriginFailed = true }
  await waitFor(() => wrongOriginFailed || wrongWs.readyState === 3)
  expect(wrongOriginFailed || wrongWs.readyState === 3).toBe(true)

  // 错误 / 缺失 token 的升级被 403
  const uiHttp = `${origin}/web/h/handoff-1/ui`
  const upgradeHeaders = (value: string) => ({ upgrade: "websocket", connection: "Upgrade", origin: value })
  expect((await fetch(`${uiHttp}?ui=wrong`, { headers: upgradeHeaders(origin) })).status).toBe(403)
  expect((await fetch(uiHttp, { headers: upgradeHeaders(origin) })).status).toBe(403)

  // 正确 token + Origin → attachRenderer 收到 channel，帧原样投递给 Browser
  const socket = new WebSocket(`${origin.replace(/^http/, "ws")}/web/h/handoff-1/ui?ui=token-1`, {
    headers: { origin },
  })
  const messages: unknown[] = []
  socket.onmessage = event => messages.push(JSON.parse(String(event.data)))
  await waitFor(() => attachCalls.length === 1)
  expect(attachCalls[0].handoffId).toBe("handoff-1")
  expect(attachCalls[0].presentedToken).toBe("token-1")
  await waitFor(() => messages.length >= 1)
  expect(messages[0]).toEqual({ type: "handoff.state", state: { phase: "opening-web", handoffId: "handoff-1" } })
  socket.close()
  await waitFor(() => socket.readyState === WebSocket.CLOSED)
  await server.stop()
})

test("客户端帧原样投递给 coordinator；二进制不解析；超大帧原样入队由网关拒绝", async () => {
  const { server, attachCalls, activeHandoffs, validTokens } = createHarness()
  await server.start()
  activeHandoffs.add("handoff-1")
  validTokens.add("token-1")
  const origin = server.origin
  const socket = new WebSocket(`${origin.replace(/^http/, "ws")}/web/h/handoff-1/ui?ui=token-1`, {
    headers: { origin },
  })
  await waitFor(() => attachCalls.length === 1)
  await waitFor(() => socket.readyState === WebSocket.OPEN)
  const iterator = attachCalls[0].channel.messages[Symbol.asyncIterator]()
  socket.send(JSON.stringify({ type: "handoff.ready" }))
  const first = await Promise.race([iterator.next(), sleep(500)])
  expect(first && "value" in first ? first.value : undefined).toBe(JSON.stringify({ type: "handoff.ready" }))
  socket.send(new Uint8Array([1, 2, 3]))
  const second = await Promise.race([iterator.next(), sleep(500)])
  // 二进制帧被服务器解码为文本后原样投递（不做 JSON 解析）
  expect(second && "value" in second ? second.value : undefined).toBe("\u0001\u0002\u0003")

  // 超大帧不在此处关闭：服务器只解码入队，尺寸/形状校验统一由网关按协议违规
  // fail-closed（parseClientFrame 拒绝 → notifyInvalidMessage），保证与畸形帧同路径。
  socket.send("x".repeat(MAX_UI_FRAME_BYTES + 1))
  const third = await Promise.race([iterator.next(), sleep(500)])
  expect(third && "value" in third ? third.value : undefined).toBe("x".repeat(MAX_UI_FRAME_BYTES + 1))
  await server.stop()
})

function sleep(ms: number): Promise<{ timedOut: true }> {
  return new Promise(resolve => setTimeout(() => resolve({ timedOut: true }), ms))
}
