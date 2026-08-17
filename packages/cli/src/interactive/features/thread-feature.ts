/** Thread Feature：管理 Thread 列表拉取、恢复打开、generation 时代管理及避免迟到响应。 */

import type { IntentOutcome } from "../ports"
import {
  applyThreadMode,
  applyWorkItem,
  clearThread,
  restoreThread,
  type ComposeStageId,
  type RestoredComposeActivity,
  type RestoredThreadMessage,
} from "../state"
import type { FeatureContext } from "./types"

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

const COMPOSE_STAGES = new Set(["understand", "plan", "build", "verify", "review"])

/** 校验 canonical threads.open 返回结构；无效结果视为 not-found，防止非法数据进入 Timeline。 */
function threadOpenResult(value: unknown): {
  threadId: string
  messages: RestoredThreadMessage[]
  composeActivities: RestoredComposeActivity[]
  threadMode: unknown
  workItem: unknown
} {
  const record = value as Record<string, unknown>
  const thread = record.thread
  if (!thread || typeof thread !== "object" || !Array.isArray(record.messages)) {
    throw new Error("Agent 返回的 thread 恢复结果无效")
  }
  const threadRecord = thread as Record<string, unknown>
  const threadId = typeof threadRecord.thread_id === "string" ? threadRecord.thread_id : ""
  if (!threadId) throw new Error("Agent 返回的 thread 恢复结果无效")

  const messages: RestoredThreadMessage[] = []
  for (const item of record.messages) {
    if (!item || typeof item !== "object") throw new Error("Agent 返回了无效的 thread message")
    const message = item as Record<string, unknown>
    if (message.kind !== "user" && message.kind !== "assistant" && message.kind !== "tool") {
      throw new Error("Agent 返回了无效的 thread message")
    }
    if (typeof message.content !== "string") throw new Error("Agent 返回了无效的 thread message")
    messages.push({
      kind: message.kind,
      content: message.content,
      toolName: typeof message.tool_name === "string" ? message.tool_name : undefined,
      createdAtMs: typeof message.created_at_ms === "number" && Number.isSafeInteger(message.created_at_ms) && message.created_at_ms > 0
        ? message.created_at_ms
        : undefined,
    })
  }
  const composeActivities: RestoredComposeActivity[] = []
  const rawActivities = Array.isArray(record.compose_activities) ? record.compose_activities : []
  for (const item of rawActivities) {
    if (!item || typeof item !== "object") continue
    const activity = item as Record<string, unknown>
    const stage = typeof activity.stage === "string" ? activity.stage : ""
    const kind = activity.kind
    if (!COMPOSE_STAGES.has(stage)) continue
    if (kind !== "summary" && kind !== "tool_terminal" && kind !== "truncation") continue
    if (typeof activity.run_id !== "string" || typeof activity.activity_id !== "string") continue
    if (typeof activity.attempt !== "number" || activity.attempt < 1) continue
    if (typeof activity.label !== "string" || typeof activity.status !== "string") continue
    if (typeof activity.event_sequence !== "number" || typeof activity.created_at_ms !== "number") continue
    composeActivities.push({
      runId: activity.run_id,
      eventSequence: activity.event_sequence,
      activityId: activity.activity_id,
      stage: stage as ComposeStageId,
      attempt: activity.attempt,
      kind,
      label: activity.label,
      status: activity.status,
      createdAtMs: activity.created_at_ms,
      taskId: typeof activity.task_id === "string" ? activity.task_id : undefined,
      taskTitle: typeof activity.task_title === "string" ? activity.task_title : undefined,
      executionId: typeof activity.execution_id === "string" ? activity.execution_id : undefined,
      agentId: typeof activity.agent_id === "string" ? activity.agent_id : undefined,
      boundedText: typeof activity.bounded_text === "string" ? activity.bounded_text : undefined,
    })
  }
  return {
    threadId,
    messages,
    composeActivities,
    threadMode: record.thread_mode ?? null,
    workItem: record.work_item ?? null,
  }
}

export class ThreadFeature {
  threadEpoch = 0
  openingThread = false

  async openThread(
    threadId: string,
    ctx: FeatureContext,
    options: {
      hasPendingInteraction?: boolean
      onBeforeOpen?: () => void
      onSuccess?: () => void
    },
  ): Promise<IntentOutcome> {
    if (ctx.getState().activeRun || options.hasPendingInteraction) {
      return { status: "rejected", code: "busy", message: "Cannot open thread while run or interaction is active" }
    }
    if (this.openingThread) {
      return { status: "rejected", code: "busy", message: "Thread open operation already in progress" }
    }

    // 先清空当前显示再捕获 epoch：onBeforeOpen 的重置也递增 epoch，必须发生在捕获之前。
    this.openingThread = true
    options.onBeforeOpen?.()
    const currentEpoch = ++this.threadEpoch
    try {
      const opened = threadOpenResult(await ctx.gateway.openThread(threadId))
      if (currentEpoch !== this.threadEpoch) {
        return { status: "rejected", code: "stale-interaction", message: "Stale thread open operation" }
      }
      this.openingThread = false
      ctx.commit(current => {
        const restored = restoreThread(
          opened.threadId,
          opened.messages,
          current.workMode,
          opened.composeActivities,
          opened.threadMode === "build" || opened.threadMode === "compose" ? opened.threadMode : null,
        )
        const withMode = applyThreadMode(restored, opened.threadMode)
        return opened.workItem != null ? applyWorkItem(withMode, opened.workItem) : withMode
      })
      options.onSuccess?.()
      return { status: "accepted" }
    } catch (error) {
      if (currentEpoch === this.threadEpoch) {
        this.openingThread = false
      }
      return { status: "rejected", code: "not-found", message: `无法打开 Thread：${errorMessage(error)}` }
    }
  }

  async restoreInitialThread(
    initialThreadId: string | null,
    ctx: FeatureContext,
    options: {
      onSuccess?: () => void
    },
  ): Promise<void> {
    if (initialThreadId === null) {
      ctx.commit(current => clearThread(current))
      return
    }

    const currentEpoch = ++this.threadEpoch
    this.openingThread = true

    try {
      const opened = threadOpenResult(await ctx.gateway.openThread(initialThreadId))
      if (currentEpoch !== this.threadEpoch) return
      this.openingThread = false
      ctx.commit(current => restoreThread(
        opened.threadId,
        opened.messages,
        current.workMode,
        opened.composeActivities,
        opened.threadMode === "build" || opened.threadMode === "compose" ? opened.threadMode : null,
      ))
      options.onSuccess?.()
    } catch {
      if (currentEpoch === this.threadEpoch) {
        this.openingThread = false
        ctx.commit(current => clearThread(current))
      }
    }
  }
}
