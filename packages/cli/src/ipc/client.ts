/** transport-neutral AgentClient：隐藏请求关联、校验、事件与 Interaction 生命周期。 */

import {
  EventType,
  Method,
  assertEventEnvelope,
  type ApprovalMode,
  assertJsonRpcMessage,
  isInteractionMethod,
  validateInteractionParams,
  validateInteractionResult,
  validateOperationParams,
  validateOperationResult,
  validateProtocolErrorData,
  type EventEnvelope,
  type AgentsListResult,
  type AgentSummary,
  type ContextCompactResult,
  type ConfigChange,
  type ConfigCommitResult,
  type ConfigDetailsResult,
  type ConfigPreviewResult,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type InteractionMethod,
  type JsonRpcMessage,
  type JsonRpcResponse,
  type McpAddParams,
  type McpAddResult,
  type McpRemoveResult,
  type McpStatusResult,
  type ModelsListResult,
  type PluginsInspectResult,
  type PluginsInstallResult,
  type PluginsListResult,
  type PluginsRemoveResult,
  type PluginsSetEnabledResult,
  type PluginsSourceParams,
  type PluginsValidateResult,
  type OperationMap,
  type OperationName,
  type InitializeParams,
  type InitializeResult,
  type RunCancelResult,
  type RequestedSkill,
  type ThreadModelSelection,
  type ThreadsListResult,
  type ThreadsOpenResult,
  type TeamDefinition,
  type TeamsCancelResult,
  type TeamsGenerateParams,
  type TeamsInspectParams,
  type TeamsInspectResult,
  type TeamsListResult,
  type TeamsRunParams,
  type TeamsRunResult,
} from "@za38/protocol"
import { AsyncQueue, type RpcTransport } from "./transport"

export type { RpcTransport } from "./transport"

type PendingRequest = {
  method: OperationName
  resolve: (value: unknown) => void
  reject: (error: Error) => void
  timeout: ReturnType<typeof setTimeout> | undefined
}

export type PeerRequestHandler = (params: InteractionRequestEnvelope) => Promise<InteractionResponse> | InteractionResponse
export type InteractionHandler = PeerRequestHandler

export type StartRunInput = {
  message: string
  threadId?: string
  requestedSkill?: RequestedSkill
  modelSelection?: ThreadModelSelection
  approvalMode?: ApprovalMode
}

export type RunCompletion =
  | { outcome: "completed"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_COMPLETED }> }
  | { outcome: "cancelled"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_CANCELLED }> }
  | { outcome: "failed"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_FAILED }> }

export interface AgentRun {
  readonly ref: { threadId: string; runId: string }
  readonly accepted: Promise<void>
  readonly events: AsyncIterable<EventEnvelope>
  readonly completion: Promise<RunCompletion>
  cancel(): Promise<boolean>
}

export interface ThreadWatch {
  readonly snapshot: ThreadsOpenResult
  readonly events: AsyncIterable<EventEnvelope>
  close(): Promise<void>
}

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
export class AgentClient {
  private static readonly MAX_TIMED_OUT_REQUEST_IDS = 256
  private nextId = 1
  private readonly pending = new Map<string, PendingRequest>()
  private readonly timedOutRequestIds = new Set<string>()
  private readonly inboundRequests = new Set<string>()
  private readonly listeners = new Map<string, Set<(...args: any[]) => void>>()
  private closed = false
  private requestHandler: PeerRequestHandler | undefined
  private initializedInfo: InitializeResult | undefined

  constructor(private readonly transport: RpcTransport) {
    void this.consumeMessages()
  }

  /** 轻量事件订阅，避免把 Node EventEmitter 带进浏览器 adapter。 */
  on(event: string, listener: (...args: any[]) => void): this {
    const listeners = this.listeners.get(event) ?? new Set()
    listeners.add(listener)
    this.listeners.set(event, listeners)
    return this
  }

  off(event: string, listener: (...args: any[]) => void): this {
    this.listeners.get(event)?.delete(listener)
    return this
  }

