/** Interactive Core 薄协调器：只做 intent 路由、listener 管理、snapshot 组装与 Feature 生命周期编排；具体业务逻辑在 features/ 下按 Feature 拆分。 */

import type { Capability, ModelProfile } from "@za38/protocol"
import { contextCompactNotice, type CommandResult, type CommandRpcMethod, dispatchSlashCommand } from "./command-dispatcher"
import { builtinCommandCapabilities } from "./commands"
import { CatalogFeature, CommandFeature, InteractionFeature, McpFeature, ModelFeature, RunFeature, SkillFeature, ThreadFeature, TimelineFeature, type FeatureContext } from "./features"
import type { AgentGateway, Clock, IdGenerator, IntentOutcome, InteractiveConfirmation, InteractiveConnectionState, InteractiveController, InteractiveControllerOptions, InteractiveIntent, InteractiveSnapshot, LoadableCatalog, Scheduler } from "./ports"
import { createFallbackNoopGateway } from "./ports"
import { cryptoIdGenerator, systemClock, systemScheduler } from "../infrastructure"
import type { InteractiveRuntime } from "./runtime"
import { appendNotice, clearThread, createInitialState, finishContextCompaction, leaveChildTimeline, openChildTimeline, setWorkMode, startContextCompaction, type InteractiveState } from "./state"
import { scopeTimeline } from "../presentation-shared/timeline-scope"
export class InteractiveControllerImpl implements InteractiveController {
  private readonly gateway: AgentGateway
  private readonly clock: Clock
  private readonly idGenerator: IdGenerator
  private readonly baseRuntime: InteractiveRuntime
  private readonly scheduler: Scheduler
  private readonly listeners = new Set<(snapshot: InteractiveSnapshot) => void>()
  private readonly clearInteractionHandler: () => void
  private readonly unsubscribeProtocolError: () => void
  private readonly unsubscribeClose: () => void
  private state: InteractiveState
  private snapshot: InteractiveSnapshot
  private connection: InteractiveConnectionState = { status: "open" }
  private confirmation: InteractiveConfirmation | null = null
  private closed = false
  private compactInFlight = false

  // 九大 Feature 子模块实作
  private readonly catalogFeature = new CatalogFeature()
  private readonly skillFeature = new SkillFeature()
  private readonly mcpFeature = new McpFeature()
  private readonly modelFeature = new ModelFeature()
  private readonly threadFeature = new ThreadFeature()
  private readonly commandFeature: CommandFeature
  private readonly interactionFeature = new InteractionFeature()
  private readonly timelineFeature = new TimelineFeature()
  private readonly runFeature = new RunFeature()
  private get featureContext(): FeatureContext {
    return { gateway: this.gateway, clock: this.clock, scheduler: this.scheduler, idGenerator: this.idGenerator, baseRuntime: this.baseRuntime, getState: () => this.state, commit: u => this.commit(u), publish: () => this.publish() }
  }
  private get hasPendingInteraction(): boolean {
    return Boolean(this.interactionFeature.pendingInteraction)
  }

  /** 刷新 Model catalog 并把当前 thread 的 selection 收敛到共享选择。 */
  private refreshModelSelection(): Promise<void> {
    return this.catalogFeature.refreshModelCatalog(this.featureContext, id => this.adoptThreadSelection(id))
  }

