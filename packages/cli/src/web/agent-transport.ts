/** Agent WebSocket transport 与 attachment 认证；凭据不进入 React props 或日志。 */

import { MAX_FRAME_BYTES } from "@za38/protocol"
import type { JsonRpcMessage } from "@za38/protocol"

import { AsyncQueue, type RpcTransport } from "../ipc/transport"

/** WebSocket 的窄形状：仅暴露本模块需要的成员，便于测试注入。 */
type WebSocketLike = WebSocket

/** 基于浏览器 WebSocket 的 JSON-RPC transport；帧大小与关闭语义与 stdio 一致。 */
export class WebSocketRpcTransport implements RpcTransport {
  private readonly queue = new AsyncQueue<unknown>()
  readonly messages: AsyncIterable<unknown> = this.queue

  constructor(private readonly socket: WebSocketLike) {
    socket.addEventListener("message", event => {
      try {
        this.queue.push(JSON.parse(String(event.data)))
      } catch {
        this.queue.push(event.data)
      }
    })
    socket.addEventListener("close", () => this.queue.end())
    socket.addEventListener("error", () => this.queue.fail(new Error("AgentHost WebSocket failed")))
  }

  async send(message: JsonRpcMessage): Promise<void> {
    const text = JSON.stringify(message)
    if (new TextEncoder().encode(text).byteLength > MAX_FRAME_BYTES) {
      throw new Error("JSON-RPC frame exceeds the protocol limit")
    }
    if (this.socket.readyState !== 1) throw new Error("AgentHost WebSocket is closed")
    this.socket.send(text)
  }

  async close(): Promise<void> {
    this.socket.close()
    this.queue.end()
  }
}

/**
 * 认证 attachment：发送一次 `{type:"auth", token}`，等待服务端 `ready`。
 * abort 或任何异常帧都会立即关闭 socket，错误文案不携带 token/endpoint。
 */
export function authenticate(endpoint: string, credential: string, signal: AbortSignal): Promise<WebSocket> {
  const socket = new WebSocket(endpoint)
  return new Promise<WebSocket>((resolve, reject) => {
    let settled = false
    const cleanup = () => {
      signal.removeEventListener("abort", onAbort)
      socket.removeEventListener("open", onOpen)
      socket.removeEventListener("message", onMessage)
      socket.removeEventListener("close", onClose)
      socket.removeEventListener("error", onError)
    }
    const fail = (message: string) => {
      if (settled) return
      settled = true
      cleanup()
      try {
        socket.close()
      } catch {
        // socket 可能已经断开。
      }
      reject(new Error(message))
    }
    const onAbort = () => fail("Attachment authentication aborted")
    const onOpen = () => {
      try {
        socket.send(JSON.stringify({ type: "auth", token: credential }))
      } catch {
        fail("Attachment authentication failed")
      }
    }
    const onMessage = (event: MessageEvent) => {
      let message: unknown
      try {
        message = JSON.parse(String(event.data))
      } catch {
        fail("Attachment authentication failed")
        return
      }
      if (isRecord(message) && message.type === "ready") {
        if (settled) return
        settled = true
        cleanup()
        resolve(socket)
        return
      }
      fail("Attachment authentication failed")
    }
    const onClose = () => fail("AgentHost socket closed")
    const onError = () => fail("无法连接 AgentHost")
    socket.addEventListener("open", onOpen, { once: true })
    socket.addEventListener("message", onMessage)
    socket.addEventListener("close", onClose)
    socket.addEventListener("error", onError)
    if (signal.aborted) onAbort()
    else signal.addEventListener("abort", onAbort, { once: true })
  })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}
