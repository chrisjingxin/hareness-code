import { expect, spyOn, test } from "bun:test"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { PassThrough } from "node:stream"
import * as openTuiCore from "@opentui/core"
import * as openTuiReact from "@opentui/react"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement, type ReactElement, type ReactNode } from "react"

import { AgentClient } from "../../src/ipc/client"
import { StdioRpcTransport } from "../../src/ipc/stdio-transport"
import { runTui, TUI_RENDERER_OPTIONS, Za38Tui, type RenderedTuiOptions } from "../../src/tui/app"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway } from "../../src/interactive/ports"
import { AgentClientGateway } from "../../src/infrastructure/agent-client-gateway"
import { createTuiAdapter, type TuiAdapter } from "../../src/tui/application/adapter"
import type { InteractiveRuntime } from "../../src/interactive/runtime"
import * as clipboard from "../../src/tui/platform/clipboard"
import * as selectionCopy from "../../src/tui/presentation/selection-copy"
import { createWorkspaceExplorer } from "../../src/workspace/explorer"

const runtime: InteractiveRuntime = {
  workspace: "/workspace/harness-code",
  cliVersion: "0.1.0",
  modelConfigured: true,
  modelProfileId: "enterprise-model",
  modelName: "enterprise-model",
  executionMode: "local",
  approvalMode: "default",
}

test("runTui 在空输入 Ctrl+C 后关闭 renderer", async () => {
  await expectRunTuiCloses(adapter => adapter.dispatch({ type: "shortcut", action: "exit" }))
})

test("runTui 在 /quit 后关闭 renderer", async () => {
  await expectRunTuiCloses(adapter => adapter.dispatch({ type: "submit", value: "/quit" }))
})

async function expectRunTuiCloses(trigger: (adapter: TuiAdapter) => Promise<void>): Promise<void> {
  const { client, controller, adapter: fixtureAdapter } = createSession()
  let rendered: ReactElement | null = null
  let renderedOptions: RenderedTuiOptions | undefined
  let destroyCount = 0
  let unmountCount = 0
  const renderer = { destroy: () => { destroyCount += 1 } }
  const root = {
    render: (node: ReactNode) => { rendered = node as ReactElement },
    unmount: () => { unmountCount += 1 },
  }
  const createRenderer = spyOn(openTuiCore, "createCliRenderer").mockResolvedValue(renderer as never)
  const createReactRoot = spyOn(openTuiReact, "createRoot").mockReturnValue(root as never)
  let running: Promise<void> | undefined
  try {
    running = runTui({ controller })
    for (let attempt = 0; attempt < 10 && rendered === null; attempt += 1) {
      await Promise.resolve()
    }
    expect(rendered).not.toBeNull()
    const boundary = rendered as ReactElement<{ children: ReactElement<RenderedTuiOptions> }>
    renderedOptions = boundary.props.children.props

    await trigger(renderedOptions.adapter)
    await Promise.resolve()
    await Promise.resolve()

    expect(unmountCount).toBe(1)
    expect(destroyCount).toBe(1)
    await running
  } finally {
    if (destroyCount === 0) {
      renderedOptions?.onRequestExit()
      if (running) await running
    }
    createReactRoot.mockRestore()
    createRenderer.mockRestore()
    client.destroy()
    await fixtureAdapter.close()
    await controller.close()
  }
}

