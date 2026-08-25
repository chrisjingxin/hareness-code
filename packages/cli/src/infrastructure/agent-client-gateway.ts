/** AgentClientGateway 基础设施：将通信传输层的 AgentClient 适配为 Core 依赖的 AgentGateway 接口，并集中收敛错误映射。 */

import type {
  AgentsListResult,
  ConfigChange,
  ContextCompactResult,
  InteractionRequestEnvelope,
  InteractionResponse,
  McpAddParams,
  McpAddResult,
  McpRemoveResult,
  McpStatusResult,
  ModelsListResult,
  RunCancelResult,
  SkillsListResult,
  SkillsSetEnabledResult,
  TeamDefinition,
  TeamsCancelResult,
  TeamsGenerateParams,
  TeamsInspectParams,
  TeamsInspectResult,
  TeamsListResult,
  TeamsRunParams,
  TeamsRunResult,
  ThreadsListResult,
  ThreadsOpenResult,
  ThreadsSideQuestionParams,
  ThreadsSideQuestionResult,
} from "@za38/protocol"

import { AgentClient, JsonRpcRemoteError } from "../ipc/client"
import {
  AgentGatewayError,
  type AgentGateway,
  type AgentGatewayStartRunInput,
  type InteractiveAgentRun,
} from "../interactive/ports/agent-gateway"

/** 生产 AgentClientGateway：实现 AgentGateway 接口，并负责 RPC 远程错误收敛。 */
export class AgentClientGateway implements AgentGateway {
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

  startRun(input: AgentGatewayStartRunInput): InteractiveAgentRun {
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
    try {
      return await this.client.cancel(threadId, runId)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async compactContext(threadId: string): Promise<ContextCompactResult> {
    try {
      return await this.client.compactContext(threadId)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async abandonCompose(threadId: string, reason?: string): Promise<{ progress: unknown }> {
    try {
      return await this.client.abandonCompose(threadId, reason)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async configDetails(): Promise<{ revision: string; fields: readonly unknown[]; immutable_fields: readonly unknown[] }> {
    try {
      return await this.client.configDetails()
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async previewConfig(changes: ConfigChange[]): Promise<{ revision: string; changes: readonly unknown[]; applies_to: readonly string[] }> {
    try {
      return await this.client.previewConfig(changes)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async commitConfig(expectedRevision: string, changes: ConfigChange[]): Promise<{ revision: string; changes: readonly unknown[]; applies_to: readonly string[] }> {
    try {
      return await this.client.commitConfig(expectedRevision, changes)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async listThreads(): Promise<ThreadsListResult> {
    try {
      return await this.client.listThreads()
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async openThread(threadId: string): Promise<ThreadsOpenResult> {
    try {
      return await this.client.openThread(threadId)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async sideQuestion(params: ThreadsSideQuestionParams): Promise<ThreadsSideQuestionResult> {
    try {
      return await this.client.sideQuestion(params)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async mcpStatus(): Promise<McpStatusResult> {
    try {
      return await this.client.mcpStatus()
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async mcpAdd(params: McpAddParams): Promise<McpAddResult> {
    try {
      return await this.client.mcpAdd(params)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async mcpRemove(name: string): Promise<McpRemoveResult> {
    try {
      return await this.client.mcpRemove(name)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async listModels(threadId?: string): Promise<ModelsListResult> {
    try {
      return await this.client.listModels(threadId)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async listSkills(includeDisabled: boolean): Promise<SkillsListResult> {
    try {
      return await this.client.request("skills.list", { include_disabled: includeDisabled })
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async setSkillEnabled(skillId: string, enabled: boolean): Promise<SkillsSetEnabledResult> {
    try {
      return await this.client.request("skills.set_enabled", { id: skillId, enabled })
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async listAgents(): Promise<AgentsListResult> {
    try {
      return await this.client.listAgents()
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async listTeams(): Promise<TeamsListResult> {
    try {
      return await this.client.listTeams()
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async inspectTeam(kind: TeamsInspectParams["kind"], id: string): Promise<TeamsInspectResult> {
    try {
      return await this.client.inspectTeam(kind, id)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async generateTeam(params: TeamsGenerateParams): Promise<TeamDefinition> {
    try {
      return await this.client.generateTeam(params)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async runTeam(params: TeamsRunParams): Promise<TeamsRunResult> {
    try {
      return await this.client.runTeam(params)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  async cancelTeam(runId: string): Promise<TeamsCancelResult> {
    try {
      return await this.client.cancelTeam(runId)
    } catch (error) {
      throw this.wrapError(error)
    }
  }

  private wrapError(error: unknown): Error {
    if (error instanceof JsonRpcRemoteError) {
      return new AgentGatewayError(String(error.code), error.message)
    }
    if (error instanceof AgentGatewayError) {
      return error
    }
    return new AgentGatewayError("UNKNOWN_GATEWAY_ERROR", error instanceof Error ? error.message : String(error))
  }
}
