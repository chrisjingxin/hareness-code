/** Agent Gateway Port：定义 Interactive Core 与底层 Agent 通信的依赖倒置契约及错误类型。 */

import {
  EventType,
  type ApprovalMode,
  type ConfigChange,
  type ContextCompactResult,
  type EventEnvelope,
  type InteractionMode,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type McpAddParams,
  type McpAddResult,
  type McpRemoveResult,
  type McpStatusResult,
  type ModelsListResult,
  type RequestedSkill,
  type RunCancelResult,
  type SkillsListResult,
  type SkillsSetEnabledResult,
  type ThreadModelSelection,
  type ThreadsListResult,
  type ThreadsOpenResult,
  type ThreadsSideQuestionParams,
  type ThreadsSideQuestionResult,
} from "@za38/protocol"

/** AgentGateway 稳定错误：将底层的网络/RPC 远程异常收敛为 Core 统一可识别的错误。 */
export class AgentGatewayError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = "AgentGatewayError"
  }
}

/** 一次 AgentRun 的唯一终态；Port 控制终态与其关联事件。 */
export type InteractiveRunCompletion =
  | { outcome: "completed"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_COMPLETED }> }
  | { outcome: "cancelled"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_CANCELLED }> }
  | { outcome: "failed"; event: Extract<EventEnvelope, { type: typeof EventType.RUN_FAILED }> }

/** 启动一次 Run 的输入参数类型；纯 TypeScript 描述，不依赖特定 IPC client。 */
export type AgentGatewayStartRunInput = {
  readonly message: string
  readonly mode: InteractionMode
  readonly threadId?: string
  readonly requestedSkill?: RequestedSkill
  readonly modelSelection?: ThreadModelSelection
  readonly approvalMode?: ApprovalMode
}

/** Pending Interaction 结构 */
export type PendingInteraction = {
  request: InteractionRequestEnvelope
  resolve: (response: InteractionResponse) => void
  timerId?: () => void
}

/** Run handle：事件队列、受理和唯一终态都由 Gateway 拥有，Controller 只消费。 */
export type InteractiveAgentRun = {
  readonly ref: { threadId: string; runId: string }
  readonly accepted: Promise<void>
  readonly events: AsyncIterable<EventEnvelope>
  readonly completion: Promise<InteractiveRunCompletion>
  cancel(): Promise<boolean>
}

/**
 * Interactive Core 与底层网关之间的 Port 接口（零 IPC / 平台依赖）。
 * 生产实现由 infrastructure/agent-client-gateway 提供，测试使用内存实现。
 */
export interface AgentGateway {
  /** 订阅协议帧错误；返回取消函数。 */
  onProtocolError(listener: (error: Error) => void): () => void
  /** 订阅连接关闭；返回取消函数。 */
  onClose(listener: (error: Error) => void): () => void
  /** 注册反向 Interaction 处理器；返回取消函数。 */
  setInteractionHandler(handler: (request: InteractionRequestEnvelope) => Promise<InteractionResponse>): () => void
  /** 服务端超时或终态后停止回写已经失效的交互响应。 */
  abandonInteraction(requestId: string): void
  /** 启动一次 Run；事件流只包含该 Run 的 Event。 */
  startRun(input: AgentGatewayStartRunInput): InteractiveAgentRun
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
  /** 读取 Skill catalog。 */
  listSkills(includeDisabled: boolean): Promise<SkillsListResult>
  /** 设置 Skill 启用状态。 */
  setSkillEnabled(skillId: string, enabled: boolean): Promise<SkillsSetEnabledResult>
  /** 执行临时只读单轮问答（/btw），0 工具，不写存储。 */
  sideQuestion(params: ThreadsSideQuestionParams): Promise<ThreadsSideQuestionResult>
}

export function createFallbackNoopGateway(): AgentGateway {
  return {
    startRun() {
      return {
        ref: { runId: "fallback-run", threadId: "fallback-thread" },
        accepted: Promise.resolve(),
        events: (async function* () {})(),
        completion: new Promise(() => {}),
        cancel: async () => true,
      }
    },
    async cancel() { return { run_id: "", cancelled: false } },
    async listThreads() { return { threads: [] } },
    async openThread(id) { return { thread: { thread_id: id, created_at_ms: 0, updated_at_ms: 0, first_message: "", latest_message: "", message_count: 0 }, messages: [] } },
    async listModels() { return { profiles: [] } },
    async listSkills() { return { snapshot: { id: "empty", count: 0 }, skills: [], diagnostics: [] } },
    async setSkillEnabled() { return {} },
    async sideQuestion(params) { return { reply_text: `echo: ${params.question}`, model_profile_id: params.model_profile_id ?? "echo" } },
    async mcpStatus() { return { servers: [], total_tools: 0 } },
    async mcpAdd() { return { added: false, connected: false, tool_names: [] } },
    async mcpRemove() { return { removed: false } },
    async configDetails() { return { revision: "0", fields: [], immutable_fields: [] } },
    async previewConfig() { return { revision: "0", changes: [], applies_to: [] } },
    async commitConfig() { return { revision: "0", changes: [], applies_to: [] } },
    async compactContext() { return { compacted: true, context: { action: "manual_summary" } } },
    abandonInteraction() {},
    onProtocolError() { return () => {} },
    onClose() { return () => {} },
    setInteractionHandler() { return () => {} },
  }
}
