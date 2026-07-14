import { EventEmitter } from "node:events"
import type { Readable, Writable } from "node:stream"
import type { JsonRpcMessage } from "@za38/protocol"

/**
 * JSON-RPC 2.0 客户端，通过 stdin/stdout 通信（换行分隔）。
 * 向 Python 进程的 stdin 写请求，从 stdout 读响应/通知。
 */
export class IpcClient extends EventEmitter {
  private nextId = 1
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>()
  private buffer = ""

  constructor(
    private stdin: Writable,
    private stdout: Readable,
  ) {
    super()
    this.stdout.on("data", (chunk: Buffer) => this.onData(chunk))
    this.stdout.on("end", () => this.emit("close"))
    this.stdout.on("error", (err) => this.emit("error", err))
  }

  private onData(chunk: Buffer): void {
    this.buffer += chunk.toString("utf-8")
    const lines = this.buffer.split("\n")
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      if (!line.trim()) continue
      try {
        const msg = JSON.parse(line) as JsonRpcMessage
        this.handleMessage(msg)
      } catch (err) {
        this.emit("error", new Error(`JSON-RPC 消息解析失败: ${line}`))
      }
    }
  }

  private handleMessage(msg: JsonRpcMessage): void {
    if ("method" in msg && msg.method) {
      // 通知（无 id）—— Python 只发通知给我们
      this.emit(msg.method, msg.params)
    } else if ("id" in msg && msg.id !== undefined) {
      // 响应
      const pending = this.pending.get(msg.id)
      if (pending) {
        this.pending.delete(msg.id)
        if ("error" in msg && msg.error) {
          pending.reject(new Error(msg.error.message))
        } else {
          pending.resolve(msg.result)
        }
      }
    }
  }

  /**
   * 发送 JSON-RPC 请求并等待响应。
   */
  async call(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.send({ jsonrpc: "2.0", method, params, id })
    })
  }

  /**
   * 向 agent 内核发送 query。
   */
  async query(message: string, threadId?: string): Promise<{ thread_id: string; accepted: boolean }> {
    return this.call("query", { message, thread_id: threadId }) as Promise<{ thread_id: string; accepted: boolean }>
  }

  /**
   * 发送通知（不需要响应）。
   */
  notify(method: string, params: Record<string, unknown> = {}): void {
    this.send({ jsonrpc: "2.0", method, params })
  }

  private send(msg: Record<string, unknown>): void {
    this.stdin.write(JSON.stringify(msg) + "\n")
  }

  /**
   * 关闭时清理待处理请求。
   */
  destroy(): void {
    for (const [, { reject }] of this.pending) {
      reject(new Error("Connection closed"))
    }
    this.pending.clear()
    this.removeAllListeners()
  }
}