test("根 TUI 在 mouse-up 时复制 renderer 的非空选区并显示 Toast", async () => {
  const { client, controller, adapter } = createSession()
  const copy = spyOn(clipboard, "copyToClipboard").mockResolvedValue(true)
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 28, useMouse: true })
    })
    await act(async () => { await setup.flush() })

    const selectionRenderer = setup.renderer as unknown as {
      getSelection(): { getSelectedText(): string } | null
      clearSelection(): void
    }
    let clearCount = 0
    selectionRenderer.getSelection = () => ({ getSelectedText: () => "根层选区文本" })
    selectionRenderer.clearSelection = () => { clearCount += 1 }

    await act(async () => {
      await setup.mockMouse.click(0, 0)
      await setup.flush()
    })

    expect(copy).toHaveBeenCalledWith("根层选区文本")
    expect(clearCount).toBe(1)
    expect(adapter.getSnapshot().toasts.some(toast => (
      toast.message === "已复制到剪贴板" && toast.variant === "success"
    ))).toBe(true)
  } finally {
    copy.mockRestore()
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("完整 TUI 中 task 从运行转为长结果后仍可点击进入子时间线", async () => {
  const { client, requests, controller, adapter } = createSession()
  const reactErrors: string[] = []
  const consoleError = spyOn(console, "error").mockImplementation((...args) => {
    reactErrors.push(args.map(value => String(value)).join(" "))
  })
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 94, height: 32, useMouse: true, screenMode: "alternate-screen" })
      await setup.flush()
      await adapter.dispatch({ type: "submit", value: "先调查工作区" })
      await setup.flush()
    })
    expect(reactErrors).toEqual([])
    const run = requests.at(-1)!
    const emit = async (type: string, sequence: number, payload: Record<string, unknown>) => {
      await act(async () => {
        client.emit("event", {
          event_id: crypto.randomUUID(),
          type,
          thread_id: run.threadId,
          run_id: run.runId,
          sequence,
          timestamp_ms: Date.now(),
          payload,
        })
        await setup.flush()
      })
    }
    await emit("run.started", 1, {})
    expect(reactErrors).toEqual([])
    await emit("tool.started", 2, { tool_call_id: "task-1", name: "task" })
    expect(reactErrors).toEqual([])
    await emit("tool.delta", 3, {
      tool_call_id: "task-1",
      arguments_delta: JSON.stringify({ description: "探索当前工作区根目录，确认仓库状态和已有文件。列出所有文件和目录，确认这是一个空仓库或近似空仓库。返回：1）根目录完整 ls 输出；2）是否存在 package.json / tsconfig.json / bun.lockb 等配置；3）推荐的最小文件结构（用于一个基于 Bun.serve 的极简任务看板，包含 server.ts、public/index.html、tests/api.test.ts）。", subagent_type: "explore" }),
    })
    expect(reactErrors).toEqual([])
    await emit("tool.delta", 4, {
      tool_call_id: "task-1",
      child_execution_id: "child-live-to-completed",
      child_agent_id: "explore",
    })
    expect(reactErrors).toEqual([])

    const runningLines = setup.captureCharFrame().split("\n")
    const runningHintY = runningLines.findIndex(line => line.includes("进入子时间线"))
    if (runningHintY < 0) throw new Error("运行中 task 未显示进入子时间线入口")
    const runningSpans = setup.captureSpans().lines[runningHintY]!.spans
    const runningSpanIndex = runningSpans.findIndex(span => span.text.includes("进入子时间线"))
    const runningHintX = runningSpans.slice(0, runningSpanIndex).reduce((offset, span) => offset + span.width, 0)
    await act(async () => {
      // 真实用户点击会跨过 80ms spinner 刷新，不能只验证 10ms 的理想点击。
      await setup.mockMouse.click(runningHintX + 4, runningHintY, 0, { delayMs: 120 })
      await setup.flush()
    })
    expect(adapter.getSnapshot().interactive.childTimelineExecutionId).toBe("child-live-to-completed")
    await act(async () => {
      await adapter.dispatch({ type: "child-timeline-leave" })
      await setup.flush()
    })

    await emit("tool.completed", 5, {
      tool_call_id: "task-1",
      result: {
        content: Array.from({ length: 36 }, (_, index) => `## 调查结果 ${index + 1}\n长结果正文`).join("\n"),
        is_error: false,
      },
    })
    expect(reactErrors).toEqual([])

    for (let attempt = 0; attempt < 16 && !setup.captureCharFrame().includes("进入子时间线"); attempt += 1) {
      await act(async () => {
        await setup.mockMouse.scroll(40, 10, "up")
        await setup.flush()
      })
    }
    const lines = setup.captureCharFrame().split("\n")
    const hintY = lines.findIndex(line => line.includes("进入子时间线"))
    if (hintY < 0) throw new Error("完整 TUI 未显示进入子时间线入口")
    const spans = setup.captureSpans().lines[hintY]!.spans
    const spanIndex = spans.findIndex(span => span.text.includes("进入子时间线"))
    const hintX = spans.slice(0, spanIndex).reduce((offset, span) => offset + span.width, 0)
    await act(async () => {
      await setup.mockMouse.click(hintX, hintY)
      await setup.flush()
    })
    expect(adapter.getSnapshot().interactive.childTimelineExecutionId).toBe("child-live-to-completed")
    expect(reactErrors.join("\n")).not.toContain("Expected static flag was missing")
  } finally {
    consoleError.mockRestore()
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("首页输入 @ 前自动加载真实工作区文件并展示候选", async () => {
  const root = await mkdtemp(join(tmpdir(), "za38-tui-mention-"))
  await writeFile(join(root, "source.ts"), "export const value = 1")
  const gateway = createFallbackNoopGateway()
  const controller = createInteractiveController({ gateway })
  const explorer = await createWorkspaceExplorer(root)
  const adapter = createTuiAdapter({
    controller,
    gateway,
    workspaceExplorer: explorer,
    promptHistoryStore: { load: async () => [], save: async () => {} },
    onRequestExit: () => undefined,
  })
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        workspaceExplorer: explorer,
        onRequestExit: () => undefined,
      }), { width: 100, height: 28 })
      await setup.flush()
      for (let attempt = 0; attempt < 20 && explorer.getSnapshot().tree.status !== "ready"; attempt += 1) {
        await Bun.sleep(5)
        await setup.flush()
      }
    })
    expect(explorer.getSnapshot().tree.status).toBe("ready")
    expect(explorer.getSnapshot().tree.allEntries?.map(row => row.path)).toContain("source.ts")

    await act(async () => {
      await setup.mockInput.typeText("@")
      await setup.flush()
    })
    expect(adapter.getSnapshot().draft).toBe("@")
    expect(adapter.getSnapshot().mentionMenu.visible).toBe(true)
    expect(setup.captureCharFrame()).toContain("source.ts")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    await adapter.close()
    await explorer.close()
    await controller.close()
    await rm(root, { recursive: true, force: true })
  }
})

