/** v3 事件和交互请求的共享 reducer 归约测试。 */

import { expect, test } from "bun:test"
import type { EventEnvelope, InteractionRequestEnvelope } from "@za38/protocol"
import { applyAgentEvent, applyInteractionRequest, applyComposeState, clearPendingInteraction, clearThread, createInitialState, isHomeState, restoreThread, setWorkMode, startRun, type InteractiveState } from "../../src/interactive/state"

const run = { threadId: "thread-1", runId: "run-1" }

test("初始状态和清空后的状态进入首页", () => {
  const initial = createInitialState()
  expect(isHomeState(initial)).toBeTrue()
  expect(isHomeState(clearThread(startRun(initial, run, "生成组件")))).toBeTrue()
})

test("恢复 thread 会原子替换时间线并清空旧运行状态", () => {
  const active = startRun(createInitialState(), run, "旧 thread 消息")
  const restored = restoreThread("restored-thread", [
    { kind: "user", content: "此前请求" },
    { kind: "assistant", content: "此前回答" },
    { kind: "tool", content: "此前工具结果", toolName: "execute" },
  ])
  expect(active.activeRun).toBeDefined()
  expect(restored.currentThreadId).toBe("restored-thread")
  expect(restored.activeRun).toBeNull()
  expect(restored.sequences).toEqual({})
  expect(restored.timeline.map(item => item.type)).toEqual(["message", "message", "tool"])
})

test("v3 事件按 sequence 更新消息、工具和终态", () => {
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, event("content.delta", 1, { text: "正在" }))
  state = applyAgentEvent(state, event("tool.started", 2, { tool_call_id: "tool-1", name: "read_file" }))
  state = applyAgentEvent(state, event("tool.completed", 3, { tool_call_id: "tool-1", result: { content: "src/app.ts", is_error: false } }))
  state = applyAgentEvent(state, event("run.completed", 4, { duration_ms: 1340, usage: { input_tokens: 1200, output_tokens: 35 } }))
  expect(state.timeline.map(item => item.type)).toEqual(["message", "message", "tool"])
  expect(tools(state)[0]).toMatchObject({ name: "read_file", output: "src/app.ts", status: "completed" })
  expect(state.lastRun).toMatchObject({ outcome: "completed", durationMs: 1340, usage: { inputTokens: 1200, outputTokens: 35 } })
})

test("运行进度只存在于当前 Run，并在终态清理", () => {
  let state = startRun(createInitialState(), run, "检查代码")
  expect(state.runProgress).toEqual({ phase: "preparing", elapsedMs: 0 })
  state = applyAgentEvent(state, event("run.progress", 1, { phase: "model", elapsed_ms: 180 }))
  expect(state.runProgress).toEqual({ phase: "model", elapsedMs: 180 })
  state = applyAgentEvent(state, event("run.completed", 2, { duration_ms: 200, usage: { input_tokens: 1, output_tokens: 1 } }))
  expect(state.runProgress).toBeNull()
})

test("思考按流式顺序与消息/工具交错成时间线条目", () => {
  let state = startRun(createInitialState(), run, "检查代码")
  // 思考1 → 正文1 → 思考2 → 工具1 → 正文2
  state = applyAgentEvent(state, event("reasoning.delta", 1, { text: "正在" }))
  state = applyAgentEvent(state, event("reasoning.delta", 2, { text: "检查" }))
  expect(state.timeline).toEqual([
    expect.objectContaining({ type: "message" }),
    { type: "reasoning", reasoning: { id: expect.any(String), runId: run.runId, text: "正在检查", active: true } },
  ])
  // 正文到达：思考段冻结，正文进入 assistant 消息
  state = applyAgentEvent(state, event("content.delta", 3, { text: "结论一" }))
  expect(state.timeline.map(item => item.type)).toEqual(["message", "reasoning", "message"])
  expect((state.timeline[1] as { reasoning: { active: boolean } }).reasoning.active).toBeFalse()
  // 新一轮思考：作为新条目追加在正文之后
  state = applyAgentEvent(state, event("reasoning.delta", 4, { text: "再次" }))
  expect(state.timeline.map(item => item.type)).toEqual(["message", "reasoning", "message", "reasoning"])
  // 工具开始：冻结思考段，工具条目追加
  state = applyAgentEvent(state, event("tool.started", 5, { tool_call_id: "t-1", name: "execute" }))
  expect(state.timeline.map(item => item.type)).toEqual(["message", "reasoning", "message", "reasoning", "tool"])
  expect((state.timeline[3] as { reasoning: { active: boolean } }).reasoning.active).toBeFalse()
  // 正文再次到达：因 tool 条目在末尾，正文分段为新 assistant 消息
  state = applyAgentEvent(state, event("content.delta", 6, { text: "结论二" }))
  expect(state.timeline.map(item => item.type)).toEqual(["message", "reasoning", "message", "reasoning", "tool", "message"])
  // 终态：思考条目保留（已冻结），可回看
  state = applyAgentEvent(state, event("run.completed", 7, { duration_ms: 200, usage: { input_tokens: 1, output_tokens: 1 } }))
  expect(state.timeline.filter(item => item.type === "reasoning")).toHaveLength(2)
})

