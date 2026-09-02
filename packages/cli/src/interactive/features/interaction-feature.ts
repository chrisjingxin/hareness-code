/** Interaction Feature：管理审批/问答 Interaction 卡片、定时超时与响应校验。 */

import type { ApprovalResponse, DirectoryTrustResponse, InteractionRequestEnvelope, InteractionResponse, PlanResponse } from "@za38/protocol"
import type { Clock, IntentOutcome, InteractiveInteraction } from "../ports"
import { appendNotice, applyInteractionRequest, markInteractionResponded, markInteractionTimeout } from "../state"
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
  | { request_id: string; kind: "directory_trust"; decision: DirectoryTrustResponse["decision"] }
  | { request_id: string; kind: "plan"; decision: PlanResponse["decision"]; feedback?: string }

export class InteractionFeature {
  pendingInteraction: PendingInteraction | null = null
  private readonly queuedInteractions: PendingInteraction[] = []
  private planViewer: Extract<InteractiveInteraction, { type: "plan" }> | null = null

  close(ctx: FeatureContext): void {
    this.settlePendingInteraction(ctx)
  }

  /** 终态事件、连接关闭、Controller 关闭或 Thread 重置时 abandon 并 fail-closed 收敛整条队列。 */
  settlePendingInteraction(ctx: FeatureContext): void {
    const pending = [this.pendingInteraction, ...this.queuedInteractions].filter(
      (item): item is PendingInteraction => item !== null,
    )
    this.pendingInteraction = null
    this.planViewer = null
    this.queuedInteractions.length = 0
    for (const item of pending) {
      item.timerId?.()
      ctx.gateway.abandonInteraction(item.request.request_id)
      item.resolve(this.buildFallbackInteractionResponse(item.request))
    }
  }

  /** Run 取消或终态时收敛未完成 Interaction；与 settle 语义一致。 */
  abandonPendingInteraction(ctx: FeatureContext): void {
    this.settlePendingInteraction(ctx)
  }

  async handleInteractionRequest(request: InteractionRequestEnvelope, ctx: FeatureContext): Promise<InteractionResponse> {
    if (request.type === "plugin_consent") {
      // Plugin 管理只在独立 Shell CLI 里声明此 handle；TUI/Web 不创建管理卡片。
      throw new Error("Plugin consent is not handled by Interactive Core")
    }
    const active = ctx.getState().activeRun
    if (!active || active.threadId !== request.thread_id || active.runId !== request.run_id) {
      throw new Error("Interaction 请求不在当前 active run 内")
    }

    return new Promise<InteractionResponse>(resolve => {
      let timerId: (() => void) | undefined
      // Plan 审阅与未明确指定有限超时的交互不设倒计时超时，允许用户从容阅读与批注
      const timeoutMs = request.type === "plan" ? undefined : request.timeout_ms
      const item: PendingInteraction = { request, resolve, timerId }

      if (typeof timeoutMs === "number" && timeoutMs > 0 && Number.isFinite(timeoutMs)) {
        timerId = ctx.scheduler.setTimeout(() => {
          this.expireInteraction(request.request_id, ctx)
        }, timeoutMs)
        item.timerId = timerId
      }

      if (this.pendingInteraction) {
        this.queuedInteractions.push(item)
      } else {
        this.activateInteraction(item, ctx)
      }

      if (timeoutMs === 0) {
        this.expireInteraction(request.request_id, ctx)
      }
    })
  }

  private activateInteraction(item: PendingInteraction, ctx: FeatureContext): void {
    this.planViewer = null
    this.pendingInteraction = item
    ctx.commit(current => applyInteractionRequest(current, item.request))
    ctx.publish()
  }

  private expireInteraction(requestId: string, ctx: FeatureContext): void {
    if (this.pendingInteraction?.request.request_id === requestId) {
      const pending = this.pendingInteraction
      this.pendingInteraction = null
      pending.timerId?.()
      ctx.commit(current => markInteractionTimeout(current, requestId))
      pending.resolve(this.buildFallbackInteractionResponse(pending.request))
      this.promoteQueuedInteraction(ctx)
      return
    }
    const index = this.queuedInteractions.findIndex(item => item.request.request_id === requestId)
    if (index < 0) return
    const [queued] = this.queuedInteractions.splice(index, 1)
    if (!queued) return
    queued.timerId?.()
    queued.resolve(this.buildFallbackInteractionResponse(queued.request))
  }

  private promoteQueuedInteraction(ctx: FeatureContext): void {
    const next = this.queuedInteractions.shift()
    if (!next) return
    this.activateInteraction(next, ctx)
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
      this.promoteQueuedInteraction(ctx)
      return { status: "accepted" }
    }

    if (pending.request.type === "directory_trust") {
      if (response.kind !== "directory_trust") {
        return { status: "rejected", code: "invalid-argument", message: "响应类型与请求不匹配" }
      }
      if (!pending.request.payload.decisions.includes(response.decision)) {
        ctx.commit(current => appendNotice(current, "不支持的目录信任决定，已忽略。"))
        return { status: "rejected", code: "invalid-argument", message: "Unsupported directory trust decision" }
      }
      this.resolvePending(pending, { request_id: requestId, type: "directory_trust", decision: response.decision }, ctx)
      this.promoteQueuedInteraction(ctx)
      return { status: "accepted" }
    }

