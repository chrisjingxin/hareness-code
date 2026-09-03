/** Goal Feature：集中处理 `/goal` 的投影、创建请求与一次性续跑语义。 */

import { Capability, type GoalContinuation, type GoalInspectResult, type GoalMutateAction } from "@za38/protocol"
import type { IntentOutcome } from "../ports"
import { appendNotice, applyGoalSnapshot } from "../state"
import type { FeatureContext } from "./types"

type GoalRunCallbacks = {
  startProposal(requestId: string, displayPrompt?: string): Promise<IntentOutcome>
  startContinuation(continuation: GoalContinuation): Promise<IntentOutcome>
  openViewer(snapshot: GoalInspectResult, threadId: string): void
}

export class GoalFeature {
  private readonly consumedContinuations = new Set<string>()

  /** `/goal`：提供只读、提案与生命周期 mutation 的单一命令入口。 */
  async execute(argument: string | undefined, ctx: FeatureContext, callbacks: GoalRunCallbacks): Promise<IntentOutcome> {
    const value = argument?.trim() ?? ""
    if (!value || value === "show" || value === "status") {
      const threadId = ctx.getState().currentThreadId
      if (!threadId) {
        ctx.commit(current => appendNotice(current, "当前 thread 还没有目标。用 `/goal <目标>` 创建。"))
        return { status: "accepted" }
      }
      return this.refresh(threadId, ctx, true, callbacks)
    }

    const current = ctx.getState()
    if (!(ctx.baseRuntime.capabilities ?? [Capability.GOAL_MANAGE]).includes(Capability.GOAL_MANAGE)) {
      return { status: "rejected", code: "capability-missing", message: "Goal management is not enabled" }
    }
    if (value === "pause" || value === "resume" || value === "clear") {
      return this.mutate({ kind: value }, ctx, callbacks)
    }
    const amendMatch = /^amend(?:\s+([\s\S]+))?$/.exec(value)
    if (amendMatch) {
      const feedback = amendMatch[1]?.trim() ?? ""
      if (!feedback) {
        return { status: "rejected", code: "invalid-argument", message: "用法：/goal amend <修改说明>" }
      }
      if (!current.goal) {
        return { status: "rejected", code: "invalid-argument", message: "当前没有可修订的目标。" }
      }
      return this.request(feedback, "amend", ctx, callbacks)
    }
    if (/^(?:model|max-iterations)(?:\s|$)/.test(value)) {
      return { status: "rejected", code: "invalid-argument", message: "目标验收配置尚不可用，请使用目标生命周期命令。" }
    }
    return this.request(value, current.goal ? "replace" : "create", ctx, callbacks)
  }

  /** 任意 Run 终态后刷新 Goal，并消费服务端对账产生的下一步动作。 */
  async finishRun(
    threadId: string,
    context: Record<string, unknown> | undefined,
    ctx: FeatureContext,
    callbacks: GoalRunCallbacks,
    resumeReadyProposal = true,
  ): Promise<boolean> {
    await this.refresh(threadId, ctx, false)
    if (!resumeReadyProposal) return true
    const refreshedPending = ctx.getState().goalPending
    const proposalRequestId = parseProposalReady(context)
      ?? (refreshedPending?.status === "ready" || refreshedPending?.status === "reviewing"
        ? refreshedPending.request_id
        : null)
    if (proposalRequestId) {
      const result = await callbacks.startProposal(proposalRequestId)
      if (result.status === "rejected") {
        ctx.commit(current => appendNotice(current, `目标请求已就绪，但继续提案失败：${result.message}`))
      }
      return true
    }
    return this.consumeContinuation(parseContinuation(context), ctx, callbacks)
  }

  /** 恢复 Thread 后只续接未完成 proposal；已接受的 current goal 不会自行启动。 */
  async resumePending(ctx: FeatureContext, callbacks: GoalRunCallbacks): Promise<void> {
    const pending = ctx.getState().goalPending
    if (!pending || (pending.status !== "ready" && pending.status !== "reviewing")) return
    const result = await callbacks.startProposal(pending.request_id)
    if (result.status === "rejected") {
      ctx.commit(current => appendNotice(current, `目标提案恢复失败：${result.message}`))
    }
  }

