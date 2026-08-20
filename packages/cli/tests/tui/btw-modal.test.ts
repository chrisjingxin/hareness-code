/** BTW 临时问答浮层组件与快捷键生命周期测试。 */

import { expect, test, mock, spyOn } from "bun:test"
import type { MouseEvent } from "@opentui/core"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"
import { resolveShortcut } from "../../src/tui/application/shortcuts"
import { createTuiAdapter } from "../../src/tui/application/adapter"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway, type AgentGateway } from "../../src/interactive/ports"
import { BtwModal, handleBtwCopyMouseUp } from "../../src/tui/presentation/btw-modal"
import * as clipboard from "../../src/tui/platform/clipboard"

test("真实文本拖选结束在 BTW 复制按钮上时让事件冒泡到选区复制，不复制整段回答", () => {
  const onCopy = mock(() => {})
  const stopPropagation = mock(() => {})

  handleBtwCopyMouseUp(
    { isDragging: true, stopPropagation } as Pick<MouseEvent, "isDragging" | "stopPropagation">,
    true,
    onCopy,
  )

  expect(onCopy).not.toHaveBeenCalled()
  expect(stopPropagation).not.toHaveBeenCalled()
})

test("普通点击 BTW 复制按钮时即使 OpenTUI 标记 isDragging 也执行原有回答复制", () => {
  const onCopy = mock(() => {})
  const stopPropagation = mock(() => {})

  handleBtwCopyMouseUp(
    { isDragging: true, stopPropagation } as Pick<MouseEvent, "isDragging" | "stopPropagation">,
    false,
    onCopy,
  )

  expect(stopPropagation).toHaveBeenCalledTimes(1)
  expect(onCopy).toHaveBeenCalledTimes(1)
})

test("真实 OpenTUI 点击 BTW 复制按钮时会调用回答复制", async () => {
  const onCopy = mock(() => {})
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(BtwModal, {
        visible: true,
        question: "什么是抽象语法树？",
        answer: "抽象语法树是代码结构的树形表示。",
        status: "ready",
        terminalWidth: 100,
        terminalHeight: 28,
        onClose: () => {},
        onCopy,
      }), { width: 100, height: 28, useMouse: true })
    })
    await act(async () => { await setup.flush() })

    const lines = setup.captureCharFrame().split("\n")
    const buttonY = lines.findIndex(line => line.includes("复制"))
    const spans = buttonY < 0 ? [] : setup.captureSpans().lines[buttonY]!.spans
    const buttonSpanIndex = spans.findIndex(span => span.text.includes("复制"))
    const buttonX = spans.slice(0, buttonSpanIndex).reduce((offset, span) => offset + span.width, 0)
    if (buttonSpanIndex < 0 || buttonY < 0) throw new Error("未找到 BTW 复制按钮")

    await act(async () => {
      await setup.mockMouse.click(buttonX, buttonY)
      await setup.flush()
    })
    expect(onCopy).toHaveBeenCalledTimes(1)
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})

test("快捷键在 BTW 浮层打开时优先拦截：Esc/Enter 关闭，c 复制，其他按键静默", () => {
  const context = {
    btwModalVisible: true,
    commandMenuVisible: false,
    commandOptionCount: 0,
    activeRun: false,
    hasDraft: false,
  }

  expect(resolveShortcut({ name: "escape", ctrl: false }, context)).toBe("close-btw-modal")
  expect(resolveShortcut({ name: "return", ctrl: false }, context)).toBe("close-btw-modal")
  expect(resolveShortcut({ name: "kpenter", ctrl: false }, context)).toBe("close-btw-modal")
  expect(resolveShortcut({ name: "c", ctrl: false }, context)).toBe("copy-btw-answer")
  expect(resolveShortcut({ name: "a", ctrl: false }, context)).toBe("none")
  expect(resolveShortcut({ name: "c", ctrl: true }, context)).toBe("none")
})

