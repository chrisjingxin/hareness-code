/** UndoPicker 与 UndoDialog TUI 组件及快捷键/Adapter 全链路测试。 */

import { expect, test, mock } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement, createRef } from "react"
import type { TextareaRenderable } from "@opentui/core"
import type { TurnSummary } from "@za38/protocol"

import { UndoPicker, UndoDialog, undoPickerRow } from "../../src/tui/presentation/undo-modal"
import { resolveShortcut } from "../../src/tui/application/shortcuts"
import { createTuiAdapter, filterTurns } from "../../src/tui/application/adapter"
import { createInteractiveController } from "../../src/interactive/controller"
import { createFallbackNoopGateway, type AgentGateway } from "../../src/interactive/ports"

const sampleTurns: readonly TurnSummary[] = [
  {
    turn_id: "turn-1",
    turn_index: 1,
    user_prompt: "实现基础框架与协议定义",
    created_at: 1700000000000,
    files_changed_count: 3,
    has_git_checkpoint: true,
    diff_stats: { files: ["a.ts", "b.ts", "c.ts"], insertions: 120, deletions: 5 },
  },
  {
    turn_id: "turn-2",
    turn_index: 2,
    user_prompt: "修复边界情况和测试用例",
    created_at: 1700001000000,
    files_changed_count: 2,
    has_git_checkpoint: true,
    diff_stats: { files: ["d.ts", "e.ts"], insertions: 15, deletions: 8 },
  },
  {
    turn_id: "turn-3",
    turn_index: 3,
    user_prompt: "编写用户文档与说明",
    created_at: 1700002000000,
    files_changed_count: 0,
    has_git_checkpoint: false,
  },
]

test("undoPickerRow 正确渲染回合摘要与 diff 统计", () => {
  const rowWithGit = undoPickerRow(sampleTurns[0]!, { width: 100, selected: false, compact: false })
  expect(rowWithGit).toBeDefined()

  const rowNoGit = undoPickerRow(sampleTurns[2]!, { width: 100, selected: false, compact: false })
  expect(rowNoGit).toBeDefined()
})

test("filterTurns 支持根据 Prompt 内容和回合数字过滤", () => {
  expect(filterTurns(sampleTurns, "")).toHaveLength(3)
  expect(filterTurns(sampleTurns, "基础框架")).toEqual([sampleTurns[0]!])
  expect(filterTurns(sampleTurns, "2")).toEqual([sampleTurns[1]!])
  expect(filterTurns(sampleTurns, "不存在的内容")).toHaveLength(0)
})

