/** Interactive Core 工作流：Run/Event、Interaction、Thread/catalog、模型与 Slash Command 语义。 */

import {
  Capability,
  EventType,
  type EventEnvelope,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type McpAddResult,
  type McpServerStatus,
  type McpStatusResult,
  type ModelProfile,
  type RequestedSkill,
  type ThreadMessage,
  type ThreadSummary,
} from "@za38/protocol"

import { JsonRpcRemoteError } from "../ipc/client"
import type { InteractiveAgentPort, InteractiveAgentRun } from "./agent-port"
import { contextCompactNotice, dispatchSlashCommand, type CommandResult } from "./command-dispatcher"
import {
  builtinCommandCapabilities,
  commandRegistry,
  resolveSlashCommand,
  unknownCommandNotice,
  type CommandContext,
  type CommandMenuItem,
  type SkillMenuItem,
} from "./commands"
import { nextApprovalMode, runtimeStatusSummary, type InteractiveApprovalMode, type InteractiveRuntime } from "./runtime"
import {
  appendNotice,
  applyAgentEvent,
  applyInteractionRequest,
  clearPendingInteraction,
  clearThread,
  createInitialState,
  markCancelling,
  markInteractionTimeout,
  markRunFailed,
  restoreThread,
  startRun,
  type InteractiveState,
} from "./state"
import type {
  ApprovalDecision,
  InteractiveConfirmation,
  InteractiveConnectionState,
  InteractiveController,
  InteractiveControllerOptions,
  InteractiveIntent,
  InteractiveInteraction,
  InteractiveMcpInput,
  InteractiveQuestion,
  InteractiveResponse,
  InteractiveResult,
  InteractiveScheduler,
  InteractiveSnapshot,
  LoadableCatalog,
  McpServerSummary,
  SkillSummary,
} from "./types"

type PendingInteraction = {
  request: InteractionRequestEnvelope
  resolve: (response: InteractionResponse) => void
  deadlineAtMs: number
  clearTimer: () => void
}

type CatalogKey = "threads" | "models" | "skills" | "mcp"

/** 内部 catalog 状态；epoch 用于丢弃晚到响应。 */
type InternalCatalog<T> = LoadableCatalog<T> & { epoch: number }

type CatalogState = {
  threads: InternalCatalog<ThreadSummary>
  models: InternalCatalog<ModelProfile>
  skills: InternalCatalog<SkillSummary>
  mcp: InternalCatalog<McpServerSummary>
}

/** 默认 scheduler：真实定时器；测试注入手动 scheduler 驱动 timeout。 */
const defaultScheduler: InteractiveScheduler = {
  setTimeout(callback, ms) {
    const timer = setTimeout(callback, ms)
    return () => clearTimeout(timer)
  },
}

/** 模型同步失败使用的稳定错误分类。 */
class ModelDefaultSyncError extends Error {
  constructor(readonly reason: string) {
    super(reason)
    this.name = "ModelDefaultSyncError"
  }
}

/** Controller 的具体实现；所有可变状态都集中在这个 module 内。 */
export class InteractiveControllerImpl implements InteractiveController {
  private readonly agent: InteractiveAgentPort
  private readonly baseRuntime: InteractiveRuntime
  private readonly scheduler: InteractiveScheduler
  private readonly listeners = new Set<(snapshot: InteractiveSnapshot) => void>()
  private readonly clearInteractionHandler: () => void
  private readonly unsubscribeProtocolError: () => void
  private readonly unsubscribeClose: () => void

  private state: InteractiveState
  private snapshot: InteractiveSnapshot
  private connection: InteractiveConnectionState = { status: "open" }
  private pendingInteraction: PendingInteraction | null = null
  private confirmation: InteractiveConfirmation | null = null
  private requestedModelProfileId: string | null = null
  private approvalModeOverride: InteractiveApprovalMode | undefined
  private actualModelProfile: ModelProfile | undefined
  private armedSkill: SkillSummary | undefined
  private threadEpoch = 0
  private openingThread = false
  private closed = false
  private catalogs: CatalogState = {
    threads: { status: "idle", items: [], epoch: 0 },
    models: { status: "idle", items: [], epoch: 0 },
    skills: { status: "idle", items: [], epoch: 0 },
    mcp: { status: "idle", items: [], epoch: 0 },
  }

  constructor(options: InteractiveControllerOptions) {
    this.agent = options.agent
    this.baseRuntime = options.runtime
    this.scheduler = options.scheduler ?? defaultScheduler
    this.state = createInitialState(options.initialThreadId ?? null)
    this.snapshot = this.buildSnapshot()

    this.unsubscribeProtocolError = this.agent.onProtocolError(error => {
      if (this.closed) return
      this.connection = { status: "protocol-error", message: error.message }
      this.commit(current => appendNotice(current, `协议错误：${error.message}`))
    })
    this.unsubscribeClose = this.agent.onClose(error => {
      if (this.closed) return
      this.connection = { status: "closed", message: error.message }
      this.settlePendingInteraction()
      this.commit(current => appendNotice(current, `Agent 连接已关闭：${error.message}`))
    })
    this.clearInteractionHandler = this.agent.setInteractionHandler(request => this.handleInteractionRequest(request))

    void this.refreshSkillCatalog()
    if (options.initialThreadId !== undefined) {
      void this.restoreInitialThread(options.initialThreadId)
    }
  }

  getSnapshot(): InteractiveSnapshot {
    return this.snapshot
  }