  emit(event: string, ...args: any[]): boolean {
    const listeners = this.listeners.get(event)
    if (!listeners?.size) return false
    for (const listener of [...listeners]) listener(...args)
    return true
  }

  /** 注册 Agent 反向发起的审批或问答处理器；返回函数用于组件卸载时清理。 */
  setRequestHandler(handler: PeerRequestHandler): () => void {
    this.requestHandler = handler
    return () => {
      if (this.requestHandler === handler) this.requestHandler = undefined
    }
  }

  /** 返回握手后的稳定 Host/Connection 摘要。 */
  get info(): InitializeResult {
    if (!this.initializedInfo) throw new Error("AgentClient has not been initialized")
    return this.initializedInfo
  }

  /** 以 v3 握手初始化 Connection。 */
  async initialize(params: InitializeParams): Promise<InitializeResult> {
    const result = await this.request(Method.INITIALIZE, params)
    this.initializedInfo = result
    return result
  }

  /** `handleInteractions` 是表现层使用的语义名称。 */
  handleInteractions(handler: InteractionHandler): () => void {
    return this.setRequestHandler(handler)
  }

  /** 在发送请求前建立事件路由，返回拥有取消和唯一终态的 Run handle。 */
  startRun(input: StartRunInput): AgentRun {
    const threadId = input.threadId ?? crypto.randomUUID()
    const runId = crypto.randomUUID()
    const events = new AsyncQueue<EventEnvelope>()
    let resolveCompletion!: (value: RunCompletion) => void
    let rejectCompletion!: (error: Error) => void
    const completion = new Promise<RunCompletion>((resolve, reject) => {
      resolveCompletion = resolve
      rejectCompletion = reject
    })
    const listener = (event: EventEnvelope) => {
      if (event.thread_id !== threadId || event.run_id !== runId) return
      events.push(event)
      if (event.type === EventType.RUN_COMPLETED) finish({ outcome: "completed", event })
      if (event.type === EventType.RUN_CANCELLED) finish({ outcome: "cancelled", event })
      if (event.type === EventType.RUN_FAILED) finish({ outcome: "failed", event })
    }
    const finish = (value: RunCompletion) => {
      this.off("event", listener)
      this.off("close", closeListener)
      events.end()
      resolveCompletion(value)
    }
    const fail = (error: Error) => {
      this.off("event", listener)
      this.off("close", closeListener)
      events.fail(error)
      rejectCompletion(error)
    }
    const closeListener = (error: Error) => fail(error)
    this.on("event", listener)
    this.on("close", closeListener)
    const accepted = this.request(Method.RUN_START, {
      message: input.message,
      thread_id: threadId,
      run_id: runId,
      requested_skill: input.requestedSkill,
      model_selection: input.modelSelection,
      approval_mode: input.approvalMode,
    }, 0).then(result => {
      if (result.thread_id !== threadId || result.run_id !== runId || !result.accepted) {
        throw new Error("run.start returned a mismatched identity")
      }
    }).catch(error => {
      fail(error instanceof Error ? error : new Error(String(error)))
      throw error
    })
    return {
      ref: { threadId, runId },
      accepted,
      events,
      completion,
      cancel: async () => (await this.cancel(threadId, runId)).cancelled,
    }
  }

  /** 原子读取空闲 Thread 并订阅之后的事件。 */
  async watchThread(threadId: string): Promise<ThreadWatch> {
    const events = new AsyncQueue<EventEnvelope>()
    const listener = (event: EventEnvelope) => {
      if (event.thread_id === threadId) events.push(event)
    }
    const closeListener = (error: Error) => events.fail(error)
    this.on("event", listener)
    this.on("close", closeListener)
    let snapshot: ThreadsOpenResult
    try {
      snapshot = await this.request(Method.THREADS_WATCH, { thread_id: threadId })
    } catch (error) {
      this.off("event", listener)
      this.off("close", closeListener)
      events.fail(error instanceof Error ? error : new Error(String(error)))
      throw error
    }
    let closed = false
    return {
      snapshot,
      events,
      close: async () => {
        if (closed) return
        closed = true
        this.off("event", listener)
        this.off("close", closeListener)
        events.end()
        await this.request(Method.THREADS_UNWATCH, { thread_id: threadId })
      },
    }
  }

