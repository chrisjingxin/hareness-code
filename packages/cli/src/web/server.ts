/** Bun 静态 server adapter：白名单路由、精细 Host/Origin 校验与 UI token 升级门禁。 */

import { AsyncQueue } from "../ipc/transport"
import type { GatewayChannel } from "../presentation-coordinator"
import type { WebAssets } from "./bundle"

export type WebServerOptions = {
  html: string
  getAssets: () => Promise<WebAssets>
  isActiveHandoff: (handoffId: string) => boolean
  /** 升级请求携带的 UI token 校验；通过后该连接获得渲染资格。 */
  consumeUiToken: (handoffId: string, token: string, origin: string) => boolean
  /** 把已完成升级的渲染 channel 交给 Coordinator；生命周期与业务帧由上层处理。 */
  attachRenderer: (handoffId: string, channel: GatewayChannel) => Promise<void>
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
  "script-src 'self'",
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
    // UI token 从升级 URL 的查询参数读取（fragment 无法送达服务端）；token 绑定
    // handoffId、Origin 与 TTL，页面 fragment 中的 token 在创建 WebSocket 前已被剥离。
    // 本 server 只服务 127.0.0.1、无访问日志，token 单次 URL 使用——禁止为该 server
    // 开启访问日志或接入代理，否则 token 会以明文出现在日志中。
    const token = url.searchParams.get("ui")
    if (!token || !options.consumeUiToken(match.handoffId, token, origin())) {
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
          const channel: GatewayChannel = {
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
          void options.attachRenderer(ws.data.handoffId, channel)
        },
        message(ws: BunServerWebSocket, raw: string | Uint8Array) {
          // 帧形状/大小校验统一由网关执行：超限帧按协议违规走 notifyInvalidMessage
          // fail-closed 收敛，与畸形帧路径一致；此处只解码后原样入队。
          const text = typeof raw === "string" ? raw : new TextDecoder().decode(raw)
          queues.get(ws)?.push(text)
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
): { handoffId: string; kind: "page" | "ui" } | undefined {
  const page = path.match(/^\/web\/h\/([^/]+)$/)
  if (page) {
    const handoffId = decodePathSegment(page[1])
    if (handoffId !== undefined) return { handoffId, kind: "page" }
  }
  const ui = path.match(/^\/web\/h\/([^/]+)\/ui$/)
  if (ui) {
    const handoffId = decodePathSegment(ui[1])
    if (handoffId !== undefined) return { handoffId, kind: "ui" }
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
