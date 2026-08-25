import { expect, test } from "bun:test"

import { EventType } from "@za38/protocol"

import { flush, makeHarness, terminalEvent } from "./harness"

test("点开子时间线只改路由：卡片仍在，快照只留该 execution", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "派一个子代理" })
    const run = harness.runHandles.at(-1)!
    const child = "child-abc123"
    const taskArguments = JSON.stringify({ description: "查代码", subagent_type: "general-purpose" })
    harness.port.emitEvent(terminalEvent(EventType.TOOL_STARTED, run.threadId, run.runId, 1, { tool_call_id: "task-1", name: "task" }))
    harness.port.emitEvent(terminalEvent(EventType.TOOL_DELTA, run.threadId, run.runId, 2, { tool_call_id: "task-1", arguments_delta: taskArguments }))
    harness.port.emitEvent(terminalEvent(EventType.TOOL_DELTA, run.threadId, run.runId, 3, { tool_call_id: "task-1", child_execution_id: child, child_agent_id: "general-purpose" }))
    harness.port.emitEvent({ ...terminalEvent(EventType.TOOL_STARTED, run.threadId, run.runId, 4, { tool_call_id: "child-tool-1", name: "read_file" }), execution_id: child, parent_execution_id: `root-${run.runId}`, agent_id: "general-purpose" })
    harness.port.emitEvent({ ...terminalEvent(EventType.TOOL_DELTA, run.threadId, run.runId, 5, { tool_call_id: "child-tool-1", arguments_delta: '{"file_path":"src/app.ts"}' }), execution_id: child, parent_execution_id: `root-${run.runId}`, agent_id: "general-purpose" })
    harness.port.emitEvent({ ...terminalEvent(EventType.TOOL_COMPLETED, run.threadId, run.runId, 6, { tool_call_id: "child-tool-1", result: { content: "SECRET_CHILD_BODY", is_error: false, truncated: false, original_bytes: 17 } }), execution_id: child, parent_execution_id: `root-${run.runId}`, agent_id: "general-purpose" })
    harness.port.emitEvent({ ...terminalEvent(EventType.REASONING_DELTA, run.threadId, run.runId, 7, { text: "child reasoning" }), execution_id: child, parent_execution_id: `root-${run.runId}`, agent_id: "general-purpose" })
    harness.port.emitEvent({ ...terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 8, { text: "child conclusion" }), execution_id: child, parent_execution_id: `root-${run.runId}`, agent_id: "general-purpose" })
    await flush()

    const parent = harness.controller.getSnapshot()
    const taskCard = parent.timeline.find(item => item.type === "tool" && item.tool.name === "task")
    expect(taskCard?.type === "tool" ? taskCard.tool.childExecutionId : null).toBe(child)
    expect(taskCard?.type === "tool" ? taskCard.tool.arguments : null).toBe(taskArguments)
    expect(JSON.stringify(parent.timeline)).not.toContain("SECRET_CHILD_BODY")
    expect(JSON.stringify(parent.timeline)).not.toContain("child reasoning")
    expect(parent.childTimelineExecutionId).toBeNull()
    // 父视图不渲染 child 工具。
    expect(parent.timeline.find(item => item.type === "tool" && item.tool.name === "read_file")).toBeUndefined()

    await harness.controller.dispatch({ type: "child-timeline.open", executionId: child })
    const childView = harness.controller.getSnapshot()
    expect(childView.childTimelineExecutionId).toBe(child)
    expect(childView.timeline.map(item => item.type === "tool" ? item.tool.name : item.type)).toEqual(["read_file", "reasoning", "message"])
    expect(JSON.stringify(childView.timeline)).toContain("SECRET_CHILD_BODY")
    // 派出卡仍在 state 里，只是被路由过滤。
    expect(harness.controller.getSnapshot().timeline.some(item => item.type === "tool" && item.tool.name === "task")).toBe(false)

    await harness.controller.dispatch({ type: "child-timeline.leave" })
    const back = harness.controller.getSnapshot()
    expect(back.childTimelineExecutionId).toBeNull()
    expect(back.timeline.some(item => item.type === "tool" && item.tool.name === "task")).toBe(true)
  } finally {
    await harness.controller.close()
  }
})

test("子视图只读：child route 下提交消息被拒绝，不创建新 Run", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "派一个子代理" })
    const run = harness.runHandles.at(-1)!
    harness.port.emitEvent(terminalEvent(EventType.TOOL_STARTED, run.threadId, run.runId, 1, { tool_call_id: "task-1", name: "task" }))
    harness.port.emitEvent(terminalEvent(EventType.TOOL_DELTA, run.threadId, run.runId, 2, { tool_call_id: "task-1", child_execution_id: "child-abc123", child_agent_id: "explore" }))
    await flush()

    await harness.controller.dispatch({ type: "child-timeline.open", executionId: "child-abc123" })
    const outcome = await harness.controller.dispatch({ type: "input.submit", value: "继续" })
    expect(outcome.status).toBe("rejected")
    expect(harness.runHandles).toHaveLength(1)
    // 打开未知 child：记录路由，由视图显示空态，不报错。
    await harness.controller.dispatch({ type: "child-timeline.open", executionId: "child-unknown" })
    expect(harness.controller.getSnapshot().childTimelineExecutionId).toBe("child-unknown")
    expect(harness.controller.getSnapshot().timeline).toEqual([])
  } finally {
    await harness.controller.close()
  }
})