  /** 运行取消或服务端超时后停止回写已经失效的交互响应。 */
  abandonInteraction(requestId: string): void {
    this.inboundRequests.delete(requestId)
  }

  /** 发送带超时保护的请求，并返回对应 JSON-RPC result。 */
  request<M extends OperationName>(
    method: M,
    params: OperationMap[M]["params"],
    timeoutMs = 30_000,
  ): Promise<OperationMap[M]["result"]> {
    if (this.closed) return Promise.reject(new Error("Agent connection is closed"))
    validateOperationParams(method, params)
    const id = `req-${this.nextId++}`
    return new Promise((resolve, reject) => {
      const timeout = timeoutMs > 0
        ? setTimeout(() => {
            this.pending.delete(id)
            this.rememberTimedOutRequest(id)
            reject(new Error(`Timed out waiting for ${method}`))
          }, timeoutMs)
        : undefined
      this.pending.set(id, { method, resolve: value => resolve(value as OperationMap[M]["result"]), reject, timeout })
      void this.send({ jsonrpc: "2.0", method, params, id }).catch(error => {
        this.pending.delete(id)
        if (timeout) clearTimeout(timeout)
        reject(error)
      })
    }) as Promise<OperationMap[M]["result"]>
  }

  /** 请求取消指定运行。 */
  cancel(threadId: string, runId: string): Promise<RunCancelResult> {
    return this.request(Method.RUN_CANCEL, { thread_id: threadId, run_id: runId })
  }

  /** 在当前 thread 空闲时请求 sidecar 强制生成一次结构化上下文摘要。 */
  compactContext(threadId: string): Promise<ContextCompactResult> {
    return this.request(Method.CONTEXT_COMPACT, { thread_id: threadId }, 0)
  }

  /** 读取受控配置字段、来源锁和可修改范围；不返回 TOML 原文或秘密。 */
  configDetails(): Promise<ConfigDetailsResult> {
    return this.request(Method.CONFIG_DETAILS, {})
  }

  /** 预览白名单配置变更，并返回提交所需的 CAS revision。 */
  previewConfig(changes: ConfigChange[]): Promise<ConfigPreviewResult> {
    return this.request(Method.CONFIG_PREVIEW, { changes })
  }

  /** 使用预览 revision 原子提交白名单配置变更。 */
  commitConfig(expectedRevision: string, changes: ConfigChange[]): Promise<ConfigCommitResult> {
    return this.request(Method.CONFIG_COMMIT, { expected_revision: expectedRevision, changes })
  }

  /** 读取当前 project 的可恢复 thread 摘要；thread_id 只在 TUI 内部用于后续打开。 */
  listThreads(limit = 80): Promise<ThreadsListResult> {
    return this.request(Method.THREADS_LIST, { limit })
  }

  /** 打开当前 project 的既有 thread，并返回可以重新构造时间线的消息。 */
  openThread(threadId: string): Promise<ThreadsOpenResult> {
    return this.request(Method.THREADS_OPEN, { thread_id: threadId })
  }

  /** 查询所有已配置 MCP 服务器的运行时连接状态和工具列表。 */
  mcpStatus(): Promise<McpStatusResult> {
    return this.request(Method.MCP_STATUS, {})
  }

  /** 添加 MCP 服务器到用户配置并尝试热连接。 */
  mcpAdd(params: McpAddParams): Promise<McpAddResult> {
    return this.request(Method.MCP_ADD, { ...params })
  }

  /** 从用户配置中删除 MCP 服务器。 */
  mcpRemove(name: string): Promise<McpRemoveResult> {
    return this.request(Method.MCP_REMOVE, { name })
  }

