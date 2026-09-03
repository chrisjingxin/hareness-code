import { expect, test } from "bun:test"
import { RGBA, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement, createRef } from "react"

import type { InteractiveSnapshot } from "../../../src/interactive/types"
import { createInteractiveRuntime } from "../../../src/interactive/runtime"
import { createInitialState, restoreThread, setWorkMode, startContextCompaction, startRun, type InteractiveState } from "../../../src/interactive/state"
import { scopeTimeline } from "../../../src/presentation-shared/timeline-scope"
import { registerCommonSyntaxParsers } from "../../../src/tui/platform/syntax-parsers"
import { HomeView } from "../../../src/tui/presentation/home"
import { SkillPicker, ThreadPicker } from "../../../src/tui/presentation/pickers"
import { tuiTheme, userMessageAccent } from "../../../src/tui/presentation/theme"
import { ThreadView } from "../../../src/tui/presentation/thread"
import { tuiDiffViewForWidth } from "../../../src/tui/presentation/timeline"

const runtime = createInteractiveRuntime({
  protocol: { major: 3, minor: 0 },
  server: { name: "za38-agent", version: "0.1.0" },
  connection: { id: "test", role: "owner", project: { id: "project", label: "za38-cli" } },
  capabilities: { available: [], enabled: [], handles: [] },
  agent_commands: [],
  skills_snapshot: { id: "snapshot", count: 0 },
  skill_diagnostics: [],
  limits: { max_frame_bytes: 8388608, max_tool_payload_bytes: 1048576 },
  config_summary: {
    workspace: "/workspace/harness-code",
    model: { name: "enterprise-model", api_key_configured: true },
    security: { approval_mode: "default" },
  },
  startup_error: null,
}, "/workspace/harness-code", { gitWorkspace: { kind: "branch", branch: "main", root: "/workspace/harness-code" }, cliVersion: "0.1.0" })

function snapshotOf(state: InteractiveState): InteractiveSnapshot {
  return {
    currentThreadId: state.currentThreadId,
    activity: state.activity,
    activeRun: state.activeRun,
    timeline: state.timeline,
    runProgress: state.runProgress,
    interaction: null,
    confirmation: null,
    lastRun: state.lastRun ?? null,
    runtime,
    connection: { status: "open" },
    commands: [],
    catalogs: {
      threads: { status: "idle", items: [] },
      models: { status: "idle", items: [] },
      skills: { status: "idle", items: [] },
      mcp: { status: "idle", items: [] },
      agents: { status: "idle", items: [] },
    },
    workMode: state.workMode,
    composeState: state.composeState,
    workItem: state.workItem ?? null,
    threadMode: state.threadMode ?? null,
    childTimelineExecutionId: state.childTimelineExecutionId ?? null,
    selection: {
      requestedModelProfileId: null,
      actualModel: null,
      armedSkill: null,
    },
  }
}