  subscribe(listener: (snapshot: InteractiveSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** 执行用户意图；关闭后是无副作用 no-op，避免 React 卸载竞态产生未处理异常。 */
  async dispatch(intent: InteractiveIntent): Promise<InteractiveResult | void> {
    if (this.closed) return
    switch (intent.type) {
      case "input.submit":
        await this.submit(intent.value)
        return
      case "command.execute":
        return this.executeSlashCommand({ id: intent.commandId, name: intent.commandId, argument: intent.argument })
      case "run.cancel":
        await this.cancelActiveRun()
        return
      case "catalog.refresh":
        await this.refreshCatalog(intent.catalog)
        return
      case "thread.open":
        await this.openThread(intent.threadId)
        return
      case "model.select":
        await this.selectModel(intent.profileId)
        return
      case "skill.arm":
        this.armSkill(intent.skillId)
        return
      case "skill.clear":
        this.armedSkill = undefined
        this.publish()
        return
      case "skill.set-enabled":
        await this.setSkillEnabled(intent.skillId, intent.enabled)
        return
      case "mcp.add":
        await this.addMcpServer(intent.input)
        return
      case "mcp.remove":
        await this.removeMcpServer(intent.name)
        return
      case "interaction.respond":
        this.respondInteraction(intent.requestId, intent.response)
        return
      case "confirmation.resolve":
        await this.resolveConfirmation(intent.confirmationId, intent.confirmed)
        return
      case "approval-mode.cycle":
        this.cycleApprovalMode()
    }
  }

  /** 停止接收 intent、使全部 generation 失效、卸载 listener；不关闭外层 transport。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.invalidateAllCatalogs()
    this.threadEpoch += 1
    this.unsubscribeProtocolError()
    this.unsubscribeClose()
    this.clearInteractionHandler()
    this.settlePendingInteraction()
  }

  /** 提交一次用户输入：统一处理 Slash、转义、未知命令和普通 Run。 */
  private async submit(rawValue: string): Promise<void> {
    const input = rawValue.trim()
    if (!input) return
    const resolution = resolveSlashCommand(rawValue)
    if (resolution.kind === "command") {
      await this.executeSlashCommand(resolution.command)
      return
    }
    if (resolution.kind === "unknown") {
      this.commit(current => appendNotice(current, unknownCommandNotice(resolution)))
      return
    }
    const message = resolution.kind === "escaped" ? resolution.message : input
    await this.sendAgentMessage(message)
  }

  /** 按稳定 command ID 交给 Dispatcher；未知 ID 只输出本地提示。 */
  private async executeSlashCommand(command: { id: string; name: string; argument?: string }): Promise<InteractiveResult | void> {
    const definition = commandRegistry.get(command.id)
    if (!definition) {
      this.commit(current => appendNotice(current, `未知命令：/${command.name}。输入 /help 查看可用命令。`))
      return
    }
    return this.applyCommandResult(dispatchSlashCommand({ ...command, name: definition.name }, this.commandDispatchContext()))
  }

  /** 执行 Dispatcher 返回的 semantic operation；adapter 只解释 InteractiveResult。 */
  private async applyCommandResult(result: CommandResult): Promise<InteractiveResult | void> {
    switch (result.type) {
      case "notice":
        this.commit(current => appendNotice(current, result.message))
        return
      case "request-exit":
        return { type: "request-exit" }
      case "clear-thread":
        this.resetThread(clearThread(this.state))
        return
      case "request-confirmation":
        this.confirmation = {
          confirmationId: result.confirmationId,
          title: result.title,
          message: result.message,
          confirmLabel: result.confirmLabel,
          cancelLabel: result.cancelLabel,
        }
        this.publish()
        return
      case "present":
        if (this.state.activeRun || this.pendingInteraction) {
          this.commit(current => appendNotice(current, "当前任务结束或交互完成后可用。"))
          return
        }
        if (result.target === "models") {
          const binding = await this.modelBindingConfirmation()
          if (binding) {
            this.confirmation = binding
            this.publish()
            return
          }
        }
        await this.refreshCatalog(result.target)
        return { type: "present", target: result.target, initialQuery: result.initialQuery }
      case "compact":
        await this.compactThread(result.threadId)
        return
      case "mcp":
        await this.handleMcp(result.argument)
        return
      case "request-handoff":
        return { type: "request-handoff", threadId: result.threadId }
      case "submit-prompt":
        await this.sendAgentMessage(result.prompt, result.requestedSkill)
    }
  }

  /** 通过 port 启动 Run，只消费该 Run 的 events 队列。 */
  private async sendAgentMessage(message: string, requestedSkill?: RequestedSkill): Promise<void> {
    const current = this.state
    if (current.activeRun) {
      this.commit(state => appendNotice(state, "当前 thread 仍在执行；请等待、审批或按 Ctrl+C 取消。"))
      return
    }
    if (this.connection.status !== "open") {
      this.commit(state => appendNotice(state, "Agent 连接不可用，无法启动新的运行。"))
      return
    }
    const armedSkill = requestedSkill ?? (this.armedSkill ? { id: this.armedSkill.id, args: message } : undefined)
    const modelSelection = this.requestedModelProfileId ? { primary_profile: this.requestedModelProfileId } : undefined
    if (armedSkill && !requestedSkill) {
      this.armedSkill = undefined
    }
    const agentRun = this.agent.startRun({
      message,
      threadId: current.currentThreadId ?? undefined,
      requestedSkill: armedSkill,
      modelSelection,
      approvalMode: this.approvalModeOverride ?? this.baseRuntime.approvalMode,
    })
    const run = agentRun.ref
    this.commit(state => startRun(state, run, message))
    void this.drainEvents(agentRun)
    void agentRun.completion.catch(() => undefined)
    try {
      await agentRun.accepted
    } catch (error) {
      if (this.state.activeRun?.runId === run.runId) {
        this.commit(state => markRunFailed(state, run.runId, errorMessage(error)))
      }
    }
  }

  /** 消费当前 Run 的事件流；终态、Interaction 和 actual model 都由这里收敛。 */
  private async drainEvents(run: InteractiveAgentRun): Promise<void> {
    try {
      for await (const event of run.events) {
        if (this.closed) return
        if (event.thread_id !== run.ref.threadId || event.run_id !== run.ref.runId) continue
        if (event.type === EventType.RUN_STARTED) {
          const actual = modelProfileFromRunStarted(event.payload)
          if (actual) this.actualModelProfile = actual
        }
        if (TERMINAL_EVENT_TYPES.has(event.type)) this.settlePendingInteraction()
        this.commit(current => applyAgentEvent(current, event))
      }
    } catch (error) {
      // 只有当前 Run 可以转 failed；旧 Run 的流错误不能结束新 Run。
      if (!this.closed && this.state.activeRun?.runId === run.ref.runId) {
        this.commit(state => markRunFailed(state, run.ref.runId, errorMessage(error)))
      }
    }
  }

  /** 处理 Agent 反向 Interaction；已有 pending 时新请求立即 fail closed。 */
  private handleInteractionRequest(request: InteractionRequestEnvelope): Promise<InteractionResponse> {
    const active = this.state.activeRun
    if (!active || active.threadId !== request.thread_id || active.runId !== request.run_id) {
      return Promise.resolve(interactionCancellation(request))
    }
    if (this.pendingInteraction) {
      this.agent.abandonInteraction(request.request_id)
      return Promise.resolve(interactionCancellation(request))
    }
    return new Promise(resolve => {
      const deadlineAtMs = Date.now() + request.timeout_ms
      const pending: PendingInteraction = {
        request,
        resolve,
        deadlineAtMs,
        clearTimer: () => undefined,
      }
      // 先登记 pending 再注册 timer：timeout_ms=0 时 timer 可能在赋值前同步触发，
      // 导致已 resolve 的 Interaction 仍停留在 pending 状态。
      this.pendingInteraction = pending
      pending.clearTimer = this.scheduler.setTimeout(() => {
        const current = this.pendingInteraction
        if (!current || current.request.request_id !== request.request_id) return
        this.pendingInteraction = null
        const activeRun = this.state.activeRun
        if (activeRun && activeRun.threadId === request.thread_id && activeRun.runId === request.run_id) {
          pending.resolve(interactionCancellation(request))
        } else {
          this.agent.abandonInteraction(request.request_id)
          pending.resolve(interactionCancellation(request))
        }
        this.commit(state => markInteractionTimeout(
          state,
          request.request_id,
          request.type === "approval" ? "审批等待超时，已按拒绝处理。" : "提问等待超时，已按空回答处理。",
        ))
      }, Math.max(0, request.timeout_ms))
      this.commit(current => applyInteractionRequest(current, request))
    })
  }

  /** 校验并回写用户对当前 Interaction 的响应；stale/非法响应不产生 RPC。 */
  private respondInteraction(requestId: string, response: InteractiveResponse): void {
    const pending = this.pendingInteraction
    if (!pending || pending.request.request_id !== requestId) return
    if (pending.request.type === "approval") {
      const approval = pending.request.payload
      if (response.kind !== "approval") return
      if (!approval.decisions.includes(response.decision)) {
        this.commit(current => appendNotice(current, "不支持的审批决定，已忽略。"))
        return
      }
      this.resolveInteraction(pending, {
        type: "approval",
        request_id: requestId,
        decision: response.decision,
        ...(response.decision === "reject_with_feedback" ? { feedback: response.feedback ?? "" } : {}),
      })
      return
    }
    if (response.kind !== "question") return
    const violation = validateQuestionAnswers(pending.request, response.answers)
    if (violation) {
      this.commit(current => appendNotice(current, violation))
      return
    }
    this.resolveInteraction(pending, {
      type: "question",
      request_id: requestId,
      answers: response.answers,
    })
  }

  /** 清除 pending Interaction，回写 wire response 并更新时间线状态。 */
  private resolveInteraction(pending: PendingInteraction, response: InteractionResponse): void {
    this.pendingInteraction = null
    pending.clearTimer()
    const outcome = response.type === "approval"
      ? response.decision === "reject" || response.decision === "reject_with_feedback" ? "rejected" : "approved"
      : "answered"
    pending.resolve(response)
    this.commit(current => clearPendingInteraction(current, outcome))
  }

  /** 终态、连接关闭或 Controller 关闭时 abandon 并本地收敛未完成 Interaction。 */
  private settlePendingInteraction(): void {
    const pending = this.pendingInteraction
    if (!pending) return
    this.pendingInteraction = null
    pending.clearTimer()
    this.agent.abandonInteraction(pending.request.request_id)
    pending.resolve(interactionCancellation(pending.request))
  }

  /** Shift+Tab：循环切换审批模式，从下一次 Run 起生效并立即更新 runtime 展示。 */
  private cycleApprovalMode(): void {
    const current = this.approvalModeOverride ?? this.baseRuntime.approvalMode
    this.approvalModeOverride = nextApprovalMode(current)
    this.publish()
  }

  /** 取消当前 Run；成功返回 true，失败时当前 Run 转 failed。 */
  private async cancelActiveRun(): Promise<boolean> {
    const active = this.state.activeRun
    if (!active) return false
    if (this.state.activity.kind === "cancelling") return false
    this.commit(markCancelling)
    try {
      const result = await this.agent.cancel(active.threadId, active.runId)
      if (!result.cancelled || result.run_id !== active.runId) throw new Error("Agent 未确认取消当前运行")
      return true
    } catch (error) {
      if (this.state.activeRun?.runId === active.runId) {
        this.commit(current => markRunFailed(current, active.runId, errorMessage(error)))
      }
      return false
    }
  }

  /** 按 canonical threads.open 恢复指定 Thread；切换期间递增 thread/model generation。 */
  private async openThread(threadId: string): Promise<void> {
    if (this.openingThread) return
    if (this.state.activeRun || this.pendingInteraction) {
      this.commit(current => appendNotice(current, "当前 thread 仍在执行或等待交互，不能恢复其他 thread。"))
      return
    }
    this.openingThread = true
    const epoch = ++this.threadEpoch
    try {
      const opened = threadOpenResult(await this.agent.openThread(threadId))
      if (this.closed || epoch !== this.threadEpoch) return
      let models: readonly ModelProfile[] = []
      let selection: string | undefined
      let actual: ModelProfile | undefined
      try {
        const result = await this.agent.listModels(opened.threadId)
        models = result.profiles
        selection = result.thread_selection?.primary_profile
        actual = modelFromBinding(result.last_run_binding ?? result.thread_binding)
      } catch {
        // 模型绑定读取失败不阻断历史恢复；本次 Thread 不展示旧 Thread 的模型。
      }
      if (this.closed || epoch !== this.threadEpoch) return
      if (this.state.activeRun || this.pendingInteraction) {
        this.commit(current => appendNotice(current, "当前 thread 状态已变化，未恢复其他 thread。"))
        return
      }
      this.resetThread(restoreThread(opened.threadId, opened.messages), {
        models,
        selection,
        actual,
      })
    } catch (error) {
      if (this.closed || epoch !== this.threadEpoch) return
      this.resetThread(createInitialState(this.state.currentThreadId))
      this.commit(current => appendNotice(current, `Thread 恢复失败：${errorMessage(error)}`))
    } finally {
      this.openingThread = false
    }
  }

  /** Web 接管归还后按一次性 ID 恢复；null 进入空首页，失败不回退陈旧 Thread。 */
  private async restoreInitialThread(threadId: string | null): Promise<void> {
    if (this.closed || this.openingThread) return
    if (threadId === null) {
      this.resetThread(createInitialState(null))
      return
    }
    this.openingThread = true
    const epoch = ++this.threadEpoch
    try {
      const opened = threadOpenResult(await this.agent.openThread(threadId))
      let models: readonly ModelProfile[] = []
      let selection: string | undefined
      let actual: ModelProfile | undefined
      try {
        const result = await this.agent.listModels(opened.threadId)
        models = result.profiles
        selection = result.thread_selection?.primary_profile
        actual = modelFromBinding(result.last_run_binding ?? result.thread_binding)
      } catch {
        // 模型绑定读取失败不阻断历史恢复。
      }
      if (this.closed || epoch !== this.threadEpoch) return
      this.resetThread(restoreThread(opened.threadId, opened.messages), { models, selection, actual })
    } catch (error) {
      if (this.closed) return
      this.resetThread(createInitialState(null))
      this.commit(current => appendNotice(current, `Web 会话恢复失败，已回到空首页：${errorMessage(error)}`))
    } finally {
      this.openingThread = false
    }
  }

  /** 先改变当前 Thread 的下一次模型，再独立同步未来新 Thread 默认值。 */
  private async selectModel(profileId: string): Promise<void> {
    const catalog = this.catalogs.models
    const model = catalog.items.find(value => value.id === profileId)
    if (!model || !model.available) {
      this.commit(current => appendNotice(
        current,
        model
          ? `${model.provider_label} · ${model.model} 不可用：${model.unavailable_reason ?? "配置不可用"}`
          : "所选模型 Profile 不存在。",
      ))
      return
    }
    this.requestedModelProfileId = model.id
    this.publish()
    const label = `${model.provider_label} · ${model.model}`
    try {
      await this.syncDefaultModel(model.id)
      this.commit(current => appendNotice(current, `当前 Thread 已切换到 ${label}；后续新 Thread 默认模型已同步。`))
    } catch (error) {
      this.commit(current => appendNotice(current, `当前 Thread 已切换到 ${label}；未来新 Thread 默认未更新：${safeModelDefaultSyncError(error)}`))
    }
  }

  /** 同步未来新 Thread 的默认模型；失败只影响默认值，不回收当前选择。 */
  private async syncDefaultModel(profileId: string): Promise<void> {
    if (!this.baseRuntime.capabilities?.includes(Capability.CONFIG_WRITE)) throw new ModelDefaultSyncError("CONFIG_WRITE_CAPABILITY_REQUIRED")
    const details = await this.agent.configDetails()
    const field = details.fields.find(value => isRecord(value) && value.path === "models.default_profile")
    if (!field || !isRecord(field)) throw new ModelDefaultSyncError("CONFIG_FIELD_NOT_ALLOWED")
    if (field.editable !== true) throw new ModelDefaultSyncError(typeof field.unavailable_reason === "string" ? field.unavailable_reason : "CONFIG_FIELD_NOT_WRITABLE")
    if (field.value !== profileId) {
      const changes = [{ path: "models.default_profile", value: profileId }]
      const preview = await this.agent.previewConfig(changes)
      await this.agent.commitConfig(preview.revision, changes)
    }
    try {
      const refreshed = await this.agent.listModels(this.state.currentThreadId ?? undefined)
      this.catalogs.models = { status: "ready", items: refreshed.profiles, epoch: this.catalogs.models.epoch }
    } catch {
      const current = this.catalogs.models
      this.catalogs.models = {
        status: "ready",
        epoch: this.catalogs.models.epoch,
        items: current.items.map(value => ({ ...value, is_default: value.id === profileId })),
      }
    }
  }

  /** 从当前 catalog 校验并武装一次性 Skill。 */
  private armSkill(skillId: string): void {
    const catalog = this.catalogs.skills
    const skill = catalog.items.find(value => value.id === skillId)
    if (!skill || !skill.enabled || !skill.userInvocable) {
      this.commit(current => appendNotice(current, "所选 Skill 不可用。"))
      return
    }
    this.armedSkill = skill
    this.publish()
  }

  /**
   * 设置 Skill 启用状态：依次门禁 skills.manage 能力、当前 Run/Interaction 空闲、目标仍存在，
   * 通过后再调用 port。失败保留原状态并输出脱敏 notice，禁用当前 armed Skill 时一并清除。
   */
  private async setSkillEnabled(skillId: string, enabled: boolean): Promise<void> {
    if (!this.baseRuntime.capabilities?.includes(Capability.SKILLS_MANAGE)) {
      this.commit(current => appendNotice(current, "当前客户端未协商 skills.manage，无法启停 Skill。"))
      return
    }
    if (this.state.activeRun || this.pendingInteraction) {
      this.commit(current => appendNotice(current, "当前任务结束或交互完成后可用。"))
      return
    }
    const target = this.catalogs.skills.items.find(value => value.id === skillId)
    if (!target) {
      this.commit(current => appendNotice(current, "所选 Skill 不存在。"))
      return
    }
    try {
      await this.agent.setSkillEnabled(skillId, enabled)
    } catch (error) {
      this.commit(current => appendNotice(current, `Skill 启停失败：${errorMessage(error)}`))
      return
    }
    if (!enabled && this.armedSkill?.id === skillId) {
      this.armedSkill = undefined
    }
    await this.refreshSkillCatalog()
  }

  /** 通过共享语义执行 /mcp；Slash 文本与 typed intent 最终调用同一内部方法。 */
  private async handleMcp(argument?: string): Promise<void> {
    const subArgs = argument?.trim()
    if (!subArgs) {
      await this.refreshMcpCatalog()
      const catalog = this.catalogs.mcp
      if (catalog.status === "error") {
        this.commit(current => appendNotice(current, `MCP 状态查询失败：${catalog.message}`))
      } else {
        this.commit(current => appendNotice(current, formatMcpStatus(catalog.items as McpStatusResult["servers"])))
      }
      return
    }
    const [sub, ...rest] = subArgs.split(/\s+/)
    if (sub === "add") {
      const usage = "用法：/mcp add <name> <command> [args...]  或  /mcp add <name> --url <url> [--sse]"
      if (rest.length < 2) {
        this.commit(current => appendNotice(current, usage))
        return
      }
      const name = rest[0]!
      const remaining = rest.slice(1)
      const urlIdx = remaining.indexOf("--url")
      const hasSse = remaining.includes("--sse")
      if (urlIdx !== -1) {
        const url = remaining[urlIdx + 1]
        if (!url || url.startsWith("--")) {
          this.commit(current => appendNotice(current, `错误：--url 后需要提供 URL\n${usage}`))
          return
        }
        try {
          const result = await this.agent.mcpAdd({ name, transport: hasSse ? "sse" : "http", url })
          this.commit(current => appendNotice(current, formatMcpAddResult(name, result)))
        } catch (error) {
          this.commit(current => appendNotice(current, `添加 MCP 服务器失败：${errorMessage(error)}`))
        }
        return
      }
      const command = remaining[0]
      const args = remaining.slice(1)
      try {
        const result = await this.agent.mcpAdd({ name, transport: "stdio", command, args: args.length ? args : undefined })
        this.commit(current => appendNotice(current, formatMcpAddResult(name, result)))
      } catch (error) {
        this.commit(current => appendNotice(current, `添加 MCP 服务器失败：${errorMessage(error)}`))
      }
      return
    }
    if (sub === "remove") {
      if (!rest.length) {
        this.commit(current => appendNotice(current, "用法：/mcp remove <name>"))
        return
      }
      await this.removeMcpServer(rest[0]!)
      return
    }
    this.commit(current => appendNotice(current, `未知子命令 "${sub}"\n用法：/mcp [add|remove] ...`))
  }

  /** typed mcp.add 入口；与 Slash 文本解析共享同一校验和 RPC。 */
  private async addMcpServer(input: InteractiveMcpInput): Promise<void> {
    if (!input.name) {
      this.commit(current => appendNotice(current, "MCP 服务器名称不能为空。"))
      return
    }
    if (input.transport === "stdio" && !input.command) {
      this.commit(current => appendNotice(current, "stdio 传输需要提供 command。"))
      return
    }
    if (input.transport !== "stdio" && !input.url) {
      this.commit(current => appendNotice(current, "http/sse 传输需要提供 url。"))
      return
    }
    try {
      const result = await this.agent.mcpAdd(input)
      this.commit(current => appendNotice(current, formatMcpAddResult(input.name, result)))
    } catch (error) {
      this.commit(current => appendNotice(current, `添加 MCP 服务器失败：${errorMessage(error)}`))
    }
  }

  /** typed mcp.remove 入口。 */
  private async removeMcpServer(name: string): Promise<void> {
    if (!name) {
      this.commit(current => appendNotice(current, "用法：/mcp remove <name>"))
      return
    }
    try {
      await this.agent.mcpRemove(name)
      this.commit(current => appendNotice(current, `已删除 MCP 服务器 "${name}"`))
    } catch (error) {
      this.commit(current => appendNotice(current, `删除 MCP 服务器失败：${errorMessage(error)}`))
    }
  }

  /** 执行 context.compact 并输出不泄漏归档正文的本地通知。 */
  private async compactThread(threadId: string): Promise<void> {
    try {
      const result = await this.agent.compactContext(threadId)
      this.commit(current => appendNotice(current, contextCompactNotice(result)))
    } catch (error) {
      this.commit(current => appendNotice(current, `上下文压缩失败：${errorMessage(error)}`))
    }
  }

  /** 处理 confirmation.resolve；取消失败保留原 Thread 和 Timeline。 */
  private async resolveConfirmation(confirmationId: string, confirmed: boolean): Promise<void> {
    const confirmation = this.confirmation
    if (!confirmation || confirmation.confirmationId !== confirmationId) return
    this.confirmation = null
    this.publish()
    if (!confirmed) return
    if (confirmationId === "clear-thread") {
      if (this.state.activeRun) {
        const cancelled = await this.cancelActiveRun()
        if (!cancelled) {
          this.commit(current => appendNotice(current, "未能取消当前任务，已保留当前 thread。请等待任务结束后重试。"))
          return
        }
      }
      this.resetThread(clearThread(this.state))
      return
    }
    if (confirmationId === "model-binding") {
      this.resetThread(clearThread(this.state))
    }
  }

  /** 检查当前 Thread 是否使用 legacy immutable binding；返回 confirmation 或 null。 */
  private async modelBindingConfirmation(): Promise<InteractiveConfirmation | null> {
    const current = this.state
    if (!current.currentThreadId) return null
    if (this.baseRuntime.capabilities?.includes(Capability.MODELS_SELECT) === true) return null
    try {
      const result = await this.agent.listModels(current.currentThreadId)
      const binding = result.thread_binding
      const roles = binding && typeof binding === "object" ? (binding as Record<string, unknown>).roles : undefined
      const executor = roles && typeof roles === "object"
        ? (roles as Record<string, unknown>).executor
        : undefined
      const executorRecord = executor && typeof executor === "object" ? executor as Record<string, unknown> : undefined
      return {
        confirmationId: "model-binding",
        title: "当前 Thread 的模型不可变",
        message: executorRecord
          ? `当前 Thread 已绑定 ${stringValue(executorRecord.provider_label, "unknown")} · ${stringValue(executorRecord.model, "unknown")}（${stringValue(executorRecord.id, "unknown")}）。请新建 Thread 后使用 /model 选择模型。`
          : "当前 Thread 使用 legacy immutable binding，不能热切换模型。请新建 Thread 后使用 /model 选择模型。",
        confirmLabel: "新建 Thread",
        cancelLabel: "保留当前 Thread",
      }
    } catch {
      return {
        confirmationId: "model-binding",
        title: "模型绑定不可读取",
        message: "无法读取当前 Thread 的模型绑定。请新建 Thread 后再选择模型。",
        confirmLabel: "新建 Thread",
        cancelLabel: "保留当前 Thread",
      }
    }
  }

  /** 统一清空 Thread、模型意图、Skill 和领域选择状态。 */
  private resetThread(
    nextState: InteractiveState = clearThread(this.state),
    modelState: { models?: readonly ModelProfile[]; selection?: string; actual?: ModelProfile } = {},
  ): void {
    this.threadEpoch += 1
    this.state = nextState
    this.requestedModelProfileId = modelState.selection ?? null
    this.actualModelProfile = modelState.actual
    this.armedSkill = undefined
    this.confirmation = null
    this.settlePendingInteraction()
    this.catalogs.threads = { status: "idle", items: [], epoch: this.catalogs.threads.epoch }
    this.catalogs.skills = { status: "idle", items: this.catalogs.skills.items, epoch: this.catalogs.skills.epoch }
    this.catalogs.mcp = { status: "idle", items: [], epoch: this.catalogs.mcp.epoch }
    if (modelState.models) {
      this.catalogs.models = { status: "ready", items: modelState.models, epoch: this.catalogs.models.epoch }
    } else {
      this.catalogs.models = { status: "idle", items: [], epoch: this.catalogs.models.epoch }
    }
    this.publish()
    void this.refreshSkillCatalog()
  }

  /** 刷新指定 catalog；每个 catalog 有独立 epoch，刷新 A 不取消 B。 */
  private async refreshCatalog(key: CatalogKey): Promise<void> {
    if (key === "threads") await this.refreshThreadCatalog()
    else if (key === "models") await this.refreshModelCatalog()
    else if (key === "skills") await this.refreshSkillCatalog()
    else await this.refreshMcpCatalog()
  }

  /** 读取 Thread catalog；异步结果只允许写回对应打开轮次。 */
  private async refreshThreadCatalog(): Promise<void> {
    const epoch = ++this.catalogs.threads.epoch
    this.catalogs.threads = { status: "loading", items: this.catalogs.threads.items, epoch }
    this.publish()
    try {
      const result = await this.agent.listThreads()
      if (this.closed || epoch !== this.catalogs.threads.epoch) return
      this.catalogs.threads = { status: "ready", items: result.threads, epoch }
      this.publish()
    } catch (error) {
      if (this.closed || epoch !== this.catalogs.threads.epoch) return
      this.catalogs.threads = { status: "error", items: this.catalogs.threads.items, message: errorMessage(error), epoch }
      this.publish()
    }
  }

  /** 读取 Model catalog；与当前 thread 的绑定和最近运行绑定一起收敛。 */
  private async refreshModelCatalog(): Promise<void> {
    const epoch = ++this.catalogs.models.epoch
    this.catalogs.models = { status: "loading", items: this.catalogs.models.items, epoch }
    this.publish()
    try {
      const result = await this.agent.listModels(this.state.currentThreadId ?? undefined)
      if (this.closed || epoch !== this.catalogs.models.epoch) return
      this.catalogs.models = { status: "ready", items: result.profiles, epoch }
      if (result.thread_selection?.primary_profile && !this.state.activeRun) {
        this.requestedModelProfileId = result.thread_selection.primary_profile
      }
      this.publish()
    } catch (error) {
      if (this.closed || epoch !== this.catalogs.models.epoch) return
      this.catalogs.models = { status: "error", items: this.catalogs.models.items, message: errorMessage(error), epoch }
      this.publish()
    }
  }

  /** 读取 Skill catalog；始终拉取权威全集（含 disabled），命令菜单与选择继续按 enabled && userInvocable 过滤。 */
  private async refreshSkillCatalog(): Promise<void> {
    const epoch = ++this.catalogs.skills.epoch
    this.catalogs.skills = { status: "loading", items: this.catalogs.skills.items, epoch }
    this.publish()
    try {
      const result = await this.agent.listSkills(true)
      if (this.closed || epoch !== this.catalogs.skills.epoch) return
      const next = Array.isArray(result.skills)
        ? result.skills.map(skillMenuItem).filter((item): item is SkillMenuItem => item !== undefined)
        : []
      this.catalogs.skills = { status: "ready", items: next, epoch }
      this.publish()
    } catch (error) {
      if (this.closed || epoch !== this.catalogs.skills.epoch) return
      this.catalogs.skills = { status: "error", items: this.catalogs.skills.items, message: errorMessage(error), epoch }
      this.publish()
    }
  }

  /** 读取 MCP catalog；/mcp 状态入口与 typed refresh 共用。 */
  private async refreshMcpCatalog(): Promise<void> {
    const epoch = ++this.catalogs.mcp.epoch
    this.catalogs.mcp = { status: "loading", items: this.catalogs.mcp.items, epoch }
    this.publish()
    try {
      const result = await this.agent.mcpStatus()
      if (this.closed || epoch !== this.catalogs.mcp.epoch) return
      this.catalogs.mcp = { status: "ready", items: result.servers, epoch }
      this.publish()
    } catch (error) {
      if (this.closed || epoch !== this.catalogs.mcp.epoch) return
      this.catalogs.mcp = { status: "error", items: this.catalogs.mcp.items, message: errorMessage(error), epoch }
      this.publish()
    }
  }

  /** 关闭时使全部 catalog 的晚到结果失效。 */
  private invalidateAllCatalogs(): void {
    this.catalogs = {
      threads: { ...this.catalogs.threads, epoch: this.catalogs.threads.epoch + 1 },
      models: { ...this.catalogs.models, epoch: this.catalogs.models.epoch + 1 },
      skills: { ...this.catalogs.skills, epoch: this.catalogs.skills.epoch + 1 },
      mcp: { ...this.catalogs.mcp, epoch: this.catalogs.mcp.epoch + 1 },
    }
  }

  /** 生成当前 Dispatcher 所需的最小上下文。 */
  private commandDispatchContext() {
    const context = this.commandContext()
    return {
      commandContext: context,
      threadId: this.state.currentThreadId,
      runtimeStatus: runtimeStatusSummary(this.runtimeWithModel()),
      versionSummary: `za38-cli ${this.baseRuntime.cliVersion} · JSON-RPC v3`,
    }
  }

  /** 从当前领域状态派生 Registry 可用性上下文。 */
  private commandContext(): CommandContext {
    return {
      capabilities: new Set(this.baseRuntime.capabilities ?? builtinCommandCapabilities),
      hasThread: this.state.currentThreadId !== null,
      activeRun: Boolean(this.state.activeRun),
      hasPendingInteraction: Boolean(this.pendingInteraction),
    }
  }

  /** 合并实际模型到 runtime，供 /status 和底栏展示。 */
  private runtimeWithModel(): InteractiveRuntime {
    const actual = this.actualModelProfile
    const requested = this.requestedModelProfileId
    const merged = actual || requested
      ? {
          ...this.baseRuntime,
          modelName: actual?.model ?? this.baseRuntime.modelName,
          modelProfileId: actual?.id ?? requested ?? undefined,
          modelConfigured: true,
        }
      : this.baseRuntime
    return this.approvalModeOverride
      ? { ...merged, approvalMode: this.approvalModeOverride }
      : merged
  }

  /** 生成稳定的只读快照；数组均不复用外部可变容器。 */
  private buildSnapshot(): InteractiveSnapshot {
    const commands = this.buildCommandItems()
    const selectedModel = this.catalogs.models.items.find(model => model.id === this.requestedModelProfileId)
    return {
      currentThreadId: this.state.currentThreadId,
      activity: this.state.activity,
      activeRun: this.state.activeRun,
      timeline: this.state.timeline,
      interaction: this.pendingInteraction ? interactionDto(this.pendingInteraction) : null,
      confirmation: this.confirmation,
      lastRun: this.state.lastRun ?? null,
      runtime: this.runtimeWithModel(),
      connection: this.connection,
      commands,
      catalogs: {
        threads: publicCatalog(this.catalogs.threads),
        models: publicCatalog(this.catalogs.models),
        skills: publicCatalog(this.catalogs.skills),
        mcp: publicCatalog(this.catalogs.mcp),
      },
      selection: {
        requestedModelProfileId: this.requestedModelProfileId,
        actualModel: this.actualModelProfile ?? null,
        armedSkill: this.armedSkill ?? null,
      },
    }
  }

  /** 计算已过 capability/Thread/busy 计算的全部可见命令项；draft 过滤属于 adapter。 */
  private buildCommandItems(): CommandMenuItem[] {
    const context = this.commandContext()
    const commands: CommandMenuItem[] = commandRegistry.list(context)
      .map(({ definition, availability }) => ({ kind: "command" as const, command: definition, availability }))
    const skills: CommandMenuItem[] = this.catalogs.skills.items
      .filter(skill => skill.enabled && skill.userInvocable)
      .map(skill => ({ kind: "skill" as const, skill }))
    return [...commands, ...skills]
  }

  /** 发布状态时复制 listener 集合，避免回调中取消订阅影响当前轮次。 */
  private publish(): void {
    this.snapshot = this.buildSnapshot()
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }

  /** 把 state reducer 转换和领域状态一起发布为新 snapshot。 */
  private commit(transition: (current: InteractiveState) => InteractiveState): void {
    if (this.closed) return
    this.state = transition(this.state)
    this.publish()
  }
}

/** 创建 Interactive Core Controller；它是共享交互语义的唯一业务入口。 */
export function createInteractiveController(options: InteractiveControllerOptions): InteractiveController {
  return new InteractiveControllerImpl(options)
}

const TERMINAL_EVENT_TYPES = new Set<EventEnvelope["type"]>([
  EventType.INTERACTION_RESOLVED,
  EventType.RUN_COMPLETED,
  EventType.RUN_CANCELLED,
  EventType.RUN_FAILED,
])

/** 把 pending Interaction 转成共享 DTO；deadline 由本地 scheduler 计算。 */
function interactionDto(pending: PendingInteraction): InteractiveInteraction {
  if (pending.request.type === "approval") {
    return {
      type: "approval",
      requestId: pending.request.request_id,
      description: stringValue(pending.request.payload.description, "有操作需要你的审批"),
      requests: pending.request.payload.requests,
      decisions: pending.request.payload.decisions,
      deadlineAtMs: pending.deadlineAtMs,
    }
  }
  const questions: InteractiveQuestion[] = (pending.request.payload.questions ?? []).map(question => {
    const record = asRecord(question)
    const options = Array.isArray(record.options) ? record.options.map(option => ({
      label: stringValue(asRecord(option).label ?? option, ""),
      value: stringValue(asRecord(option).value ?? option, ""),
      description: stringValue(asRecord(option).description, ""),
    })) : []
    return {
      id: stringValue(record.id, "question-1"),
      question: stringValue(record.question, "Agent 需要补充信息"),
      header: stringValue(record.header, ""),
      body: stringValue(record.body, ""),
      options,
      multiSelect: record.multi_select === true,
      allowOther: record.allow_other === true,
    }
  })
  return {
    type: "question",
    requestId: pending.request.request_id,
    questions,
    deadlineAtMs: pending.deadlineAtMs,
  }
}

/** 校验 question 回答；返回拒绝原因或 null。 */
function validateQuestionAnswers(request: Extract<InteractionRequestEnvelope, { type: "question" }>, answers: Record<string, string[]>): string | null {
  const questions = request.payload.questions
  if (!questions.length) return "提问不包含任何问题，已忽略。"
  for (const question of questions) {
    const record = asRecord(question)
    const id = stringValue(record.id, "")
    const values = (answers[id] ?? []).filter(value => typeof value === "string" && value.trim() !== "")
    if (!values.length) return `缺少问题「${id}」的回答，已忽略。`
    if (record.multi_select !== true && values.length > 1) return `问题「${id}」只允许单选，已忽略。`
    if (record.allow_other !== true) {
      const allowed = new Set((Array.isArray(record.options) ? record.options : []).map(option => stringValue(asRecord(option).value ?? option, "")))
      const invalid = values.find(value => !allowed.has(value))
      if (invalid) return `问题「${id}」包含无效选项，已忽略。`
    }
  }
  return null
}

/** 取消/超时/关闭时使用的 fail-closed 响应。 */
function interactionCancellation(request: InteractionRequestEnvelope): InteractionResponse {
  return request.type === "approval"
    ? { type: "approval", request_id: request.request_id, decision: "reject" }
    : { type: "question", request_id: request.request_id, answers: {} }
}

function skillMenuItem(value: unknown): SkillMenuItem | undefined {
  if (!value || typeof value !== "object") return undefined
  const record = value as Record<string, unknown>
  if (typeof record.id !== "string" || !record.id || typeof record.name !== "string" || !record.name || typeof record.description !== "string") return undefined
  return {
    id: record.id,
    name: record.name,
    description: record.description,
    source: typeof record.source === "string" ? record.source : "unknown",
    enabled: record.enabled !== false,
    userInvocable: record.user_invocable !== false,
    argumentHint: typeof record.argument_hint === "string" ? record.argument_hint : undefined,
  }
}

function modelProfileFromRunStarted(payload: Record<string, unknown>): ModelProfile | undefined {
  const primary = payload.primary_model
  if (!primary || typeof primary !== "object") return undefined
  const profile = (primary as Record<string, unknown>).profile
  if (!profile || typeof profile !== "object") return undefined
  return modelProfile(profile)
}

/** 从 models.list 的 binding 结果中提取实际模型；legacy binding 只读兼容。 */
function modelFromBinding(binding: unknown): ModelProfile | undefined {
  if (!binding || typeof binding !== "object") return undefined
  const record = binding as Record<string, unknown>
  const roles = record.roles
  if (roles && typeof roles === "object") {
    const executor = (roles as Record<string, unknown>).executor
    if (executor && typeof executor === "object") return modelProfile(executor)
    const primary = (roles as Record<string, unknown>).primary
    if (primary && typeof primary === "object") return modelProfile(primary)
  }
  const profile = record.profile
  if (profile && typeof profile === "object") return modelProfile(profile)
  return undefined
}

function modelProfile(value: unknown): ModelProfile | undefined {
  if (!value || typeof value !== "object") return undefined
  const record = value as Record<string, unknown>
  if (
    typeof record.id !== "string" || !record.id
    || typeof record.model !== "string" || !record.model
    || typeof record.provider_label !== "string" || !record.provider_label
    || typeof record.context_window_tokens !== "number" || !Number.isInteger(record.context_window_tokens)
    || !Array.isArray(record.capabilities) || !record.capabilities.every(item => typeof item === "string")
    || typeof record.is_default !== "boolean" || typeof record.available !== "boolean"
    || typeof record.source !== "string" || !record.source
  ) return undefined
  return {
    id: record.id,
    model: record.model,
    provider_label: record.provider_label,
    context_window_tokens: record.context_window_tokens,
    capabilities: record.capabilities,
    is_default: record.is_default,
    available: record.available,
    unavailable_reason: typeof record.unavailable_reason === "string" ? record.unavailable_reason : null,
    source: record.source,
  }
}

function threadOpenResult(value: unknown): { threadId: string; messages: Array<{ kind: "user" | "assistant" | "tool"; content: string; toolName?: string }> } {
  if (!value || typeof value !== "object") throw new Error("Agent 返回的 thread 恢复结果无效")
  const record = value as Record<string, unknown>
  const thread = record.thread
  if (!thread || typeof thread !== "object" || !Array.isArray(record.messages)) throw new Error("Agent 返回的 thread 恢复结果无效")
  const threadRecord = thread as Record<string, unknown>
  const threadId = stringValue(threadRecord.thread_id, "")
  if (!threadId) throw new Error("Agent 返回的 thread 恢复结果无效")
  const messages = record.messages.map(threadMessage).filter((message): message is ThreadMessage => message !== undefined)
  if (messages.length !== record.messages.length) throw new Error("Agent 返回了无效的 thread message")
  return {
    threadId,
    messages: messages.map(message => ({ kind: message.kind, content: message.content, toolName: message.tool_name })),
  }
}

function threadMessage(value: unknown): ThreadMessage | undefined {
  if (!value || typeof value !== "object") return undefined
  const record = value as Record<string, unknown>
  if (
    (record.kind !== "user" && record.kind !== "assistant" && record.kind !== "tool")
    || typeof record.content !== "string"
    || (record.tool_name !== undefined && typeof record.tool_name !== "string")
  ) return undefined
  return {
    kind: record.kind,
    content: record.content,
    tool_name: typeof record.tool_name === "string" ? record.tool_name : undefined,
  }
}

function formatMcpAddResult(name: string, result: McpAddResult): string {
  const lines = [`已添加 MCP 服务器 "${name}"`]
  if (result.connected) {
    lines.push(`连接成功，加载 ${result.tool_names.length} 个工具`)
    if (result.tool_names.length > 0) lines.push(`工具：${result.tool_names.join(", ")}`)
  } else {
    lines.push("连接失败，配置已保存，重启后自动重试")
    if (result.error) lines.push(`错误：${result.error}`)
  }
  return lines.join("\n")
}

function formatMcpStatus(servers: McpStatusResult["servers"]): string {
  if (!servers.length) return "未配置 MCP 服务器\n在 ~/.harness/config.toml 的 [[mcp.servers]] 中添加配置。"
  const lines: string[] = ["MCP 服务器状态", ""]
  for (const server of servers) {
    const icon = server.status === "connected" ? "●" : server.status === "failed" ? "✗" : "○"
    lines.push(`${icon} ${server.name}  [${server.transport}]  ${server.status}`)
    if (server.error) lines.push(`  错误：${server.error}`)
    if (server.tool_names.length) lines.push(`  工具：${server.tool_names.join(", ")}`)
  }
  lines.push("", `共 ${servers.length} 个服务器，${servers.reduce((total, server) => total + server.tool_names.length, 0)} 个工具`)
  return lines.join("\n")
}

function safeModelDefaultSyncError(error: unknown): string {
  const reason = error instanceof ModelDefaultSyncError
    ? error.reason
    : error instanceof JsonRpcRemoteError
      ? remoteConfigReason(error)
      : "配置服务暂时不可用"
  const messages: Record<string, string> = {
    CONFIG_WRITE_CAPABILITY_REQUIRED: "当前客户端未协商 config.write",
    CONFIG_FIELD_NOT_ALLOWED: "默认模型字段不可写",
    CONFIG_FIELD_NOT_WRITABLE: "默认模型字段当前不可写",
    CONFIG_USER_FILE_MISSING: "用户配置文件不存在",
    MANAGED_POLICY_LOCKED: "默认模型字段受受管策略锁定",
    SOURCE_OVERRIDE_ACTIVE: "默认模型由更高优先级来源覆盖",
    UNTRUSTED_PROJECT_CONFIGURATION: "项目配置不允许写入用户默认值",
    EXPLICIT_CONFIGURATION_ACTIVE: "当前显式配置来源不可写",
    CONFIG_REVISION_CONFLICT: "配置已被其他操作修改，请重试",
    CONFIG_WRITE_FAILED: "用户配置写入失败",
    MODEL_PROFILE_NOT_FOUND: "所选模型 Profile 不存在",
    MODEL_PROFILE_UNAVAILABLE: "所选模型不可用",
    MODEL_PROFILE_CAPABILITY_MISSING: "所选模型缺少 Single Agent 所需能力",
  }
  return messages[reason] ?? "配置服务拒绝本次更新"
}

function remoteConfigReason(error: JsonRpcRemoteError): string {
  if (error.data && typeof error.data === "object") {
    const code = (error.data as Record<string, unknown>).code
    if (typeof code === "string") return code
  }
  return error.message
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback
}

/** 从不可信 payload 读取普通对象；null/数组/原始值回退为空对象。 */
function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 去掉内部 epoch，只暴露共享 LoadableCatalog 形状。 */
function publicCatalog<T>(catalog: InternalCatalog<T>): LoadableCatalog<T> {
  const { epoch: _epoch, ...rest } = catalog
  return rest
}
