import { expect, test } from "bun:test"
import type { ScrollBoxRenderable, TextareaRenderable } from "@opentui/core"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement, createRef } from "react"

import { HomeView, SessionView } from "../../src/tui/components"
import type { TuiRuntime } from "../../src/tui/model"
import { registerCommonSyntaxParsers } from "../../src/tui/syntax-parsers"
import { createInitialState, startRun, type TuiState } from "../../src/tui/state"

const runtime: TuiRuntime = {
  workspace: "/workspace/harness-code",
  gitBranch: "main",
  cliVersion: "0.1.0",
  modelName: "enterprise-model",
  modelConfigured: true,
  executionMode: "local",
  approvalMode: "default",
}

test("紧凑首页保留品牌、输入框和真实底栏信息", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(HomeView, viewProps(createInitialState(), 80, 24)),
      { width: 80, height: 24 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("HARNESS CODE")
    expect(frame).toContain("powered by za38")
    expect(frame).toContain("harness-code")
    expect(frame).toContain("v0.1.0")
    expect(frame).toContain("default")
    expect(frame).not.toContain("未隔离")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("首页模型靠左、审批模式靠右，且不重复显示品牌", async () => {
  const longModelRuntime: TuiRuntime = {
    ...runtime,
    modelName: "deepseek-v4-flash",
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(HomeView, { ...viewProps(createInitialState(), 130, 40), runtime: longModelRuntime }),
      { width: 130, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const lines = setup.captureCharFrame().split("\n")
    const runtimeLine = lines.find(line => line.includes("deepseek-v4-flash"))

    expect(runtimeLine).toContain("default")
    expect(runtimeLine).not.toContain("Harness Code")
    expect(runtimeLine).not.toContain("本机执行")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("会话渲染显示工具卡片和底部 composer", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "读取文件")
  state = {
    ...state,
    timeline: [
      state.timeline[0]!,
      { type: "tool", tool: { id: "tool-1", runId: run.runId, name: "read_file", detail: "src/app.ts", status: "completed" } },
    ],
    activeRun: undefined,
    status: "已完成",
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(SessionView, viewProps(state, 130, 40)),
      { width: 130, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("read_file")
    expect(frame).toContain("Harness Code")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("会话通过原生 Markdown renderer 隐藏标题和代码围栏标记", async () => {
  registerCommonSyntaxParsers()
  const run = { threadId: "thread-markdown", runId: "run-markdown" }
  const started = startRun(createInitialState(), run, "展示 Markdown")
  const state: TuiState = {
    ...started,
    activeRun: undefined,
    status: "已完成",
    timeline: [
      started.timeline[0]!,
      {
        type: "message",
        message: {
          id: "assistant-markdown",
          role: "assistant",
          content: "## 示例标题\n\n- **重点内容**\n\n```java\npublic class Demo {}\n```",
          runId: run.runId,
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(SessionView, viewProps(state, 100, 28)), { width: 100, height: 28 })
  })
  try {
    // Markdown 的 Tree-sitter 高亮在异步 worker 返回后提交一帧；不能只检查初始占位帧。
    await act(async () => {
      await Bun.sleep(150)
      await setup.flush()
    })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("示例标题")
    expect(frame).toContain("重点内容")
    expect(frame).toContain("public class Demo {}")
    expect(frame).not.toContain("## 示例标题")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("审批 dock 保留选项高度并隐藏重复 Thinking", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "写入文件")
  const state: TuiState = {
    ...started,
    status: "等待工具审批",
    pendingApproval: {
      requestId: "approval-1",
      description: "执行 shell 命令",
      requests: { action_requests: [{ name: "execute", args: { command: "pwd" } }] },
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(SessionView, viewProps(state, 100, 28)), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("需要审批")
    expect(frame).toContain("允许一次")
    expect(frame).toContain("拒绝")
    expect(frame).not.toContain("Thinking")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

function viewProps(state: TuiState, terminalWidth: number, terminalHeight: number) {
  return {
    runtime,
    state,
    terminalWidth,
    terminalHeight,
    inputRef: createRef<TextareaRenderable>(),
    conversationScrollRef: createRef<ScrollBoxRenderable>(),
    value: "",
    onInput: () => undefined,
    onComposerKeyDown: () => undefined,
    onSubmit: () => undefined,
    commandMenu: { visible: false, selectedIndex: 0 },
    onSelectCommand: () => undefined,
    onHoverCommand: () => undefined,
    showToolDetails: false,
    expandedTools: new Set<string>(),
    onToggleTool: () => undefined,
    onApproval: () => undefined,
    onQuestion: () => undefined,
  }
}
