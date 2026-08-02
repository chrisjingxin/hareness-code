/** 本机 Web 表现层 launcher：提供静态资源并向 AgentHost 申请一次性 attachment。 */

import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"
import { Method } from "@za38/protocol"

import type { ThreadWatch } from "../ipc/client"
import type { AgentClient } from "../ipc/client"
import { webHtml } from "./html"

export class WebLauncher {
  private server?: ReturnType<typeof Bun.serve>
  private script?: string
  private readonly watches = new Map<string, ThreadWatch>()

  constructor(private readonly client: AgentClient) {}

  /** 在当前空闲 Thread 上建立 TUI observer，再打开带 fragment 凭证的浏览器页面。 */
  async open(threadId: string): Promise<string> {
    if (!this.watches.has(threadId)) {
      const watch = await this.client.watchThread(threadId)
      this.watches.set(threadId, watch)
      void drain(watch.events)
    }
    await this.ensureServer()
    const origin = `http://127.0.0.1:${this.server!.port}`
    const attachment = await this.client.request(Method.HOST_ATTACHMENT_CREATE, { origin })
    const fragment = new URLSearchParams({
      endpoint: attachment.endpoint,
      token: attachment.token,
      thread: threadId,
    })
    const url = `${origin}/#${fragment}`
    openBrowser(url)
    return url
  }

  /** CLI 退出时停止静态服务并释放所有 Thread watches。 */
  async close(): Promise<void> {
    this.server?.stop(true)
    this.server = undefined
    await Promise.allSettled([...this.watches.values()].map(watch => watch.close()))
    this.watches.clear()
  }

  private async ensureServer(): Promise<void> {
    if (this.server) return
    this.script = await browserBundle()
    this.server = Bun.serve({
      hostname: "127.0.0.1",
      port: 0,
      fetch: request => {
        const path = new URL(request.url).pathname
        if (path === "/app.js") {
          return new Response(this.script, {
            headers: { "content-type": "text/javascript; charset=utf-8" },
          })
        }
        if (path === "/" || path === "/index.html") {
          return new Response(webHtml, {
            headers: {
              "content-type": "text/html; charset=utf-8",
              "cache-control": "no-store",
              "content-security-policy": "default-src 'self'; connect-src ws://127.0.0.1:*; style-src 'self' 'unsafe-inline'; script-src 'self'",
            },
          })
        }
        return new Response("Not found", { status: 404 })
      },
    })
  }
}

async function drain(events: AsyncIterable<unknown>): Promise<void> {
  try {
    for await (const _event of events) {
      // TUI 已通过 AgentClient 的统一 event 订阅渲染；这里只释放 watch 队列。
    }
  } catch {
    // CLI 关闭时 watch 会随 Connection 一并结束。
  }
}

/** 加载构建产物，源码开发模式下则即时构建浏览器脚本。 */
export async function browserBundle(): Promise<string> {
  const built = resolve(import.meta.dir, "web.js")
  if (existsSync(built)) return readFile(built, "utf8")
  const result = await Bun.build({
    entrypoints: [resolve(import.meta.dir, "app.ts")],
    target: "browser",
    minify: true,
  })
  if (!result.success || !result.outputs[0]) {
    throw new Error(result.logs.map(log => log.message).join("\n") || "Web bundle build failed")
  }
  return result.outputs[0].text()
}

function openBrowser(url: string): void {
  const executable = process.platform === "darwin"
    ? "open"
    : process.platform === "win32"
      ? "cmd"
      : "xdg-open"
  const args = process.platform === "win32" ? ["/c", "start", "", url] : [url]
  const child = spawn(executable, args, { detached: true, stdio: "ignore" })
  child.unref()
}
