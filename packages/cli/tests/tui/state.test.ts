import { expect, test } from "bun:test"

import { applyAgentEvent, createInitialState, startRun } from "../../src/tui/state"

test("流式事件按 sequence 更新消息和工具卡片", () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 1, text: "正在" })
  state = applyAgentEvent(state, "tool/started", { ...snakeCase(run), sequence: 2, tool_id: "tool-1", tool_name: "read_file" })
  state = applyAgentEvent(state, "tool/completed", { ...snakeCase(run), sequence: 3, tool_id: "tool-1", result: "src/app.ts", error: false })
  state = applyAgentEvent(state, "run/completed", { ...snakeCase(run), sequence: 4 })

  expect(state.messages.at(-1)).toMatchObject({ role: "assistant", content: "正在", streaming: false })
  expect(state.tools).toEqual([{ id: "tool-1", name: "read_file", detail: "src/app.ts", status: "completed" }])
  expect(state.activeRun).toBeUndefined()
})

test("忽略旧 run 与乱序事件，避免过期流污染当前会话", () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "生成组件")
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 2, text: "新内容" })
  state = applyAgentEvent(state, "message/delta", { ...snakeCase(run), sequence: 1, text: "旧内容" })
  state = applyAgentEvent(state, "message/delta", { thread_id: "thread-1", run_id: "run-old", sequence: 99, text: "过期内容" })

  expect(state.messages.at(-1)).toMatchObject({ content: "新内容" })
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

function snakeCase(run: { threadId: string; runId: string }): { thread_id: string; run_id: string } {
  return { thread_id: run.threadId, run_id: run.runId }
}
