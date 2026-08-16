/** TUI 侧边栏小部件单元测试（CWD、Context、MCP、Modified Files）。 */

import { expect, test } from "bun:test"
import type { InteractiveSnapshot, TimelineItem } from "../../../src/interactive/types"
import { formatWorkspacePath } from "../../../src/tui/presentation/sidebar/cwd-widget"
import { formatTokenCount, renderProgressBar, calculateTps } from "../../../src/tui/presentation/sidebar/context-widget"
import { extractModifiedFiles } from "../../../src/tui/presentation/sidebar/modified-files-widget"

test("formatWorkspacePath: 缩写 $HOME 路径并提取工作区名称", () => {
  const home = process.env.HOME || "/Users/testuser"
  const fullPath = `${home}/Code/MyProject`
  const result = formatWorkspacePath(fullPath)
  expect(result.basename).toBe("MyProject")
  expect(result.displayPath).toBe("~/Code/MyProject")
})

test("Context 统计辅助函数: Token 格式化、进度条与 TPS 计算", () => {
  expect(formatTokenCount(0)).toBe("0")
  expect(formatTokenCount(512)).toBe("512")
  expect(formatTokenCount(1536)).toBe("1.5k")
  expect(formatTokenCount(128000)).toBe("128.0k")
  expect(formatTokenCount(1048576)).toBe("1.0M")

  const bar50 = renderProgressBar(50, 10)
  expect(bar50).toBe("█████░░░░░")

  const bar100 = renderProgressBar(100, 10)
  expect(bar100).toBe("██████████")

  const bar0 = renderProgressBar(0, 10)
  expect(bar0).toBe("░░░░░░░░░░")

  expect(calculateTps(100, 2000)).toBe("50.0")
  expect(calculateTps(0, 2000)).toBe(null)
  expect(calculateTps(100, 0)).toBe(null)
})

test("extractModifiedFiles: 从 timeline 工具卡片中提取变更文件及行数", () => {
  const timeline: TimelineItem[] = [
    {
      type: "message",
      message: { id: "m1", role: "user", content: "hello" },
    },
    {
      type: "tool",
      tool: {
        id: "t1",
        runId: "r1",
        name: "write_file",
        arguments: JSON.stringify({ file_path: "src/index.ts", content: "line 1\nline 2\nline 3\n" }),
        output: "ok",
        status: "completed",
      },
    },
    {
      type: "tool",
      tool: {
        id: "t2",
        runId: "r1",
        name: "edit_file",
        arguments: JSON.stringify({
          file_path: "src/utils.ts",
          old_string: "const a = 1\n",
          new_string: "const a = 2\nconst b = 3\n",
        }),
        output: "ok",
        status: "completed",
      },
    },
  ]

  const files = extractModifiedFiles(timeline)
  expect(files.length).toBe(2)
  expect(files[0].relativePath).toBe("src/index.ts")
  expect(files[0].status).toBe("added")
  expect(files[0].addedLines).toBe(3)
  expect(files[0].removedLines).toBe(0)

  expect(files[1].relativePath).toBe("src/utils.ts")
  expect(files[1].status).toBe("modified")
  expect(files[1].addedLines).toBe(2)
  expect(files[1].removedLines).toBe(1)
})

test("Sidebar 组件集成渲染四个小部件", async () => {
  const { Sidebar } = await import("../../../src/tui/presentation/sidebar")
  const { CwdWidget } = await import("../../../src/tui/presentation/sidebar/cwd-widget")
  const { ContextWidget } = await import("../../../src/tui/presentation/sidebar/context-widget")
  const { McpWidget } = await import("../../../src/tui/presentation/sidebar/mcp-widget")
  const { ModifiedFilesWidget } = await import("../../../src/tui/presentation/sidebar/modified-files-widget")

  const fakeInteractive = {
    currentThreadId: "thread-1",
    activity: { kind: "idle" },
    activeRun: null,
    timeline: [],
    runProgress: null,
    interaction: null,
    confirmation: null,
    lastRun: {
      runId: "r1",
      outcome: "completed",
      durationMs: 1500,
      usage: { inputTokens: 2048, outputTokens: 512 },
      context: { action: "run", inputCapTokens: 128000 },
    },
    runtime: {
      workspace: "/test/workspace",
      modelName: "claude-3-7-sonnet",
      cliVersion: "0.1.0",
      approvalMode: "default",
      modelConfigured: true,
      gitWorkspace: { kind: "branch", branch: "main", isClean: true },
    },
    connection: { status: "open" },
    commands: [],
    catalogs: {
      threads: { status: "ready", items: [] },
      models: { status: "ready", items: [] },
      skills: { status: "ready", items: [] },
      mcp: {
        status: "ready",
        items: [
          { name: "github", status: "connected" },
          { name: "filesystem", status: "failed" },
        ],
      },
    },
    selection: {
      requestedModelProfileId: null,
      actualModel: null,
      armedSkill: null,
    },
    workMode: "build",
    composeState: null,
    workItem: null,
    threadMode: null,
  } as any

  const element = Sidebar({
    sidebar: {
      mode: "show",
      drawerOpen: false,
      focus: "chat",
      fileTree: { rootPath: "/test", rootNodes: [], selectedIndex: 0, visibleRows: [] },
      preview: null,
    },
    interactive: fakeInteractive,
    terminalWidth: 140,
    terminalHeight: 40,
    onToggle: () => {},
  })

  expect(element).not.toBeNull()
})