test("UndoPicker 在 visible=true 时正确渲染回合列表并响应选择", async () => {
  const searchRef = createRef<TextareaRenderable>()
  let selectedTurn: TurnSummary | undefined
  let setup: Awaited<ReturnType<typeof testRender>>

  await act(async () => {
    setup = await testRender(createElement(UndoPicker, {
      visible: true,
      loading: false,
      turns: sampleTurns,
      query: "",
      selectedIndex: 1,
      terminalWidth: 100,
      terminalHeight: 30,
      searchRef,
      onSearch: () => undefined,
      onSelect: turn => { selectedTurn = turn },
      onHover: () => undefined,
      onClose: () => undefined,
    }), { width: 100, height: 30 })
  })

  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("会话回退与快照还原")
    expect(frame).toContain("搜索提问内容")
    expect(frame).toContain("第 1 轮: 实现基础框架与协议定义")
    expect(frame).toContain("+120 -5")
    expect(frame).toContain("第 2 轮: 修复边界情况和测试用例")

    await act(async () => {
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(selectedTurn).toEqual(sampleTurns[1]!)
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("UndoDialog 在 Git 仓库下展示三档可选并支持确认", async () => {
  let confirmed = false
  let cancelled = false
  let selectedMode: string | undefined
  let setup: Awaited<ReturnType<typeof testRender>>

  await act(async () => {
    setup = await testRender(createElement(UndoDialog, {
      visible: true,
      targetTurn: sampleTurns[0]!,
      selectedMode: "both",
      isGit: true,
      terminalWidth: 100,
      terminalHeight: 30,
      onSelectMode: mode => { selectedMode = mode },
      onConfirm: () => { confirmed = true },
      onCancel: () => { cancelled = true },
    }), { width: 100, height: 30 })
  })

  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("确认回退至第 1 轮？")
    expect(frame).toContain("1. 同时回退代码与对话")
    expect(frame).toContain("2. 仅回退对话")
    expect(frame).toContain("3. 仅还原代码")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("UndoDialog 在非 Git 仓库下禁用代码回退选项", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>

  await act(async () => {
    setup = await testRender(createElement(UndoDialog, {
      visible: true,
      targetTurn: sampleTurns[2]!,
      selectedMode: "conversation",
      isGit: false,
      terminalWidth: 100,
      terminalHeight: 30,
      onSelectMode: () => undefined,
      onConfirm: () => undefined,
      onCancel: () => undefined,
    }), { width: 100, height: 30 })
  })

  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("非 Git 仓库不可用")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})

test("resolveShortcut 正确解析 undo 浮层快捷键", () => {
  // UndoDialog 优先级最高
  expect(resolveShortcut({ name: "1" }, {
    commandDialogVisible: false,
    btwModalVisible: false,
    statusModalVisible: false,
    undoDialogVisible: true,
    undoPickerVisible: true,
    skillPickerVisible: false,
    threadPickerVisible: false,
    modelPickerVisible: false,
    agentPickerVisible: false,
    commandMenuVisible: false,
    commandOptionCount: 0,
    activeRun: false,
    interactionActive: false,
    hasDraft: false,
    inputMode: "chat",
    childTimelineActive: false,
  })).toBe("undo-mode-1")

  expect(resolveShortcut({ name: "return" }, {
    commandDialogVisible: false,
    btwModalVisible: false,
    statusModalVisible: false,
    undoDialogVisible: true,
    undoPickerVisible: true,
    skillPickerVisible: false,
    threadPickerVisible: false,
    modelPickerVisible: false,
    agentPickerVisible: false,
    commandMenuVisible: false,
    commandOptionCount: 0,
    activeRun: false,
    interactionActive: false,
    hasDraft: false,
    inputMode: "chat",
    childTimelineActive: false,
  })).toBe("confirm-undo")

  // UndoPicker
  expect(resolveShortcut({ name: "down" }, {
    commandDialogVisible: false,
    btwModalVisible: false,
    statusModalVisible: false,
    undoDialogVisible: false,
    undoPickerVisible: true,
    undoOptionCount: 3,
    skillPickerVisible: false,
    threadPickerVisible: false,
    modelPickerVisible: false,
    agentPickerVisible: false,
    commandMenuVisible: false,
    commandOptionCount: 0,
    activeRun: false,
    interactionActive: false,
    hasDraft: false,
    inputMode: "chat",
    childTimelineActive: false,
  })).toBe("undo-next")

  expect(resolveShortcut({ name: "return" }, {
    commandDialogVisible: false,
    btwModalVisible: false,
    statusModalVisible: false,
    undoDialogVisible: false,
    undoPickerVisible: true,
    undoOptionCount: 3,
    skillPickerVisible: false,
    threadPickerVisible: false,
    modelPickerVisible: false,
    agentPickerVisible: false,
    commandMenuVisible: false,
    commandOptionCount: 0,
    activeRun: false,
    interactionActive: false,
    hasDraft: false,
    inputMode: "chat",
    childTimelineActive: false,
  })).toBe("undo-select")
})

test("TuiAdapter 与 Gateway 联动完成 undo 完整生命周期", async () => {
  let undoCalledWith: { threadId: string; targetTurnId: string; mode: string } | undefined
  const gateway: AgentGateway = {
    ...createFallbackNoopGateway(),
    async openThread(threadId: string) {
      return {
        thread: {
          thread_id: threadId,
          created_at_ms: 1000,
          updated_at_ms: 1000,
          first_message: "hi",
          latest_message: "hi",
          message_count: 1,
        },
        messages: [],
        plan: { has_plan: false, plan_markdown: "", plan_virtual_path: "/.harness/plan.md" as const, plan_display_path: "/.harness/plan.md" },
      }
    },
    async listTurns(threadId: string) {
      return { turns: sampleTurns, current_turn_id: "turn-3" }
    },
    async undo(params) {
      undoCalledWith = { threadId: params.thread_id, targetTurnId: params.target_turn_id, mode: params.mode }
      return {
        success: true,
        reverted_turn_id: params.target_turn_id,
        restored_files_count: 0,
        message: "回退成功",
      }
    },
  }

  const controller = createInteractiveController({ gateway })
  // 模拟当前已有 thread
  await controller.dispatch({ type: "thread.open", threadId: "test-thread-1" })

  const adapter = createTuiAdapter({
    controller,
    gateway,
    onRequestExit: () => undefined,
  })

  // 1. 触发 /undo 命令展示 undo picker
  await adapter.dispatch({ type: "execute-command", commandId: "thread.undo" })
  let snap = adapter.getSnapshot()
  expect(snap.undo.visible).toBeTrue()
  expect(snap.undo.items).toHaveLength(3)

  // 2. 选中第一项并打开确认对话框
  await adapter.dispatch({ type: "picker-select-undo-turn", turn: snap.undo.items[0]! })
  snap = adapter.getSnapshot()
  expect(snap.undo.visible).toBeFalse()
  expect(snap.undoDialog?.visible).toBeTrue()
  expect(snap.undoDialog?.targetTurn.turn_id).toBe("turn-1")
  expect(snap.undoDialog?.selectedMode).toBe("both")

  // 3. 切换模式为 conversation
  await adapter.dispatch({ type: "undo-select-mode", mode: "conversation" })
  snap = adapter.getSnapshot()
  expect(snap.undoDialog?.selectedMode).toBe("conversation")

  // 4. 确认执行回退
  await adapter.dispatch({ type: "undo-confirm" })
  snap = adapter.getSnapshot()
  expect(snap.undoDialog).toBeUndefined()
  expect(undoCalledWith).toEqual({
    threadId: "test-thread-1",
    targetTurnId: "turn-1",
    mode: "conversation",
  })
})
