/** Main 线程语法高亮 Client：管理 Worker 实例、LRU 缓存、请求超时与熔断机制。 */

import { resolveSyntaxLanguage } from "./catalog.generated"
import type { SyntaxWorkerRequest, SyntaxWorkerResponse } from "./protocol"

const HIGHLIGHT_TIMEOUT_MS = 1500
const MAX_CACHE_SIZE = 128
const MAX_CACHE_BYTES = 4 * 1024 * 1024
const MAX_CODE_BYTES = 64 * 1024
const MAX_CODE_LINES = 2_000
const MAX_CONSECUTIVE_FATALS = 3

type CachedHighlight = {
  readonly response: SyntaxWorkerResponse
  readonly bytes: number
}

/** 主线程 Syntax Worker 管理器：负责限额、缓存、超时、熔断与生命周期收敛。 */
export class SyntaxClient {
  private worker: Worker | null = null
  private requestIdCounter = 0
  private consecutiveFatals = 0
  private circuitBroken = false
  private pendingRequests = new Map<
    number,
    {
      resolve: (response: SyntaxWorkerResponse) => void
      timer: ReturnType<typeof setTimeout>
    }
  >()
  private cache = new Map<string, CachedHighlight>()
  private cacheBytes = 0

  constructor(private workerUrl = "/web/syntax-worker.js") {}

  private getOrCreateWorker(): Worker | null {
    if (this.circuitBroken) return null
    if (this.worker) return this.worker
    if (typeof globalThis.Worker === "undefined") return null

    try {
      this.worker = new globalThis.Worker(this.workerUrl, { type: "module" })
      this.worker.onmessage = (event: MessageEvent<SyntaxWorkerResponse>) => {
        this.handleResponse(event.data)
      }
      this.worker.onerror = () => {
        this.handleWorkerError()
      }
      return this.worker
    } catch {
      this.circuitBroken = true
      return null
    }
  }

  private handleWorkerError(): void {
    this.consecutiveFatals++
    if (this.consecutiveFatals >= MAX_CONSECUTIVE_FATALS) {
      this.circuitBroken = true
    }
    if (this.worker) {
      try {
        this.worker.terminate()
      } catch {}
      this.worker = null
    }
    // 清理所有待决请求
    for (const [id, req] of this.pendingRequests.entries()) {
      clearTimeout(req.timer)
      req.resolve({ type: "plain", requestId: id, reason: "load-failed" })
    }
    this.pendingRequests.clear()
  }

  private handleResponse(response: SyntaxWorkerResponse): void {
    const pending = this.pendingRequests.get(response.requestId)
    if (!pending) return
    clearTimeout(pending.timer)
    this.pendingRequests.delete(response.requestId)

    if (response.type === "highlighted") {
      this.consecutiveFatals = 0
    }
    pending.resolve(response)
  }

  async highlight(language: string, code: string): Promise<SyntaxWorkerResponse> {
    const catalogEntry = resolveSyntaxLanguage(language)
    if (!catalogEntry) {
      return { type: "plain", requestId: 0, reason: "unknown-language" }
    }

    const codeBytes = new TextEncoder().encode(code).length
    if (codeBytes > MAX_CODE_BYTES || lineCount(code) > MAX_CODE_LINES) {
      return { type: "plain", requestId: 0, reason: "too-large" }
    }

    const cacheKey = `${catalogEntry.filetype}:${code}`
    if (this.cache.has(cacheKey)) {
      const cached = this.cache.get(cacheKey)!
      // 命中时刷新 LRU 顺序；Map 的迭代首项始终是下一条淘汰项。
      this.cache.delete(cacheKey)
      this.cache.set(cacheKey, cached)
      return cached.response
    }

    const worker = this.getOrCreateWorker()
    if (!worker) {
      return { type: "plain", requestId: 0, reason: "load-failed" }
    }

    const requestId = ++this.requestIdCounter
    const request: SyntaxWorkerRequest = {
      type: "highlight",
      requestId,
      language: catalogEntry.filetype,
      code,
    }

    return new Promise<SyntaxWorkerResponse>(resolve => {
      const timer = setTimeout(() => {
        this.pendingRequests.delete(requestId)
        this.consecutiveFatals++
        if (this.consecutiveFatals >= MAX_CONSECUTIVE_FATALS) {
          this.circuitBroken = true
        }
        resolve({ type: "plain", requestId, reason: "timeout" })
      }, HIGHLIGHT_TIMEOUT_MS)

      this.pendingRequests.set(requestId, {
        resolve: (res: SyntaxWorkerResponse) => {
          if (res.type === "highlighted") {
            this.cacheHighlight(cacheKey, res, codeBytes)
          }
          resolve(res)
        },
        timer,
      })

      worker.postMessage(request)
    })
  }

  close(): void {
    if (this.worker) {
      try {
        this.worker.postMessage({ type: "dispose" })
        this.worker.terminate()
      } catch {}
      this.worker = null
    }
    for (const [requestId, req] of this.pendingRequests.entries()) {
      clearTimeout(req.timer)
      req.resolve({ type: "plain", requestId, reason: "load-failed" })
    }
    this.pendingRequests.clear()
    this.cache.clear()
    this.cacheBytes = 0
  }

  /** 写入有界 LRU，避免长会话将整段代码和 token 无限保留在浏览器内存中。 */
  private cacheHighlight(cacheKey: string, response: SyntaxWorkerResponse, codeBytes: number): void {
    const responseBytes = response.type === "highlighted" ? response.spans.length * 24 : 0
    const bytes = codeBytes + responseBytes
    if (bytes > MAX_CACHE_BYTES) return
    while (this.cache.size >= MAX_CACHE_SIZE || this.cacheBytes + bytes > MAX_CACHE_BYTES) {
      const firstKey = this.cache.keys().next().value as string | undefined
      if (!firstKey) break
      const first = this.cache.get(firstKey)
      this.cache.delete(firstKey)
      this.cacheBytes -= first?.bytes ?? 0
    }
    this.cache.set(cacheKey, { response, bytes })
    this.cacheBytes += bytes
  }
}

let globalSyntaxClient: SyntaxClient | null = null
let syntaxClientUsers = 0

/** 按需创建页面内唯一 SyntaxClient；只能由 acquire/release 生命周期使用。 */
function getSyntaxClient(): SyntaxClient {
  if (!globalSyntaxClient) {
    globalSyntaxClient = new SyntaxClient()
  }
  return globalSyntaxClient
}

/** CodeBlock 开始一次高亮任务时获取共享 client；最后一个释放者负责终止 Worker。 */
export function acquireSyntaxClient(): SyntaxClient {
  syntaxClientUsers += 1
  return getSyntaxClient()
}

/** CodeBlock 卸载或替换代码时归还 client；不影响仍在显示的其它代码块。 */
export function releaseSyntaxClient(): void {
  syntaxClientUsers = Math.max(0, syntaxClientUsers - 1)
  if (syntaxClientUsers === 0) closeSyntaxClient()
}

/** 页面卸载时释放全局 Worker 与缓存；下次代码块按需重新创建。 */
export function closeSyntaxClient(): void {
  globalSyntaxClient?.close()
  globalSyntaxClient = null
  syntaxClientUsers = 0
}

function lineCount(code: string): number {
  return code ? code.split("\n").length : 0
}
