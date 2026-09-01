/** Status 运行状态仪表盘浮层组件与快捷键生命周期测试。 */

import { expect, test, mock } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"
import { resolveShortcut } from "../../src/tui/application/shortcuts"
import { createTuiAdapter } from "../../src/tui/application/adapter"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway } from "../../src/interactive/ports"
import { StatusModal } from "../../src/tui/presentation/status-modal"
import type { InteractiveRuntime } from "../../src/interactive/runtime"

test("StatusModal visible=false 时不渲染任何内容", async () => {
  const runtime: InteractiveRuntime = {
    workspace: "/test/project",
    cliVersion: "0.1.0",
    modelName: "claude-3-5-sonnet",
    modelConfigured: true,
    executionMode: "local",
    approvalMode: "default",
  }
  const controller = createInteractiveController({
    runtime,
    gateway: createFallbackNoopGateway(),
    idGenerator: { uuid: () => "00000000-0000-4000-8000-000000000000" },
  })
  const interactive = controller.getSnapshot()
  const onClose = mock(() => {})

  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(StatusModal, {
        visible: false,
        interactive,
        terminalWidth: 100,
        terminalHeight: 28,
        onClose,
      }), { width: 100, height: 28 })
    })
    await act(async () => { await setup.flush() })

    const output = setup.captureCharFrame()
    expect(output.trim()).toBe("")
  } finally {
    setup?.renderer.destroy()
    await controller.close()
  }
})

test("StatusModal visible=true 时正确渲染 4 大卡片模块与快捷键", async () => {
  const runtime: InteractiveRuntime = {
    workspace: "/test/project",
    cliVersion: "0.1.0",
    modelName: "claude-3-5-sonnet",
    modelProfileId: "pro",
    modelConfigured: true,
    executionMode: "local",
    approvalMode: "default",
    gitWorkspace: { kind: "branch", branch: "main", root: "/test/project" },
  }
  const controller = createInteractiveController({
    runtime,
    gateway: createFallbackNoopGateway(),
    idGenerator: { uuid: () => "00000000-0000-4000-8000-000000000000" },
  })
  const interactive = controller.getSnapshot()
  const onClose = mock(() => {})

  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(StatusModal, {
        visible: true,
        interactive,
        terminalWidth: 100,
        terminalHeight: 28,
        onClose,
      }), { width: 100, height: 28 })
    })
    await act(async () => { await setup.flush() })

    const output = setup.captureCharFrame()
    expect(output).toContain("运行状态仪表盘")
    expect(output).toContain("工作区与环境")
    expect(output).toContain("运行模式与模型")
    expect(output).toContain("会话与上下文")
    expect(output).toContain("扩展生态与连接")
    expect(output).toContain("za38-cli 0.1.0")
    expect(output).not.toContain("JSON-RPC")
    expect(output).not.toContain("Python Sidecar")
    expect(output).toContain("🟢 正常")
    expect(output).toContain("pro")
    expect(output).toContain("BUILD")
    expect(output).toContain("Esc / Enter / q 关闭")
  } finally {
    setup?.renderer.destroy()
    await controller.close()
  }
})

test("快捷键在 statusModalVisible 时拦截 Esc / Enter / q 并关闭", () => {
  const base = {
    commandMenuVisible: false,
    commandOptionCount: 0,
    activeRun: false,
    hasDraft: false,
    statusModalVisible: true,
  }

  expect(resolveShortcut({ name: "escape", ctrl: false }, base)).toBe("close-status-modal")
  expect(resolveShortcut({ name: "return", ctrl: false }, base)).toBe("close-status-modal")
  expect(resolveShortcut({ name: "kpenter", ctrl: false }, base)).toBe("close-status-modal")
  expect(resolveShortcut({ name: "q", ctrl: false }, base)).toBe("close-status-modal")
  expect(resolveShortcut({ name: "a", ctrl: false }, base)).toBe("none")
})

test("Adapter 执行 /status 会打开 statusModal，通过 status-close 或 Esc 能够关闭", async () => {
  const runtime: InteractiveRuntime = {
    workspace: "/test/project",
    cliVersion: "0.1.0",
    modelName: "claude-3-5-sonnet",
    modelConfigured: true,
    executionMode: "local",
    approvalMode: "default",
  }
  const controller = createInteractiveController({
    runtime,
    gateway: createFallbackNoopGateway(),
    idGenerator: { uuid: () => "00000000-0000-4000-8000-000000000000" },
  })
  const adapter = createTuiAdapter({
    controller,
    inputHistory: { load: async () => [], append: async () => {} },
  })

  expect(adapter.getSnapshot().statusModal.visible).toBe(false)

  // 模拟输入 /status
  await adapter.dispatch({ type: "execute-command", commandId: "system.status" })
  expect(adapter.getSnapshot().statusModal.visible).toBe(true)

  // 模拟快捷键 Esc
  await adapter.dispatch({ type: "shortcut", action: "close-status-modal" })
  expect(adapter.getSnapshot().statusModal.visible).toBe(false)

  // 再次打开并用 status-close 意图关闭
  await adapter.dispatch({ type: "execute-command", commandId: "system.status" })
  expect(adapter.getSnapshot().statusModal.visible).toBe(true)

  await adapter.dispatch({ type: "status-close" })
  expect(adapter.getSnapshot().statusModal.visible).toBe(false)

  await adapter.close()
})

test("StatusModal 正确统计 Timeline 消息、工具调用与 Token 估算", async () => {
  const runtime: InteractiveRuntime = {
    workspace: "/test/project",
    cliVersion: "0.1.0",
    modelName: "claude-3-5-sonnet",
    modelConfigured: true,
    executionMode: "local",
    approvalMode: "default",
  }
  const controller = createInteractiveController({
    runtime,
    gateway: createFallbackNoopGateway(),
    idGenerator: { uuid: () => "00000000-0000-4000-8000-000000000000" },
  })

  // 模拟时间线中的消息与工具
  const snapshot = {
    ...controller.getSnapshot(),
    currentThreadId: "thread-abc-12345678",
    timeline: [
      { type: "message" as const, message: { id: "m1", role: "user" as const, content: "你好，请帮我重构代码" } },
      { type: "message" as const, message: { id: "m2", role: "assistant" as const, content: "好的，我先读取文件。" } },
      { type: "tool" as const, tool: { id: "t1", runId: "r1", name: "read_file", arguments: "{}", output: "const a = 1;", status: "completed" as const } },
    ],
  }

  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(StatusModal, {
        visible: true,
        interactive: snapshot,
        terminalWidth: 100,
        terminalHeight: 28,
        onClose: () => {},
      }), { width: 100, height: 28 })
    })
    await act(async () => { await setup.flush() })

    const output = setup.captureCharFrame()
    expect(output).toContain("2 消息 · 1 工具")
    expect(output).toContain("thread-abc-1")
  } finally {
    setup?.renderer.destroy()
    await controller.close()
  }
})
