/** v2 事件和交互请求的 TUI 归约测试。 */

import { expect, test } from "bun:test"
import type { EventEnvelope, InteractionRequestEnvelope } from "@za38/protocol"
import { applyAgentEvent, applyInteractionRequest, clearThread, createInitialState, isHomeState, startRun, type TuiState } from "../../src/tui/state"

const run = { threadId: "thread-1", runId: "run-1" }

test("初始状态和清空后的状态进入首页", () => {
  const initial = createInitialState()
  expect(isHomeState(initial)).toBeTrue()
  expect(isHomeState(clearThread(startRun(initial, run, "生成组件")))).toBeTrue()
})

test("v2 事件按 sequence 更新消息、工具和终态", () => {
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, event("content.delta", 1, { text: "正在" }))
  state = applyAgentEvent(state, event("tool.started", 2, { tool_call_id: "tool-1", name: "read_file" }))
  state = applyAgentEvent(state, event("tool.completed", 3, { tool_call_id: "tool-1", result: { content: "src/app.ts", is_error: false } }))
  state = applyAgentEvent(state, event("run.completed", 4, { duration_ms: 1340, usage: { input_tokens: 1200, output_tokens: 35 } }))
  expect(state.timeline.map(item => item.type)).toEqual(["message", "message", "tool"])
  expect(tools(state)[0]).toMatchObject({ name: "read_file", detail: "src/app.ts", status: "completed" })
  expect(state.lastRun).toMatchObject({ outcome: "completed", durationMs: 1340, usage: { inputTokens: 1200, outputTokens: 35 } })
})

test("审批和稳定 question ID 通过反向 request 进入状态", () => {
  let state = startRun(createInitialState(), run, "修改文件")
  state = applyInteractionRequest(state, request("approval", 1, { description: "写入源文件", requests: { action_requests: [] } }))
  expect(state.pendingApproval).toMatchObject({ requestId: "request-1", description: "写入源文件" })
  state = { ...state, pendingApproval: undefined }
  state = applyInteractionRequest(state, request("question", 2, { questions: [{ id: "question-1", question: "选择目录", options: [{ label: "src", value: "src" }, { label: "tests", value: "tests" }] }] }))
  expect(state.pendingQuestion).toEqual({ requestId: "request-2", questionId: "question-1", question: "选择目录", options: [{ name: "src", value: "src" }, { name: "tests", value: "tests" }] })
})

test("重复和倒序事件被忽略，sequence 缺口产生诊断但继续应用", () => {
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, event("content.delta", 2, { text: "新内容" }))
  state = applyAgentEvent(state, event("content.delta", 1, { text: "旧内容" }))
  state = applyAgentEvent(state, event("content.delta", 4, { text: "继续" }))
  expect(messages(state).some(message => message.content.includes("旧内容"))).toBeFalse()
  expect(messages(state).some(message => message.content.includes("协议序号缺口"))).toBeTrue()
  expect(messages(state).at(-1)?.content).toBe("继续")
})

test("工具之后的文本保持协议顺序", () => {
  let state = startRun(createInitialState(), run, "读取")
  state = applyAgentEvent(state, event("content.delta", 1, { text: "先读取。" }))
  state = applyAgentEvent(state, event("tool.started", 2, { tool_call_id: "tool-1", name: "read_file" }))
  state = applyAgentEvent(state, event("tool.completed", 3, { tool_call_id: "tool-1", result: { content: "ok", is_error: false } }))
  state = applyAgentEvent(state, event("content.delta", 4, { text: "读取完成。" }))
  expect(state.timeline.map(item => item.type)).toEqual(["message", "message", "tool", "message"])
})

function event(type: string, sequence: number, payload: Record<string, unknown>): EventEnvelope {
  return { event_id: `event-${sequence}`, type, thread_id: run.threadId, run_id: run.runId, sequence, timestamp_ms: 1, payload }
}

function request(type: "approval" | "question", sequence: number, payload: Record<string, unknown>): InteractionRequestEnvelope {
  return { request_id: `request-${sequence}`, type, thread_id: run.threadId, run_id: run.runId, sequence, timeout_ms: 1000, payload }
}

function messages(state: TuiState) { return state.timeline.flatMap(item => item.type === "message" ? [item.message] : []) }
function tools(state: TuiState) { return state.timeline.flatMap(item => item.type === "tool" ? [item.tool] : []) }
