/** Main 线程 Shiki 高亮 Service：管理 Worker 实例、LRU 缓存、请求超时与熔断机制。 */

import { resolveLanguage } from "../../presentation-shared/language-catalog"
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

/** 主线程 Shiki Worker 管理服务：负责限额、LRU 缓存、超时、熔断与生命周期收敛。 */
export class ShikiHighlightService {
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

  async highlight(language: string, code: string, theme = "dark-plus"): Promise<SyntaxWorkerResponse> {
    const catalogEntry = resolveLanguage(language)
    if (catalogEntry.canonical === "plaintext" && language && language.trim() !== "" && language !== "plaintext" && language !== "text" && language !== "txt") {
      return { type: "plain", requestId: 0, reason: "unknown-language" }
    }

    const codeBytes = new TextEncoder().encode(code).length
    if (codeBytes > MAX_CODE_BYTES || lineCount(code) > MAX_CODE_LINES) {
      return { type: "plain", requestId: 0, reason: "too-large" }
    }

    const cacheKey = `${theme}:${catalogEntry.canonical}:${code}`
    if (this.cache.has(cacheKey)) {
      const cached = this.cache.get(cacheKey)!
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
      language: catalogEntry.canonical,
      code,
      theme,
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

let globalService: ShikiHighlightService | null = null
let serviceUsers = 0

function getService(): ShikiHighlightService {
  if (!globalService) {
    globalService = new ShikiHighlightService()
  }
  return globalService
}

export function acquireHighlightService(): ShikiHighlightService {
  serviceUsers += 1
  return getService()
}

export function releaseHighlightService(): void {
  serviceUsers = Math.max(0, serviceUsers - 1)
  if (serviceUsers === 0) closeHighlightService()
}

export function closeHighlightService(): void {
  globalService?.close()
  globalService = null
  serviceUsers = 0
}

export const closeSyntaxClient = closeHighlightService
export const acquireSyntaxClient = acquireHighlightService
export const releaseSyntaxClient = releaseHighlightService



function lineCount(code: string): number {
  return code ? code.split("\n").length : 0
}
