/** Run Feature：管理 Run 的启动、取消、底层 Agent 订阅句柄与终态清理。 */

import type { ApprovalMode, ModelProfile, RequestedSkill } from "@za38/protocol"
import type { IntentOutcome, InteractiveAgentRun, SkillSummary } from "../ports"
import { markCancelling, markRunFailed, startRun as startRunState } from "../state"
import { nextApprovalMode, type InteractiveApprovalMode } from "../runtime"
import type { FeatureContext } from "./types"

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export class RunFeature {
  approvalModeOverride: InteractiveApprovalMode | undefined
  private activeRunHandle: InteractiveAgentRun | null = null

  /** 当前审批模式：覆盖值优先，否则回落到底层 runtime 的握手值。 */
  currentApprovalMode(fallback: InteractiveApprovalMode): ApprovalMode {
    return (this.approvalModeOverride ?? fallback) as unknown as ApprovalMode
  }

  cycleApprovalMode(ctx: FeatureContext): IntentOutcome {
    const current = this.approvalModeOverride ?? ctx.baseRuntime.approvalMode
    this.approvalModeOverride = nextApprovalMode(current)
    ctx.publish()
    return { status: "accepted" }
  }

  async startRun(
    value: string,
    ctx: FeatureContext,
    options: {
      requestedModelProfileId: string | null
      armedSkill: SkillSummary | undefined
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
    if (options.armedSkill) {
      requestedSkill = { id: options.armedSkill.id, args: value }
    }

    try {
      const run = ctx.gateway.startRun({
        message: value,
        threadId: currentThreadId ?? undefined,
        requestedSkill,
        modelSelection: options.requestedModelProfileId ? { primary_profile: options.requestedModelProfileId } : undefined,
        approvalMode: this.currentApprovalMode(ctx.baseRuntime.approvalMode),
      })

      this.activeRunHandle = run
      ctx.commit(current => startRunState(current, run.ref, value))

      // accepted 被拒绝：当前 Run 立即收敛为 failed，不残留 activeRun。
      void run.accepted.catch(error => {
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        ctx.commit(current => markRunFailed(current, run.ref.runId, errorMessage(error)))
        options.onRunFinish()
      })

      // 消费事件流；终态事件经 applyAgentEvent 收敛 activeRun，事件流随之自然结束。
      void (async () => {
        try {
          for await (const event of run.events) {
            const active = ctx.getState().activeRun
            if (!active || active.runId !== run.ref.runId) return
            if (event.thread_id !== run.ref.threadId || event.run_id !== run.ref.runId) continue
            options.onEvent(event)
          }
        } catch (error) {
          // 只有当前 Run 可以转 failed；旧 Run 的流错误不能结束新 Run。
          const active = ctx.getState().activeRun
          if (active?.runId === run.ref.runId) {
            this.activeRunHandle = null
            ctx.commit(current => markRunFailed(current, run.ref.runId, errorMessage(error)))
            options.onRunFinish()
          }
        }
      })()

      void run.completion.then(() => {
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        options.onAbandonInteraction()
        options.onRunFinish()
      }).catch(() => {
        // completion 拒绝（非事件流路径的失败）也要收敛 Thread catalog 与选择。
        if (this.activeRunHandle?.ref.runId !== run.ref.runId) return
        this.activeRunHandle = null
        options.onRunFinish()
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
