/** stdio JSONL adapter：负责 UTF-8 分帧、大小限制、背压和 EOF。 */

import { once } from "node:events"
import { StringDecoder } from "node:string_decoder"
import type { Readable, Writable } from "node:stream"
import { MAX_FRAME_BYTES, type JsonRpcMessage } from "@za38/protocol"
import { AsyncQueue, type RpcTransport } from "./transport"

export class StdioRpcTransport implements RpcTransport {
  private readonly queue = new AsyncQueue<unknown>()
  private readonly decoder = new StringDecoder("utf8")
  private buffer = ""
  private closed = false

  readonly messages: AsyncIterable<unknown> = this.queue

  constructor(
    private readonly stdin: Writable,
    private readonly stdout: Readable,
    private readonly maxFrameBytes = MAX_FRAME_BYTES,
  ) {
    stdout.on("data", (chunk: Buffer | Uint8Array | string) => this.onData(chunk))
    stdout.on("end", () => this.finish())
    stdout.on("error", error => this.finish(error))
    stdin.on("error", error => this.finish(error))
  }

  async send(message: JsonRpcMessage): Promise<void> {
    if (this.closed) throw new Error("Agent transport is closed")
    const line = `${JSON.stringify(message)}\n`
    if (Buffer.byteLength(line, "utf8") > this.maxFrameBytes) {
      throw new Error(`JSON-RPC frame exceeds ${this.maxFrameBytes} bytes`)
    }
    if (!this.stdin.write(line)) await once(this.stdin, "drain")
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.stdin.end()
    this.queue.end()
  }

  private onData(chunk: Buffer | Uint8Array | string): void {
    this.buffer += typeof chunk === "string" ? chunk : this.decoder.write(Buffer.from(chunk))
    if (Buffer.byteLength(this.buffer, "utf8") > this.maxFrameBytes && !this.buffer.includes("\n")) {
      this.finish(new Error(`JSON-RPC frame exceeds ${this.maxFrameBytes} bytes`))
      return
    }
    const lines = this.buffer.split("\n")
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      if (!line.trim()) continue
      if (Buffer.byteLength(line, "utf8") > this.maxFrameBytes) {
        this.finish(new Error(`JSON-RPC frame exceeds ${this.maxFrameBytes} bytes`))
        return
      }
      try {
        this.queue.push(JSON.parse(line))
      } catch {
        // 由 AgentClient 的统一协议校验 seam 报错，transport 不解释消息语义。
        this.queue.push(line)
      }
    }
  }

  private finish(error?: Error): void {
    if (this.closed) return
    this.closed = true
    if (error) this.queue.fail(error)
    else this.queue.end()
  }
}