test("无选区时首次 Ctrl+C 清空草稿并保持 TUI，第二次才请求退出", async () => {
  let exitCount = 0
  const { client, controller, adapter } = createSession(false, () => { exitCount += 1 })
  const shouldAttempt = spyOn(selectionCopy, "shouldAttemptSelectionCopy").mockImplementation((_platform, input) => (
    input.type === "key-down" && input.name === "c" && input.ctrl
  ))
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { ...TUI_RENDERER_OPTIONS, width: 100, height: 28 })
    })
    await act(async () => {
      await setup.mockInput.typeText("待清空的输入")
      await setup.flush()
    })
    expect(adapter.getSnapshot().draft).toBe("待清空的输入")

    await act(async () => {
      setup.mockInput.pressCtrlC()
      await setup.flush()
    })
    expect(adapter.getSnapshot().draft).toBe("")
    expect(setup.captureCharFrame()).not.toContain("待清空的输入")
    expect(setup.renderer.isDestroyed).toBe(false)
    expect(exitCount).toBe(0)

    await act(async () => {
      setup.mockInput.pressCtrlC()
      await setup.flush()
    })
    expect(exitCount).toBe(1)
  } finally {
    shouldAttempt.mockRestore()
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("在输入框中按 Ctrl+C 即时同步清空原生 textarea 缓冲区与 Adapter draft", async () => {
  const { client, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { ...TUI_RENDERER_OPTIONS, width: 100, height: 28 })
    })
    await act(async () => {
      await setup.mockInput.typeText("输入框测试文本")
      await setup.flush()
    })
    expect(adapter.getSnapshot().draft).toBe("输入框测试文本")
    expect(setup.captureCharFrame()).toContain("输入框测试文本")

    await act(async () => {
      setup.mockInput.pressCtrlC()
      await setup.flush()
    })
    expect(adapter.getSnapshot().draft).toBe("")
    expect(setup.captureCharFrame()).not.toContain("输入框测试文本")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})


test("真实 textarea 在光标边界用上下键回填历史，而不是被全局快捷键截获", async () => {
  const historyHome = await mkdtemp(join(tmpdir(), "za38-tui-history-"))
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        promptHistoryFile: join(historyHome, "prompt-history.jsonl"),
        onRequestExit: () => undefined,
      }), { width: 80, height: 24 })
    })
    await act(async () => { await setup.flush() })

    await sendAndFinish(setup, client, requests, "第一条")
    await sendAndFinish(setup, client, requests, "第二条")

    // 空 composer 的两次 ↑ 应依次取回最新和上一条；Enter 读取 textarea 当前缓冲区。
    await act(async () => {
      setup.mockInput.pressArrow("up")
      setup.mockInput.pressArrow("up")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)?.message).toBe("第一条")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
    await rm(historyHome, { recursive: true, force: true })
  }
})

test("/status 只展示本地运行摘要，不创建 Agent run", async () => {
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 28 })
    })
    await act(async () => {
      await setup.mockInput.typeText("/status")
      await setup.flush()
    })
    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })

    const frame = await setup.waitForFrame(value => value.includes("工作区"))
    expect(requests).toHaveLength(0)
    expect(frame).toContain("工作区")
    expect(frame).toContain("本机执行")
    expect(frame).toContain("default")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})


test("未知 Slash Command 只显示本地建议，不会创建 Agent run", async () => {
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/contnue")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    const frame = await setup.waitForFrame(value => value.includes("未知命令"))
    expect(frame).toContain("/resume")
    expect(requests).toHaveLength(0)
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("双斜杠转义会原样向 Agent 提交单个前导斜杠", async () => {
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("//api/users 的路由在哪里")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)?.message).toBe("/api/users 的路由在哪里")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})


test("/skills 打开可搜索选择器，并把选中的 Skill 附到下一次运行", async () => {
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await sendAndFinish(setup, client, requests, "保留 thread 上下文")
    await act(async () => {
      await setup.mockInput.typeText("/skills")
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    let frame = await setup.waitForFrame(value => value.includes("repo-review-demo"))
    expect(frame).toContain("Skills")
    expect(frame).toContain("搜索 Skills")
    expect(frame).toContain("保留 thread")
    expect(frame).toContain("一条用于验证浮层描述单行")
    expect(frame).not.toContain("显示的长说明")
    expect(frame).not.toContain("┌")

    await act(async () => {
      await setup.mockInput.typeText("review")
      await setup.flush()
    })
    frame = setup.captureCharFrame()
    expect(frame).toContain("repo-review-demo")

    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => !value.includes("Skills") && value.includes("Skill") && value.includes("user/repo-review-demo"))
    expect(frame).toContain("下一条消息使用")

    await act(async () => {
      await setup.mockInput.typeText("审查当前改动")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)).toMatchObject({
      message: "审查当前改动",
      requestedSkill: { id: "user/repo-review-demo", args: "审查当前改动" },
    })
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})


test("SearchPicker 的 Esc 关闭浮层后会恢复 composer，且不把搜索文字带入下一次输入", async () => {
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/skills")
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    await setup.waitForFrame(value => value.includes("搜索 Skills"))

    await act(async () => {
      await setup.mockInput.typeText("review")
      setup.mockInput.pressEscape()
      // OpenTUI 会等待 20ms，以区分单独 Esc 与 Alt/Meta 组合键前缀。
      await Bun.sleep(30)
      await setup.flush()
    })
    await setup.waitForFrame(value => !value.includes("Skills"))

    await act(async () => {
      await setup.mockInput.typeText("关闭后继续执行")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)?.message).toBe("关闭后继续执行")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})


test("启动 --resume 等价于打开同一 thread 选择器", async () => {
  const { client, controller, adapter } = createSession(true)
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await Bun.sleep(0)
      await setup.flush()
    })
    const frame = await setup.waitForFrame(value => value.includes("Threads") && value.includes("修复索引结果"))
    expect(frame).toContain("修复索引结果")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("Slash 菜单显示 skill:<id> 并可直接选择", async () => {
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/skill:repo")
      await setup.flush()
    })
    let frame = await setup.waitForFrame(value => value.includes("/skill:user/repo-review-demo"))
    expect(frame).toContain("user · 只读代码审查")

    await act(async () => {
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("Skill") && value.includes("user/repo-review-demo"))
    expect(frame).toContain("下一条消息使用")

    await act(async () => {
      await setup.mockInput.typeText("检查这个变更")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)).toMatchObject({
      message: "检查这个变更",
      requestedSkill: { id: "user/repo-review-demo", args: "检查这个变更" },
    })
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})


