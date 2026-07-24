/** Node 侧双向 JSON-RPC Peer：管理 stdio 分帧、请求关联、服务端反向请求与连接背压。 */

import { EventEmitter, once } from "node:events"
import { StringDecoder } from "node:string_decoder"
import type { Readable, Writable } from "node:stream"
import {
  MAX_FRAME_BYTES,
  Method,
  assertEventEnvelope,
  assertInteractionRequest,
  assertJsonRpcMessage,
  type EventEnvelope,
  type ContextCompactResult,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type JsonRpcMessage,
  type JsonRpcResponse,
  type RunCancelResult,
  type RequestedSkill,
  type RunStartResult,
  type ModelsListResult,
  type ThreadsListResult,
  type ThreadsOpenResult,
} from "@za38/protocol"

type PendingRequest = {
  resolve: (value: unknown) => void
  reject: (error: Error) => void
  timeout: ReturnType<typeof setTimeout> | undefined
}

export type PeerRequestHandler = (params: InteractionRequestEnvelope) => Promise<InteractionResponse> | InteractionResponse

/** 保留远端错误码和 data，调用方可据此区分协议、配置和 Agent 故障。 */
export class JsonRpcRemoteError extends Error {
  constructor(
    public readonly code: number,
    message: string,
    public readonly data?: unknown,
  ) {
    super(message)
    this.name = "JsonRpcRemoteError"
  }
}

/** 连接 Python Agent sidecar 的双向 JSON-RPC Peer。 */
export class JsonRpcPeer extends EventEmitter {
  private nextId = 1
  private readonly pending = new Map<string, PendingRequest>()
  private readonly decoder = new StringDecoder("utf8")
  private readonly inboundRequests = new Set<string>()
  private buffer = ""
  private closed = false
  private requestHandler: PeerRequestHandler | undefined

  constructor(
    private readonly stdin: Writable,
    private readonly stdout: Readable,
    private readonly maxFrameBytes = MAX_FRAME_BYTES,
  ) {
    super()
    this.stdout.on("data", (chunk: Buffer | Uint8Array | string) => this.onData(chunk))
    this.stdout.on("end", () => this.close(new Error("Agent stdout closed")))
    this.stdout.on("error", error => this.close(error))
    this.stdin.on("error", error => this.close(error))
  }

  /** 注册 Agent 反向发起的审批或问答处理器；返回函数用于组件卸载时清理。 */
  setRequestHandler(handler: PeerRequestHandler): () => void {
    this.requestHandler = handler
    return () => {
      if (this.requestHandler === handler) this.requestHandler = undefined
    }
  }

  /** 运行取消或服务端超时后停止回写已经失效的交互响应。 */
  abandonInteraction(requestId: string): void {
    this.inboundRequests.delete(requestId)
  }

  /** 发送带超时保护的请求，并返回对应 JSON-RPC result。 */
  call(method: string, params: Record<string, unknown> = {}, timeoutMs = 30_000): Promise<unknown> {
    if (this.closed) return Promise.reject(new Error("Agent connection is closed"))
    const id = `req-${this.nextId++}`
    return new Promise((resolve, reject) => {
      const timeout = timeoutMs > 0
        ? setTimeout(() => {
            this.pending.delete(id)
            reject(new Error(`Timed out waiting for ${method}`))
          }, timeoutMs)
        : undefined
      this.pending.set(id, { resolve, reject, timeout })
      void this.send({ jsonrpc: "2.0", method, params, id }).catch(error => {
        this.pending.delete(id)
        if (timeout) clearTimeout(timeout)
        reject(error)
      })
    })
  }

  /** 启动一次 Agent 运行，保留可选线程和运行标识。 */
  startRun(message: string, threadId?: string, runId?: string, requestedSkill?: RequestedSkill, modelProfile?: string): Promise<RunStartResult> {
    return this.call(Method.RUN_START, {
      message,
      thread_id: threadId,
      run_id: runId,
      requested_skill: requestedSkill,
      model_profile: modelProfile,
    }) as Promise<RunStartResult>
  }

  /** 兼容现有调用点的语义别名；wire 上已使用 run.start。 */
  query(message: string, threadId?: string, runId?: string, requestedSkill?: RequestedSkill, modelProfile?: string): Promise<RunStartResult> {
    return this.startRun(message, threadId, runId, requestedSkill, modelProfile)
  }

  /** 请求取消指定运行。 */
  cancel(threadId: string, runId: string): Promise<RunCancelResult> {
    return this.call(Method.RUN_CANCEL, { thread_id: threadId, run_id: runId }) as Promise<RunCancelResult>
  }

  /** 在当前 thread 空闲时请求 sidecar 强制生成一次结构化上下文摘要。 */
  compactContext(threadId: string): Promise<ContextCompactResult> {
    return this.call(Method.CONTEXT_COMPACT, { thread_id: threadId }) as Promise<ContextCompactResult>
  }

  /** 读取当前 project 的可恢复 thread 摘要；thread_id 只在 TUI 内部用于后续打开。 */
  listThreads(limit = 80): Promise<ThreadsListResult> {
    return this.call(Method.THREADS_LIST, { limit }) as Promise<ThreadsListResult>
  }

  /** 打开当前 project 的既有 thread，并返回可以重新构造时间线的消息。 */
  openThread(threadId: string): Promise<ThreadsOpenResult> {
    return this.call(Method.THREADS_OPEN, { thread_id: threadId }) as Promise<ThreadsOpenResult>
  }

