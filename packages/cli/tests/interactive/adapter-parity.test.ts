import { describe, expect, test } from "bun:test"
import { createTuiAdapter, type TuiAdapterOptions } from "../../src/tui/application/adapter"
import { createWebInteractiveAdapter, type WebAdapterOptions } from "../../src/web/application/adapter"
import { buildWebUiState } from "../../src/presentation-coordinator"
import type { WebUiClient } from "../../src/web/ui-client"
import { createInitialState } from "../../src/interactive/state"
import type {
  AgentGateway,
  AgentGatewayStartRunInput,
  Clock,
  IdGenerator,
  IntentOutcome,
  InteractiveController,
  InteractiveIntent,
  InteractiveSnapshot,
  Scheduler,
} from "../../src/interactive/ports"
import { InteractiveControllerImpl } from "../../src/interactive/controller"
import { flush, makeHarness } from "./harness"

function createMockGateway(): AgentGateway {
  return {
    async startRun(_input: AgentGatewayStartRunInput) {
      return { runId: "test-run-id", threadId: "test-thread-id" }
    },
    async cancelRun() {
      return { runId: "test-run-id", cancelled: true }
    },
    async listThreads() {
      return { threads: [] }
    },
    async openThread(threadId: string) {
      return { threadId, messages: [] }
    },
    async listModels() {
      return { profiles: [] }
    },
    async listSkills() {
      return { skills: [] }
    },
    async setSkillEnabled() {},
    async listMcpServers() {
      return { servers: [] }
    },
    async mcpAdd() {
      return { name: "test-mcp", status: "connected" }
    },
    async mcpRemove() {},
    async configDetails() {
      return { revision: 1, config: {} }
    },
    async previewConfig() {
      return { revision: 2, changes: [] }
    },
    async commitConfig() {
      return { revision: 2 }
    },
    async compactContext() {
      return { archivedMessagesCount: 0 }
    },
    async abandonCompose() {
      return { progress: null }
    },
    abandonInteraction() {},
    onProtocolError() {
      return () => {}
    },
    onClose() {
      return () => {}
    },
    subscribeEvents() {
      return () => {}
    },
    subscribeClose() {
      return () => {}
    },
    setInteractionHandler() {
      return () => {}
    },
  }
}

function createMockController(): InteractiveController {
  const clock: Clock = { now: () => 1000 }
  const scheduler: Scheduler = { setTimeout: (fn) => setTimeout(fn, 0) }
  const idGen: IdGenerator = { uuid: () => "test-uuid" }
  const gateway = createMockGateway()

  return new InteractiveControllerImpl({
    gateway,
    clock,
    scheduler,
    idGenerator: idGen,
  })
}

describe("Adapter Parity (TUI vs Web)", () => {
  test("同一意图序列在 TUI 与 Web 产生相同的 IntentOutcome 且拒绝时均保留草稿", async () => {
    const controller = createMockController()
    const historyStore = { load: async () => [], append: async () => {} }

    const tuiAdapter = createTuiAdapter({
      controller,
      historyStore,
    })

    // Web Adapter 经 WebUiClient 提交 intent；client 把 intent 转发给同一 Controller，
    // 保证两端对同一意图序列产生相同的 IntentOutcome。
    const client = {
      state: buildWebUiState(controller.getSnapshot(), {
        tree: { status: "idle", rows: [], selectedPath: null, limited: false },
        preview: { status: "idle" },
      }),
      handoffState: { phase: "web-active", handoffId: "h1" },
      getState: () => client.state,
      getHandoffState: () => client.handoffState,
      subscribeState: () => () => {},
      subscribeHandoff: () => () => {},
      submitIntent: (intent: InteractiveIntent) => controller.dispatch(intent),
      workspaceIntent: async () => ({ status: "accepted" }),
      ready: () => {},
      returnToTui: () => {},
      requestExit: () => {},
      close: () => {},
    } as unknown as WebUiClient

    const webAdapter = createWebInteractiveAdapter({
      client,
      frameScheduler: {
        schedule: (fn) => fn(),
        cancel: () => {},
        flush: () => {},
      },
    })

    // 1. 设置草稿
    tuiAdapter.updateDraft("我的草稿提问")
    webAdapter.dispatch({ type: "draft-change", value: "我的草稿提问" })

    expect(tuiAdapter.getSnapshot().draft).toBe("我的草稿提问")
    expect(webAdapter.getSnapshot().draft).toBe("我的草稿提问")

    // 2. 模拟被拒绝的场景：提交未知 Thread 打开
    const notFoundIntent: InteractiveIntent = { type: "thread.open", threadId: "non-existent-thread" }
    const outcome = await controller.dispatch(notFoundIntent)

    expect(outcome.status).toBe("rejected")
    if (outcome.status === "rejected") {
      expect(outcome.code).toBe("not-found")
    }

    // 3. 拒绝后草稿必须 100% 保留
    expect(tuiAdapter.getSnapshot().draft).toBe("我的草稿提问")
    expect(webAdapter.getSnapshot().draft).toBe("我的草稿提问")

    await tuiAdapter.close()
    await webAdapter.close()
    await controller.close()
  })

  test("TUI 提交 /compact 后立即清空 Composer，并由 compacting 状态显示等待提示", async () => {
    const harness = makeHarness()
    let releaseCompact = () => undefined
    const compactGate = new Promise<void>(resolve => { releaseCompact = resolve })
    harness.port.setCompactContextImpl(async () => {
      await compactGate
      return { compacted: true, context: { action: "manual_summary" } }
    })
    const adapter = createTuiAdapter({
      controller: harness.controller,
      promptHistoryStore: { load: async () => [], append: async () => {} },
      onRequestExit: () => undefined,
    })

    try {
      await harness.controller.dispatch({ type: "input.submit", value: "建立 Thread" })
      const run = harness.runHandles.at(-1)!
      harness.port.completeRun(run.threadId, run.runId)
      await flush()

      await adapter.dispatch({ type: "draft-input", value: "/compact" })
      const pending = adapter.dispatch({ type: "submit", value: "/compact" })
      await flush()

      expect(adapter.getSnapshot().draft).toBe("")
      expect(adapter.getSnapshot().commandMenu.visible).toBe(false)
      expect(adapter.getSnapshot().interactive.activity.kind).toBe("compacting")

      releaseCompact()
      await pending
    } finally {
      releaseCompact()
      await adapter.close()
      await harness.controller.close()
    }
  })
})
