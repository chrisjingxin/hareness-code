/** v3 事件和交互请求的共享 reducer 归约测试。 */

import { expect, test } from "bun:test"
import type { EventEnvelope, InteractionRequestEnvelope } from "@za38/protocol"
import { applyAgentEvent, applyComposeState, applyInteractionRequest, clearThread, createInitialState, finishContextCompaction, isHomeState, markInteractionTimeout, restoreThread, setWorkMode, startContextCompaction, startRun, type InteractiveState } from "../../src/interactive/state"

const run = { threadId: "thread-1", runId: "run-1" }

test("初始状态和清空后的状态进入首页", () => {
  const initial = createInitialState()
  expect(isHomeState(initial)).toBeTrue()
  expect(isHomeState(clearThread(startRun(initial, run, "生成组件")))).toBeTrue()
})

test("手动压缩使用独立 pending 状态并在终态恢复空闲", () => {
  const compacting = startContextCompaction(createInitialState("thread-compact"))
  expect(compacting.activeRun).toBeNull()
  expect(compacting.pendingOperation).toBe("context.compact")
  expect(compacting.activity.kind).toBe("compacting")

  const idle = finishContextCompaction(compacting)
  expect(idle.pendingOperation).toBeNull()
  expect(idle.activity.kind).toBe("idle")
})

test("恢复 thread 会原子替换时间线并清空旧运行状态", () => {
  const active = startRun(createInitialState(), run, "旧 thread 消息")
  const restored = restoreThread("restored-thread", [
    { kind: "user", content: "此前请求", createdAtMs: 1_706_000_000_000 },
    { kind: "assistant", content: "此前回答", createdAtMs: 1_706_000_001_000 },
    { kind: "tool", content: "此前工具结果", toolName: "execute" },
  ])
  expect(active.activeRun).toBeDefined()
  expect(restored.currentThreadId).toBe("restored-thread")
  expect(restored.activeRun).toBeNull()
  expect(restored.sequences).toEqual({})
  expect(restored.timeline.map(item => item.type)).toEqual(["message", "message", "tool"])
  expect(messages(restored).map(message => message.createdAtMs)).toEqual([1_706_000_000_000, 1_706_000_001_000])
})

