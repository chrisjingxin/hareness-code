/** Run Feature：管理 Run 的启动、取消、底层 Agent 订阅句柄与终态清理。 */

import { EventType, type ApprovalMode, type InteractionMode, type ModelProfile, type RequestedSkill } from "@za38/protocol"
import type { IntentOutcome, InteractiveAgentRun, SkillSummary } from "../ports"
import { markCancelling, markRunFailed, startRun as startRunState } from "../state"
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

/** 批准计划后自动开实现轮的固定用户消息。 */
export const PLAN_IMPLEMENT_PROMPT = "用户已批准计划，Plan 模式约束已解除。请读取 `/.harness/plan.md` 中的设计方案并开始实现。"

export class RunFeature {
  approvalModeOverride: InteractiveApprovalMode | undefined
  /** 进入 plan 之前的审批档位；不在 plan 时为空。 */
  prePlanMode: InteractiveApprovalMode | undefined
  /** 进入/离开 plan 时递增，供后续计划审批校验过期切档。 */
  planRevision = 0
  private planInteractionRevision: number | undefined
  private planContinue: { decision: "approved" | "abandoned"; feedback?: string } | undefined
  private activeRunHandle: InteractiveAgentRun | null = null

  /** 当前审批模式：覆盖值优先，否则回落到底层 runtime 的握手值。 */
  currentApprovalMode(fallback: InteractiveApprovalMode): ApprovalMode {
    return (this.approvalModeOverride ?? fallback) as unknown as ApprovalMode
  }

  /** Shift+Tab 循环：进入 plan 时记下上一档，离开 plan 时丢掉并走到 default。 */
  cycleApprovalMode(ctx: FeatureContext): IntentOutcome {
    const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
    this.applyApprovalModeChange(current, nextApprovalMode(current))
    ctx.publish()
    return { status: "accepted" }
  }

  /** 直接选择审批档位；进入/离开 plan 时维护 prePlanMode 与 revision。 */
  setApprovalMode(mode: InteractiveApprovalMode, ctx: FeatureContext): IntentOutcome {
    const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
    this.applyApprovalModeChange(current, mode)
    ctx.publish()
    return { status: "accepted" }
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
  restoreApprovalMode(ctx: FeatureContext): IntentOutcome {
    const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
    if (current === "plan") {
      this.applyApprovalModeChange(current, this.prePlanMode ?? "default")
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
      onRunFinish: (actualModel?: ModelProfile) => void
      onAbandonInteraction: () => void
    },
  ): Promise<IntentOutcome> {
    if (ctx.getState().activeRun) {
      return { status: "rejected", code: "busy", message: "Cannot start run while another run is active" }
    }

    const currentThreadId = ctx.getState().currentThreadId
    let requestedSkill: RequestedSkill | undefined
    if (options.requestedSkill) {
      requestedSkill = options.requestedSkill
    } else if (options.armedSkill) {
      requestedSkill = { id: options.armedSkill.id, args: value }
    }

    try {
      const startedAtMs = ctx.clock.now()
      const run = ctx.gateway.startRun({
        message: value,
        mode: options.mode,
        threadId: currentThreadId ?? undefined,
        requestedSkill,
        modelSelection: options.requestedModelProfileId ? { primary_profile: options.requestedModelProfileId } : undefined,
        approvalMode: this.currentApprovalMode(ctx.baseRuntime.approvalMode),
      })

      this.activeRunHandle = run
      ctx.commit(current => startRunState(current, run.ref, options.displayPrompt ?? value, startedAtMs))

      // accepted 被拒绝：当前 Run 立即收敛为 failed，不残留 activeRun。
      void run.accepted.catch(error => {
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        ctx.commit(current => markRunFailed(current, run.ref.runId, errorMessage(error)))
        options.onRunFinish()
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
            options.onRunFinish(actualModel)
          }
        }
      })()

      void run.completion.then(() => {
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        options.onAbandonInteraction()
        options.onRunFinish(actualModel)
      }).catch(() => {
        // completion 拒绝（非事件流路径的失败）也要收敛 Thread catalog 与选择。
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        options.onRunFinish(actualModel)
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