test("skill.loaded 事件加入可追踪的系统时间线项", () => {
  let state = startRun(createInitialState(), run, "检查变更")
  state = applyAgentEvent(state, event("skill.loaded", 1, {
    skill_id: "project/review",
    source: "project",
    version: null,
    snapshot_id: "snapshot-1",
  }))
  expect(messages(state).at(-1)).toMatchObject({ role: "system", content: "skill-loaded: project/review" })
})

test("审批和稳定 question ID 通过时间线 request 进入状态", () => {
  let state = startRun(createInitialState(), run, "修改文件")
  state = applyInteractionRequest(state, request("approval", 1, { description: "写入源文件", requests: { action_requests: [] } }))
  expect(state.activity.kind).toBe("waiting-interaction")
  expect(interactions(state)[0]).toMatchObject({ id: "request-1", type: "approval", status: "pending" })
  state = clearPendingInteraction(state, "request-1")
  expect(interactions(state)[0]).toMatchObject({ id: "request-1", status: "cancelled" })
  state = applyAgentEvent(state, event("interaction.resolved", 2, { request_id: "request-1", type: "approval" }))
  state = applyInteractionRequest(state, request("question", 3, { questions: [{ id: "question-1", question: "选择目录", options: [{ label: "src", value: "src" }, { label: "tests", value: "tests" }] }] }))
  expect(state.activity.kind).toBe("waiting-interaction")
  expect(interactions(state)[1]).toMatchObject({ id: "request-3", type: "question", status: "pending" })
})

