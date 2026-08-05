/** Thread Feature：管理 Thread 列表拉取、恢复打开、generation 时代管理及避免迟到响应。 */

import type { IntentOutcome } from "../ports"
import { clearThread, restoreThread, type RestoredThreadMessage } from "../state"
import type { FeatureContext } from "./types"

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 校验 canonical threads.open 返回结构；无效结果视为 not-found，防止非法数据进入 Timeline。 */
function threadOpenResult(value: unknown): { threadId: string; messages: RestoredThreadMessage[] } {
  if (!value || typeof value !== "object") throw new Error("Agent 返回的 thread 恢复结果无效")
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
    })
  }
  return { threadId, messages }
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
      ctx.commit(() => restoreThread(opened.threadId, opened.messages))
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
      ctx.commit(() => restoreThread(opened.threadId, opened.messages))
      options.onSuccess?.()
    } catch {
      if (currentEpoch === this.threadEpoch) {
        this.openingThread = false
        ctx.commit(current => clearThread(current))
      }
    }
  }
}