test("窄终端中的 /skills 使用单列浮层且保持可操作", async () => {
  const { client, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 58, height: 18 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/skills")
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })

    const frame = await setup.waitForFrame(value => value.includes("repo-review-demo"))
    expect(frame).toContain("Skills")
    expect(frame).toContain("repo-review-demo")
    expect(frame).not.toContain("只读代码审查")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("无 Web launcher 时 /web 显示宿主级通知，不创建 Agent run", async () => {
  const { client, requests, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 28 })
    })
    await act(async () => { await setup.flush() })
    await act(async () => {
      await setup.mockInput.typeText("/web")
      await setup.flush()
    })
    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    const frame = await setup.waitForFrame(value => value.includes("未提供 Web launcher"))
    expect(frame).toContain("未提供 Web launcher")
    expect(requests).toHaveLength(0)
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("串行审批竞态下第二个对话框回车仍可回写", async () => {
  const { client, requests, approvals, writeServer, controller, adapter } = createSession()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })

    await act(async () => {
      await setup.mockInput.typeText("删除两个文件")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    const run = requests.at(-1)
    expect(run?.message).toBe("删除两个文件")

    await act(async () => {
      client.emit("event", {
        event_id: crypto.randomUUID(),
        type: "run.started",
        thread_id: run?.threadId,
        run_id: run?.runId,
        sequence: 1,
        timestamp_ms: Date.now(),
        payload: {},
      })
      await setup.flush()
    })

    // 第一个审批反向请求
    await act(async () => {
      writeServer({ jsonrpc: "2.0", id: "request-1", method: "interaction.approval", params: approvalParams(run!, "（第 1/2 个待审批操作）删除 a.txt") })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("需要审批") && frame.includes("1/2"))

    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    expect(approvals.map(item => [item.id, item.decision])).toEqual([["request-1", "approve_once"]])

    // 真实竞态：第二个请求先于第一个的 resolved 事件到达
    await act(async () => {
      writeServer({ jsonrpc: "2.0", id: "request-2", method: "interaction.approval", params: approvalParams(run!, "（第 2/2 个待审批操作）删除 b.txt") })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("2/2"))
    await act(async () => {
      client.emit("event", {
        event_id: crypto.randomUUID(),
        type: "interaction.resolved",
        thread_id: run?.threadId,
        run_id: run?.runId,
        sequence: 2,
        timestamp_ms: Date.now(),
        payload: { request_id: "request-1", type: "approval" },
      })
      await setup.flush()
    })

    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    expect(approvals.map(item => [item.id, item.decision])).toEqual([
      ["request-1", "approve_once"],
      ["request-2", "approve_once"],
    ])
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})

test("审批挂起时 Ctrl+End 滚动当前文件 Diff 而不是底层时间线", async () => {
  const { client, requests, writeServer, controller, adapter } = createSession()
  const diff = [
    "--- /src/scroll.ts",
    "+++ /src/scroll.ts",
    "@@ -1,1 +1,32 @@",
    "-oldValue",
    ...Array.from({ length: 32 }, (_, index) => `+approval-tail-${index + 1}`),
  ].join("\n")
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 100, height: 28 })
      await setup.flush()
      await adapter.dispatch({ type: "submit", value: "滚动审批预览" })
      await setup.flush()
    })
    const run = requests.at(-1)
    expect(run?.message).toBe("滚动审批预览")

    await act(async () => {
      writeServer({
        jsonrpc: "2.0",
        id: "run-started-scroll",
        method: "interaction.approval",
        params: {
          thread_id: run!.threadId,
          run_id: run!.runId,
          timeout_ms: 5_000,
          payload: {
            interrupt_id: "approval-scroll",
            description: "文件变更需要审批",
            requests: JSON.stringify({ action_requests: [{ name: "edit_file", args: { file_path: "/src/scroll.ts" } }] }),
            presentation: {
              kind: "file_diff",
              operation: "edit",
              path: "/src/scroll.ts",
              added_lines: 32,
              removed_lines: 1,
              truncated: false,
              unified_diff: diff,
            },
            decisions: ["approve_once", "reject"],
          },
        },
      })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("文件变更需要审批") && frame.includes("允许一次"))
    expect(setup.captureCharFrame()).not.toContain("approval-tail-32")

    await act(async () => {
      setup.mockInput.pressKey("END", { ctrl: true })
      await setup.flush()
    })
    expect(setup.captureCharFrame()).toContain("approval-tail-32")
    const approvalFrame = setup.captureCharFrame()
    expect(approvalFrame).toContain("PgUp/PgDn 滚动预览")
    expect(approvalFrame).toContain("↑↓")
    expect(approvalFrame).toContain("Enter 确认")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("127x40 长 TODO 的文件审批首屏保留摘要和 Diff", async () => {
  const { client, requests, writeServer, controller, adapter } = createSession()
  const diff = [
    "--- /workspace/src/approval.ts",
    "+++ /workspace/src/approval.ts",
    "@@ -1,1 +1,196 @@",
    "-const before = 1",
    ...Array.from({ length: 196 }, (_, index) => `+visible-diff-line-${index + 1}`),
  ].join("\n")
  const todos = {
    todos: Array.from({ length: 7 }, (_, index) => ({
      content: `阶段 ${index + 1}：${"检查审批前后的工作区变更、快照一致性和回滚边界，保留完整验证证据。".repeat(3)}`,
      status: index === 1 ? "in_progress" : "pending",
    })),
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 127, height: 40, useMouse: true, screenMode: "alternate-screen" })
      await setup.flush()
      await adapter.dispatch({ type: "submit", value: "长 TODO 文件审批" })
      await setup.flush()
    })
    const run = requests.at(-1)
    expect(run?.message).toBe("长 TODO 文件审批")

    await act(async () => {
      const eventBase = {
        thread_id: run!.threadId,
        run_id: run!.runId,
        timestamp_ms: Date.now(),
      }
      client.emit("event", { event_id: "long-todo-run", type: "run.started", sequence: 1, payload: {}, ...eventBase })
      client.emit("event", { event_id: "long-todo-started", type: "tool.started", sequence: 2, payload: { tool_call_id: "todo-long", name: "write_todos" }, ...eventBase })
      client.emit("event", {
        event_id: "long-todo-delta",
        type: "tool.delta",
        sequence: 3,
        payload: { tool_call_id: "todo-long", arguments_delta: JSON.stringify(todos) },
        ...eventBase,
      })
      client.emit("event", {
        event_id: "long-todo-completed",
        type: "tool.completed",
        sequence: 4,
        payload: { tool_call_id: "todo-long", result: { content: "", is_error: false } },
        ...eventBase,
      })
      client.emit("event", {
        event_id: "long-todo-history",
        type: "content.delta",
        sequence: 5,
        payload: { text: Array.from({ length: 36 }, (_, index) => `长时间线历史第 ${index + 1} 行：审批前仍需保留上下文和操作依据`).join("\n") },
        ...eventBase,
      })
      await setup.flush()
    })
    await act(async () => {
      writeServer({
        jsonrpc: "2.0",
        id: "long-todo-approval",
        method: "interaction.approval",
        params: {
          thread_id: run!.threadId,
          run_id: run!.runId,
          timeout_ms: 5_000,
          payload: {
            interrupt_id: "long-todo-approval",
            description: "（第 2/7 个待审批操作）编辑 /workspace/src/approval.ts",
            requests: JSON.stringify({ action_requests: [{ name: "edit_file", args: { file_path: "/workspace/src/approval.ts" } }] }),
            presentation: {
              kind: "file_diff",
              operation: "edit",
              path: "/workspace/src/approval.ts",
              added_lines: 196,
              removed_lines: 1,
              truncated: true,
              unified_diff: diff,
            },
            decisions: ["approve_once", "approve_thread", "approve_project", "reject", "reject_with_feedback"],
          },
        },
      })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("（第 2/7 个待审批操作）") && frame.includes("允许一次"))

    let frame = setup.captureCharFrame()
    const diffRows = frame.split("\n").filter(row => row.includes("visible-diff-line-"))
    expect(frame).toContain("需要审批")
    expect(frame).toContain("编辑文件 · /workspace/src/approval.ts · +196 / -1")
    expect(frame).toContain("visible-diff-line-1")
    expect(frame).toContain("预览已按 200 行或 16 KiB 上限截断；批准仍会应用完整变更。")
    expect(diffRows.length).toBeGreaterThanOrEqual(4)
    expect(diffRows.length).toBeLessThanOrEqual(14)
    expect(frame).toContain("▶ 允许一次")
    expect(frame).toContain("↑↓ 选择 · Enter 确认")
    expect(frame).toContain("v0.1.0")

    await act(async () => {
      setup.mockInput.pressKey("END", { ctrl: true })
      await setup.flush()
    })
    await setup.waitForFrame(nextFrame => nextFrame.includes("visible-diff-line-196"))
    frame = setup.captureCharFrame()
    expect(frame).toContain("编辑文件 · /workspace/src/approval.ts · +196 / -1")
    expect(frame).toContain("visible-diff-line-196")
    expect(frame).toContain("▶ 允许一次")
    expect(frame).toContain("↑↓ 选择 · Enter 确认")
    expect(frame).toContain("v0.1.0")

    await act(async () => {
      setup.mockInput.pressKey("HOME", { ctrl: true })
      await setup.flush()
    })
    await setup.waitForFrame(nextFrame => nextFrame.includes("visible-diff-line-1"))
    frame = setup.captureCharFrame()
    const selectedRow = frame.split("\n").findIndex(row => row.includes("▶ 允许一次"))
    expect(selectedRow).toBeGreaterThanOrEqual(0)

    await act(async () => {
      await setup.mockMouse.scroll(20, selectedRow, "down")
      await setup.flush()
    })
    await setup.waitForFrame(nextFrame => nextFrame.includes("▶ 本会话允许"))
    frame = setup.captureCharFrame()
    expect(frame).toContain("▶ 本会话允许")
    expect(frame).not.toContain("▶ 允许一次")
    expect(frame).toContain("visible-diff-line-1")
    expect(frame).toContain("编辑文件 · /workspace/src/approval.ts · +196 / -1")
    expect(frame).toContain("v0.1.0")

    await act(async () => {
      await setup.mockMouse.scroll(20, selectedRow, "up")
      await setup.flush()
    })
    await setup.waitForFrame(nextFrame => nextFrame.includes("▶ 允许一次"))
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("长 TODO 被钉住时通用审批 Dock 在缩放和时间线滚动后仍完整可操作", async () => {
  const { client, requests, writeServer, controller, adapter } = createSession()
  const todos = {
    todos: Array.from({ length: 9 }, (_, index) => ({
      content: `阶段 ${index + 1}：${"检查审批上下文、执行快照和恢复边界，保留可复核的验证证据。".repeat(3)}`,
      status: index === 2 ? "in_progress" : "pending",
    })),
  }
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 127, height: 24 })
      await setup.flush()
      await adapter.dispatch({ type: "submit", value: "长 TODO 通用执行审批" })
      await setup.flush()
    })
    const run = requests.at(-1)
    expect(run?.message).toBe("长 TODO 通用执行审批")

    await act(async () => {
      const eventBase = {
        thread_id: run!.threadId,
        run_id: run!.runId,
        timestamp_ms: Date.now(),
      }
      client.emit("event", { event_id: "generic-todo-run", type: "run.started", sequence: 1, payload: {}, ...eventBase })
      client.emit("event", { event_id: "generic-todo-started", type: "tool.started", sequence: 2, payload: { tool_call_id: "todo-generic", name: "write_todos" }, ...eventBase })
      client.emit("event", {
        event_id: "generic-todo-delta",
        type: "tool.delta",
        sequence: 3,
        payload: { tool_call_id: "todo-generic", arguments_delta: JSON.stringify(todos) },
        ...eventBase,
      })
      client.emit("event", {
        event_id: "generic-todo-completed",
        type: "tool.completed",
        sequence: 4,
        payload: { tool_call_id: "todo-generic", result: { content: "", is_error: false } },
        ...eventBase,
      })
      client.emit("event", {
        event_id: "generic-todo-reasoning",
        type: "reasoning.delta",
        sequence: 5,
        payload: { text: "正在根据清单检查执行前提。" },
        ...eventBase,
      })
      client.emit("event", {
        event_id: "generic-todo-execute-started",
        type: "tool.started",
        sequence: 6,
        payload: { tool_call_id: "execute-generic", name: "execute" },
        ...eventBase,
      })
      client.emit("event", {
        event_id: "generic-todo-execute-completed",
        type: "tool.completed",
        sequence: 7,
        payload: { tool_call_id: "execute-generic", result: { content: "准备执行审批中的操作。", is_error: false } },
        ...eventBase,
      })
      client.emit("event", {
        event_id: "generic-todo-history",
        type: "content.delta",
        sequence: 8,
        payload: { text: Array.from({ length: 32 }, (_, index) => `审批时间线第 ${index + 1} 行：保留清单上下文和操作依据`).join("\n") },
        ...eventBase,
      })
      await setup.flush()
    })
    await act(async () => {
      writeServer({ jsonrpc: "2.0", id: "generic-todo-approval", method: "interaction.approval", params: approvalParams(run!, "执行命令需要审批") })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("执行命令需要审批"))

    await act(async () => {
      setup.resize(127, 18)
      await setup.flush()
    })
    let frame = setup.captureCharFrame()
    expect(frame).toContain("需要审批")
    expect(frame).toContain("执行命令需要审批")
    expect(frame).toContain("▶ 允许一次")
    expect(frame).toContain("↑↓ 选择 · Enter 确认")
    expect(frame).toContain("v0.1.0")

    await act(async () => {
      setup.mockInput.pressArrow("down")
      await setup.flush()
    })
    frame = setup.captureCharFrame()
    expect(frame).toContain("▶ 本会话允许")

    await act(async () => {
      setup.mockInput.pressKey("j")
      await setup.flush()
    })
    frame = setup.captureCharFrame()
    expect(frame).toContain("▶ 本项目允许")

    await act(async () => {
      setup.mockInput.pressKey("HOME", { ctrl: true })
      await setup.flush()
    })
    frame = setup.captureCharFrame()
    expect(frame).toContain("需要审批")
    expect(frame).toContain("执行命令需要审批")
    expect(frame).toContain("▶ 本项目允许")
    expect(frame).toContain("↑↓ 选择 · Enter 确认")
    expect(frame).toContain("v0.1.0")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("无文件预览的五项审批在受限高度滚动选择且保留底栏", async () => {
  const { client, requests, writeServer, controller, adapter } = createSession()
  const history = Array.from({ length: 32 }, (_, index) => `approval-history-${index + 1}`).join("\n")
  const decisions = ["允许一次", "本会话允许", "本项目允许", "拒绝", "拒绝并反馈"]
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 127, height: 24 })
      await setup.flush()
      await adapter.dispatch({ type: "submit", value: "受限高度五项审批" })
      await setup.flush()
    })
    const run = requests.at(-1)
    expect(run?.message).toBe("受限高度五项审批")

    await act(async () => {
      client.emit("event", {
        event_id: "approval-history",
        type: "content.delta",
        thread_id: run!.threadId,
        run_id: run!.runId,
        sequence: 1,
        timestamp_ms: Date.now(),
        payload: { text: history },
      })
      await setup.flush()
    })
    await act(async () => {
      writeServer({ jsonrpc: "2.0", id: "approval-five-decisions", method: "interaction.approval", params: approvalParams(run!, "五项决定需要审批") })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("五项决定需要审批") && frame.includes("允许一次"))

    let frame = setup.captureCharFrame()
    expect(frame).toContain("▶ 允许一次")
    expect(frame).toContain("↑↓ 选择 · Enter 确认")
    expect(frame).not.toContain("PgUp/PgDn 滚动预览")
    expect(frame).toContain("v0.1.0")

    for (let index = 1; index < decisions.length; index += 1) {
      await act(async () => {
        if (index % 2 === 1) setup.mockInput.pressArrow("down")
        else setup.mockInput.pressKey("j")
        await setup.flush()
      })
      frame = setup.captureCharFrame()
      expect(frame).toContain(`▶ ${decisions[index]}`)
      expect(frame).toContain("v0.1.0")
    }
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("127x40 无文件预览的五项审批用鼠标滚轮只滚动决定选择器", async () => {
  const { client, requests, writeServer, controller, adapter } = createSession()
  const history = [
    "mouse-history-top",
    ...Array.from({ length: 42 }, (_, index) => `mouse-history-middle-${index + 1}`),
    "mouse-history-tail",
  ].join("\n")
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 127, height: 40, useMouse: true })
      await setup.flush()
      await adapter.dispatch({ type: "submit", value: "鼠标滚轮审批选择" })
      await setup.flush()
    })
    const run = requests.at(-1)
    expect(run?.message).toBe("鼠标滚轮审批选择")

    await act(async () => {
      client.emit("event", {
        event_id: "mouse-approval-history",
        type: "content.delta",
        thread_id: run!.threadId,
        run_id: run!.runId,
        sequence: 1,
        timestamp_ms: Date.now(),
        payload: { text: history },
      })
      await setup.flush()
      writeServer({ jsonrpc: "2.0", id: "mouse-five-decisions", method: "interaction.approval", params: approvalParams(run!, "鼠标五项决定") })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("鼠标五项决定") && frame.includes("▶ 允许一次"))
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 75))
      await setup.flush()
    })

    await act(async () => {
      setup.mockInput.pressKey("HOME", { ctrl: true })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("mouse-history-top"))
    let frame = setup.captureCharFrame()
    const selectedRow = frame.split("\n").findIndex(row => row.includes("▶ 允许一次"))
    expect(selectedRow).toBeGreaterThanOrEqual(0)
    expect(frame).toContain("mouse-history-top")
    expect(frame).not.toContain("mouse-history-tail")
    expect(frame).toContain("v0.1.0")

    await act(async () => {
      await setup.mockMouse.scroll(20, selectedRow, "down")
      await setup.flush()
    })
    await setup.waitForFrame(nextFrame => nextFrame.includes("▶ 本会话允许"))
    frame = setup.captureCharFrame()
    expect(frame).toContain("▶ 本会话允许")
    expect(frame).not.toContain("▶ 允许一次")
    expect(frame).toContain("mouse-history-top")
    expect(frame).not.toContain("mouse-history-tail")
    expect(frame).toContain("↑↓ 选择 · Enter 确认")
    expect(frame).toContain("v0.1.0")

    await act(async () => {
      await setup.mockMouse.scroll(20, selectedRow, "up")
      await setup.flush()
    })
    await setup.waitForFrame(nextFrame => nextFrame.includes("▶ 允许一次"))
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("无文件预览的审批滚动键回退到底层时间线", async () => {
  const { client, requests, writeServer, controller, adapter } = createSession()
  const history = [
    "fallback-history-top",
    ...Array.from({ length: 34 }, (_, index) => `fallback-history-middle-${index + 1}`),
    "fallback-history-tail",
  ].join("\n")
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        controller,
        adapter,
        onRequestExit: () => undefined,
      }), { width: 127, height: 40 })
      await setup.flush()
      await adapter.dispatch({ type: "submit", value: "回退时间线滚动" })
      await setup.flush()
    })
    const run = requests.at(-1)
    expect(run?.message).toBe("回退时间线滚动")

    await act(async () => {
      client.emit("event", {
        event_id: "fallback-history",
        type: "content.delta",
        thread_id: run!.threadId,
        run_id: run!.runId,
        sequence: 1,
        timestamp_ms: Date.now(),
        payload: { text: history },
      })
      await setup.flush()
      writeServer({ jsonrpc: "2.0", id: "approval-no-preview-scroll", method: "interaction.approval", params: approvalParams(run!, "没有文件预览") })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("没有文件预览") && frame.includes("允许一次"))
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 75))
      await setup.flush()
    })

    await act(async () => {
      setup.mockInput.pressKey("HOME", { ctrl: true })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("fallback-history-top"))
    let frame = setup.captureCharFrame()
    expect(frame).toContain("fallback-history-top")
    expect(frame).not.toContain("fallback-history-tail")

    await act(async () => {
      setup.mockInput.pressKey("\u001b[6~")
      await setup.flush()
    })
    await setup.waitForFrame(frame => !frame.includes("fallback-history-top"))
    frame = setup.captureCharFrame()
    expect(frame).not.toContain("fallback-history-top")

    await act(async () => {
      setup.mockInput.pressKey("END", { ctrl: true })
      await setup.flush()
    })
    await setup.waitForFrame(frame => frame.includes("fallback-history-tail"))
    expect(setup.captureCharFrame()).toContain("fallback-history-tail")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

test("运行中出现开放式问题时请求滚动到最新问答卡", async () => {
  const { client, requests, writeServer, controller, adapter } = createSession()
  try {
    await adapter.dispatch({ type: "submit", value: "创建 jsondiff" })
    const run = requests.at(-1)
    expect(run).toBeDefined()
    const scrollAfterSubmit = adapter.getSnapshot().scrollRequest

    writeServer({
      jsonrpc: "2.0",
      id: "question-scroll-1",
      method: "interaction.question",
      params: {
        thread_id: run!.threadId,
        run_id: run!.runId,
        timeout_ms: 300_000,
        payload: {
          interrupt_id: "question-scroll-1",
          questions: [{
            id: "task-interview",
            question: "差异输出格式需要确定为哪种？",
            header: "任务澄清",
            body: "",
            options: [],
            multi_select: false,
            allow_other: true,
          }],
        },
      },
    })
    await Bun.sleep(0)

    expect(adapter.getSnapshot().interactive.interaction).toMatchObject({
      type: "question",
      requestId: "question-scroll-1",
    })
    expect(adapter.getSnapshot().scrollRequest).toBeGreaterThan(scrollAfterSubmit)
  } finally {
    client.destroy()
    await adapter.close()
    await controller.close()
  }
})

/** 与 run_coordinator 串行审批相同形状的 wire params。 */
function approvalParams(run: { threadId: string; runId: string }, description: string) {
  return {
    thread_id: run.threadId,
    run_id: run.runId,
    timeout_ms: 5000,
    payload: {
      interrupt_id: "interrupt-1",
      description,
      requests: JSON.stringify({ action_requests: [{ name: "delete_file", args: { file_path: "/a.txt" } }] }),
      decisions: ["approve_once", "approve_thread", "approve_project", "reject", "reject_with_feedback"],
    },
  }
}

async function sendAndFinish(
  setup: Awaited<ReturnType<typeof testRender>>,
  client: AgentClient,
  requests: Array<{ message: string; threadId: string; runId: string; requestedSkill?: { id: string; args?: string }; modelProfile?: string; modelSelection?: { primary_profile: string } }>,
  message: string,
) {
  await act(async () => {
    await setup.mockInput.typeText(message)
    setup.mockInput.pressEnter()
    await setup.flush()
  })
  const run = requests.at(-1)
  expect(run?.message).toBe(message)
  await act(async () => {
    client.emit("event", {
      event_id: crypto.randomUUID(),
      type: "run.completed",
      thread_id: run?.threadId,
      run_id: run?.runId,
      sequence: 1,
      timestamp_ms: Date.now(),
      payload: { duration_ms: 1, usage: { input_tokens: 0, output_tokens: 0 } },
    })
    await setup.flush()
  })
}

/** Composition 语义测试辅助：AgentClient → Controller → TUI Adapter（镜像 index.ts 组合路径）。 */
function createSession(resume = false, onRequestExit: () => void = () => undefined) {
  const { client, requests, approvals, writeServer } = createMockClient()
  const controller = createInteractiveController({
    gateway: new AgentClientGateway(client),
    runtime,
  })
  const adapter = createTuiAdapter({
    controller,
    resume,
    onRequestExit,
  })
  return { client, requests, approvals, writeServer, controller, adapter }
}

function createMockClient() {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const client = new AgentClient(new StdioRpcTransport(stdin, stdout))
  const requests: Array<{ message: string; threadId: string; runId: string; requestedSkill?: { id: string; args?: string }; modelProfile?: string; modelSelection?: { primary_profile: string } }> = []
  const approvals: Array<{ id: string; decision: string }> = []
  const writeServer = (message: Record<string, unknown>) => {
    stdout.write(`${JSON.stringify(message)}\n`)
  }

  stdin.on("data", data => {
    for (const line of data.toString("utf8").split("\n")) {
      if (!line.trim()) continue
      const request = JSON.parse(line) as { id?: string; method?: string; params?: Record<string, unknown>; result?: Record<string, unknown> }
      if (typeof request.id !== "string") continue
      if (request.method === "initialize") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            protocol_version: "v3",
            agent: { name: "test-agent", version: "1.0.0" },
            capabilities: { enabled: ["skills.manage", "models.read", "mcp.manage", "config.manage"] },
            security: { approval_mode: "default" },
            execution: { mode: "local" },
          },
        })}
`)
        continue
      }
      if (request.method === "config.details") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            revision: 1,
            config: { model: { default: "enterprise-model" } },
          },
        })}
`)
        continue
      }
      if (request.method === "models.list") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            profiles: [{ id: "enterprise-model", name: "Enterprise Model" }],
            thread_selection: { primary_profile: "enterprise-model" },
          },
        })}
