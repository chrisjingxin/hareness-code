/** Timeline Feature：负责 Agent 事件流的增量消费、sequence 序校验与 Timeline 投影。 */

import { EventType, type EventEnvelope } from "@za38/protocol"
import { applyAgentEvent, type InteractiveState } from "../state"
import type { FeatureContext } from "./types"

const TERMINAL_EVENT_TYPES = new Set<EventEnvelope["type"]>([
  EventType.INTERACTION_RESOLVED,
  EventType.RUN_COMPLETED,
  EventType.RUN_CANCELLED,
  EventType.RUN_FAILED,
])

export class TimelineFeature {
  private lastRunSequence = 0

  resetSequence(): void {
    this.lastRunSequence = 0
  }

  processAgentEvent(event: EventEnvelope, ctx: FeatureContext): void {
    const active = ctx.getState().activeRun
    if (!active || active.threadId !== event.thread_id || active.runId !== event.run_id) return

    if (event.sequence <= this.lastRunSequence) return
    if (this.lastRunSequence > 0 && event.sequence > this.lastRunSequence + 1) {
      ctx.commit(current => ({
        ...current,
        timeline: [
          ...current.timeline,
          {
            type: "message",
            message: {
              id: `seq-gap-${event.sequence}`,
              runId: event.run_id,
              role: "system",
              content: `sequence-gap: expected ${this.lastRunSequence + 1}, got ${event.sequence}`,
            },
          },
        ],
      }))
    }

    this.lastRunSequence = event.sequence
    ctx.commit(current => applyAgentEvent(current, event))

    if (TERMINAL_EVENT_TYPES.has(event.type) && event.type !== EventType.INTERACTION_RESOLVED) {
      this.lastRunSequence = 0
    }
  }
}
