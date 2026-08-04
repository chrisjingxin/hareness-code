/** Bun 静态 server adapter：handoff 路由白名单、精确 Host/Origin 与 lifecycle upgrade。 */

import { AsyncQueue } from "../ipc/transport"
import type { WebAssets } from "./bundle"
import type { LifecycleChannel } from "./handoff-coordinator"

export type WebServerOptions = {
  html: string
  getAssets: () => Promise<WebAssets>
  isActiveHandoff: (handoffId: string) => boolean
  attachLifecycle: (handoffId: string, channel: LifecycleChannel) => Promise<void>
}

export type WebServer = {
  readonly origin: string
  pathFor(handoffId: string): string
  start(): Promise<void>
  stop(): Promise<void>
}

type BunWebSocketData = { handoffId: string }
type BunServerWebSocket = import("bun").ServerWebSocket<BunWebSocketData>

const COMMON_HEADERS = {
  "cache-control": "no-store",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  "cross-origin-resource-policy": "same-origin",
} as const

const HTML_CSP = [
  "default-src 'none'",
  "script-src 'self' 'wasm-unsafe-eval'",
  "style-src 'self'",
  "connect-src ws://127.0.0.1:*",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join("; ")

/** 创建只服务当前 handoff 的本机静态 server；start 前不绑定端口。 */
export function createWebServer(options: WebServerOptions): WebServer {
  let server: ReturnType<typeof Bun.serve<BunWebSocketData>> | undefined
  let assets: WebAssets = {
    script: "",
    style: "",
    syntaxWorkerScript: "",
    treeSitterWasm: new Uint8Array(),
    languageWasms: new Map(),
  }
  const queues = new WeakMap<BunServerWebSocket, AsyncQueue<unknown>>()

  const origin = () => (server ? `http://127.0.0.1:${server.port}` : "")

  function handleFetch(request: Request): Response | undefined {
    if (!server) return new Response("Not Found", { status: 404 })
    const url = new URL(request.url)
    if (url.hostname !== "127.0.0.1" || url.port !== String(server.port)) {
      return new Response("Forbidden", { status: 403 })
    }
    const isUpgrade = request.headers.get("upgrade")?.toLowerCase() === "websocket"
    const path = url.pathname
    if (path === "/web/app.js") {
      if (request.method !== "GET" || isUpgrade) {
        return new Response("Method Not Allowed", { status: 405 })
      }
      return staticResponse("text/javascript; charset=utf-8", assets.script)
    }
    if (path === "/web/app.css") {
      if (request.method !== "GET" || isUpgrade) {
        return new Response("Method Not Allowed", { status: 405 })
      }
      return staticResponse("text/css; charset=utf-8", assets.style)
    }
    if (path === "/web/syntax-worker.js") {
      if (request.method !== "GET" || isUpgrade) {
        return new Response("Method Not Allowed", { status: 405 })
      }
      return staticResponse("text/javascript; charset=utf-8", assets.syntaxWorkerScript || "")
    }
    if (path === "/web/syntax/tree-sitter.wasm") {
      if (request.method !== "GET" || isUpgrade) {
        return new Response("Method Not Allowed", { status: 405 })
      }
      return binaryResponse("application/wasm", assets.treeSitterWasm || new Uint8Array())
    }
    const langWasmMatch = path.match(/^\/web\/syntax\/lang\/([a-zA-Z0-9_-]+)\.wasm$/)
    if (langWasmMatch) {
      if (request.method !== "GET" || isUpgrade) {
        return new Response("Method Not Allowed", { status: 405 })
      }
      const assetId = langWasmMatch[1]
      const wasm = assets.languageWasms?.get(assetId)
      if (!wasm) {
        return new Response("Not Found", { status: 404 })
      }
      return binaryResponse("application/wasm", wasm)
    }
    const match = matchHandoffPath(path)
    if (!match) return new Response("Not Found", { status: 404 })
    if (!options.isActiveHandoff(match.handoffId)) {
      return new Response("Not Found", { status: 404 })
    }
    if (match.kind === "page") {
      if (request.method !== "GET" || isUpgrade) {
        return new Response("Method Not Allowed", { status: 405 })
      }
      return htmlResponse(options.html)
    }
    if (request.method !== "GET" || !isUpgrade) {
      return new Response("Method Not Allowed", { status: 405 })
    }
    if (request.headers.get("origin") !== origin()) {
      return new Response("Forbidden", { status: 403 })
    }
    if (!server.upgrade(request, { data: { handoffId: match.handoffId } })) {
      return new Response("Upgrade Failed", { status: 500 })
    }
    return undefined
  }

  return {
    get origin() {
      return origin()
    },
    pathFor: handoffId => `/web/h/${handoffId}`,
    start: async () => {
      if (server) return
      assets = await options.getAssets()
      const websocket = {
        open(ws: BunServerWebSocket) {
          const queue = new AsyncQueue<unknown>()
          queues.set(ws, queue)
          const channel: LifecycleChannel = {
            messages: queue,
            send: async message => {
              if (ws.readyState === 1) {
                ws.send(JSON.stringify(message))
              }
            },
            close: async (code, reason) => {
              try {
                ws.close(code, reason)
              } catch {
                // 已断开时忽略。
              }
              queue.end()
            },
          }
          void options.attachLifecycle(ws.data.handoffId, channel)
        },
        message(ws: BunServerWebSocket, raw: string | Uint8Array) {
          queues.get(ws)?.push(raw)
        },
        close(ws: BunServerWebSocket) {
          queues.get(ws)?.end()
          queues.delete(ws)
        },
      }
      let lastError: unknown
      for (let attempt = 0; attempt < 8; attempt += 1) {
        try {
          server = Bun.serve<BunWebSocketData>({
            hostname: "127.0.0.1",
            port: randomLoopbackPort(attempt),
            fetch: handleFetch,
            websocket,
          })
          break
        } catch (error) {
          lastError = error
        }
      }
      if (!server) throw lastError instanceof Error ? lastError : new Error("Failed to start Web server")
    },
    stop: async () => {
      if (!server) return
      server.stop(true)
      server = undefined
    },
  }
}

function randomLoopbackPort(attempt: number): number {
  if (attempt === 0) return 0
  return 40_000 + Math.floor(Math.random() * 20_000)
}

/** 只解析两条精确路径；不做任何文件系统读取。 */
function matchHandoffPath(
  path: string,
): { handoffId: string; kind: "page" | "lifecycle" } | undefined {
  const page = path.match(/^\/web\/h\/([^/]+)$/)
  if (page) {
    const handoffId = decodePathSegment(page[1])
    if (handoffId !== undefined) return { handoffId, kind: "page" }
  }
  const lifecycle = path.match(/^\/web\/h\/([^/]+)\/lifecycle$/)
  if (lifecycle) {
    const handoffId = decodePathSegment(lifecycle[1])
    if (handoffId !== undefined) return { handoffId, kind: "lifecycle" }
  }
  return undefined
}

function decodePathSegment(value: string): string | undefined {
  try {
    return decodeURIComponent(value)
  } catch {
    return undefined
  }
}

function staticResponse(contentType: string, body: string): Response {
  return new Response(body, {
    headers: { "content-type": contentType, ...COMMON_HEADERS },
  })
}

function binaryResponse(contentType: string, body: Uint8Array): Response {
  return new Response(body as unknown as BodyInit, {
    headers: { "content-type": contentType, ...COMMON_HEADERS },
  })
}

function htmlResponse(html: string): Response {
  return new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      ...COMMON_HEADERS,
      "cross-origin-opener-policy": "same-origin",
      "content-security-policy": HTML_CSP,
    },
  })
}
