/** Main 线程语法高亮 Client：管理 Worker 实例、LRU 缓存、请求超时与熔断机制。 */

import { resolveSyntaxLanguage } from "./catalog.generated"
import type { SyntaxWorkerRequest, SyntaxWorkerResponse } from "./protocol"

const HIGHLIGHT_TIMEOUT_MS = 1500
const MAX_CACHE_SIZE = 128
const MAX_CODE_BYTES = 64 * 1024
const MAX_CONSECUTIVE_FATALS = 3

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
  private cache = new Map<string, SyntaxWorkerResponse>()

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

    if (new TextEncoder().encode(code).length > MAX_CODE_BYTES) {
      return { type: "plain", requestId: 0, reason: "too-large" }
    }

    const cacheKey = `${catalogEntry.filetype}:${code}`
    if (this.cache.has(cacheKey)) {
      return this.cache.get(cacheKey)!
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
            if (this.cache.size >= MAX_CACHE_SIZE) {
              const firstKey = this.cache.keys().next().value
              if (firstKey) this.cache.delete(firstKey)
            }
            this.cache.set(cacheKey, res)
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
    for (const req of this.pendingRequests.values()) {
      clearTimeout(req.timer)
    }
    this.pendingRequests.clear()
    this.cache.clear()
  }
}

let globalSyntaxClient: SyntaxClient | null = null

export function getSyntaxClient(): SyntaxClient {
  if (!globalSyntaxClient) {
    globalSyntaxClient = new SyntaxClient()
  }
  return globalSyntaxClient
}