  /** 列出 Plugin registry 与当前已启用 catalog。 */
  listPlugins(includeDisabled = true): Promise<PluginsListResult> {
    return this.request(Method.PLUGINS_LIST, { include_disabled: includeDisabled })
  }

  /** 查看一个 Plugin 的组件兼容性和 trust 摘要。 */
  inspectPlugin(id: string): Promise<PluginsInspectResult> {
    return this.request(Method.PLUGINS_INSPECT, { id })
  }

  /** 离线校验本地目录或 zip，不修改 PluginStore。 */
  validatePlugin(
    source: string,
    format: PluginsSourceParams["format"] = "auto",
  ): Promise<PluginsValidateResult> {
    return this.request(Method.PLUGINS_VALIDATE, { source, format })
  }

  /** copy-on-install 本地 Plugin；安装结果始终为 disabled。 */
  installPlugin(
    source: string,
    format: PluginsSourceParams["format"] = "auto",
  ): Promise<PluginsInstallResult> {
    return this.request(Method.PLUGINS_INSTALL, { source, format })
  }

  /** 使用当前 capability fingerprint 显式启用或停用 Plugin。 */
  setPluginEnabled(
    id: string,
    enabled: boolean,
    capabilityFingerprint?: string,
  ): Promise<PluginsSetEnabledResult> {
    return this.request(Method.PLUGINS_SET_ENABLED, {
      id,
      enabled,
      capability_fingerprint: capabilityFingerprint,
    })
  }

  /** 删除 Plugin 安装记录；持久数据默认保留。 */
  removePlugin(id: string, purgeData = false): Promise<PluginsRemoveResult> {
    return this.request(Method.PLUGINS_REMOVE, { id, purge_data: purgeData })
  }

  /** 列出启动期固定的 Plugin Agent 摘要。 */
  listAgents(): Promise<AgentsListResult> {
    return this.request(Method.AGENTS_LIST, {})
  }

  /** 查看一个 Plugin Agent 的脱敏定义。 */
  inspectAgent(id: string): Promise<AgentSummary> {
    return this.request(Method.AGENTS_INSPECT, { id })
  }

  /** 列出固定 Team 与当前 Host 已确认的生成预览。 */
  listTeams(): Promise<TeamsListResult> {
    return this.request(Method.TEAMS_LIST, {})
  }

  /** 查看 TeamDefinition 或可恢复 TeamRun。 */
  inspectTeam(kind: TeamsInspectParams["kind"], id: string): Promise<TeamsInspectResult> {
    return this.request(Method.TEAMS_INSPECT, { kind, id })
  }

  /** 从已验证 Agent ID 生成 fanout Team 预览。 */
  generateTeam(params: TeamsGenerateParams): Promise<TeamDefinition> {
    return this.request(Method.TEAMS_GENERATE, params)
  }

  /** 异步启动一个固定 Team。 */
  runTeam(params: TeamsRunParams): Promise<TeamsRunResult> {
    return this.request(Method.TEAMS_RUN, params)
  }

  /** 请求取消一个当前 Host 中活动的 TeamRun。 */
  cancelTeam(runId: string): Promise<TeamsCancelResult> {
    return this.request(Method.TEAMS_CANCEL, { run_id: runId })
  }

  /** 读取 `/model` Picker 所需的脱敏 Profile 目录与可选 Thread 绑定。 */
  listModels(threadId?: string): Promise<ModelsListResult> {
    return this.request(Method.MODELS_LIST, { thread_id: threadId })
  }

  /** 主动释放连接并拒绝所有尚未完成的请求。 */
  destroy(): void {
    this.closeTransport(new Error("Agent connection closed"))
    void this.transport.close()
  }

  /** 关闭当前 Connection；Host 生命周期由 launcher 管理。 */
  async close(): Promise<void> {
    this.closeTransport(new Error("Agent connection closed"))
    await this.transport.close()
  }

