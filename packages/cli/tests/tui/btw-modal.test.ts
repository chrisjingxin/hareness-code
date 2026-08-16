/** BTW 临时问答浮层组件与快捷键生命周期测试。 */

import { expect, test, mock } from "bun:test"
import { resolveShortcut } from "../../src/tui/application/shortcuts"
import { createTuiAdapter } from "../../src/tui/application/adapter"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway, type AgentGateway } from "../../src/interactive/ports"

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
})
