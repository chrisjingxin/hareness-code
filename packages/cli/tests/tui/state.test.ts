import { expect, test } from "bun:test"

import { applyAgentEvent, clearThread, createInitialState, isHomeState, startRun, type TuiState } from "../../src/tui/state"

test("初始状态和清空后的状态进入首页，首次发送后进入会话", () => {
  const initial = createInitialState()
  expect(initial.timeline).toEqual([])
  expect(isHomeState(initial)).toBeTrue()

  const active = startRun(initial, { threadId: "thread-1", runId: "run-1" }, "生成组件")
  expect(isHomeState(active)).toBeFalse()
  expect(isHomeState(clearThread(active))).toBeTrue()
})

test("流式事件按 sequence 更新消息和工具卡片", () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 1, text: "正在" })
  state = applyAgentEvent(state, "tool/started", { ...snakeCase(run), sequence: 2, tool_id: "tool-1", tool_name: "read_file" })
  state = applyAgentEvent(state, "tool/completed", { ...snakeCase(run), sequence: 3, tool_id: "tool-1", result: "src/app.ts", error: false })
  state = applyAgentEvent(state, "run/completed", {
    ...snakeCase(run),
    sequence: 4,
    duration_ms: 1340,
    usage: { input_tokens: 1200, output_tokens: 35 },
  })

  expect(state.timeline.map(item => item.type)).toEqual(["message", "message", "tool"])
  expect(messages(state).at(-1)).toMatchObject({ role: "assistant", content: "正在", streaming: false })
  expect(tools(state)).toEqual([{ id: "tool-1", runId: "run-1", name: "read_file", detail: "src/app.ts", status: "completed" }])
  expect(state.lastRun).toEqual({
    runId: "run-1",
    outcome: "completed",
    durationMs: 1340,
    usage: { inputTokens: 1200, outputTokens: 35 },
  })
  expect(state.activeRun).toBeUndefined()
})

test("审批事件保留工具请求，供界面显示风险摘要", () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "修改文件")
  const requests = { action_requests: [{ name: "write_file", args: { file_path: "src/a.ts" } }] }
  state = applyAgentEvent(state, "approval/requested", {
    ...snakeCase(run),
    sequence: 1,
    interrupt_id: "approval-1",
    description: "写入源文件",
    requests,
  })

  expect(state.pendingApproval).toEqual({
    interruptId: "approval-1",
    description: "写入源文件",
    requests,
  })
})

test("忽略旧 run 与乱序事件，避免过期流污染当前会话", () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 2, text: "新内容" })
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 1, text: "旧内容" })
  state = applyAgentEvent(state, "message/delta", { thread_id: "thread-1", run_id: "run-old", sequence: 99, text: "过期内容" })

  expect(messages(state).at(-1)).toMatchObject({ content: "新内容" })
})

test("ask_user 完整问题组优先使用首题和 choices 渲染选择控件", () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "开始")
  state = applyAgentEvent(state, "question/requested", {
    ...snakeCase(run),
    sequence: 1,
    interrupt_id: "ask-1",
    questions: [{ question: "选择目录", choices: [{ value: "src" }, { value: "tests" }] }],
  })

  expect(state.pendingQuestion).toEqual({
    interruptId: "ask-1",
    question: "选择目录",
    options: [{ name: "src", value: "src" }, { name: "tests", value: "tests" }],
  })
})

test("工具完成后的文本继续按事件顺序插入时间线", () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "读取文件后总结")
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 1, text: "我先读取文件。" })
  state = applyAgentEvent(state, "tool/started", { ...snakeCase(run), sequence: 2, tool_id: "tool-1", tool_name: "read_file" })
  state = applyAgentEvent(state, "tool/completed", { ...snakeCase(run), sequence: 3, tool_id: "tool-1", result: "src/app.ts", error: false })
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 4, text: "读取完成。" })

  expect(state.timeline.map(item => item.type)).toEqual(["message", "message", "tool", "message"])
  expect(messages(state).map(message => message.content)).toEqual(["读取文件后总结", "我先读取文件。", "读取完成。"])
  expect(tools(state)).toHaveLength(1)
})

function messages(state: TuiState) {
  return state.timeline.flatMap(item => item.type === "message" ? [item.message] : [])
}

function tools(state: TuiState) {
  return state.timeline.flatMap(item => item.type === "tool" ? [item.tool] : [])
}

function snakeCase(run: { threadId: string; runId: string }): { thread_id: string; run_id: string } {
  return { thread_id: run.threadId, run_id: run.runId }
}