test("紧凑首页保留品牌、输入框和真实底栏信息", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(HomeView, viewProps(snapshotOf(createInitialState()), 80, 24)),
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
    expect(frame).toContain("Build")
    expect(frame).toContain("tab modes")
    expect(frame).not.toContain("未隔离")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("首页 Compose 模式输入栏显示 Compose 文案，Logo 仍是品牌字标", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(HomeView, viewProps(snapshotOf(setWorkMode(createInitialState(), "compose")), 80, 24)),
      { width: 80, height: 24 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("HARNESS CODE")
    expect(frame).toContain("Compose")
    expect(frame).not.toContain("Build ·")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("TUI 模型显示跟随 /model 选择，不读陈旧的握手 runtime.modelName", async () => {
  const base = snapshotOf(createInitialState())
  const interactive = {
    ...base,
    catalogs: {
      ...base.catalogs,
      models: { status: "ready" as const, items: [
        { id: "fast", model: "fast-model", provider_label: "fast", context_window_tokens: 128000, capabilities: [], is_default: true, available: true, source: "user" },
        { id: "pro", model: "pro-model", provider_label: "pro", context_window_tokens: 256000, capabilities: [], is_default: false, available: true, source: "user" },
      ] },
    },
    selection: { ...base.selection, requestedModelProfileId: "fast" },
    // 握手 runtime 仍是陈旧的 pro 模型（会话中途握手不会更新）。
    runtime: { ...runtime, modelName: "pro-model", modelProfileId: undefined },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(HomeView, viewProps(interactive, 80, 24)),
      { width: 80, height: 24 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    // 选择 fast 后必须显示 fast 的模型，而不是握手时缓存的 pro 模型。
    expect(frame).toContain("fast-model")
    expect(frame).not.toContain("pro-model")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("首页模型靠左、模式提示靠右，且不重复显示品牌", async () => {
  const longModelRuntime = {
    ...runtime,
    modelName: "deepseek-v4-flash",
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(HomeView, { ...viewProps(snapshotOf(createInitialState()), 130, 40), interactive: { ...snapshotOf(createInitialState()), runtime: longModelRuntime } }),
      { width: 130, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const lines = setup.captureCharFrame().split("\n")
    const runtimeLine = lines.find(line => line.includes("deepseek-v4-flash"))

    expect(runtimeLine).toContain("default")
    expect(runtimeLine).toContain("shift+enter new line")
    expect(runtimeLine).toContain("tab modes")
    expect(runtimeLine).not.toContain("Harness Code")
    expect(runtimeLine).not.toContain("本机执行")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("用户消息用短竖条渲染，Build 条在会话切到 Compose 后仍显示 ▌", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "帮我重构输入栏")
  state = setWorkMode({ ...state, activeRun: null, activity: { kind: "idle" } }, "compose")
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)),
      { width: 130, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("▌")
    expect(frame).toContain("帮我重构输入栏")
    expect(userMessageAccent(state.timeline[0] && state.timeline[0].type === "message" ? state.timeline[0].message.workMode : undefined)).toBe("#EAB308")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("thread 渲染显示工具卡片和底部 composer", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "读取文件")
  state = {
    ...state,
    timeline: [
      state.timeline[0]!,
      { type: "tool", tool: { id: "tool-1", runId: run.runId, name: "read_file", arguments: "{\"file_path\":\"src/app.ts\"}", output: "src/app.ts", status: "completed" } },
    ],
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)),
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

test("write_file 进行中未拼完参数时只显示 Preparing write，不出现 JSON", async () => {
  const run = { threadId: "thread-write", runId: "run-write" }
  const started = startRun(createInitialState(), run, "写示例")
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-write",
          runId: run.runId,
          name: "write_file",
          arguments: "{\"file_path\":\"",
          output: "",
          status: "running",
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)), { width: 130, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Preparing write")
    expect(frame).not.toContain("file_path")
    expect(frame).not.toContain("{\"")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("write_file 展示路径和高亮正文，不把转义 JSON 参数铺开", async () => {
  const run = { threadId: "thread-write", runId: "run-write" }
  const started = startRun(createInitialState(), run, "写示例")
  const content = "print('hello')\nprint('world')\n"
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-write",
          runId: run.runId,
          name: "write_file",
          arguments: JSON.stringify({ file_path: "/examples/python/jsondiff_usage.py", content }),
          output: "",
          status: "completed",
        },
      },
    ],
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)), { width: 130, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("jsondiff_usage.py")
    expect(frame).toContain("print('hello')")
    expect(frame).not.toContain("file_path")
    expect(frame).not.toContain("\\n")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("write_file 默认只显示前 12 行并提示还可展开", async () => {
  const run = { threadId: "thread-write", runId: "run-write" }
  const started = startRun(createInitialState(), run, "写长文件")
  const content = Array.from({ length: 30 }, (_, index) => `line_${index + 1}`).join("\n")
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-write",
          runId: run.runId,
          name: "write_file",
          arguments: JSON.stringify({ file_path: "src/long.py", content }),
          output: "",
          status: "completed",
        },
      },
    ],
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)), { width: 130, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("line_1")
    expect(frame).toContain("展开")
    expect(frame).toContain("还有 18 行")
    expect(frame).not.toContain("line_30")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("edit_file 用 old/new 显示高亮 Diff，不铺 JSON 参数或结果", async () => {
  const run = { threadId: "thread-edit", runId: "run-edit" }
  const started = startRun(createInitialState(), run, "改分类")
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-edit",
          runId: run.runId,
          name: "edit_file",
          arguments: JSON.stringify({
            file_path: "/file-organizer.py",
            snapshot_id: "snap-1",
            old_string: "    '.pptx': 'documents',",
            new_string: "    '.pptx': 'media',",
          }),
          output: JSON.stringify({ ok: true, path: "/file-organizer.py", replaced: 1 }),
          status: "completed",
        },
      },
    ],
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)), { width: 130, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("file-organizer.py")
    expect(frame).toContain("documents")
    expect(frame).toContain("media")
    expect(frame).not.toContain("snapshot_id")
    expect(frame).not.toContain("old_string")
    expect(frame).not.toContain("\"ok\":true")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("恢复 Thread 的 edit_file 不显示 Preparing，用结果窗口高亮而不是 JSON", async () => {
  const state = restoreThread("restored-thread", [
    { kind: "user", content: "改分类" },
    {
      kind: "tool",
      toolName: "edit_file",
      content: JSON.stringify({
        ok: true,
        path: "/file-organizer.py",
        snapshot_id: "snap-1",
        content: "    '.csv': 'documents',\n",
      }),
    },
  ])
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)), { width: 130, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("file-organizer.py")
    expect(frame).toContain("documents")
    expect(frame).not.toContain("Preparing edit")
    expect(frame).not.toContain("snapshot_id")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("write_todos 显示清单而不是 JSON，运行中底部跟踪当前进度", async () => {
  const run = { threadId: "thread-todo", runId: "run-todo" }
  const started = startRun(createInitialState(), run, "做 jsondiff")
  const argumentsText = JSON.stringify({
    todos: [
      { content: "实现核心 diff 模块 jsondiff/diff.py", status: "in_progress" },
      { content: "实现 CLI jsondiff/cli.py", status: "pending" },
      { content: "编写 pytest 测试", status: "pending" },
    ],
  })
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-todos",
          runId: run.runId,
          name: "write_todos",
          arguments: argumentsText,
          output: "Updated todo list to [{'content': '实现核心 diff 模块 jsondiff/diff.py', 'status': 'in_progress'}]",
          status: "completed",
        },
      },
      {
        type: "tool",
        tool: {
          id: "t-read",
          runId: run.runId,
          name: "read_file",
          arguments: "{\"file_path\":\"README.md\"}",
          output: "ok",
          status: "running",
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 120, 36)), { width: 120, height: 36 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("实现核心 diff 模块")
    expect(frame).toContain("实现 CLI")
    expect(frame).toContain("TODO")
    expect(frame).not.toContain("\"todos\"")
    expect(frame).not.toContain("in_progress")
    expect(frame.split("实现 CLI").length - 1).toBe(1)
    const panelAt = frame.lastIndexOf("TODO")
    const readAt = frame.indexOf("read_file")
    expect(panelAt).toBeGreaterThan(readAt)
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("task 派出显示角色和任务，不铺 JSON", async () => {
  const run = { threadId: "thread-task", runId: "run-task" }
  const started = startRun(createInitialState(), run, "查压缩实现")
  const argumentsText = JSON.stringify({
    description: "查找 harness-code 项目中与代码上下文压缩相关的实现",
    subagent_type: "general-purpose",
  })
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-task",
          runId: run.runId,
          name: "task",
          arguments: argumentsText,
          output: "",
          status: "running",
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 120, 36)), { width: 120, height: 36 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("派出 general-purpose")
    expect(frame).toContain("任务")
    expect(frame).toContain("查找 harness-code 项目中与代码上下文压缩相关的实现")
    expect(frame).not.toContain("结论")
    expect(frame).not.toContain("subagent_type")
    expect(frame).not.toContain("\"description\"")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("task 结论按 Markdown 渲染，任务与结论分区", async () => {
  registerCommonSyntaxParsers()
  const run = { threadId: "thread-task-md", runId: "run-task-md" }
  const started = startRun(createInitialState(), run, "验证写入")
  const argumentsText = JSON.stringify({
    description: "在沙箱执行一条可写 shell 命令",
    subagent_type: "general-purpose",
  })
  const state: InteractiveState = {
    ...started,
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-task",
          runId: run.runId,
          name: "task",
          arguments: argumentsText,
          output: "- **命令是否成功**: 未执行成功（被拒绝）\n- **exit code**: 无",
          status: "completed",
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 120, 40)), { width: 120, height: 40 })
  })
  try {
    let frame = setup.captureCharFrame()
    for (let attempt = 0; attempt < 20; attempt += 1) {
      if (frame.includes("命令是否成功") && !frame.includes("**命令是否成功**")) break
      await act(async () => {
        await Bun.sleep(25)
        await setup.flush()
      })
      frame = setup.captureCharFrame()
    }
    expect(frame).toContain("派出 general-purpose")
    expect(frame).toContain("任务")
    expect(frame).toContain("在沙箱执行一条可写 shell 命令")
    expect(frame).toContain("结论")
    expect(frame).toContain("命令是否成功")
    expect(frame).toContain("未执行成功")
    expect(frame).not.toContain("**命令是否成功**")
    expect(frame).not.toContain("subagent_type")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("task 长结论默认折叠，点击展开后显示剩余行", async () => {
  registerCommonSyntaxParsers()
  const run = { threadId: "thread-task-expand", runId: "run-task-expand" }
  const started = startRun(createInitialState(), run, "看长报告")
  const output = Array.from({ length: 17 }, (_, index) => `RESULT-${String(index + 1).padStart(2, "0")}`).join("\n")
  const state: InteractiveState = {
    ...started,
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-task",
          runId: run.runId,
          name: "task",
          arguments: JSON.stringify({
            description: "验证结论折叠",
            subagent_type: "general-purpose",
          }),
          output,
          status: "completed",
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 120, 40)), { width: 120, height: 40, useMouse: true })
  })
  try {
    let frame = setup.captureCharFrame()
    for (let attempt = 0; attempt < 20; attempt += 1) {
      if (frame.includes("RESULT-01") && frame.includes("还有 5 行")) break
      await act(async () => {
        await Bun.sleep(25)
        await setup.flush()
      })
      frame = setup.captureCharFrame()
    }
    expect(frame).toContain("结论")
    expect(frame).toContain("展开")
    expect(frame).toContain("还有 5 行")
    expect(frame).toContain("RESULT-01")
    expect(frame).not.toContain("RESULT-17")

    const lines = frame.split("\n")
    const buttonY = lines.findIndex(line => line.includes("还有 5 行"))
    const spans = buttonY < 0 ? [] : setup.captureSpans().lines[buttonY]!.spans
    const buttonSpanIndex = spans.findIndex(span => span.text.includes("还有 5 行"))
    const buttonX = spans.slice(0, buttonSpanIndex).reduce((offset, span) => offset + span.width, 0)
    if (buttonSpanIndex < 0 || buttonY < 0) throw new Error("未找到结论剩余行提示")

    await act(async () => {
      await setup.mockMouse.click(buttonX, buttonY)
      await setup.flush()
    })
    frame = setup.captureCharFrame()
    for (let attempt = 0; attempt < 20; attempt += 1) {
      if (frame.includes("RESULT-17") && frame.includes("收起")) break
      await act(async () => {
        await Bun.sleep(25)
        await setup.flush()
      })
      frame = setup.captureCharFrame()
    }
    expect(frame).toContain("收起")
    expect(frame).toContain("RESULT-17")
    expect(frame).not.toContain("还有 5 行")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("四种工具分流：读文件一行、命令有界块、可解析 diff、未知工具仍能画", async () => {
  const run = { threadId: "thread-tools", runId: "run-tools" }
  const started = startRun(createInitialState(), run, "改代码")
  const longOutput = Array.from({ length: 20 }, (_, index) => `OUT${index + 1}`).join("\n")
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      { type: "tool", tool: { id: "t-read", runId: run.runId, name: "read_file", arguments: "{\"file_path\":\"src/app.ts\"}", output: "ok", status: "completed" } },
      { type: "tool", tool: { id: "t-exec", runId: run.runId, name: "execute", arguments: "{\"command\":\"bun test\"}", output: longOutput, status: "completed" } },
      { type: "tool", tool: { id: "t-edit", runId: run.runId, name: "edit_file", arguments: "{\"file_path\":\"src/app.ts\"}", output: "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new\n", status: "completed" } },
      { type: "tool", tool: { id: "t-task", runId: run.runId, name: "task", arguments: "{\"prompt\":\"explore\"}", output: "done", status: "completed" } },
    ],
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)), { width: 130, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("read_file")
    expect(frame).toContain("src/app.ts")
    expect(frame).toContain("execute")
    expect(frame).toContain("OUT1")
    expect(frame).toContain("还有 8 行")
    expect(frame).not.toContain("OUT20")
    expect(frame).toContain("edit_file")
    expect(frame).toContain("派出")
    expect(frame).not.toContain("Unsupported")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("手动压缩期间 composer 失焦并显示专用等待状态", async () => {
  const state = startContextCompaction({
    ...createInitialState("thread-compact"),
    timeline: [{ type: "message", message: { id: "user-1", role: "user", content: "需要压缩的历史" } }],
  })
  const inputRef = createRef<TextareaRenderable>()
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, { ...viewProps(snapshotOf(state), 130, 40), inputRef }), { width: 130, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("正在压缩上下文")
    expect(frame).toContain("上下文压缩中 · 请稍候")
    expect(inputRef.current?.focused).toBeFalse()
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("thread 通过原生 Markdown renderer 隐藏标题和代码围栏标记", async () => {
  registerCommonSyntaxParsers()
  const run = { threadId: "thread-markdown", runId: "run-markdown" }
  const started = startRun(createInitialState(), run, "展示 Markdown")
  const state: InteractiveState = {
    ...started,
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
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
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 28)), { width: 100, height: 28 })
  })
  try {
    // Markdown 的 Tree-sitter 高亮由 renderer 调度器之外的异步 worker 返回，需带墙钟
    // 间隔轮询完整帧；一旦内容齐全立即结束，最迟等待 500ms。
    let frame = setup.captureCharFrame()
    for (let attempt = 0; attempt < 20; attempt += 1) {
      if (
        frame.includes("示例标题")
        && frame.includes("重点内容")
        && frame.includes("public class Demo {}")
      ) break
      await act(async () => {
        await Bun.sleep(25)
        await setup.flush()
      })
      frame = setup.captureCharFrame()
    }
    expect(frame).toContain("示例标题")
    expect(frame).toContain("重点内容")
    expect(frame).toContain("public class Demo {}")
    expect(frame).not.toContain("## 示例标题")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("审批 pending 时底部是 Dock，输入栏失焦且时间线没有审批选择器", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "写入文件")
  const state: InteractiveState = {
    ...started,
    activity: { kind: "waiting-interaction", label: "等待工具审批" },
    timeline: [
      started.timeline[0]!,
      {
        type: "interaction",
        interaction: {
          id: "approval-1",
          runId: run.runId,
          type: "approval",
          status: "pending",
          description: "执行 shell 命令",
        },
      },
    ],
  }
  const snapshot = {
    ...snapshotOf(state),
    interaction: {
      type: "approval" as const,
      requestId: "approval-1",
      description: "执行 shell 命令",
      requests: {},
      presentation: null,
      decisions: ["approve_once" as const, "reject" as const],
      deadlineAtMs: Date.now() + 5_000,
    },
  }
  const inputRef = createRef<TextareaRenderable>()
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, { ...viewProps(snapshot, 100, 28), inputRef }), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("需要审批")
    expect(frame).toContain("允许一次")
    expect(frame).not.toContain("输入消息")
    expect(frame).not.toContain("等待中")
    expect(inputRef.current?.focused ?? false).toBeFalse()
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("审批决定后 Dock 消失、结果行存在、焦点回输入栏", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "写入文件")
  const state: InteractiveState = {
    ...started,
    activeRun: null,
    activity: { kind: "idle", label: "就绪" },
    timeline: [
      started.timeline[0]!,
      {
        type: "interaction",
        interaction: {
          id: "approval-1",
          runId: run.runId,
          type: "approval",
          status: "approved",
          description: "执行 shell 命令",
        },
      },
    ],
  }
  const inputRef = createRef<TextareaRenderable>()
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, { ...viewProps(snapshotOf(state), 100, 28), inputRef }), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).not.toContain("需要审批")
    expect(frame).not.toContain("允许一次")
    expect(frame).toContain("已允许")
    expect(frame).toContain("输入消息")
    expect(inputRef.current?.focused ?? false).toBeTrue()
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("计划审阅 Dock 展示原始行号批注入口，批准与打回共享批注", async () => {
  const run = { threadId: "thread-plan", runId: "run-plan" }
  const started = startRun(createInitialState(), run, "规划停点 4")
  const snapshot = {
    ...snapshotOf(started),
    interaction: {
      type: "plan" as const,
      requestId: "plan-1",
      revision: 1,
      hasPlan: true,
      planMarkdown: "# 方案\n保留协议\n替换界面\n补充测试",
      planVirtualPath: "/.harness/plan.md",
      planDisplayPath: "~/.harness/plans/thread-plan.md",
      decisions: ["approved" as const, "revise" as const, "abandoned" as const],
      deadlineAtMs: Date.now() + 5_000,
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshot, 100, 32)), { width: 100, height: 32 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("审阅计划")
    expect(frame).toContain("添加批注")
    expect(frame).toContain("批准并开始实现")
    expect(frame).toContain("继续打磨")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("带选项的问答 pending 时底部是 QuestionDock，输入栏不出现", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "选格式")
  const state: InteractiveState = {
    ...started,
    activity: { kind: "waiting-interaction", label: "等待回答" },
    timeline: [
      started.timeline[0]!,
      {
        type: "interaction",
        interaction: {
          id: "question-1",
          runId: run.runId,
          type: "question",
          status: "pending",
          description: "选一个输出格式",
        },
      },
    ],
  }
  const snapshot = {
    ...snapshotOf(state),
    interaction: {
      type: "question" as const,
      requestId: "question-1",
      questions: [{
        id: "fmt",
        question: "选一个输出格式",
        header: "",
        body: "",
        options: [
          { label: "JSON", value: "json", description: "" },
          { label: "YAML", value: "yaml", description: "" },
        ],
        multiSelect: false,
        allowOther: false,
      }],
      deadlineAtMs: Date.now() + 5_000,
    },
  }
  const inputRef = createRef<TextareaRenderable>()
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, { ...viewProps(snapshot, 100, 28), inputRef }), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Agent 需要你的回答")
    expect(frame).toContain("选一个输出格式")
    expect(frame).toContain("JSON")
    expect(frame).not.toContain("输入消息")
    expect(inputRef.current?.focused ?? false).toBeFalse()
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("开放式 ask_user 也弹出 QuestionDock，不把问题藏进输入栏占位", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "分析文本")
  const snapshot = {
    ...snapshotOf(started),
    interaction: {
      type: "question" as const,
      requestId: "ask-txt",
      questions: [{
        id: "question-1",
        question: "要分析哪个本地 .txt 文件？",
        header: "",
        body: "",
        options: [],
        multiSelect: false,
        allowOther: true,
      }],
      deadlineAtMs: Date.now() + 5_000,
    },
  }
  const inputRef = createRef<TextareaRenderable>()
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, { ...viewProps(snapshot, 100, 28), inputRef }), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Agent 需要你的回答")
    expect(frame).toContain("要分析哪个本地 .txt 文件？")
    expect(frame).toContain("输入回答后按 Enter")
    expect(frame).not.toContain("输入消息")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("ask_user 式多题单选显示 QuestionDock 选项，时间线不铺 JSON", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "写一个 Java 示例")
  const argumentsText = JSON.stringify({
    questions: [
      {
        question: "你想要什么类型的 Java 示例？",
        type: "multiple_choice",
        choices: [
          { value: "基础语法示例（变量、循环、方法、面向对象）" },
          { value: "集合与常用 API（List/Map/Stream）" },
        ],
      },
      { question: "示例的用途或目标是什么？（可选，自由填写）", type: "text", required: false },
    ],
  })
  const state: InteractiveState = {
    ...started,
    activity: { kind: "waiting-interaction", label: "等待回答" },
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-ask",
          runId: run.runId,
          name: "ask_user",
          arguments: argumentsText,
          output: "",
          status: "running",
        },
      },
    ],
  }
  const snapshot = {
    ...snapshotOf(state),
    interaction: {
      type: "question" as const,
      requestId: "ask-1",
      questions: [
        {
          id: "question-1",
          question: "你想要什么类型的 Java 示例？",
          header: "",
          body: "",
          options: [
            { label: "基础语法示例（变量、循环、方法、面向对象）", value: "基础语法示例（变量、循环、方法、面向对象）", description: "" },
            { label: "集合与常用 API（List/Map/Stream）", value: "集合与常用 API（List/Map/Stream）", description: "" },
          ],
          multiSelect: false,
          allowOther: true,
        },
        {
          id: "question-2",
          question: "示例的用途或目标是什么？（可选，自由填写）",
          header: "",
          body: "",
          options: [],
          multiSelect: false,
          allowOther: true,
        },
      ],
      deadlineAtMs: Date.now() + 5_000,
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshot, 120, 32)), { width: 120, height: 32 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Agent 需要你的回答 · 1/2")
    expect(frame).toContain("你想要什么类型的 Java 示例？")
    expect(frame).not.toContain("示例的用途或目标是什么")
    expect(frame).toContain("基础语法示例")
    expect(frame).toContain("其他")
    expect(frame).not.toContain("multiple_choice")
    expect(frame).not.toContain("\"questions\"")
    expect(frame).not.toContain("输入消息")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("审批作为内联时间线事件保留选项高度", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "写入文件")
  const state: InteractiveState = {
    ...started,
    activity: { kind: "waiting-interaction", label: "等待工具审批" },
    timeline: [
      started.timeline[0]!,
      { type: "tool", tool: { id: "tool-1", runId: run.runId, name: "execute", arguments: "{\"command\":\"pwd\"}", output: "/workspace", status: "completed" } },
      {
        type: "interaction",
        interaction: {
          id: "approval-1",
          runId: run.runId,
          type: "approval",
          status: "pending",
          description: "执行 shell 命令",
          requests: { action_requests: [{ name: "execute", args: { command: "pwd" } }] },
        },
      },
    ],
  }
  const snapshot = {
    ...snapshotOf(state),
    interaction: {
      type: "approval" as const,
      requestId: "approval-1",
      description: "执行 shell 命令",
      requests: { action_requests: [{ name: "execute", args: { command: "pwd" } }] },
      presentation: null,
      decisions: ["approve_once" as const, "approve_thread" as const, "approve_project" as const, "reject" as const, "reject_with_feedback" as const],
      deadlineAtMs: Date.now() + 5_000,
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshot, 100, 28)), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("需要审批")
    expect(frame).toContain("允许一次")
    expect(frame).toContain("本会话允许")
    expect(frame).toContain("本项目允许")
    expect(frame).toContain("拒绝")
    expect(frame).toContain("拒绝并反馈")
    expect(frame.indexOf("execute")).toBeLessThan(frame.indexOf("需要审批"))
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("文件审批在窄终端用行内 Diff，宽终端用双栏且保留统计与警告", async () => {
  const run = { threadId: "thread-diff", runId: "run-diff" }
  const started = startRun(createInitialState(), run, "修改文件")
  const state: InteractiveState = {
    ...started,
    activity: { kind: "waiting-interaction", label: "等待工具审批" },
    timeline: [
      started.timeline[0]!,
      {
        type: "interaction",
        interaction: {
          id: "approval-diff",
          runId: run.runId,
          type: "approval",
          status: "pending",
          description: "文件变更需要审批",
          requests: {},
        },
      },
    ],
  }
  const snapshot: InteractiveSnapshot = {
    ...snapshotOf(state),
    interaction: {
      type: "approval",
      requestId: "approval-diff",
      description: "文件变更需要审批",
      requests: {},
      presentation: {
        kind: "file_diff",
        operation: "edit",
        path: "/src/a.ts",
        added_lines: 1,
        removed_lines: 1,
        truncated: true,
        unified_diff: "--- /src/a.ts\n+++ /src/a.ts\n@@ -1,269 +1,269 @@\n-oldValue\n+newValue\n[diff 因行数或字节上限截断]",
      },
      decisions: ["approve_once", "reject"],
      deadlineAtMs: Date.now() + 5_000,
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshot, 100, 34)), { width: 100, height: 34 })
  })
  try {
    for (let attempt = 0; attempt < 10; attempt++) {
      await act(async () => {
        await Bun.sleep(20)
        await setup.flush()
      })
      const frame = setup.captureCharFrame()
      if (frame.includes("oldValue") && frame.includes("newValue")) break
    }
    const frame = setup.captureCharFrame()
    expect(frame).toContain("编辑文件 · /src/a.ts · +1 / -1")
    expect(frame).toContain("批准仍会应用完整变更")
    expect(frame).toContain("oldValue")
    expect(frame).toContain("newValue")
    expect(frame).toContain("允许一次")
    expect(frame).not.toContain("Error parsing diff")
    const diffSpans = setup.captureSpans().lines.flatMap(line => line.spans)
    expect(diffSpans.find(span => span.text.includes("oldValue"))?.bg.toInts()).toEqual(RGBA.fromHex(tuiTheme.diffRemovedBackground).toInts())
    expect(diffSpans.find(span => span.text.includes("newValue"))?.bg.toInts()).toEqual(RGBA.fromHex(tuiTheme.diffAddedBackground).toInts())
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }

  let wideSetup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    wideSetup = await testRender(createElement(ThreadView, viewProps(snapshot, 140, 34)), { width: 140, height: 34 })
  })
  try {
    for (let attempt = 0; attempt < 10; attempt++) {
      await act(async () => {
        await Bun.sleep(20)
        await wideSetup.flush()
      })
      if (wideSetup.captureCharFrame().split("\n").some(row => row.includes("oldValue") && row.includes("newValue"))) break
    }
    const rows = wideSetup.captureCharFrame().split("\n")
    expect(rows.some(row => row.includes("oldValue") && row.includes("newValue"))).toBe(true)
    expect(rows.join("\n")).not.toContain("Error parsing diff")
  } finally {
    await act(async () => { wideSetup.renderer.destroy() })
  }
})