test("恢复 compose activities 进入 Timeline 且不含 Reasoning 原始字段", () => {
  const restored = restoreThread(
    "thread-compose",
    [{ kind: "user", content: "实现搜索" }],
    "compose",
    [
      {
        runId: "run-9",
        eventSequence: 3,
        activityId: "act-u1",
        stage: "understand",
        attempt: 1,
        kind: "summary",
        label: "understand",
        status: "passed",
        createdAtMs: 10,
        boundedText: "目标：实现搜索",
        agentId: "understand",
        executionId: "child-1",
      },
      {
        runId: "run-9",
        eventSequence: 4,
        activityId: "act-v1",
        stage: "verify",
        attempt: 1,
        kind: "tool_terminal",
        label: "execute",
        status: "completed",
        createdAtMs: 11,
        boundedText: "3 passed",
      },
    ],
  )
  expect(restored.workMode).toBe("compose")
  expect(restored.timeline.map(item => item.type)).toEqual(["message", "compose-summary", "tool"])
  const summary = restored.timeline[1]
  if (summary?.type === "compose-summary") {
    expect(summary.summary.text).toBe("目标：实现搜索")
    expect(summary.summary.activityId).toBe("act-u1")
    expect(summary.summary.composeScope?.stage).toBe("understand")
  }
  const tool = restored.timeline[2]
  if (tool?.type === "tool") {
    expect(tool.tool.name).toBe("execute")
    expect(tool.tool.output).toBe("3 passed")
    expect(tool.tool.activityId).toBe("act-v1")
  }
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

test("task timeout 作为失败工具结果后仍归约为已完成 Run", () => {
  let state = startRun(createInitialState(), run, "委派超时任务")
  state = applyAgentEvent(state, event("tool.started", 1, { tool_call_id: "task-1", name: "task" }))
  state = applyAgentEvent(state, event("tool.delta", 2, {
    tool_call_id: "task-1",
    arguments_delta: JSON.stringify({ description: "执行超时任务", subagent_type: "general-purpose" }),
  }))
  state = applyAgentEvent(state, event("tool.completed", 3, {
    tool_call_id: "task-1",
    result: {
      content: "子代理未在执行时限内完成，已终止。\n\n- status: timed_out\n- error_code: DELEGATION_TIMEOUT",
      is_error: true,
    },
  }))
  state = applyAgentEvent(state, event("content.delta", 4, { text: "主 Agent 已继续处理" }))
  state = applyAgentEvent(state, event("run.completed", 5, { duration_ms: 320, usage: { input_tokens: 1, output_tokens: 2 } }))

  expect(tools(state)[0]).toMatchObject({
    name: "task",
    status: "failed",
    output: expect.stringContaining("DELEGATION_TIMEOUT"),
  })
  expect(messages(state).at(-1)?.content).toBe("主 Agent 已继续处理")
  expect(state.lastRun).toMatchObject({ outcome: "completed" })
  expect(state.activity.kind).toBe("completed")
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
  state = markInteractionTimeout(state, "request-1")
  expect(interactions(state)[0]).toMatchObject({ id: "request-1", status: "cancelled" })
  state = applyAgentEvent(state, event("interaction.resolved", 2, { request_id: "request-1", type: "approval" }))
  state = applyInteractionRequest(state, request("question", 3, { questions: [{ id: "question-1", header: "任务澄清", question: "差异输出应使用哪种格式？", body: "请选择默认输出格式。", options: [{ label: "text", value: "text" }, { label: "json", value: "json" }] }] }))
  expect(state.activity.kind).toBe("waiting-interaction")
  expect(interactions(state)[1]).toMatchObject({
    id: "request-3",
    type: "question",
    status: "pending",
    question: "差异输出应使用哪种格式？",
  })
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

test("同 tool call ID 跨 execution/activity 不会互相覆盖", () => {
  let state = startRun(createInitialState(), run, "compose 多 stage")
  const scopeA = { activity_id: "act-a", stage: "understand", attempt: 1 }
  const scopeB = { activity_id: "act-b", stage: "plan", attempt: 1 }
  state = applyAgentEvent(state, scopedEvent("tool.started", 1, { tool_call_id: "call-1", name: "read_file" }, {
    execution_id: "child-a",
    agent_id: "understand",
    compose_scope: scopeA,
  }))
  state = applyAgentEvent(state, scopedEvent("tool.completed", 2, {
    tool_call_id: "call-1",
    result: { content: "from-a", is_error: false },
  }, {
    execution_id: "child-a",
    agent_id: "understand",
    compose_scope: scopeA,
  }))
  state = applyAgentEvent(state, scopedEvent("tool.started", 3, { tool_call_id: "call-1", name: "read_file" }, {
    execution_id: "child-b",
    agent_id: "plan",
    compose_scope: scopeB,
  }))
  state = applyAgentEvent(state, scopedEvent("tool.completed", 4, {
    tool_call_id: "call-1",
    result: { content: "from-b", is_error: false },
  }, {
    execution_id: "child-b",
    agent_id: "plan",
    compose_scope: scopeB,
  }))
  expect(tools(state)).toHaveLength(2)
  expect(tools(state).map(tool => tool.output)).toEqual(["from-a", "from-b"])
  expect(tools(state)[0]).toMatchObject({
    id: "call-1",
    executionId: "child-a",
    activityId: "act-a",
    agentId: "understand",
  })
  expect(tools(state)[1]).toMatchObject({
    id: "call-1",
    executionId: "child-b",
    activityId: "act-b",
    agentId: "plan",
  })
})

test("非法 compose_scope 丢弃内容且不污染其他 activity", () => {
  let state = startRun(createInitialState(), run, "compose")
  const good = { activity_id: "act-good", stage: "understand", attempt: 1 }
  state = applyAgentEvent(state, scopedEvent("tool.started", 1, { tool_call_id: "call-1", name: "read_file" }, {
    execution_id: "child-good",
    compose_scope: good,
  }))
  // 非法 stage：sequence 前进但 tool 不写入
  state = applyAgentEvent(state, scopedEvent("tool.started", 2, { tool_call_id: "call-1", name: "execute" }, {
    execution_id: "child-bad",
    compose_scope: { activity_id: "act-bad", stage: "deploy", attempt: 1 },
  }))
  expect(tools(state)).toHaveLength(1)
  expect(tools(state)[0]).toMatchObject({ name: "read_file", activityId: "act-good" })
  // 后续合法帧仍可应用
  state = applyAgentEvent(state, scopedEvent("tool.completed", 3, {
    tool_call_id: "call-1",
    result: { content: "ok", is_error: false },
  }, {
    execution_id: "child-good",
    compose_scope: good,
  }))
  expect(tools(state)[0]?.status).toBe("completed")
})

test("compose.summary 进入非 assistant Timeline 摘要", () => {
  let state = startRun(createInitialState(), run, "compose")
  const scope = { activity_id: "act-u1", stage: "understand", attempt: 1 }
  state = applyAgentEvent(state, scopedEvent("compose.summary", 1, {
    status: "passed",
    text: "目标：实现搜索",
  }, {
    execution_id: "child-u",
    agent_id: "understand",
    compose_scope: scope,
  }))
  const summaries = state.timeline.filter(item => item.type === "compose-summary")
  expect(summaries).toHaveLength(1)
  if (summaries[0]?.type === "compose-summary") {
    expect(summaries[0].summary).toMatchObject({
      status: "passed",
      text: "目标：实现搜索",
      executionId: "child-u",
      activityId: "act-u1",
      agentId: "understand",
      composeScope: {
        activityId: "act-u1",
        stage: "understand",
        attempt: 1,
      },
    })
  }
})

test("scoped Interaction 保留 child provenance 与 activity", () => {
  let state = startRun(createInitialState(), run, "compose")
  state = applyInteractionRequest(state, {
    request_id: "int-1",
    type: "approval",
    thread_id: run.threadId,
    run_id: run.runId,
    timeout_ms: 1000,
    execution_id: "child-builder",
    agent_id: "build",
    compose_scope: { activity_id: "act-b1", stage: "build", attempt: 2, task_id: "t1" },
    payload: {
      interrupt_id: "int-1",
      description: "run tests",
      requests: { action_requests: [] },
      decisions: ["approve_once", "reject"],
    },
  } as InteractionRequestEnvelope)
  expect(interactions(state)[0]).toMatchObject({
    id: "int-1",
    type: "approval",
    status: "pending",
    executionId: "child-builder",
    activityId: "act-b1",
    agentId: "build",
    composeScope: { activityId: "act-b1", stage: "build", attempt: 2, taskId: "t1" },
  })
  state = applyAgentEvent(state, event("interaction.resolved", 1, { request_id: "int-1", type: "approval" }))
  expect(interactions(state)[0]?.status).toBe("resolved")
})

test("Build 无 scope 快照形状保持兼容", () => {
  let state = startRun(createInitialState(), run, "检查目录")
  state = applyAgentEvent(state, event("tool.started", 1, { tool_call_id: "call-1", name: "execute" }))
  state = applyAgentEvent(state, event("tool.completed", 2, {
    tool_call_id: "call-1",
    result: { content: "ok", is_error: false },
  }))
  expect(tools(state)[0]).toEqual({
    id: "call-1",
    runId: "run-1",
    name: "execute",
    arguments: "",
    output: "ok",
    status: "completed",
  })
})

test("run.completed 正确解析并保留 usage.cached_tokens 到 lastRun", () => {
  let state = startRun(createInitialState(), run, "执行任务")
  state = applyAgentEvent(state, event("run.completed", 1, {
    duration_ms: 1200,
    finish_reason: "completed",
    usage: {
      input_tokens: 1500,
      output_tokens: 200,
      cached_tokens: 1200,
    },
    context: { action: "run", estimated_tokens: 1500, input_cap_tokens: 128000 },
  }))
  expect(state.lastRun?.usage).toEqual({
    inputTokens: 1500,
    outputTokens: 200,
    cachedTokens: 1200,
  })
})

function event(type: string, sequence: number, payload: Record<string, unknown>): EventEnvelope {
  return { event_id: `event-${sequence}`, type, thread_id: run.threadId, run_id: run.runId, sequence, timestamp_ms: 1, payload } as EventEnvelope
}

function scopedEvent(
  type: string,
  sequence: number,
  payload: Record<string, unknown>,
  meta: {
    execution_id?: string
    agent_id?: string
    compose_scope?: Record<string, unknown>
  },
): EventEnvelope {
  return {
    ...event(type, sequence, payload),
    ...(meta.execution_id ? { execution_id: meta.execution_id } : {}),
    ...(meta.agent_id ? { agent_id: meta.agent_id } : {}),
    ...(meta.compose_scope ? { compose_scope: meta.compose_scope } : {}),
  } as EventEnvelope
}

function request(type: "approval" | "question", sequence: number, payload: Record<string, unknown>): InteractionRequestEnvelope {
  return { request_id: `request-${sequence}`, type, thread_id: run.threadId, run_id: run.runId, timeout_ms: 1000, payload } as InteractionRequestEnvelope
}

function messages(state: InteractiveState) { return state.timeline.flatMap(item => item.type === "message" ? [item.message] : []) }
function tools(state: InteractiveState) { return state.timeline.flatMap(item => item.type === "tool" ? [item.tool] : []) }
function interactions(state: InteractiveState) { return state.timeline.flatMap(item => item.type === "interaction" ? [item.interaction] : []) }


function composeEvent(sequence: number, payload: Record<string, unknown>) {
  return event("compose.progress", sequence, payload)
}

function grillProgress(revision: number, waiting: string = "ask_user"): Record<string, unknown> {
  return {
    thread_id: "thread-1",
    slug: "jsondiff",
    complexity: "simple",
    status: waiting === "none" ? "active" : "waiting_user",
    current_stage: "grill",
    waiting,
    stages: [
      { id: "requirement", state: "current" },
      { id: "spec", state: "pending" },
      { id: "plan", state: "pending" },
      { id: "implement", state: "pending" },
      { id: "review", state: "pending" },
    ],
    documents: [],
    fix_rounds: 0,
    revision,
  }
}

test("用户消息在 startRun 写入当时 workMode，切换会话 Mode 不回写旧消息", () => {
  let state = startRun(createInitialState(), run, "先 Build")
  const first = messages(state)[0]
  expect(first).toMatchObject({ role: "user", content: "先 Build", workMode: "build" })

  state = setWorkMode({ ...state, activeRun: null, activity: { kind: "idle" } }, "compose")
  expect(messages(state)[0]?.workMode).toBe("build")

  state = startRun(state, { threadId: "thread-1", runId: "run-2" }, "再 Compose")
  expect(messages(state).map(item => item.workMode)).toEqual(["build", "compose"])
})

test("run.started 只给尚未带 Mode 的本 Run 用户消息补 mode", () => {
  let state = startRun(createInitialState(), run, "补 Mode")
  const timeline = state.timeline.map(item => {
    if (item.type !== "message" || item.message.role !== "user") return item
    const { workMode: _ignored, ...message } = item.message
    return { type: "message" as const, message }
  })
  state = applyAgentEvent({ ...state, timeline }, event("run.started", 1, { mode: "compose", resumed: false }))
  expect(messages(state)[0]?.workMode).toBe("compose")
})

test("恢复 Thread 时缺 workMode 的用户消息用 threadMode，否则 build；缺字段不抛错", () => {
  const withoutMode = restoreThread("restored-thread", [
    { kind: "user", content: "历史请求" },
    { kind: "assistant", content: "历史回答" },
  ])
  expect(messages(withoutMode)[0]).toMatchObject({ role: "user", content: "历史请求", workMode: "build" })
  expect(messages(withoutMode)[1]?.workMode).toBeUndefined()

  const composeLocked = restoreThread(
    "compose-thread",
    [{ kind: "user", content: "Compose 历史" }],
    "build",
    [],
    "compose",
  )
  expect(messages(composeLocked)[0]?.workMode).toBe("compose")
})

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

test("compose.progress 折叠为 projection，waiting 进入等待交互", () => {
  let state = startRun(createInitialState(), run, "实现搜索")
  const projection = grillProgress(2)
  state = applyAgentEvent(state, composeEvent(1, projection))
  expect(state.composeState?.currentStage).toBe("grill")
  expect(state.composeState?.slug).toBe("jsondiff")
  expect(state.activity.kind).toBe("waiting-interaction")
  expect(state.composeState?.revision).toBe(2)
})

test("compose.progress 迟到帧（revision 不递增）被拒绝", () => {
  let state = startRun(createInitialState(), run, "实现搜索")
  const base = grillProgress(1)
  state = applyAgentEvent(state, composeEvent(1, base))
  const stale = applyAgentEvent(state, composeEvent(2, base))
  expect(stale.composeState?.revision).toBe(1)
})

test("畸形 compose.progress 被拒绝且不改变状态", () => {
  const state = startRun(createInitialState(), run, "实现搜索")
  const malformed = applyAgentEvent(state, composeEvent(1, { revision: -1, current_stage: "deploy" }))
  expect(malformed.composeState).toBeNull()
  const nonObject = applyComposeState(state, "not-an-object")
  expect(nonObject.composeState).toBeNull()
})

test("终态清理 compose projection", () => {
  let state = startRun(createInitialState(), run, "实现搜索")
  state = applyAgentEvent(state, composeEvent(1, grillProgress(1)))
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