    if (pending.request.type === "plan") {
      if (response.kind !== "plan") {
        return { status: "rejected", code: "invalid-argument", message: "响应类型与请求不匹配" }
      }
      if (!pending.request.payload.decisions.includes(response.decision)) {
        ctx.commit(current => appendNotice(current, "不支持的计划决定，已忽略。"))
        return { status: "rejected", code: "invalid-argument", message: "Unsupported plan decision" }
      }
      this.resolvePending(pending, {
        request_id: requestId,
        type: "plan",
        decision: response.decision,
        feedback: response.decision === "abandoned" ? undefined : (response.feedback ?? ""),
      }, ctx)
      this.promoteQueuedInteraction(ctx)
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
    this.promoteQueuedInteraction(ctx)
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
    const status = response.type === "question"
      ? "answered"
      : response.type === "plan"
        ? response.decision === "approved" ? "approved" : response.decision === "abandoned" ? "rejected" : "answered"
      : response.decision === "reject" || response.decision === "reject_with_feedback"
        ? "rejected"
        : "approved"
    ctx.commit(current => markInteractionResponded(current, pending.request.request_id, status))
  }

  /** 取消/超时/关闭时使用的 fail-closed 响应。 */
  private buildFallbackInteractionResponse(request: InteractionRequestEnvelope): InteractionResponse {
    if (request.type === "approval") {
      return { request_id: request.request_id, type: "approval", decision: "reject" }
    }
    if (request.type === "directory_trust") {
      return { request_id: request.request_id, type: "directory_trust", decision: "deny" }
    }
    if (request.type === "plan") {
      return { request_id: request.request_id, type: "plan", decision: "abandoned" }
    }
    if (request.type === "plugin_consent") {
      return { request_id: request.request_id, type: "plugin_consent", decision: "cancel" }
    }
    return { request_id: request.request_id, type: "question", answers: {} }
  }

  /** 把 pending Interaction 转成共享 DTO；deadline 由注入 clock 计算。 */
  interactionDto(pending: PendingInteraction | null, clock: Clock): InteractiveInteraction | null {
    if (!pending) return this.planViewer
    const request = pending.request
    if (request.type === "plugin_consent") return null
    const timeoutMs = request.type === "plan" ? undefined : request.timeout_ms
    const deadlineAtMs = typeof timeoutMs === "number" && Number.isFinite(timeoutMs) && timeoutMs > 0
      ? clock.now() + timeoutMs
      : Number.POSITIVE_INFINITY

    const agentId = typeof request.agent_id === "string" && request.agent_id ? request.agent_id : undefined
    if (request.type === "approval") {
      return {
        type: "approval",
        requestId: request.request_id,
        description: request.payload.description,
        requests: request.payload.requests,
        presentation: request.payload.presentation ?? null,
        decisions: request.payload.decisions,
        deadlineAtMs,
        ...(agentId ? { agentId } : {}),
      }
    }

    if (request.type === "directory_trust") {
      const payload = request.payload
      return {
        type: "directory_trust",
        requestId: request.request_id,
        directory: payload.directory,
        targetPath: payload.target_path,
        toolName: payload.tool_name,
        access: payload.access,
        shadowsWorkspace: payload.shadows_workspace,
        decisions: payload.decisions,
        deadlineAtMs,
        ...(agentId ? { agentId } : {}),
      }
    }

    if (request.type === "plan") {
      const payload = request.payload
      return {
        type: "plan",
        requestId: request.request_id,
        revision: payload.revision,
        hasPlan: payload.has_plan,
        planMarkdown: payload.plan_markdown,
        planVirtualPath: payload.plan_virtual_path,
        planDisplayPath: payload.plan_display_path,
        decisions: payload.decisions,
        deadlineAtMs,
        ...(agentId ? { agentId } : {}),
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
      ...(agentId ? { agentId } : {}),
    }
  }

  /** 打开当前 thread 的只读计划预览；它不是审批，不进入 pendingInteraction。 */
  openPlanViewer(input: {
    threadId: string
    markdown: string
    virtualPath: string
    displayPath: string
  }, ctx: FeatureContext): void {
    this.planViewer = {
      type: "plan",
      requestId: `view-plan:${input.threadId}`,
      revision: 0,
      hasPlan: true,
      planMarkdown: input.markdown,
      planVirtualPath: input.virtualPath,
      planDisplayPath: input.displayPath,
      decisions: [],
      deadlineAtMs: Number.POSITIVE_INFINITY,
      readOnly: true,
    }
    ctx.publish()
  }

  /** 关闭 /plan-view 的本地预览；挂起审批不能用该入口关闭。 */
  closePlanViewer(ctx: FeatureContext): IntentOutcome {
    if (!this.planViewer) {
      return { status: "rejected", code: "not-found", message: "No plan viewer is open" }
    }
    this.planViewer = null
    ctx.publish()
    return { status: "accepted" }
  }
}
