import { EventEmitter } from "node:events"
import type { Readable, Writable } from "node:stream"
import type { JsonRpcMessage, JsonRpcResponse, QueryResult } from "@za38/protocol"

type PendingRequest = {
  resolve: (value: unknown) => void
  reject: (error: Error) => void
  timeout: ReturnType<typeof setTimeout> | undefined
}

/** 连接 Python Agent sidecar 的 JSONL JSON-RPC 客户端。 */
export class IpcClient extends EventEmitter {
  private nextId = 1
  private readonly pending = new Map<number, PendingRequest>()
  private buffer = ""
  private closed = false

  constructor(
    private readonly stdin: Writable,
    private readonly stdout: Readable,
  ) {
    super()
    this.stdout.on("data", (chunk: Buffer | Uint8Array | string) => this.onData(chunk))
    this.stdout.on("end", () => this.close(new Error("Agent stdout closed")))
    this.stdout.on("error", error => this.close(error))
    this.stdin.on("error", error => this.close(error))
  }

  call(method: string, params: Record<string, unknown> = {}, timeoutMs = 30_000): Promise<unknown> {
    if (this.closed) return Promise.reject(new Error("Agent connection is closed"))
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const timeout = timeoutMs > 0
        ? setTimeout(() => {
            this.pending.delete(id)
            reject(new Error(`Timed out waiting for ${method}`))
          }, timeoutMs)
        : undefined
      this.pending.set(id, { resolve, reject, timeout })
      this.send({ jsonrpc: "2.0", method, params, id })
    })
  }

  query(message: string, threadId?: string, runId?: string): Promise<QueryResult> {
    return this.call("query", { message, thread_id: threadId, run_id: runId }) as Promise<QueryResult>
  }

  cancel(threadId: string, runId: string): Promise<{ cancelled: boolean; run_id: string }> {
    return this.call("cancel", { thread_id: threadId, run_id: runId }) as Promise<{ cancelled: boolean; run_id: string }>
  }

  respond(threadId: string, runId: string, interruptId: string, decisions: unknown): Promise<{ accepted: boolean }> {
    return this.call("respond", {
      thread_id: threadId,
      run_id: runId,
      interrupt_id: interruptId,
      decisions,
    }) as Promise<{ accepted: boolean }>
  }

  async shutdown(): Promise<void> {
    if (!this.closed) await this.call("shutdown", {}, 2_000)
  }

  destroy(): void {
    this.close(new Error("Agent connection closed"))
    this.removeAllListeners()
  }

  private onData(chunk: Buffer | Uint8Array | string): void {
    this.buffer += typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf-8")
    const lines = this.buffer.split("\n")
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      if (!line.trim()) continue
      try {
        this.handleMessage(JSON.parse(line) as JsonRpcMessage)
      } catch {
        this.emit("protocolError", new Error(`Invalid JSON-RPC frame: ${line.slice(0, 200)}`))
      }
    }
  }

  private handleMessage(message: JsonRpcMessage): void {
    if ("method" in message && typeof message.method === "string") {
      this.emit(message.method, message.params ?? {})
      return
    }
    if (!("id" in message) || typeof message.id !== "number") return
    const pending = this.pending.get(message.id)
    if (!pending) return
    this.pending.delete(message.id)
    if (pending.timeout) clearTimeout(pending.timeout)
    const response = message as JsonRpcResponse
    if (response.error) pending.reject(new Error(response.error.message))
    else pending.resolve(response.result)
  }

  private send(message: Record<string, unknown>): void {
    try {
      this.stdin.write(JSON.stringify(message) + "\n")
    } catch (error) {
      this.close(error instanceof Error ? error : new Error(String(error)))
    }
  }

  private close(error: Error): void {
    if (this.closed) return
    this.closed = true
    for (const [id, pending] of this.pending) {
      this.pending.delete(id)
      if (pending.timeout) clearTimeout(pending.timeout)
      pending.reject(error)
    }
    this.emit("close", error)
  }
}