`)
        continue
      }
      if (!request.method && typeof request.result?.decision === "string") {
        approvals.push({ id: request.id, decision: request.result.decision })
        continue
      }
      if (request.method === "skills.list") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            snapshot: { id: "snapshot", count: 2 },
            skills: [{
              id: "user/repo-review-demo",
              name: "repo-review-demo",
              description: "只读代码审查",
              source: "user",
              enabled: true,
              user_invocable: true,
              argument_hint: "下一条消息使用",
            }, {
              id: "builtin/long-description-demo",
              name: "long-description-demo",
              description: "一条用于验证浮层描述单行截断且不应换行显示的长说明",
              source: "builtin",
              enabled: true,
              user_invocable: true,
            }],
            diagnostics: [],
          },
        })}
`)
        continue
      }
      if (request.method === "threads.list") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            threads: [{
              thread_id: "opaque-thread-1",
              created_at_ms: 1,
              updated_at_ms: 2,
              first_message: "此前的需求",
              latest_message: "此前的回答",
              message_count: 2,
              title: null,
            }, {
              thread_id: "opaque-thread-2",
              created_at_ms: 3,
              updated_at_ms: 4,
              first_message: "修复索引结果",
              latest_message: "需要继续处理索引",
              message_count: 4,
              title: null,
            }],
          },
        })}
`)
        continue
      }
      if (request.method !== "run.start") continue
      const input = request.params?.input
      const message = input && typeof input === "object" && input.kind === "user" && typeof input.message === "string" ? input.message : ""
      const threadId = typeof request.params?.thread_id === "string" ? request.params.thread_id : "thread-1"
      const runId = typeof request.params?.run_id === "string" ? request.params.run_id : "run-1"
      const requestedSkill = input && typeof input === "object" ? input.requested_skill : undefined
      requests.push({
        message,
        threadId,
        runId,
        requestedSkill: requestedSkill && typeof requestedSkill === "object"
          ? requestedSkill as { id: string; args?: string }
          : undefined,
      })
      stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { accepted: true, thread_id: threadId, run_id: runId } })}
`)
    }
  })
  return { client, requests, approvals, writeServer }
}
