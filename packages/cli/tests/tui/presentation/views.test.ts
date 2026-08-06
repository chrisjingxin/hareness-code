import { expect, test } from "bun:test"
import { RGBA, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement, createRef } from "react"

import type { InteractiveSnapshot } from "../../../src/interactive/types"
import { createInteractiveRuntime } from "../../../src/interactive/runtime"
import { createInitialState, startRun, type InteractiveState } from "../../../src/interactive/state"
import { registerCommonSyntaxParsers } from "../../../src/tui/platform/syntax-parsers"
import { HomeView } from "../../../src/tui/presentation/home"
import { SkillPicker, ThreadPicker } from "../../../src/tui/presentation/pickers"
import { tuiTheme } from "../../../src/tui/presentation/theme"
import { ThreadView } from "../../../src/tui/presentation/thread"

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
    },
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
    expect(frame).toContain("default")
    expect(frame).not.toContain("未隔离")
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

test("首页模型靠左、审批模式靠右，且不重复显示品牌", async () => {  const longModelRuntime = {
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
    expect(runtimeLine).not.toContain("Harness Code")
    expect(runtimeLine).not.toContain("本机执行")
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
      decisions: ["approve_once" as const, "approve_thread" as const, "approve_always" as const, "reject" as const, "reject_with_feedback" as const],
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
    expect(frame).toContain("本线程允许")
    expect(frame).toContain("永久允许")
    expect(frame).toContain("拒绝")
    expect(frame).toContain("拒绝并反馈")
    expect(frame.indexOf("execute")).toBeLessThan(frame.indexOf("需要审批"))
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
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
    onComposerKeyDown: () => undefined,
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
    onQuestion: () => undefined,
  }
}
