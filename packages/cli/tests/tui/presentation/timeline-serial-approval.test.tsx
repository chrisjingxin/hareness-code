/** 无头复现串行审批焦点交接：第一个审批回写后，第二个对话框的 select 必须能接收回车。 */

import { expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { createRoot } from "@opentui/react"
import type { InteractionRequestEnvelope } from "@za38/protocol"

import {
  applyInteractionRequest,
  clearPendingInteraction,
  createInitialState,
  startRun,
  type TuiState,
} from "../../../src/tui/application/state"
import { ConversationTimeline } from "../../../src/tui/presentation/timeline"

const run = { threadId: "thread-1", runId: "run-1" }

/** 构造与 run_coordinator 串行审批相同形状的 approval 请求帧。 */
function approvalRequest(sequence: number, description: string): InteractionRequestEnvelope {
  return {
    request_id: `request-${sequence}`,
    type: "approval",
    thread_id: run.threadId,
    run_id: run.runId,
    timeout_ms: 1000,
    payload: { description, requests: { action_requests: [] }, decisions: [] },
  } as InteractionRequestEnvelope
}

test("串行审批第二个对话框的 select 能接收回车", async () => {
  const { renderer, mockInput, flush, waitFor } = await createTestRenderer({ width: 100, height: 40 })
  const tick = () => new Promise<void>(resolve => setTimeout(resolve, 0))
  const root = createRoot(renderer)
  const decisions: string[] = []

  const renderState = (state: TuiState) => {
    root.render(
      <ConversationTimeline
        state={state}
        scrollRef={{ current: null }}
        showToolDetails
        expandedTools={new Set()}
        onToggleTool={() => {}}
        onApproval={decision => decisions.push(decision)}
        onQuestion={() => {}}
      />,
    )
  }

  let state = startRun(createInitialState(), run, "删除三个文件")
  state = applyInteractionRequest(state, approvalRequest(1, "（第 1/3 个待审批操作）删除 a.txt"))
  renderState(state)
  await tick()
  await flush()
  await waitFor(() => renderer.currentFocusedRenderable !== null)

  mockInput.pressEnter()
  await tick()
  await flush()
  expect(decisions).toEqual(["approve_once"])

  // 用户回写后清除第一个弹窗；第二个请求按真实竞态先于 resolved 事件到达
  state = clearPendingInteraction(state, "approved")
  renderState(state)
  await tick()
  await flush()
  state = applyInteractionRequest(state, approvalRequest(2, "（第 2/3 个待审批操作）删除 b.txt"))
  renderState(state)
  await tick()
  await flush()

  await waitFor(() => renderer.currentFocusedRenderable !== null)
  mockInput.pressEnter()
  await tick()
  await flush()
  expect(decisions).toEqual(["approve_once", "approve_once"])

  root.unmount()
  renderer.destroy()
})
