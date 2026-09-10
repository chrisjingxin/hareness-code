/** Run Feature：管理 Run 的启动、取消、底层 Agent 订阅句柄与终态清理。 */

import { EventType, type ApprovalMode, type InteractionMode, type ModelProfile, type RequestedSkill, type RunInput } from "@za38/protocol"
import { AgentGatewayError, type IntentOutcome, type InteractiveAgentRun, type SkillSummary } from "../ports"
import { markCancelling, markRunFailed, startInternalRun, startRun as startRunState } from "../state"
import { nextApprovalMode, type InteractiveApprovalMode } from "../runtime"
import type { FeatureContext } from "./types"

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 从 RUN_STARTED 事件提取实际主模型绑定；字段缺失或形状不符时不回填。 */
function actualModelFromStarted(payload: Record<string, unknown>): ModelProfile | undefined {
  const binding = objectRecord(payload.primary_model)
  const profile = binding ? objectRecord(binding.profile) : undefined
  if (!profile) return undefined
  const id = stringValue(profile.id, "")
  const model = stringValue(profile.model, "")
  const providerLabel = stringValue(profile.provider_label, "")
  if (!id || !model || !providerLabel) return undefined
  return {
    id,
    model,
    provider_label: providerLabel,
    context_window_tokens: typeof profile.context_window_tokens === "number" && Number.isFinite(profile.context_window_tokens) ? profile.context_window_tokens : 0,
    capabilities: [],
    is_default: Boolean(profile.is_default),
    available: profile.available !== false,
    source: stringValue(profile.source, "agent"),
  }
}

/** 类型守卫：把未知值窄化为可读对象。 */
function objectRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : undefined
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value ? value : fallback
}

/** 校验 Host 返回的审批模式枚举，避免错误响应污染本地 UI 状态。 */
function isApprovalMode(value: unknown): value is ApprovalMode {
  return value === "plan" || value === "default" || value === "auto-edit" || value === "auto" || value === "yolo"
}

/** 批准计划后自动开实现轮的固定用户消息。 */
export const PLAN_IMPLEMENT_PROMPT = "用户已批准计划，Plan 模式约束已解除。请读取 `/.harness/plan.md` 中的设计方案并开始实现。"

export class RunFeature {
  approvalModeOverride: InteractiveApprovalMode | undefined
  /** 活动 Run 的 Host 审批状态 revision；失败时保持上一次已确认值。 */
  approvalModeRevision = 0
  /** 进入 plan 之前的审批档位；不在 plan 时为空。 */
  prePlanMode: InteractiveApprovalMode | undefined
  /** 进入/离开 plan 时递增，供后续计划审批校验过期切档。 */
  planRevision = 0
  private planInteractionRevision: number | undefined
  private planContinue: { decision: "approved" | "abandoned"; feedback?: string } | undefined
  private activeRunHandle: InteractiveAgentRun | null = null
  /** 同一活动 Run 的模式 RPC 串行化，保证 prePlanMode 使用最新确认事实。 */
  private readonly approvalModeQueues = new Map<string, Promise<void>>()

  /** 当前审批模式：覆盖值优先，否则回落到底层 runtime 的握手值。 */
  currentApprovalMode(fallback: InteractiveApprovalMode): ApprovalMode {
    return (this.approvalModeOverride ?? fallback) as unknown as ApprovalMode
  }

  /** Shift+Tab 循环：进入 plan 时记下上一档，离开 plan 时丢掉并走到 default。 */
  async cycleApprovalMode(ctx: FeatureContext): Promise<IntentOutcome> {
    const activeRun = ctx.getState().activeRun
    if (activeRun) {
      // 活动 Run 的 cycle 必须在队列真正执行时读取最新确认档位，不能在入队前冻结目标。
      return this.enqueueActiveApprovalMode(activeRun, () => {
        const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
        return nextApprovalMode(current)
      }, ctx)
    }
    const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
    return this.setApprovalMode(nextApprovalMode(current), ctx)
  }

  /** 直接选择审批档位；进入/离开 plan 时维护 prePlanMode 与 revision。 */
  async setApprovalMode(mode: InteractiveApprovalMode, ctx: FeatureContext): Promise<IntentOutcome> {
    const activeRun = ctx.getState().activeRun
    if (activeRun) {
      return this.enqueueActiveApprovalMode(activeRun, () => mode, ctx)
    }
    const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
    this.approvalModeRevision = 0
    this.applyApprovalModeChange(current, mode)
    ctx.publish()
    return { status: "accepted" }
  }

  /** 将活动 Run 的审批切换加入串行队列；resolveMode 在任务开始时才执行。 */
  private enqueueActiveApprovalMode(
    requestedRun: { threadId: string; runId: string },
    resolveMode: () => InteractiveApprovalMode,
    ctx: FeatureContext,
  ): Promise<IntentOutcome> {
    const queueKey = `${requestedRun.threadId}\u0000${requestedRun.runId}`
    const previous = this.approvalModeQueues.get(queueKey) ?? Promise.resolve()
    const queued = previous
      .catch(() => undefined)
      .then(() => this.setActiveApprovalMode(resolveMode, requestedRun, ctx))
    let tail!: Promise<void>
    tail = queued.then(
      () => {
        if (this.approvalModeQueues.get(queueKey) === tail) this.approvalModeQueues.delete(queueKey)
      },
      () => {
        if (this.approvalModeQueues.get(queueKey) === tail) this.approvalModeQueues.delete(queueKey)
      },
    )
    this.approvalModeQueues.set(queueKey, tail)
    return queued
  }

