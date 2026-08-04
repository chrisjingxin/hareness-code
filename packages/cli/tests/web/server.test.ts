/** Bun 静态 server adapter：路由白名单、安全 headers 与 lifecycle upgrade 测试。 */

import { expect, test } from "bun:test"

import { createWebServer, type WebServer } from "../../src/web/server"
import type { LifecycleChannel } from "../../src/web/handoff-coordinator"

type Harness = {
  server: WebServer
  attachCalls: Array<{ handoffId: string; channel: LifecycleChannel }>
  activeHandoffs: Set<string>
}

function createHarness(): Harness {
  const attachCalls: Array<{ handoffId: string; channel: LifecycleChannel }> = []
  const activeHandoffs = new Set<string>()
  const server = createWebServer({
    html: "<!doctype html><title>shell</title>",
    getAssets: async () => ({
      script: "console.log('app')",
      style: "body{}",
      syntaxWorkerScript: "console.log('syntax worker')",
      treeSitterWasm: new Uint8Array([0, 97, 115, 109]),
      languageWasms: new Map([["python", new Uint8Array([1, 2, 3])]]),
    }),
    isActiveHandoff: handoffId => activeHandoffs.has(handoffId),
    attachLifecycle: async (handoffId, channel) => {
      attachCalls.push({ handoffId, channel })
      await channel.send({ type: "accepted" })
    },
  })
  return { server, attachCalls, activeHandoffs }
}

async function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor 超时")
    await new Promise(resolve => setTimeout(resolve, 2))
  }
}

test("白名单路由与安全 headers 精确存在；未知路由返回 404/405", async () => {
  const { server, activeHandoffs } = createHarness()
  await server.start()
  activeHandoffs.add("handoff-1")
  const origin = server.origin

  const page = await fetch(`${origin}/web/h/handoff-1`)
  expect(page.status).toBe(200)
  expect(await page.text()).toContain("<title>shell</title>")
  expect(page.headers.get("content-security-policy")).toContain("default-src 'none'")
  expect(page.headers.get("content-security-policy")).toContain("connect-src ws://127.0.0.1:*")
  expect(page.headers.get("content-security-policy")).toContain("wasm-unsafe-eval")
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
  const treeSitter = await fetch(`${origin}/web/syntax/tree-sitter.wasm`)
  expect(treeSitter.status).toBe(200)
  expect(treeSitter.headers.get("content-type")).toContain("application/wasm")
  expect(new Uint8Array(await treeSitter.arrayBuffer())).toEqual(new Uint8Array([0, 97, 115, 109]))
  const language = await fetch(`${origin}/web/syntax/lang/python.wasm`)
  expect(language.status).toBe(200)
  expect(new Uint8Array(await language.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]))

  expect((await fetch(`${origin}/`)).status).toBe(404)
  expect((await fetch(`${origin}/index.html`)).status).toBe(404)
  expect((await fetch(`${origin}/web/syntax/lang/not-in-catalog.wasm`)).status).toBe(404)
  expect((await fetch(`${origin}/web/syntax/lang/python.wasm`, { method: "POST" })).status).toBe(405)
  expect((await fetch(`${origin}/web/h/unknown-handoff`)).status).toBe(404)
  expect((await fetch(`${origin}/web/h/handoff-1/../app.js`)).status).toBe(404)
  expect((await fetch(`${origin}/web/h/handoff-1`, { method: "POST" })).status).toBe(405)
  expect((await fetch(`${origin}/web/h/handoff-1/lifecycle`)).status).toBe(405)

  // idle 后旧 handoff path 立即失效。
  activeHandoffs.delete("handoff-1")
  expect((await fetch(`${origin}/web/h/handoff-1`)).status).toBe(404)

  await server.stop()
})

test("错误 Host 被拒绝；lifecycle upgrade 校验 Origin 并投递 accepted", async () => {
  const { server, attachCalls, activeHandoffs } = createHarness()
  await server.start()
  activeHandoffs.add("handoff-1")
  const origin = server.origin

  const forged = await fetch(`${origin}/web/h/handoff-1`, {
    headers: { host: "evil.example:9999" },
  })
  expect(forged.status).toBe(403)

  let wrongOriginFailed = false
  const wrongWs = new WebSocket(`${origin.replace(/^http/, "ws")}/web/h/handoff-1/lifecycle`, {
    headers: { origin: "http://evil.example" },
  })
  wrongWs.onerror = () => { wrongOriginFailed = true }
  await waitFor(() => wrongOriginFailed || wrongWs.readyState === 3)
  expect(wrongOriginFailed || wrongWs.readyState === 3).toBe(true)

  const socket = new WebSocket(`${origin.replace(/^http/, "ws")}/web/h/handoff-1/lifecycle`, {
    headers: { origin },
  })
  const messages: unknown[] = []
  socket.onmessage = event => messages.push(JSON.parse(String(event.data)))
  await waitFor(() => attachCalls.length === 1)
  expect(attachCalls[0].handoffId).toBe("handoff-1")
  await waitFor(() => messages.length >= 1)
  expect(messages[0]).toEqual({ type: "accepted" })
  socket.close()
  await waitFor(() => socket.readyState === WebSocket.CLOSED)
  await server.stop()
})

test("lifecycle 消息原样投递给 coordinator，二进制/畸形帧不解析", async () => {
  const { server, attachCalls, activeHandoffs } = createHarness()
  await server.start()
  activeHandoffs.add("handoff-1")
  const origin = server.origin
  const socket = new WebSocket(`${origin.replace(/^http/, "ws")}/web/h/handoff-1/lifecycle`, {
    headers: { origin },
  })
  await waitFor(() => attachCalls.length === 1)
  // 通过 channel.messages 消费一条帧验证投递。
  const iterator = attachCalls[0].channel.messages[Symbol.asyncIterator]()
  socket.send(JSON.stringify({ type: "thread.changed", thread_id: "t-1" }))
  const first = await Promise.race([iterator.next(), sleep(500)])
  expect(first && "value" in first ? first.value : undefined).toBe(JSON.stringify({ type: "thread.changed", thread_id: "t-1" }))
  socket.send(new Uint8Array([1, 2, 3]))
  const second = await Promise.race([iterator.next(), sleep(500)])
  expect(second && "value" in second ? second.value : undefined).toBeInstanceOf(Uint8Array)
  socket.close()
  await server.stop()
})

function sleep(ms: number): Promise<{ timedOut: true }> {
  return new Promise(resolve => setTimeout(() => resolve({ timedOut: true }), ms))
}