  private async request(
    inputText: string,
    kind: "create" | "replace" | "amend",
    ctx: FeatureContext,
    callbacks: GoalRunCallbacks,
  ): Promise<IntentOutcome> {
    const current = ctx.getState()
    const threadId = current.currentThreadId ?? ctx.idGenerator.uuid()
    const requestId = ctx.idGenerator.uuid()
    try {
      const requested = await ctx.gateway.requestGoal({
        thread_id: threadId,
        request_id: requestId,
        kind,
        input_text: inputText,
        expected_goal_id: current.goal?.goal_id ?? null,
        expected_revision: current.goal?.revision ?? null,
      })
      ctx.commit(state => applyGoalSnapshot({
        ...state,
        currentThreadId: threadId,
        activity: state.activity.kind === "home" ? { kind: "idle" } : state.activity,
      }, {
        goal: state.goal,
        pending: requested.pending,
      }))
      if (requested.disposition === "queued") {
        ctx.commit(state => appendNotice(state, "目标请求已排队，将在当前任务结束后继续。"))
        return { status: "accepted" }
      }
      const displayPrompt = kind === "amend" ? `/goal amend ${inputText}` : `/goal ${inputText}`
      return callbacks.startProposal(requested.pending.request_id, displayPrompt)
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `目标请求失败：${errorMessage(error)}` }
    }
  }

  private async mutate(
    action: GoalMutateAction,
    ctx: FeatureContext,
    callbacks: GoalRunCallbacks,
  ): Promise<IntentOutcome> {
    const current = ctx.getState()
    if (!current.currentThreadId) {
      return { status: "rejected", code: "invalid-argument", message: "当前 thread 还没有目标。" }
    }
    if ((action.kind === "pause" || action.kind === "resume") && !current.goal) {
      return { status: "rejected", code: "invalid-argument", message: "当前没有可操作的目标。" }
    }
    try {
      const result = await ctx.gateway.mutateGoal({
        thread_id: current.currentThreadId,
        operation_id: ctx.idGenerator.uuid(),
        expected_goal_id: current.goal?.goal_id ?? null,
        expected_revision: current.goal?.revision ?? null,
        action,
      })
      ctx.commit(state => applyGoalSnapshot(state, { goal: result.goal, pending: result.pending }))
      if (result.disposition === "queued") {
        ctx.commit(state => appendNotice(state, `目标操作 ${action.kind} 已排队，将在当前任务结束后生效。`))
        return { status: "accepted" }
      }
      ctx.commit(state => appendNotice(state, mutationNotice(action.kind)))
      await this.consumeContinuation(result.continuation, ctx, callbacks)
      return { status: "accepted" }
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `目标操作失败：${errorMessage(error)}` }
    }
  }

  private async consumeContinuation(
    continuation: GoalContinuation | null,
    ctx: FeatureContext,
    callbacks: GoalRunCallbacks,
  ): Promise<boolean> {
    if (!continuation || this.consumedContinuations.has(continuation.continuation_id)) return false
    this.consumedContinuations.add(continuation.continuation_id)
    const result = await callbacks.startContinuation(continuation)
    if (result.status === "rejected") {
      ctx.commit(current => appendNotice(current, `目标已保存，但自动开始失败：${result.message}`))
    }
    return true
  }

  private async refresh(
    threadId: string,
    ctx: FeatureContext,
    announce: boolean,
    callbacks?: GoalRunCallbacks,
  ): Promise<IntentOutcome> {
    try {
      const snapshot = await ctx.gateway.inspectGoal(threadId)
      ctx.commit(current => applyGoalSnapshot(current, {
        goal: snapshot.goal,
        pending: snapshot.pending,
        latestEvaluation: snapshot.latest_evaluation,
      }))
      if (announce) {
        ctx.commit(current => appendNotice(current, goalSummary(snapshot)))
        callbacks?.openViewer(snapshot, threadId)
      }
      return { status: "accepted" }
    } catch (error) {
      return { status: "rejected", code: "agent-error", message: `读取目标失败：${errorMessage(error)}` }
    }
  }
}

function parseProposalReady(context: Record<string, unknown> | undefined): string | null {
  const value = context?.goal_proposal_ready
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const requestId = (value as Record<string, unknown>).request_id
  return typeof requestId === "string" && requestId ? requestId : null
}

function parseContinuation(context: Record<string, unknown> | undefined): GoalContinuation | null {
  const value = context?.goal_continuation
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const continuation = value as Record<string, unknown>
  if (typeof continuation.continuation_id !== "string" || !continuation.continuation_id) return null
  if (typeof continuation.goal_id !== "string" || !continuation.goal_id) return null
  if (!Number.isInteger(continuation.goal_revision) || (continuation.goal_revision as number) < 1) return null
  if (continuation.reason !== "accepted" && continuation.reason !== "amended" && continuation.reason !== "resumed") return null
  return {
    continuation_id: continuation.continuation_id,
    goal_id: continuation.goal_id,
    goal_revision: continuation.goal_revision as number,
    reason: continuation.reason,
  }
}

function goalSummary(snapshot: GoalInspectResult): string {
  if (snapshot.pending) return `目标正在处理中：${snapshot.pending.input_text}`
  if (!snapshot.goal) return "当前 thread 还没有目标。用 `/goal <目标>` 创建。"
  return `当前目标（${snapshot.goal.status} · r${snapshot.goal.revision}）：${snapshot.goal.objective}`
}

function mutationNotice(kind: GoalMutateAction["kind"]): string {
  if (kind === "pause") return "目标已暂停。"
  if (kind === "resume") return "目标已恢复，正在继续执行。"
  if (kind === "clear") return "目标已清空，历史记录仍保留。"
  return "目标操作已完成。"
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