  /** 读取 `/model` Picker 所需的脱敏 Profile 目录与可选 Thread 绑定。 */
  listModels(threadId?: string): Promise<ModelsListResult> {
    return this.call(Method.MODELS_LIST, { thread_id: threadId }) as Promise<ModelsListResult>
  }

  /** 请求 sidecar 优雅关闭，并给关闭响应设置较短超时。 */
  async shutdown(): Promise<void> {
    if (!this.closed) await this.call(Method.SHUTDOWN, {}, 2_000)
  }

  /** 主动释放连接并拒绝所有尚未完成的请求。 */
  destroy(): void {
    this.close(new Error("Agent connection closed"))
    this.removeAllListeners()
  }

  /** 使用 StringDecoder 保留跨 chunk UTF-8 字符，并限制无换行缓冲区大小。 */
  private onData(chunk: Buffer | Uint8Array | string): void {
    this.buffer += typeof chunk === "string" ? chunk : this.decoder.write(Buffer.from(chunk))
    if (Buffer.byteLength(this.buffer, "utf8") > this.maxFrameBytes && !this.buffer.includes("\n")) {
      const error = new Error(`JSON-RPC frame exceeds ${this.maxFrameBytes} bytes`)
      this.emit("protocolError", error)
      this.close(error)
      return
    }
    const lines = this.buffer.split("\n")
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      if (!line.trim()) continue
      if (Buffer.byteLength(line, "utf8") > this.maxFrameBytes) {
        this.emit("protocolError", new Error(`JSON-RPC frame exceeds ${this.maxFrameBytes} bytes`))
        continue
      }
      try {
        const message: unknown = JSON.parse(line)
        assertJsonRpcMessage(message)
        this.handleMessage(message)
      } catch (error) {
        this.emit("protocolError", new Error(`Invalid JSON-RPC frame: ${errorMessage(error)}`))
      }
    }
  }

  /** 区分通知、反向请求与响应，避免 request 被误当成无需响应的 event。 */
  private handleMessage(message: JsonRpcMessage): void {
    if ("method" in message && typeof message.method === "string") {
      if ("id" in message && typeof message.id === "string") {
        void this.handleInboundRequest(message.method, message.id, message.params ?? {})
        return
      }
      if (message.method === Method.EVENT) {
        assertEventEnvelope(message.params)
        const event = message.params as unknown as EventEnvelope
        this.emit("event", event)
        this.emit(event.type, event)
      } else {
        this.emit(message.method, message.params ?? {})
      }
      return
    }
    if (!("id" in message) || typeof message.id !== "string") return
    const pending = this.pending.get(message.id)
    if (!pending) {
      this.emit("protocolError", new Error(`Unknown JSON-RPC response id: ${message.id}`))
      return
    }
    this.pending.delete(message.id)
    if (pending.timeout) clearTimeout(pending.timeout)
    const response = message as JsonRpcResponse
    if (response.error) pending.reject(new JsonRpcRemoteError(response.error.code, response.error.message, response.error.data))
    else pending.resolve(response.result)
  }

  /** 处理 Agent 发起的 request；无处理器或非法结果都返回标准错误响应。 */
  private async handleInboundRequest(method: string, id: string, params: Record<string, unknown>): Promise<void> {
    if (method !== Method.REQUEST) {
      await this.sendError(id, -32601, `Unsupported server request: ${method}`)
      return
    }
    this.inboundRequests.add(id)
    try {
      assertInteractionRequest(params)
      if (params.request_id !== id) throw new Error("JSON-RPC id 与 request_id 不一致")
      if (!this.requestHandler) throw new Error("Client has no interaction request handler")
      const result = await this.requestHandler(params)
      if (!this.inboundRequests.has(id)) return
      if (result.request_id !== id || result.type !== params.type) throw new Error("Interaction response does not match request")
      this.inboundRequests.delete(id)
      await this.send({ jsonrpc: "2.0", id, result })
    } catch (error) {
      if (!this.inboundRequests.has(id)) return
      this.inboundRequests.delete(id)
      await this.sendError(id, -32602, errorMessage(error))
    }
  }

  /** 串行交给 Node Writable 并在高水位触发时等待 drain。 */
  private async send(message: Record<string, unknown>): Promise<void> {
    if (this.closed) throw new Error("Agent connection is closed")
    const line = JSON.stringify(message) + "\n"
    if (Buffer.byteLength(line, "utf8") > this.maxFrameBytes) throw new Error(`JSON-RPC frame exceeds ${this.maxFrameBytes} bytes`)
    if (!this.stdin.write(line)) await once(this.stdin, "drain")
  }

  private async sendError(id: string, code: number, message: string): Promise<void> {
    await this.send({ jsonrpc: "2.0", id, error: { code, message } })
  }

  /** 只执行一次关闭流程，清理定时器并结束全部等待请求。 */
  private close(error: Error): void {
    if (this.closed) return
    this.closed = true
    this.inboundRequests.clear()
    for (const [id, pending] of this.pending) {
      this.pending.delete(id)
      if (pending.timeout) clearTimeout(pending.timeout)
      pending.reject(error)
    }
    this.emit("close", error)
  }
}

/** 兼容旧导入名称；实现已经是 v2 双向 Peer。 */
export { JsonRpcPeer as IpcClient }

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