  /** 消费 transport 消息，并把 framing 之外的错误统一截断在协议 seam。 */
  private async consumeMessages(): Promise<void> {
    try {
      for await (const message of this.transport.messages) {
        if (this.closed) return
        try {
          assertJsonRpcMessage(message)
          this.handleMessage(message)
        } catch (error) {
          this.emit("protocolError", new Error(`Invalid JSON-RPC frame: ${errorMessage(error)}`))
        }
      }
      this.closeTransport(new Error("Agent transport closed"))
    } catch (error) {
      const normalized = error instanceof Error ? error : new Error(String(error))
      this.emit("protocolError", normalized)
      this.closeTransport(normalized)
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
      if (this.timedOutRequestIds.delete(message.id)) return
      this.emit("protocolError", new Error(`Unknown JSON-RPC response id: ${message.id}`))
      return
    }
    this.pending.delete(message.id)
    if (pending.timeout) clearTimeout(pending.timeout)
    const response = message as JsonRpcResponse
    if (response.error) {
      if (response.error.code <= -32000 && response.error.code >= -32099) {
        validateProtocolErrorData(response.error.data)
      }
      pending.reject(new JsonRpcRemoteError(response.error.code, response.error.message, response.error.data))
    }
    else {
      try {
        pending.resolve(validateOperationResult(pending.method, response.result))
      } catch (error) {
        pending.reject(error instanceof Error ? error : new Error(String(error)))
      }
    }
  }

  /** 有界记住本地已超时 ID，只屏蔽对应迟到响应而不吞未知帧。 */
  private rememberTimedOutRequest(id: string): void {
    this.timedOutRequestIds.add(id)
    while (this.timedOutRequestIds.size > AgentClient.MAX_TIMED_OUT_REQUEST_IDS) {
      const oldest = this.timedOutRequestIds.values().next().value
      if (typeof oldest !== "string") return
      this.timedOutRequestIds.delete(oldest)
    }
  }

  /** 处理 Agent 发起的 request；无处理器或非法结果都返回标准错误响应。 */
  private async handleInboundRequest(method: string, id: string, params: Record<string, unknown>): Promise<void> {
    if (!isInteractionMethod(method)) {
      await this.sendError(id, -32601, `Unsupported server request: ${method}`)
      return
    }
    this.inboundRequests.add(id)
    try {
      const validated = validateInteractionParams(method, params)
      const request = {
        ...validated,
        request_id: id,
        type: method === Method.INTERACTION_APPROVAL ? "approval" as const : "question" as const,
      } as InteractionRequestEnvelope
      if (!this.requestHandler) throw new Error("Client has no interaction request handler")
      const result = await this.requestHandler(request)
      if (!this.inboundRequests.has(id)) return
      if (result.request_id !== id || result.type !== request.type) throw new Error("Interaction response does not match request")
      const wireResult = result.type === "approval"
        ? { decision: result.decision, feedback: result.feedback }
        : { answers: result.answers }
      validateInteractionResult(method as InteractionMethod, wireResult)
      this.inboundRequests.delete(id)
      await this.send({ jsonrpc: "2.0", id, result: wireResult })
    } catch (error) {
      if (!this.inboundRequests.has(id)) return
      this.inboundRequests.delete(id)
      await this.sendError(id, -32602, errorMessage(error))
    }
  }

  /** 通过当前 adapter 发送已关联的 JSON-RPC 消息。 */
  private async send(message: JsonRpcMessage): Promise<void> {
    if (this.closed) throw new Error("Agent connection is closed")
    await this.transport.send(message)
  }

  private async sendError(id: string, code: number, message: string): Promise<void> {
    await this.send({ jsonrpc: "2.0", id, error: { code, message } })
  }

  /** 只执行一次关闭流程，清理定时器并结束全部等待请求。 */
  private closeTransport(error: Error): void {
    if (this.closed) return
    this.closed = true
    this.inboundRequests.clear()
    this.timedOutRequestIds.clear()
    for (const [id, pending] of this.pending) {
      this.pending.delete(id)
      if (pending.timeout) clearTimeout(pending.timeout)
      pending.reject(error)
    }
    this.emit("close", error)
    this.listeners.clear()
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
