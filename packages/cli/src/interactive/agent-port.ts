/** Interactive Core 的 remote-owned seam：生产 adapter 包装 AgentClient，测试使用内存实现。 */

import {
  EventType,
  type ConfigChange,
  type ContextCompactResult,
  type EventEnvelope,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type McpAddParams,
  type McpAddResult,
  type McpRemoveResult,
  type McpStatusResult,
  type ModelsListResult,
  type RunCancelResult,
  type SkillsListResult,
  type ThreadsListResult,
  type ThreadsOpenResult,
} from "@za38/protocol"

import { AgentClient, type StartRunInput } from "../ipc/client"

/** 一次 AgentRun 的唯一终态；与 AgentClient 的 RunCompletion 形状一致但不依赖其类型。 */
export type InteractiveRunCompletion =
  | { outcome: "completed"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_COMPLETED }> }
  | { outcome: "cancelled"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_CANCELLED }> }
  | { outcome: "failed"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_FAILED }> }

/** Run handle：事件队列、受理和唯一终态都由 port 拥有，Controller 只消费。 */
export type InteractiveAgentRun = {
  readonly ref: { threadId: string; runId: string }
  readonly accepted: Promise<void>
  readonly events: AsyncIterable<EventEnvelope>
  readonly completion: Promise<InteractiveRunCompletion>
  cancel(): Promise<boolean>
}

/**
 * Interactive Core 与远端之间的窄 seam。它不是让 UI 选择 transport 的扩展点，
 * 也不暴露底层 request(method, params)；生产实现包装 AgentClient，测试使用内存实现。
 */
export interface InteractiveAgentPort {
  /** 订阅协议帧错误；返回取消函数。 */
  onProtocolError(listener: (error: Error) => void): () => void
  /** 订阅连接关闭；返回取消函数。 */
  onClose(listener: (error: Error) => void): () => void
  /** 注册反向 Interaction 处理器；返回取消函数。 */
  setInteractionHandler(handler: (request: InteractionRequestEnvelope) => Promise<InteractionResponse>): () => void
  /** 服务端超时或终态后停止回写已经失效的交互响应。 */
  abandonInteraction(requestId: string): void
  /** 启动一次 Run；事件流只包含该 Run 的 Event。 */
  startRun(input: StartRunInput): InteractiveAgentRun
  cancel(threadId: string, runId: string): Promise<RunCancelResult>
  compactContext(threadId: string): Promise<ContextCompactResult>
  configDetails(): Promise<{ revision: string; fields: readonly unknown[]; immutable_fields: readonly unknown[] }>
  previewConfig(changes: ConfigChange[]): Promise<{ revision: string; changes: readonly unknown[]; applies_to: readonly string[] }>
  commitConfig(expectedRevision: string, changes: ConfigChange[]): Promise<{ revision: string; changes: readonly unknown[]; applies_to: readonly string[] }>
  listThreads(): Promise<ThreadsListResult>
  openThread(threadId: string): Promise<ThreadsOpenResult>
  mcpStatus(): Promise<McpStatusResult>
  mcpAdd(params: McpAddParams): Promise<McpAddResult>
  mcpRemove(name: string): Promise<McpRemoveResult>
  listModels(threadId?: string): Promise<ModelsListResult>
  listSkills(): Promise<SkillsListResult>
}

/** 生产 adapter：把 transport-neutral 的 AgentClient 适配为 InteractiveAgentPort。 */
export class AgentClientInteractiveAdapter implements InteractiveAgentPort {
  constructor(private readonly client: AgentClient) {}

  onProtocolError(listener: (error: Error) => void): () => void {
    this.client.on("protocolError", listener)
    return () => this.client.off("protocolError", listener)
  }

  onClose(listener: (error: Error) => void): () => void {
    this.client.on("close", listener)
    return () => this.client.off("close", listener)
  }

  setInteractionHandler(handler: (request: InteractionRequestEnvelope) => Promise<InteractionResponse>): () => void {
    return this.client.setRequestHandler(handler)
  }

  abandonInteraction(requestId: string): void {
    this.client.abandonInteraction(requestId)
  }

  startRun(input: StartRunInput): InteractiveAgentRun {
    const run = this.client.startRun(input)
    return {
      ref: run.ref,
      accepted: run.accepted,
      events: run.events,
      completion: run.completion,
      cancel: () => run.cancel(),
    }
  }

  async cancel(threadId: string, runId: string): Promise<RunCancelResult> {
    return this.client.cancel(threadId, runId)
  }

  async compactContext(threadId: string): Promise<ContextCompactResult> {
    return this.client.compactContext(threadId)
  }

  async configDetails(): Promise<{ revision: string; fields: readonly unknown[]; immutable_fields: readonly unknown[] }> {
    return this.client.configDetails()
  }

  async previewConfig(changes: ConfigChange[]): Promise<{ revision: string; changes: readonly unknown[]; applies_to: readonly string[] }> {
    return this.client.previewConfig(changes)
  }

  async commitConfig(expectedRevision: string, changes: ConfigChange[]): Promise<{ revision: string; changes: readonly unknown[]; applies_to: readonly string[] }> {
    return this.client.commitConfig(expectedRevision, changes)
  }

  async listThreads(): Promise<ThreadsListResult> {
    return this.client.listThreads()
  }

  async openThread(threadId: string): Promise<ThreadsOpenResult> {
    return this.client.openThread(threadId)
  }

  async mcpStatus(): Promise<McpStatusResult> {
    return this.client.mcpStatus()
  }

  async mcpAdd(params: McpAddParams): Promise<McpAddResult> {
    return this.client.mcpAdd(params)
  }

  async mcpRemove(name: string): Promise<McpRemoveResult> {
    return this.client.mcpRemove(name)
  }

  async listModels(threadId?: string): Promise<ModelsListResult> {
    return this.client.listModels(threadId)
  }

  async listSkills(): Promise<SkillsListResult> {
    return this.client.request("skills.list", { include_disabled: false })
  }
}