test("TUI Diff 内容宽度 120 列切换为双栏", () => {
  expect(tuiDiffViewForWidth(119)).toBe("unified")
  expect(tuiDiffViewForWidth(120)).toBe("split")
})

test("继续执行只作为历史事件之后的底部活动行", async () => {
  const run = { threadId: "thread-2", runId: "run-2" }
  const started = startRun(createInitialState(), run, "继续任务")
  const state: InteractiveState = {
    ...started,
    activity: { kind: "running", label: "正在继续执行" },
    timeline: [
      started.timeline[0]!,
      { type: "tool", tool: { id: "tool-1", runId: run.runId, name: "read_file", arguments: "{\"file_path\":\"src/app.ts\"}", output: "export const value = 1", status: "completed" } },
      { type: "interaction", interaction: { id: "approval-1", runId: run.runId, type: "approval", status: "approved", description: "读取文件" } },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 28)), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("已允许")
    expect(frame).toContain("继续任务")
    expect(frame.indexOf("继续任务")).toBeLessThan(frame.indexOf("read_file"))
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("TUI 运行期间显示事实阶段、活动时长和取消提示", async () => {
  const run = { threadId: "thread-progress", runId: "run-progress" }
  const started = startRun(createInitialState(), run, "检查")
  const state: InteractiveState = {
    ...started,
    runProgress: { phase: "model", elapsedMs: 1_200 },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 40)), { width: 100, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("等待模型响应")
    expect(frame).toContain("已运行")
    expect(frame).toContain("Esc 取消")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("TUI 时间线交错显示思考中条目与思考文本", async () => {
  const run = { threadId: "thread-reasoning", runId: "run-reasoning" }
  const started = startRun(createInitialState(), run, "检查")
  const state: InteractiveState = {
    ...started,
    timeline: [
      ...started.timeline,
      { type: "reasoning", reasoning: { id: "r-1", runId: run.runId, text: "正在检查代码路径", active: true } },
      { type: "message", message: { id: "a-1", role: "assistant", content: "结论", runId: run.runId, streaming: false } },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 40)), { width: 100, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Thinking")
    expect(frame).toContain("正在检查代码路径")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("超长思考进行中只显示最后 12 行和剩余行数", async () => {
  const run = { threadId: "thread-reasoning", runId: "run-reasoning" }
  const started = startRun(createInitialState(), run, "检查")
  const text = Array.from({ length: 80 }, (_, index) => `T${String(index + 1).padStart(2, "0")}`).join("\n")
  const state: InteractiveState = {
    ...started,
    timeline: [
      ...started.timeline,
      { type: "reasoning", reasoning: { id: "r-long", runId: run.runId, text, active: true } },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 40)), { width: 100, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Thinking")
    expect(frame).toContain("T80")
    expect(frame).toContain("还有 68 行")
    expect(frame).not.toContain("T01")
    expect(frame).not.toContain("T68")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("TUI 思考段冻结后显示折叠头与展开提示", async () => {
  const run = { threadId: "thread-reasoning", runId: "run-reasoning" }
  const started = startRun(createInitialState(), run, "检查")
  const state: InteractiveState = {
    ...started,
    timeline: [
      ...started.timeline,
      { type: "reasoning", reasoning: { id: "r-1", runId: run.runId, text: "第一行思考\n后续细节", active: false } },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 40)), { width: 100, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("Thinking")
    expect(frame).toContain("展开")
    expect(frame).toContain("第一行思考")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Skills 与 Threads 选择器压暗底层 thread，但不压暗自身面板", async () => {
  const pickerCases = [
    {
      title: "Skills",
      picker: createElement(SkillPicker, {
        visible: true,
        loading: false,
        skills: [{ id: "review", name: "review", description: "审查改动", source: "user", enabled: true, userInvocable: true }],
        query: "",
        selectedIndex: 0,
        terminalWidth: 80,
        terminalHeight: 24,
        searchRef: createRef<TextareaRenderable>(),
        onSearch: () => undefined,
        onSelect: () => undefined,
        onHover: () => undefined,
        onClose: () => undefined,
      }),
    },
    {
      title: "Threads",
      picker: createElement(ThreadPicker, {
        visible: true,
        loading: false,
        threads: [{
          threadId: "thread-id-not-rendered",
          createdAtMs: 0,
          updatedAtMs: 0,
          firstMessage: "恢复索引",
          latestMessage: "索引已修复",
          messageCount: 2,
        }],
        query: "",
        selectedIndex: 0,
        terminalWidth: 80,
        terminalHeight: 24,
        searchRef: createRef<TextareaRenderable>(),
        onSearch: () => undefined,
        onSelect: () => undefined,
        onHover: () => undefined,
        onClose: () => undefined,
      }),
    },
  ]

  for (const pickerCase of pickerCases) {
    let setup: Awaited<ReturnType<typeof testRender>>
    await act(async () => {
      setup = await testRender(
        createElement(
          "box",
          { width: "100%", height: "100%", backgroundColor: tuiTheme.background },
          createElement("text", { fg: tuiTheme.text }, "底层 thread 内容"),
          pickerCase.picker,
        ),
        { width: 80, height: 24 },
      )
    })
    try {
      await act(async () => { await setup.flush() })
      const spans = setup.captureSpans().lines.flatMap(line => line.spans)
      const backgroundText = spans.find(span => span.text.includes("底层 thread 内容"))
      const panelTitle = spans.find(span => span.text === pickerCase.title)

      expect(backgroundText?.fg.toInts()).not.toEqual(RGBA.fromHex(tuiTheme.text).toInts())
      expect(panelTitle?.fg.toInts()).toEqual(RGBA.fromHex(tuiTheme.text).toInts())
    } finally {
      await act(async () => { setup.renderer.destroy() })
    }
  }
})

test("FooterRail 展示分支标签与 detached 标签", async () => {
  const base = snapshotOf(createInitialState())
  const cases: Array<{ interactive: InteractiveSnapshot; expected: string }> = [
    {
      interactive: { ...base, runtime: { ...base.runtime, gitWorkspace: { kind: "branch", branch: "main", root: "/workspace/harness-code" } } },
      expected: ":main",
    },
    {
      interactive: { ...base, runtime: { ...base.runtime, gitWorkspace: { kind: "detached", shortSha: "abc1234", root: "/workspace/harness-code" } } },
      expected: ":detached@abc1234",
    },
  ]
  for (const c of cases) {
    let setup: Awaited<ReturnType<typeof testRender>>
    await act(async () => {
      setup = await testRender(
        createElement(ThreadView, viewProps(c.interactive, 130, 40)),
        { width: 130, height: 40 },
      )
    })
    try {
      await act(async () => { await setup.flush() })
      const frame = setup.captureCharFrame()
      expect(frame).toContain(c.expected)
    } finally {
      await act(async () => { await setup.renderer.destroy() })
    }
  }
})

function viewProps(interactive: InteractiveSnapshot, terminalWidth: number, terminalHeight: number) {
  return {
    interactive,
    terminalWidth,
    terminalHeight,
    inputRef: createRef<TextareaRenderable>(),
    conversationScrollRef: createRef<ScrollBoxRenderable>(),
    value: "",
    onInput: () => undefined,
    onInputBarKeyDown: () => undefined,
    onSubmit: () => undefined,
    commandMenu: { visible: false, selectedIndex: 0 },
    commandOptions: [],
    onSelectCommand: () => undefined,
    onHoverCommand: () => undefined,
    pickerVisible: false,
    onClearSelectedSkill: () => undefined,
    showToolDetails: false,
    expandedTools: new Set<string>(),
    onToggleTool: () => undefined,
    onApproval: () => undefined,
    onDirectoryTrust: () => undefined,
    onPlan: () => undefined,
    onPlanViewClose: () => undefined,
    onQuestion: () => undefined,
  }
}

test("Compose activity 分组标题与终态折叠摘要可见", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "实现搜索")
  state = {
    ...state,
    workMode: "compose",
    timeline: [
      state.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "call-1",
          runId: run.runId,
          name: "read_file",
          arguments: "",
          output: "hidden-when-collapsed",
          status: "completed",
          executionId: "child-a",
          activityId: "act-a",
          agentId: "understand",
        },
      },
      {
        type: "compose-summary",
        summary: {
          id: "sum-a",
          runId: run.runId,
          status: "passed",
          text: "理解完成：目标已确认",
          executionId: "child-a",
          activityId: "act-a",
          agentId: "understand",
          composeScope: { activityId: "act-a", stage: "understand", attempt: 1 },
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)),
      { width: 130, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("理解")
    expect(frame).toContain("理解完成：目标已确认")
    // 终态默认折叠：不暴露 tool 全文
    expect(frame).not.toContain("hidden-when-collapsed")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Compose 投影渲染五阶段、任务与 blocked 摘要", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "实现搜索")
  state = {
    ...state,
    composeState: {
      threadId: "thread-1",
      slug: "search",
      complexity: "simple",
      status: "waiting_user",
      currentStage: "implement",
      waiting: "ask_user",
      stages: [
        { id: "requirement", state: "confirmed" },
        { id: "spec", state: "skipped" },
        { id: "plan", state: "confirmed" },
        { id: "implement", state: "failed" },
        { id: "review", state: "pending" },
      ],
      documents: [],
      fixRounds: 0,
      revision: 5,
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)),
      { width: 130, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("需求")
    expect(frame).toContain("规格")
    expect(frame).toContain("计划")
    expect(frame).toContain("实现")
    expect(frame).toContain("检视")
    expect(frame).toContain("✕")
    expect(frame).toContain("✓")
    expect(frame).not.toContain("实现 · 失败")
    expect(frame).not.toContain("▸")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Compose 进度钉在时间线上方，长对话滚动时仍占首行", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "实现搜索")
  const history: InteractiveState["timeline"] = [state.timeline[0]!]
  for (let index = 0; index < 24; index += 1) {
    history.push({
      type: "message",
      message: { id: `user-${index}`, role: "user", content: `用户补充 ${index}：把进度条顶出视口的长历史` },
    })
    history.push({
      type: "message",
      message: { id: `assistant-${index}`, role: "assistant", content: `助手回复 ${index}` },
    })
  }
  state = {
    ...state,
    timeline: history,
    composeState: {
      threadId: "thread-1",
      slug: "search",
      complexity: "simple",
      status: "waiting_user",
      currentStage: "plan",
      waiting: "plan_confirm",
      stages: [
        { id: "requirement", state: "confirmed" },
        { id: "spec", state: "skipped" },
        { id: "plan", state: "current" },
        { id: "implement", state: "pending" },
        { id: "review", state: "pending" },
      ],
      documents: [],
      fixRounds: 0,
      revision: 2,
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, viewProps(snapshotOf(state), 100, 20)),
      { width: 100, height: 20 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const lines = setup.captureCharFrame().split("\n")
    expect(lines[0] ?? "").not.toContain("需求")
    const head = lines.slice(0, 4).join("\n")
    expect(head).toContain("需求")
    expect(head).toContain("规格")
    expect(head).toContain("计划")
    expect(head).toContain("实现")
    expect(head).toContain("检视")
    expect(head).toContain("●")
    expect(head).not.toContain("用户补充")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Compose 失败后仍渲染冻结的终态阶段面板", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  let state = startRun(createInitialState(), run, "实现搜索")
  state = {
    ...state,
    activeRun: null,
    activity: { kind: "failed", label: "失败" },
    composeState: null,
    lastRun: {
      runId: run.runId,
      outcome: "failed",
      composeSummary: {
        threadId: "thread-1",
        slug: "search",
        complexity: "simple",
        status: "waiting_user",
        currentStage: "implement",
        waiting: "none",
        stages: [
          { id: "requirement", state: "confirmed" },
          { id: "spec", state: "skipped" },
          { id: "plan", state: "confirmed" },
          { id: "implement", state: "failed" },
          { id: "review", state: "pending" },
        ],
        documents: [],
        fixRounds: 0,
        revision: 2,
      },
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, viewProps(snapshotOf(state), 130, 40)),
      { width: 130, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("需求")
    expect(frame).toContain("检视")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Compose 有 Work Item 时对话页不再出现阶段顶栏", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(null, "compose"), run, "写搜索")
  const state: InteractiveState = {
    ...started,
    threadMode: "compose",
    workItem: {
      workItemId: "wi-search",
      slug: "feature-search",
      title: "实现搜索索引顶栏",
      revision: 3,
      status: "active",
      currentActivity: "编写搜索索引",
      pendingDecision: null,
      blockedReason: null,
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 28)), { width: 100, height: 28 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("写搜索")
    expect(frame).not.toContain("实现搜索索引顶栏")
    expect(frame).not.toContain("feature-search")
    expect(frame).not.toContain("已锁定")
    expect(frame).not.toContain("活动：编写搜索索引")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("skill.loaded 以系统事件一行展示，不铺原始键名", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "用 skill")
  const state: InteractiveState = {
    ...started,
    timeline: [
      started.timeline[0]!,
      {
        type: "message",
        message: {
          id: "skill-1",
          role: "system",
          content: "skill-loaded: project/review",
          runId: run.runId,
        },
      },
    ],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(snapshotOf(state), 100, 24)), { width: 100, height: 24 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("已加载 Skill")
    expect(frame).toContain("project/review")
    expect(frame).not.toContain("skill-loaded:")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("Run 级失败走 ErrorBlock，工具失败留在工具行", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "跑测试")
  const failedTool: InteractiveState = {
    ...started,
    activity: { kind: "running", label: "正在运行" },
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "t-exec",
          runId: run.runId,
          name: "execute",
          arguments: "{\"command\":\"false\"}",
          output: "command failed",
          status: "failed",
        },
      },
    ],
  }
  let toolSetup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    toolSetup = await testRender(createElement(ThreadView, viewProps(snapshotOf(failedTool), 100, 24)), { width: 100, height: 24 })
  })
  try {
    await act(async () => { await toolSetup.flush() })
    const frame = toolSetup.captureCharFrame()
    expect(frame).toContain("execute")
    expect(frame).toContain("command failed")
    expect(frame).not.toContain("运行失败")
  } finally {
    await act(async () => { toolSetup.renderer.destroy() })
  }

  const failedRun: InteractiveState = {
    ...started,
    activeRun: null,
    activity: { kind: "failed", label: "模型不可用" },
    lastRun: { runId: run.runId, outcome: "failed", durationMs: 1_200 },
  }
  let runSetup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    runSetup = await testRender(createElement(ThreadView, viewProps(snapshotOf(failedRun), 100, 24)), { width: 100, height: 24 })
  })
  try {
    await act(async () => { await runSetup.flush() })
    const frame = runSetup.captureCharFrame()
    expect(frame).toContain("运行失败")
  } finally {
    await act(async () => { runSetup.renderer.destroy() })
  }
})

test("一轮结束后 RunFooter 是一行 muted 摘要", async () => {
  const run = { threadId: "thread-1", runId: "run-1" }
  const started = startRun(createInitialState(), run, "问好")
  const state: InteractiveState = {
    ...started,
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
    lastRun: {
      runId: run.runId,
      outcome: "completed",
      durationMs: 2_400,
      usage: { inputTokens: 120, outputTokens: 40 },
    },
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, { ...viewProps(snapshotOf(state), 100, 24), modelName: "enterprise-model" }), { width: 100, height: 24 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("enterprise-model")
    expect(frame).toContain("2.4s")
    expect(frame).toContain("120 in")
    expect(frame).toContain("40 out")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

/** HC-162：派出卡与子时间线共用一套父/子过滤视图。 */
function childTimelineState(): InteractiveState {
  const run = { threadId: "thread-child", runId: "run-child" }
  const started = startRun(createInitialState(), run, "派子代理查一下")
  return {
    ...started,
    activeRun: null,
    activity: { kind: "completed", label: "已完成" },
    timeline: [
      started.timeline[0]!,
      {
        type: "tool",
        tool: {
          id: "task-1",
          runId: run.runId,
          executionId: "root",
          name: "task",
          arguments: JSON.stringify({ description: "查 src 下定义", subagent_type: "general-purpose" }),
          output: "查到了",
          status: "completed",
          childExecutionId: "child-abc",
          childAgentId: "general-purpose",
        },
      },
      {
        type: "tool",
        tool: { id: "child-tool-1", runId: run.runId, executionId: "child-abc", agentId: "general-purpose", name: "read_file", arguments: "{\"file_path\":\"src/app.ts\"}", output: "ok", status: "completed" },
      },
      {
        type: "message",
        message: { id: "assistant-1", role: "assistant", content: "父级总结", streaming: false },
      },
    ],
  }
}

test("父视图过滤 child 过程：只有派出卡，没有子代理工具", async () => {
  const state = childTimelineState()
  const interactive: InteractiveSnapshot = {
    ...snapshotOf(state),
    childTimelineExecutionId: null,
    timeline: [...scopeTimeline(state.timeline, "root")],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(interactive, 120, 40)), { width: 120, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("派出 general-purpose")
    expect(frame).toContain("父级总结")
    expect(frame).not.toContain("read_file")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("子视图只渲染该 execution，头部有返回提示，composer 关闭", async () => {
  const state = childTimelineState()
  const interactive: InteractiveSnapshot = {
    ...snapshotOf(state),
    childTimelineExecutionId: "child-abc",
    timeline: [...scopeTimeline(state.timeline, "child-abc")],
  }
  const opened: string[] = []
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, { ...viewProps(interactive, 120, 40), onOpenChildTimeline: id => { opened.push(id) } }),
      { width: 120, height: 40 },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("子代理时间线")
    expect(frame).toContain("Esc")
    expect(frame).toContain("read_file")
    expect(frame).not.toContain("派出 general-purpose")
    expect(frame).not.toContain("父级总结")
    expect(frame).not.toContain("输入消息")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("子视图刚绑定且暂无事件时显示运行中专用空态", async () => {
  const state = childTimelineState()
  const interactive: InteractiveSnapshot = {
    ...snapshotOf(state),
    activeRun: { threadId: "thread-child", runId: "run-child" },
    activity: { kind: "running", label: "正在运行" },
    childTimelineExecutionId: "child-not-started",
    timeline: [],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(interactive, 120, 40)), { width: 120, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("子代理刚开始，暂无过程")
    expect(frame).toContain("Esc")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("子视图终结但没有过程事件时显示诊断空态", async () => {
  const state = childTimelineState()
  const interactive: InteractiveSnapshot = {
    ...snapshotOf(state),
    childTimelineExecutionId: "child-missing-events",
    timeline: [],
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(ThreadView, viewProps(interactive, 120, 40)), { width: 120, height: 40 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("未收到该子代理的过程事件")
    expect(frame).toContain("Esc")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("点派出卡进入对应子时间线", async () => {
  const state = childTimelineState()
  const interactive: InteractiveSnapshot = {
    ...snapshotOf(state),
    childTimelineExecutionId: null,
    timeline: [...scopeTimeline(state.timeline, "root")],
  }
  const opened: string[] = []
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(
      createElement(ThreadView, { ...viewProps(interactive, 120, 40), onOpenChildTimeline: id => { opened.push(id) } }),
      { width: 120, height: 40, useMouse: true },
    )
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    const lines = frame.split("\n")
    const hintY = lines.findIndex(line => line.includes("进入子时间线"))
    if (hintY < 0) throw new Error("派出卡未显示进入子时间线入口")
    const spans = setup.captureSpans().lines[hintY]!.spans
    const spanIndex = spans.findIndex(span => span.text.includes("进入子时间线"))
    const hintX = spans.slice(0, spanIndex).reduce((offset, span) => offset + span.width, 0)
    await act(async () => {
      await setup.mockMouse.click(hintX, hintY)
      await setup.flush()
    })
    expect(opened).toEqual(["child-abc"])
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})