  /**
   * 采纳服务端持久化的线程模型选择（重连/切换 Thread 恢复语义）。
   * 用户本会话已显式 /model 选择时不再采纳，避免陈旧的持久化值覆盖用户意图。
   */
  private adoptThreadSelection(id: string): void {
    if (this.modelFeature.explicitlySelected) return
    this.modelFeature.requestedModelProfileId = id
  }
  constructor(options: InteractiveControllerOptions) {
    this.gateway = options.gateway ?? options.agent ?? createFallbackNoopGateway()
    const defaultRuntime: InteractiveRuntime = { workspace: "", cliVersion: "0.1.0", modelConfigured: false, executionMode: "local", approvalMode: "default", capabilities: builtinCommandCapabilities }
    const rawRuntime = options.baseRuntime ?? options.runtime ?? defaultRuntime
    this.baseRuntime = { ...defaultRuntime, ...rawRuntime, capabilities: rawRuntime.capabilities ?? builtinCommandCapabilities }
    this.commandFeature = new CommandFeature(
      this.baseRuntime.agentCommands,
      this.baseRuntime.commandRegistry,
    )
    this.clock = options.clock ?? systemClock
    this.scheduler = options.scheduler ?? systemScheduler
    this.idGenerator = options.idGenerator ?? cryptoIdGenerator
    this.state = createInitialState(options.initialThreadId ?? null)
    this.snapshot = this.buildSnapshot()
    this.unsubscribeProtocolError = this.gateway.onProtocolError?.(error => {
      if (this.closed) return
      this.connection = { status: "protocol-error", message: error.message }
      this.commit(current => appendNotice(current, `protocol-error: ${error.message}`))
    }) ?? (() => {})

    this.unsubscribeClose = this.gateway.onClose?.(error => {
      if (this.closed) return
      this.connection = { status: "closed", message: error.message }
      this.interactionFeature.settlePendingInteraction(this.featureContext)
      this.commit(current => appendNotice(finishContextCompaction(current), `connection-closed: ${error.message}`))
    }) ?? (() => {})

    this.clearInteractionHandler = this.gateway.setInteractionHandler?.(request =>
      this.interactionFeature.handleInteractionRequest(request, this.featureContext)
    ) ?? (() => {})

    void this.catalogFeature.refreshSkillCatalog(this.featureContext)
    if (options.initialThreadId !== undefined) {
      void this.threadFeature.restoreInitialThread(options.initialThreadId, this.featureContext, {
        onSuccess: () => this.refreshModelSelection(),
      })
    }
  }
  getSnapshot(): InteractiveSnapshot {
    return this.snapshot
  }
  getGateway(): AgentGateway {
    return this.gateway
  }
  subscribe(listener: (snapshot: InteractiveSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  async dispatch(intent: InteractiveIntent): Promise<IntentOutcome> {
    if (this.closed) return { status: "rejected", code: "connection-closed", message: "Controller is closed" }
    if (this.state.pendingOperation && blocksPendingOperation(intent)) {
      return { status: "rejected", code: "busy", message: "上下文正在压缩；完成前不能执行该操作" }
    }

    switch (intent.type) {
      case "input.submit":
        if (this.state.childTimelineExecutionId) {
          return { status: "rejected", code: "busy", message: "子代理时间线只读，返回主对话后再发送" }
        }
        return this.handleSubmit(intent.value, intent.mode)
      case "child-timeline.open":
        this.commit(current => openChildTimeline(current, intent.executionId))
        return { status: "accepted" }
      case "child-timeline.leave":
        this.commit(leaveChildTimeline)
        return { status: "accepted" }

      case "command.execute":
        return this.commandFeature.executeSlashCommand({ id: intent.commandId, name: intent.commandId, argument: intent.argument }, this.featureContext, {
          hasPendingInteraction: this.hasPendingInteraction, applyResult: res => this.applyCommandResult(res),
        })

      case "thread.open":
        return this.threadFeature.openThread(intent.threadId, this.featureContext, {
          hasPendingInteraction: this.hasPendingInteraction, onBeforeOpen: () => this.resetThreadState(), onSuccess: () => {
            this.refreshModelSelection()
            void this.catalogFeature.refreshThreadCatalog(this.featureContext)
          },
        })

      case "model.select":
        if (this.state.activeRun || this.hasPendingInteraction) {
          return { status: "rejected", code: "busy", message: "任务运行中或存在待处理交互，暂不能切换模型" }
        }
        return this.modelFeature.selectModel(intent.profileId, this.featureContext, {
          models: this.catalogFeature.state.models.items, onModelsRefreshed: () => this.refreshModelSelection(),
        })

      case "skill.arm":
        return this.skillFeature.armSkill(intent.skillId, this.featureContext, {
          skills: this.catalogFeature.state.skills.items,
        })

      case "skill.clear":
        this.skillFeature.clearArmedSkill(this.featureContext)
        return { status: "accepted" }

      case "skill.set-enabled":
        return this.skillFeature.setSkillEnabled(intent.skillId, intent.enabled, this.featureContext, {
          hasCapability: this.hasCapability("skills.manage"), hasSkill: this.catalogFeature.state.skills.items.some(item => item.id === intent.skillId),
          onSuccess: () => this.catalogFeature.refreshSkillCatalog(this.featureContext),
        })

      case "mcp.add":
        return this.mcpFeature.addMcpServer(intent.input, this.featureContext, {
          hasCapability: this.hasCapability("mcp.manage"), onSuccess: () => this.catalogFeature.refreshMcpCatalog(this.featureContext),
        })

      case "mcp.remove":
        return this.mcpFeature.removeMcpServer(intent.name, this.featureContext, {
          hasCapability: this.hasCapability("mcp.manage"), onSuccess: () => this.catalogFeature.refreshMcpCatalog(this.featureContext),
        })

      case "catalog.refresh":
        await this.catalogFeature.refreshCatalog(intent.catalog, this.featureContext, id => this.adoptThreadSelection(id))
        return { status: "accepted" }

      case "interaction.respond":
        return this.interactionFeature.respondInteraction(intent.requestId, { request_id: intent.requestId, ...intent.response } as any, this.featureContext)

      case "confirmation.resolve":
        return this.resolveConfirmation(intent.confirmationId, intent.confirmed)

      case "approval-mode.cycle":
        if (this.state.activeRun || this.hasPendingInteraction) {
          return { status: "rejected", code: "busy", message: "任务运行中或存在待处理交互，暂不能切换审批模式" }
        }
        return this.runFeature.cycleApprovalMode(this.featureContext)
      case "work-mode.cycle":
        if (this.state.activeRun || this.hasPendingInteraction || this.state.activity.kind === "cancelling" || this.compactInFlight) {
          return { status: "rejected", code: "busy", message: "任务运行中、上下文压缩中或存在待处理交互，暂不能切换工作模式" }
        }
        this.commit(current => setWorkMode(current, current.workMode === "build" ? "compose" : "build"))
        return { status: "accepted" }
      case "approval-mode.set":
        if (this.state.activeRun || this.hasPendingInteraction) {
          return { status: "rejected", code: "busy", message: "任务运行中或存在待处理交互，暂不能切换审批模式" }
        }
        return this.runFeature.setApprovalMode(intent.mode, this.featureContext)
      case "run.cancel":
        return this.runFeature.cancelActiveRun(this.featureContext, () => this.interactionFeature.abandonPendingInteraction(this.featureContext))

      default:
        return { status: "rejected", code: "invalid-argument", message: "Unknown intent" }
    }
  }
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.unsubscribeProtocolError()
    this.unsubscribeClose()
    this.clearInteractionHandler()
    this.interactionFeature.close(this.featureContext)
    this.catalogFeature.close()
    this.listeners.clear()
  }
  private async handleSubmit(rawValue: string, modeOverride?: "build" | "compose" | "direct_shell"): Promise<IntentOutcome> {
    const value = rawValue.trim()
    if (!value) return { status: "rejected", code: "invalid-argument", message: "Empty submit value" }

    const resolution = this.commandFeature.resolveInputSlashCommand(rawValue)
    if (resolution.kind === "command") {
      const result = dispatchSlashCommand(
        resolution.command,
        this.commandFeature.commandDispatchContext(this.featureContext, Boolean(this.interactionFeature.pendingInteraction)),
        this.commandFeature.commandRegistry,
      )
      return this.applyCommandResult(result)
    }
    if (resolution.kind === "unknown") {
      this.commit(current => appendNotice(current, this.commandFeature.unknownNotice(resolution)))
      return { status: "accepted" }
    }

    const message = resolution.kind === "escaped" ? resolution.message : value
    return this.runFeature.startRun(message, this.featureContext, {
      // 下一次 Run 的工作模式由共享状态或 direct_shell 显式指定决定，受理后冻结。
      mode: modeOverride ?? this.state.workMode,
      requestedModelProfileId: this.modelFeature.requestedModelProfileId,
      armedSkill: this.skillFeature.armedSkill,
      onEvent: event => this.timelineFeature.processAgentEvent(event, this.featureContext),
      onRunFinish: (actualModel?: ModelProfile) => {
        // 实际绑定优先于显式选择：Run 真的用了哪个模型就显示哪个。
        if (actualModel) this.modelFeature.actualModelProfile = actualModel
        this.refreshModelSelection()
        void this.catalogFeature.refreshThreadCatalog(this.featureContext)
      },
      onAbandonInteraction: () => this.interactionFeature.abandonPendingInteraction(this.featureContext),
    })
  }
  private async applyCommandResult(result: CommandResult): Promise<IntentOutcome> {
    switch (result.type) {
      case "notice":
        this.commit(current => appendNotice(current, result.message))
        return { status: "accepted" }
      case "request-exit":
        return { status: "accepted", effects: [{ type: "request-exit" }] }
      case "clear-thread":
        this.beginNewThread()
        return { status: "accepted" }
      case "request-confirmation":
        this.confirmation = { confirmationId: result.confirmationId, title: result.title, message: result.message, confirmLabel: result.confirmLabel, cancelLabel: result.cancelLabel }
        this.publish()
        return { status: "accepted" }
      case "present":
        if (this.state.activeRun || this.interactionFeature.pendingInteraction) {
          return { status: "rejected", code: "busy", message: "Active run or pending interaction in progress" }
        }
        if (result.target === "models") {
          const binding = await this.modelFeature.modelBindingConfirmation(this.featureContext)
          if (binding) {
            this.confirmation = binding
            this.publish()
            return { status: "accepted" }
          }
        }
        await this.catalogFeature.refreshCatalog(result.target, this.featureContext, id => this.adoptThreadSelection(id))
        return { status: "accepted", effects: [{ type: "present", target: result.target, initialQuery: result.initialQuery }] }
      case "compact":
        if (this.compactInFlight) {
          return { status: "rejected", code: "busy", message: "上下文正在压缩，请等待当前操作完成" }
        }
        this.compactInFlight = true
        this.commit(startContextCompaction)
        try {
          const compacted = await this.gateway.compactContext(result.threadId)
          this.commit(current => appendNotice(current, contextCompactNotice(compacted)))
        } catch (error) {
          this.commit(current => appendNotice(current, `上下文压缩失败：${error instanceof Error ? error.message : String(error)}`))
        } finally {
          this.compactInFlight = false
          this.commit(finishContextCompaction)
        }
        return { status: "accepted" }
      case "mcp":
        await this.catalogFeature.refreshMcpCatalog(this.featureContext)
        return { status: "accepted" }
      case "request-handoff":
        return { status: "accepted", effects: [{ type: "request-handoff", threadId: result.threadId }] }
      case "side-question":
        return { status: "accepted", effects: [{ type: "side-question", question: result.question, threadId: result.threadId }] }
      case "submit-prompt":
        return this.runFeature.startRun(result.prompt, this.featureContext, {
          mode: this.state.workMode,
          requestedModelProfileId: this.modelFeature.requestedModelProfileId,
          armedSkill: this.skillFeature.armedSkill,
          requestedSkill: result.requestedSkill,
          onEvent: event => this.timelineFeature.processAgentEvent(event, this.featureContext),
          onRunFinish: (actualModel?: ModelProfile) => {
            if (actualModel) this.modelFeature.actualModelProfile = actualModel
            this.refreshModelSelection()
            void this.catalogFeature.refreshThreadCatalog(this.featureContext)
          },
          onAbandonInteraction: () => this.interactionFeature.abandonPendingInteraction(this.featureContext),
        })
      case "rpc":
        try {
          const value = await this.invokeCommandRpc(result.method, result.params)
          return this.applyCommandResult(result.onSuccess(value))
        } catch (error) {
          return this.applyCommandResult(result.onError(error))
        }
      default:
        return { status: "accepted" }
    }
  }
  /** 把 Slash 命令产出的 RPC 方法名映射到 Gateway 的类型化调用。 */
  private invokeCommandRpc(method: CommandRpcMethod, params: Record<string, unknown>): Promise<unknown> {
    switch (method) {
      case "agents.list":
        return this.gateway.listAgents()
      case "teams.list":
        return this.gateway.listTeams()
      case "teams.inspect": {
        const kind = params.kind
        const id = params.id
        if ((kind !== "definition" && kind !== "run") || typeof id !== "string" || !id) {
          return Promise.reject(new Error("Team 详情参数无效"))
        }
        return this.gateway.inspectTeam(kind, id)
      }
      case "teams.generate":
        return this.gateway.generateTeam({
          id: String(params.id ?? ""),
          lead_agent_id: String(params.lead_agent_id ?? ""),
          worker_agent_ids: Array.isArray(params.worker_agent_ids)
            ? params.worker_agent_ids.map(value => String(value))
            : [],
          ...(typeof params.max_parallelism === "number" ? { max_parallelism: params.max_parallelism } : {}),
        })
      case "teams.run":
        return this.gateway.runTeam({
          team_id: String(params.team_id ?? ""),
          request: String(params.request ?? ""),
          thread_id: String(params.thread_id ?? ""),
          run_id: String(params.run_id ?? ""),
        })
      case "teams.cancel":
        if (typeof params.run_id !== "string" || !params.run_id) {
          return Promise.reject(new Error("Team 取消参数无效"))
        }
        return this.gateway.cancelTeam(params.run_id)
    }
  }
  private async resolveConfirmation(confirmationId: string, confirmed: boolean): Promise<IntentOutcome> {
    if (!this.confirmation || this.confirmation.confirmationId !== confirmationId) {
      return { status: "rejected", code: "stale-interaction", message: "Stale confirmation" }
    }
    this.confirmation = null
    this.publish()
    if (!confirmed) return { status: "accepted" }
    if (confirmationId === "clear-thread" && this.state.activeRun) {
      const cancelled = await this.runFeature.cancelActiveRun(this.featureContext, () => this.interactionFeature.abandonPendingInteraction(this.featureContext))
      if (cancelled.status !== "accepted") {
        this.commit(current => appendNotice(current, "未能取消当前任务，已保留当前 thread。请等待任务结束后重试。"))
        return { status: "accepted" }
      }
    }
    if (confirmationId === "clear-thread") {
      this.beginNewThread()
    } else if (confirmationId === "model-binding") {
      this.resetThreadState(clearThread(this.state))
    } else if (confirmationId === "compose-abandon") {
      const threadId = this.state.currentThreadId
      if (!threadId) {
        this.commit(current => appendNotice(current, "当前没有可用 thread。"))
        return { status: "accepted" }
      }
      try {
        await this.gateway.abandonCompose(threadId)
        this.commit(current => ({ ...current, composeState: null }))
        this.commit(current => appendNotice(current, "已废弃当前 Compose 需求。文档仍保留。"))
      } catch (error) {
        this.commit(current => appendNotice(current, `废弃失败：${error instanceof Error ? error.message : String(error)}`))
      }
    }
    return { status: "accepted" }
  }
  /** 重置 conversation scope（Thread/Timeline/模型与 Skill 选择/Interaction/Confirmation/sequence），不清全局 Catalog。 */
  private resetConversationScope(nextState: InteractiveState = clearThread(this.state)): void {
    this.threadFeature.threadEpoch += 1
    this.state = nextState
    this.modelFeature.requestedModelProfileId = null
    this.modelFeature.actualModelProfile = undefined
    this.modelFeature.explicitlySelected = false
    this.skillFeature.armedSkill = undefined
    this.confirmation = null
    this.interactionFeature.settlePendingInteraction(this.featureContext)
    this.timelineFeature.resetSequence()
  }

  /** 新建 Thread 唯一领域入口：/new 命令结果与确认对话框共用；保留全局 Catalog（侧栏历史立即可切回）。 */
  private beginNewThread(): void {
    this.resetConversationScope()
    this.publish()
  }

  /** 打开 Thread 前的旧状态重置：保持既有行为（catalog 重置后由打开流程刷新）。 */
  private resetThreadState(nextState: InteractiveState = clearThread(this.state)): void {
    this.resetConversationScope(nextState)
    this.catalogFeature.reset({}, this.featureContext)
  }
  private hasCapability(capability: string): boolean {
    return (this.baseRuntime.capabilities ?? builtinCommandCapabilities).includes(capability as any)
  }
  private commit(updater: (current: InteractiveState) => InteractiveState): void {
    if (this.closed) return
    this.state = updater(this.state)
    this.publish()
  }
  private publish(): void {
    this.snapshot = this.buildSnapshot()
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }
  private buildSnapshot(): InteractiveSnapshot {
    return {
      currentThreadId: this.state.currentThreadId,
      activity: this.state.activity,
      activeRun: this.state.activeRun,
      timeline: [...scopeTimeline(this.state.timeline, this.state.childTimelineExecutionId ?? "root")],
      childTimelineExecutionId: this.state.childTimelineExecutionId,
      runProgress: this.state.runProgress,
      interaction: this.interactionFeature.interactionDto(this.interactionFeature.pendingInteraction, this.clock),
      confirmation: this.confirmation,
      lastRun: this.state.lastRun ?? null,
      runtime: { ...this.baseRuntime, approvalMode: this.runFeature.currentApprovalMode(this.baseRuntime.approvalMode), modelProfileId: this.modelFeature.requestedModelProfileId ?? undefined },
      connection: this.connection,
      catalogs: { threads: publicCatalog(this.catalogFeature.state.threads), models: publicCatalog(this.catalogFeature.state.models), skills: publicCatalog(this.catalogFeature.state.skills), mcp: publicCatalog(this.catalogFeature.state.mcp), agents: publicCatalog(this.catalogFeature.state.agents) },
      commands: this.commandFeature.buildCommandItems(this.catalogFeature.state.skills.items, this.featureContext, this.hasPendingInteraction),
      selection: { requestedModelProfileId: this.modelFeature.requestedModelProfileId, actualModel: this.modelFeature.actualModelProfile ?? null, armedSkill: this.skillFeature.armedSkill ?? null },
      workMode: this.state.workMode,
      composeState: this.state.composeState,
      workItem: this.state.workItem,
      threadMode: this.state.threadMode,
    }
  }
}

export function createInteractiveController(options: InteractiveControllerOptions): InteractiveController {
  return new InteractiveControllerImpl(options)
}

function publicCatalog<T>(catalog: { status: any; items: readonly T[]; message?: string }): LoadableCatalog<T> {
  return { status: catalog.status, items: catalog.items, message: catalog.message }
}

/** 压缩期间只允许只读刷新、运行取消和 Interaction 收尾；其余可变入口失败关闭。 */
function blocksPendingOperation(intent: InteractiveIntent): boolean {
  if (intent.type === "catalog.refresh" || intent.type === "run.cancel" || intent.type === "interaction.respond") return false
  if (intent.type === "command.execute") {
    return !["system.help", "system.status", "system.version", "system.quit"].includes(intent.commandId)
  }
  return true
}