test("Adapter 接收 /btw 命令后打开浮层，异步调用 sideQuestion 并更新状态", async () => {
  const sideQuestionMock = mock(async (params: { thread_id: string; question: string }) => {
    return {
      reply_text: `解答：${params.question}`,
      model_profile_id: "test-model-profile",
    }
  })

  const customGateway: AgentGateway = {
    ...createFallbackNoopGateway(),
    sideQuestion: sideQuestionMock,
  }

  const controller = createInteractiveController({
    gateway: customGateway,
  })

  const adapter = createTuiAdapter({
    controller,
    gateway: customGateway,
    onRequestExit: () => {},
  })

  expect(adapter.getSnapshot().btw.visible).toBe(false)

  await adapter.dispatch({
    type: "execute-command",
    commandId: "assist.btw",
    argument: "什么是抽象语法树？",
  })

  const loadingSnapshot = adapter.getSnapshot()
  expect(loadingSnapshot.btw.visible).toBe(true)
  expect(loadingSnapshot.btw.question).toBe("什么是抽象语法树？")

  // 等待微任务异步完成
  await new Promise(resolve => setTimeout(resolve, 20))

  const readySnapshot = adapter.getSnapshot()
  expect(readySnapshot.btw.visible).toBe(true)
  expect(readySnapshot.btw.status).toBe("ready")
  expect(readySnapshot.btw.answer).toBe("解答：什么是抽象语法树？")
  expect(readySnapshot.btw.modelProfileId).toBe("test-model-profile")
  expect(sideQuestionMock).toHaveBeenCalledTimes(1)

  // 关闭浮层
  await adapter.dispatch({ type: "btw-close" })
  expect(adapter.getSnapshot().btw.visible).toBe(false)
})

test("sideQuestion 失败时浮层状态更新为 error", async () => {
  const customGateway: AgentGateway = {
    ...createFallbackNoopGateway(),
    sideQuestion: async () => {
      throw new Error("RPC network timeout")
    },
  }

  const controller = createInteractiveController({
    gateway: customGateway,
  })

  const adapter = createTuiAdapter({
    controller,
    gateway: customGateway,
    onRequestExit: () => {},
  })

  await adapter.dispatch({
    type: "execute-command",
    commandId: "assist.btw",
    argument: "测试失败问题",
  })

  await new Promise(resolve => setTimeout(resolve, 20))

  const errorSnapshot = adapter.getSnapshot()
  expect(errorSnapshot.btw.visible).toBe(true)
  expect(errorSnapshot.btw.status).toBe("error")
  expect(errorSnapshot.btw.error).toContain("RPC network timeout")
})

test("触发复制动作后推入右上角 Toast 气泡通知，不污染背景 transientNotice", async () => {
  const copyToClipboard = spyOn(clipboard, "copyToClipboard").mockResolvedValue(true)
  const customGateway: AgentGateway = {
    ...createFallbackNoopGateway(),
    sideQuestion: async () => ({
      reply_text: "可复制的回答内容",
      model_profile_id: "test-model",
    }),
  }

  const controller = createInteractiveController({ gateway: customGateway })
  const adapter = createTuiAdapter({ controller, gateway: customGateway, onRequestExit: () => {} })

  await adapter.dispatch({
    type: "execute-command",
    commandId: "assist.btw",
    argument: "测试复制",
  })
  await new Promise(resolve => setTimeout(resolve, 20))

  expect(adapter.getSnapshot().btw.status).toBe("ready")
  expect(adapter.getSnapshot().toasts.length).toBe(0)
  expect(adapter.getSnapshot().transientNotice).toBeUndefined()

  await adapter.dispatch({ type: "btw-copy" })

  expect(adapter.getSnapshot().toasts.length).toBe(1)
  expect(adapter.getSnapshot().toasts[0].variant).toBe("success")
  expect(adapter.getSnapshot().toasts[0].message).toBe("已复制到系统剪贴板")
  expect(adapter.getSnapshot().transientNotice).toBeUndefined()
  copyToClipboard.mockRestore()
})