test("重复和倒序事件被忽略，sequence 缺口产生诊断但继续应用", () => {
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, event("content.delta", 2, { text: "新内容" }))
  state = applyAgentEvent(state, event("content.delta", 1, { text: "旧内容" }))
  state = applyAgentEvent(state, event("content.delta", 4, { text: "继续" }))
  expect(messages(state).some(message => message.content.includes("旧内容"))).toBeFalse()
  expect(messages(state).some(message => message.content.includes("sequence-gap"))).toBeTrue()
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

test("连续工具调用保留各自的参数和真实结果", () => {
  let state = startRun(createInitialState(), run, "检查目录")
  state = applyAgentEvent(state, event("tool.started", 1, { tool_call_id: "call-1", name: "execute" }))
  state = applyAgentEvent(state, event("tool.delta", 2, { tool_call_id: "call-1", arguments_delta: "{\"command\":\"ls\"}" }))
  state = applyAgentEvent(state, event("tool.completed", 3, { tool_call_id: "call-1", result: { content: "README.md", is_error: false } }))
  state = applyAgentEvent(state, event("tool.started", 4, { tool_call_id: "call-2", name: "execute" }))
  state = applyAgentEvent(state, event("tool.delta", 5, { tool_call_id: "call-2", arguments_delta: "{\"command\":\"pwd\"}" }))
  state = applyAgentEvent(state, event("tool.completed", 6, { tool_call_id: "call-2", result: { content: "/workspace", is_error: false } }))

  expect(tools(state)).toEqual([
    { id: "call-1", runId: "run-1", name: "execute", arguments: "{\"command\":\"ls\"}", output: "README.md", status: "completed" },
    { id: "call-2", runId: "run-1", name: "execute", arguments: "{\"command\":\"pwd\"}", output: "/workspace", status: "completed" },
  ])
})

test("不同 run 中重复的 tool call ID 不会覆盖历史调用", () => {
  let state = startRun(createInitialState(), run, "第一次")
  state = applyAgentEvent(state, event("tool.started", 1, { tool_call_id: "call-1", name: "execute" }))
  state = applyAgentEvent(state, event("tool.completed", 2, { tool_call_id: "call-1", result: { content: "first", is_error: false } }))
  state = applyAgentEvent(state, event("run.completed", 3, {}))

  const secondRun = { threadId: run.threadId, runId: "run-2" }
  state = startRun(state, secondRun, "第二次")
  state = applyAgentEvent(state, {
    event_id: "run-2-event-1",
    type: "tool.started",
    thread_id: secondRun.threadId,
    run_id: secondRun.runId,
    sequence: 1,
    timestamp_ms: 1,
    payload: { tool_call_id: "call-1", name: "execute" },
  })
  state = applyAgentEvent(state, {
    event_id: "run-2-event-2",
    type: "tool.completed",
    thread_id: secondRun.threadId,
    run_id: secondRun.runId,
    sequence: 2,
    timestamp_ms: 1,
    payload: { tool_call_id: "call-1", result: { content: "second", is_error: false } },
  })

  expect(tools(state).map(tool => tool.output)).toEqual(["first", "second"])
})

function event(type: string, sequence: number, payload: Record<string, unknown>): EventEnvelope {
  return { event_id: `event-${sequence}`, type, thread_id: run.threadId, run_id: run.runId, sequence, timestamp_ms: 1, payload }
}

function request(type: "approval" | "question", sequence: number, payload: Record<string, unknown>): InteractionRequestEnvelope {
  return { request_id: `request-${sequence}`, type, thread_id: run.threadId, run_id: run.runId, timeout_ms: 1000, payload } as InteractionRequestEnvelope
}

function messages(state: InteractiveState) { return state.timeline.flatMap(item => item.type === "message" ? [item.message] : []) }
function tools(state: InteractiveState) { return state.timeline.flatMap(item => item.type === "tool" ? [item.tool] : []) }
function interactions(state: InteractiveState) { return state.timeline.flatMap(item => item.type === "interaction" ? [item.interaction] : []) }


function composeEvent(sequence: number, payload: Record<string, unknown>) {
  return event("compose.state", sequence, payload)
}

test("workMode 默认 build，可在空闲时切换并跨 Run 保留", () => {
  const initial = createInitialState()
  expect(initial.workMode).toBe("build")
  const switched = setWorkMode(initial, "compose")
  expect(switched.workMode).toBe("compose")
  // 相同模式幂等
  expect(setWorkMode(switched, "compose")).toBe(switched)
  // startRun 保留选择（Run 受理后冻结）
  const started = startRun(switched, run, "请求")
  expect(started.workMode).toBe("compose")
})

test("compose.state 折叠为 projection，waiting_user 进入等待交互", () => {
  let state = startRun(createInitialState(), run, "实现搜索")
  const projection = {
    revision: 2,
    stage: "plan",
    status: "waiting_user",
    stages: [
      { id: "understand", status: "passed", attempts: 1 },
      { id: "plan", status: "passed", attempts: 1 },
      { id: "build", status: "pending", attempts: 0 },
      { id: "verify", status: "pending", attempts: 0 },
      { id: "review", status: "pending", attempts: 0 },
    ],
    tasks: [{ id: "task-1", title: "实现搜索", status: "pending" }],
    evidence: [],
    blocked_reason: null,
  }
  state = applyAgentEvent(state, composeEvent(1, projection))
  expect(state.composeState).toEqual({
    revision: 2,
    stage: "plan",
    status: "waiting_user",
    stages: projection.stages,
    tasks: projection.tasks,
    evidence: projection.evidence,
    blockedReason: null,
  })
  expect(state.activity.kind).toBe("waiting-interaction")
  expect(state.composeState?.revision).toBe(2)
})

test("compose.state 迟到帧（revision 不递增）被拒绝", () => {
  let state = startRun(createInitialState(), run, "实现搜索")
  const base = {
    revision: 1,
    stage: "understand",
    status: "running",
    stages: [
      { id: "understand", status: "running", attempts: 1 },
      { id: "plan", status: "pending", attempts: 0 },
      { id: "build", status: "pending", attempts: 0 },
      { id: "verify", status: "pending", attempts: 0 },
      { id: "review", status: "pending", attempts: 0 },
    ],
    tasks: [],
    evidence: [],
    blocked_reason: null,
  }
  state = applyAgentEvent(state, composeEvent(1, base))
  const stale = applyAgentEvent(state, composeEvent(2, base))
  expect(stale.composeState?.revision).toBe(1)
})

test("畸形 compose.state 被拒绝且不改变状态", () => {
  const state = startRun(createInitialState(), run, "实现搜索")
  const malformed = applyAgentEvent(state, composeEvent(1, { revision: -1, stage: "deploy", status: "running", stages: [], tasks: [], evidence: [] }))
  expect(malformed.composeState).toBeNull()
  const nonObject = applyComposeState(state, "not-an-object")
  expect(nonObject.composeState).toBeNull()
})

test("终态清理 compose projection", () => {
  let state = startRun(createInitialState(), run, "实现搜索")
  state = applyAgentEvent(state, composeEvent(1, {
    revision: 1, stage: "understand", status: "running",
    stages: [
      { id: "understand", status: "running", attempts: 1 },
      { id: "plan", status: "pending", attempts: 0 },
      { id: "build", status: "pending", attempts: 0 },
      { id: "verify", status: "pending", attempts: 0 },
      { id: "review", status: "pending", attempts: 0 },
    ],
    tasks: [], evidence: [], blocked_reason: null,
  }))
  expect(state.composeState).not.toBeNull()
  state = applyAgentEvent(state, event("run.completed", 2, { duration_ms: 10, usage: { input_tokens: 1, output_tokens: 1 } }))
  expect(state.composeState).toBeNull()
})

test("restoreThread 保留会话级 workMode 并清空 compose projection", () => {
  const state = setWorkMode(createInitialState(), "compose")
  const restored = restoreThread("thread-2", [{ kind: "user", content: "历史" }], state.workMode)
  expect(restored.workMode).toBe("compose")
  expect(restored.composeState).toBeNull()
})
