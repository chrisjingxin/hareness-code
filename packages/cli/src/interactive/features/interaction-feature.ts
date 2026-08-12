/** Interaction Feature：管理审批/问答 Interaction 卡片、定时超时与响应校验。 */

import type { ApprovalResponse, InteractionRequestEnvelope, InteractionResponse } from "@za38/protocol"
import type { Clock, IntentOutcome, InteractiveInteraction } from "../ports"
import { appendNotice, applyInteractionRequest, clearPendingInteraction } from "../state"
import type { FeatureContext } from "./types"

export type PendingInteraction = {
  request: InteractionRequestEnvelope
  resolve: (response: InteractionResponse) => void
  timerId?: () => void
}

/** Adapter 传入的用户响应；与协议 union 同构，仅 kind 命名差异。 */
export type ClientInteractionResponse =
  | { request_id: string; kind: "approval"; decision: ApprovalResponse["decision"]; feedback?: string }
  | { request_id: string; kind: "question"; answers: Record<string, string[]> }

export class InteractionFeature {
  pendingInteraction: PendingInteraction | null = null

  close(ctx: FeatureContext): void {
    this.settlePendingInteraction(ctx)
  }

  /** 终态事件、连接关闭、Controller 关闭或 Thread 重置时 abandon 并 fail-closed 收敛。 */
  settlePendingInteraction(ctx: FeatureContext): void {
    const pending = this.pendingInteraction
    if (!pending) return
    this.pendingInteraction = null
    pending.timerId?.()
    ctx.gateway.abandonInteraction(pending.request.request_id)
    pending.resolve(this.buildFallbackInteractionResponse(pending.request))
  }

  /** Run 取消或终态时收敛未完成 Interaction；与 settle 语义一致。 */
  abandonPendingInteraction(ctx: FeatureContext): void {
    this.settlePendingInteraction(ctx)
  }

  async handleInteractionRequest(request: InteractionRequestEnvelope, ctx: FeatureContext): Promise<InteractionResponse> {
    const active = ctx.getState().activeRun
    if (!active || active.threadId !== request.thread_id || active.runId !== request.run_id) {
      throw new Error("Interaction 请求不在当前 active run 内")
    }

    // 新请求替换旧 pending：旧请求 abandon + fail-closed，避免悬挂 RPC。
    this.settlePendingInteraction(ctx)
    ctx.commit(current => applyInteractionRequest(current, request))

    return new Promise<InteractionResponse>(resolve => {
      let timerId: (() => void) | undefined
      const timeoutMs = request.timeout_ms ?? 300_000

      if (timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        timerId = ctx.scheduler.setTimeout(() => {
          if (this.pendingInteraction?.request.request_id !== request.request_id) return
          this.pendingInteraction = null
          ctx.commit(current => clearPendingInteraction(current, request.request_id))
          resolve(this.buildFallbackInteractionResponse(request))
        }, timeoutMs)
      }

      this.pendingInteraction = { request, resolve, timerId }
      ctx.publish()

      if (timeoutMs === 0) {
        this.pendingInteraction = null
        ctx.commit(current => clearPendingInteraction(current, request.request_id))
        resolve(this.buildFallbackInteractionResponse(request))
      }
    })
  }

  /** 校验并回写用户响应；非法 decision/answer 不产生 RPC，只输出本地提示。 */
  respondInteraction(
    requestId: string,
    response: ClientInteractionResponse,
    ctx: FeatureContext,
  ): IntentOutcome {
    const pending = this.pendingInteraction
    if (!pending || pending.request.request_id !== requestId) {
      return { status: "rejected", code: "stale-interaction", message: "Stale or missing interaction request" }
    }

    if (pending.request.type === "approval") {
      if (response.kind !== "approval") {
        return { status: "rejected", code: "invalid-argument", message: "响应类型与请求不匹配" }
      }
      if (!pending.request.payload.decisions.includes(response.decision)) {
        ctx.commit(current => appendNotice(current, "不支持的审批决定，已忽略。"))
        return { status: "rejected", code: "invalid-argument", message: "Unsupported approval decision" }
      }
      const resolved: InteractionResponse = response.decision === "reject_with_feedback"
        ? { request_id: requestId, type: "approval", decision: "reject_with_feedback", feedback: response.feedback ?? "" }
        : { request_id: requestId, type: "approval", decision: response.decision }
      this.resolvePending(pending, resolved, ctx)
      return { status: "accepted" }
    }

    if (response.kind !== "question") {
      return { status: "rejected", code: "invalid-argument", message: "响应类型与请求不匹配" }
    }
    const violation = this.validateQuestionAnswers(pending.request, response.answers)
    if (violation) {
      ctx.commit(current => appendNotice(current, violation))
      return { status: "rejected", code: "invalid-argument", message: violation }
    }
    this.resolvePending(pending, { request_id: requestId, type: "question", answers: response.answers }, ctx)
    return { status: "accepted" }
  }

  /** 校验 question 回答；返回拒绝原因或 undefined。 */
  validateQuestionAnswers(
    request: InteractionRequestEnvelope,
    answers: Record<string, string[]>,
  ): string | undefined {
    if (request.type !== "question") return undefined
    const questions = request.payload.questions
    if (!questions.length) return "提问不包含任何问题，已忽略。"

    for (const question of questions) {
      const values = (answers[question.id] ?? []).filter(value => typeof value === "string" && value.trim() !== "")
      if (!values.length) return `缺少问题「${question.id}」的回答，已忽略。`
      if (!question.multi_select && values.length > 1) return `问题「${question.id}」只允许单选，已忽略。`
      if (!question.allow_other) {
        const allowed = new Set(question.options.map(option => option.value))
        const invalid = values.find(value => !allowed.has(value))
        if (invalid) return `问题「${question.id}」包含无效选项，已忽略。`
      }
    }
    return undefined
  }

  private resolvePending(pending: PendingInteraction, response: InteractionResponse, ctx: FeatureContext): void {
    this.pendingInteraction = null
    pending.timerId?.()
    pending.resolve(response)
    ctx.commit(current => clearPendingInteraction(current, pending.request.request_id))
  }

  /** 取消/超时/关闭时使用的 fail-closed 响应。 */
  private buildFallbackInteractionResponse(request: InteractionRequestEnvelope): InteractionResponse {
    return request.type === "approval"
      ? { request_id: request.request_id, type: "approval", decision: "reject" }
      : { request_id: request.request_id, type: "question", answers: {} }
  }

  /** 把 pending Interaction 转成共享 DTO；deadline 由注入 clock 计算。 */
  interactionDto(pending: PendingInteraction | null, clock: Clock): InteractiveInteraction | null {
    if (!pending) return null
    const request = pending.request
    const timeoutMs = request.timeout_ms ?? 300_000
    const deadlineAtMs = clock.now() + timeoutMs

    if (request.type === "approval") {
      return {
        type: "approval",
        requestId: request.request_id,
        description: request.payload.description,
        requests: request.payload.requests,
        presentation: request.payload.presentation ?? null,
        decisions: request.payload.decisions,
        deadlineAtMs,
      }
    }

    return {
      type: "question",
      requestId: request.request_id,
      questions: request.payload.questions.map(question => ({
        id: question.id,
        question: question.question,
        header: question.header,
        body: question.body,
        options: question.options.map(option => ({
          label: option.label,
          value: option.value,
          description: option.description,
        })),
        multiSelect: question.multi_select,
        allowOther: question.allow_other,
      })),
      deadlineAtMs,
    }
  }
}