  /** 串行提交一个活动 Run 的模式请求，并在响应时读取最新本地确认档位。 */
  private async setActiveApprovalMode(
    resolveMode: () => InteractiveApprovalMode,
    requestedRun: { threadId: string; runId: string },
    ctx: FeatureContext,
  ): Promise<IntentOutcome> {
    const currentActive = ctx.getState().activeRun
    const activeRunHandle = this.activeRunHandle
    if (
      !activeRunHandle
      || !currentActive
      || currentActive.threadId !== requestedRun.threadId
      || currentActive.runId !== requestedRun.runId
      || activeRunHandle.ref.threadId !== requestedRun.threadId
      || activeRunHandle.ref.runId !== requestedRun.runId
    ) {
      return {
        status: "rejected",
        code: "agent-error",
        message: "审批模式切换失败：活动 Run 已结束，忽略旧审批模式响应",
      }
    }
    const mode = resolveMode()
    try {
      const result = await ctx.gateway.setApprovalMode(requestedRun.threadId, requestedRun.runId, mode)
      const latestActive = ctx.getState().activeRun
      if (
        this.activeRunHandle !== activeRunHandle
        || !latestActive
        || latestActive.threadId !== requestedRun.threadId
        || latestActive.runId !== requestedRun.runId
        || this.activeRunHandle?.ref.threadId !== requestedRun.threadId
        || this.activeRunHandle?.ref.runId !== requestedRun.runId
      ) {
        throw new AgentGatewayError("RUN_APPROVAL_MODE_STALE", "活动 Run 已结束，忽略旧审批模式响应")
      }
      if (
        result.thread_id !== requestedRun.threadId
        || result.run_id !== requestedRun.runId
        || !isApprovalMode(result.approval_mode)
        || !Number.isInteger(result.revision)
        || result.revision < 0
      ) {
        throw new AgentGatewayError("RUN_APPROVAL_MODE_RESPONSE_INVALID", "服务端返回了无效的审批模式状态")
      }
      if (result.revision < this.approvalModeRevision) {
        throw new AgentGatewayError("RUN_APPROVAL_MODE_STALE_REVISION", "收到过期的审批模式 revision")
      }
      this.approvalModeRevision = result.revision
      // RPC 已串行化；此处读取响应前最新的已确认 mode，不能使用入队时的旧 current。
      const latestMode = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
      this.applyApprovalModeChange(latestMode, result.approval_mode)
      ctx.publish()
      return { status: "accepted" }
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `审批模式切换失败：${errorMessage(error)}` }
    }
  }

  /** 计划交互弹出时记下当时的 revision，供批准时校验是否中途换档。 */
  notePlanInteraction(): void {
    this.planInteractionRevision = this.planRevision
  }

  /** 用户做出计划决定；超时/断开不调用。revision 过期则不切档、不开实现轮。 */
  recordPlanDecision(decision: "approved" | "revise" | "abandoned", feedback?: string): void {
    if (this.planInteractionRevision !== undefined && this.planInteractionRevision !== this.planRevision) {
      this.planContinue = undefined
      this.planInteractionRevision = undefined
      return
    }
    this.planContinue = decision === "approved" || decision === "abandoned"
      ? { decision, ...(feedback?.trim() ? { feedback: feedback.trim() } : {}) }
      : undefined
    this.planInteractionRevision = undefined
  }

  /** 取出并清空计划 Run 终态后的续跑决策。 */
  consumePlanContinue(): { decision: "approved" | "abandoned"; feedback?: string } | undefined {
    const decision = this.planContinue
    this.planContinue = undefined
    return decision
  }

  /** `/plan exit`：恢复进入 plan 前的档位；缺失时回退 default。 */
  async restoreApprovalMode(ctx: FeatureContext): Promise<IntentOutcome> {
    const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
    if (current === "plan") {
      return this.setApprovalMode(this.prePlanMode ?? "default", ctx)
    }
    ctx.publish()
    return { status: "accepted" }
  }

  /** 进出 plan 时递增 revision；从 plan 用非 restore 路径离开则丢掉 prePlanMode。 */
  private applyApprovalModeChange(current: InteractiveApprovalMode, next: InteractiveApprovalMode): void {
    if (next === current) return
    if (next === "plan") {
      this.prePlanMode = current
      this.planRevision += 1
    } else if (current === "plan") {
      this.prePlanMode = undefined
      this.planRevision += 1
    }
    this.approvalModeOverride = next
  }

  async startRun(
    value: string,
    ctx: FeatureContext,
    options: {
      mode: InteractionMode
      requestedModelProfileId: string | null
      armedSkill: SkillSummary | undefined
      requestedSkill?: RequestedSkill
      displayPrompt?: string
      onEvent: (event: any) => void
      onRunFinish: (actualModel?: ModelProfile, context?: Record<string, unknown>, outcome?: "completed" | "cancelled" | "failed") => void
      onAbandonInteraction: () => void
      onAccepted?: () => void
    },
  ): Promise<IntentOutcome> {
    let requestedSkill: RequestedSkill | undefined
    if (options.requestedSkill) requestedSkill = options.requestedSkill
    else if (options.armedSkill) requestedSkill = { id: options.armedSkill.id, args: value }
    return this.startTypedRun(
      { kind: "user", message: value, requested_skill: requestedSkill },
      ctx,
      options,
    )
  }

  async startTypedRun(
    input: RunInput,
    ctx: FeatureContext,
    options: {
      mode: InteractionMode
      requestedModelProfileId: string | null
      runId?: string
      displayPrompt?: string
      onEvent: (event: any) => void
      onRunFinish: (actualModel?: ModelProfile, context?: Record<string, unknown>, outcome?: "completed" | "cancelled" | "failed") => void
      onAbandonInteraction: () => void
      onAccepted?: () => void
      armedSkill?: SkillSummary
      requestedSkill?: RequestedSkill
    },
  ): Promise<IntentOutcome> {
    if (ctx.getState().activeRun) {
      return { status: "rejected", code: "busy", message: "Cannot start run while another run is active" }
    }

    const currentThreadId = ctx.getState().currentThreadId

    try {
      const startedAtMs = ctx.clock.now()
      const run = ctx.gateway.startRun({
        input,
        mode: options.mode,
        threadId: currentThreadId ?? undefined,
        runId: options.runId,
        modelSelection: options.requestedModelProfileId ? { primary_profile: options.requestedModelProfileId } : undefined,
        approvalMode: this.currentApprovalMode(ctx.baseRuntime.approvalMode),
      })

      this.activeRunHandle = run
      this.approvalModeRevision = 0
      const localDisplayPrompt = options.displayPrompt ?? (input.kind === "user" ? input.message : undefined)
      ctx.commit(current => localDisplayPrompt !== undefined
        ? startRunState(current, run.ref, localDisplayPrompt, startedAtMs)
        : startInternalRun(current, run.ref))

      // accepted 被拒绝：当前 Run 立即收敛为 failed，不残留 activeRun。
      void run.accepted.then(() => {
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        options.onAccepted?.()
      }).catch(error => {
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        ctx.commit(current => markRunFailed(current, run.ref.runId, errorMessage(error)))
        options.onRunFinish(undefined, undefined, "failed")
      })

      // 消费事件流；终态事件经 applyAgentEvent 收敛 activeRun，事件流随之自然结束。
      let actualModel: ModelProfile | undefined
      void (async () => {
        try {
          for await (const event of run.events) {
            const active = ctx.getState().activeRun
            if (!active || active.runId !== run.ref.runId) return
            if (event.thread_id !== run.ref.threadId || event.run_id !== run.ref.runId) continue
            if (event.type === EventType.RUN_STARTED) {
              actualModel = actualModelFromStarted(event.payload)
            }
            options.onEvent(event)
          }
        } catch (error) {
          // 只有当前 Run 可以转 failed；旧 Run 的流错误不能结束新 Run。
          const active = ctx.getState().activeRun
          if (active?.runId === run.ref.runId) {
            this.activeRunHandle = null
            ctx.commit(current => markRunFailed(current, run.ref.runId, errorMessage(error)))
            options.onRunFinish(actualModel, undefined, "failed")
          }
        }
      })()

      void run.completion.then(completion => {
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        this.approvalModeRevision = 0
        options.onAbandonInteraction()
        options.onRunFinish(
          actualModel,
          completion.outcome === "completed" ? completion.event.payload.context : undefined,
          completion.outcome,
        )
      }).catch(() => {
        // completion 拒绝（非事件流路径的失败）也要收敛 Thread catalog 与选择。
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        options.onRunFinish(actualModel, undefined, "failed")
      })

      return { status: "accepted" }
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `Run 启动失败：${errorMessage(error)}` }
    }
  }

  async cancelActiveRun(
    ctx: FeatureContext,
    onAbandonInteraction: () => void,
  ): Promise<IntentOutcome> {
    const active = ctx.getState().activeRun
    if (!active) {
      return { status: "rejected", code: "not-found", message: "No active run to cancel" }
    }
    if (ctx.getState().activity.kind === "cancelling") {
      return { status: "rejected", code: "busy", message: "Run is already being cancelled" }
    }

    ctx.commit(markCancelling)
    try {
      const result = await ctx.gateway.cancel(active.threadId, active.runId)
      if (!result.cancelled || result.run_id !== active.runId) {
        throw new Error("Agent 未确认取消当前运行")
      }
      onAbandonInteraction()
      return { status: "accepted" }
    } catch (error) {
      if (ctx.getState().activeRun?.runId === active.runId) {
        ctx.commit(current => markRunFailed(current, active.runId, errorMessage(error)))
      }
      return { status: "rejected", code: "agent-error", message: `取消失败：${errorMessage(error)}` }
    }
  }
}
