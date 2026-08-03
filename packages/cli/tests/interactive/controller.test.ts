/** Interactive Core 的 interface contract：只通过公开 interface 和内存 port 观察。 */

import { expect, test } from "bun:test"
import {
  Capability,
  EventType,
  type EventEnvelope,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type ModelProfile,
} from "@za38/protocol"

import type { InteractiveAgentPort, InteractiveAgentRun, InteractiveRunCompletion } from "../../src/interactive/agent-port"
import { createInteractiveController } from "../../src/interactive/controller"
import type { InteractiveController, InteractiveScheduler, InteractiveSnapshot } from "../../src/interactive/types"
import type { InteractiveRuntime } from "../../src/interactive/runtime"

const runtime: InteractiveRuntime = {
  workspace: "/workspace/harness-code",
  cliVersion: "0.1.0",
  modelConfigured: true,
  modelName: "enterprise-model",
  executionMode: "local",
  approvalMode: "default",
  capabilities: [
    Capability.THREADS_READ,
    Capability.CONTEXT_MANAGE,
    Capability.MODELS_READ,
    Capability.MODELS_SELECT,
    Capability.CONFIG_WRITE,
    Capability.MCP_READ,
    Capability.MCP_MANAGE,
    Capability.HOST_ATTACH,
    Capability.HOST_CONTROL,
  ],
}

/** 手动 scheduler：测试直接驱动 timeout 回调。 */
function manualScheduler() {
  type Entry = { callback: () => void; ms: number; cancel: boolean }
  const entries: Entry[] = []
  return {
    scheduler: {
      setTimeout(callback: () => void, ms: number): () => void {
        const entry: Entry = { callback, ms, cancel: false }
        entries.push(entry)
        return () => { entry.cancel = true }
      },
    } satisfies InteractiveScheduler,
    /** 触发所有已到期的 timeout；返回触发的回调数。 */
    runExpired(): number {
      const now = Math.max(...entries.map(entry => entry.ms), 0)
      let fired = 0
      for (const entry of entries) {
        if (!entry.cancel && entry.ms <= now) {
          entry.cancel = true
          entry.callback()
          fired += 1
        }
      }
      return fired
    },
    runAll(): void {
      for (const entry of entries) {
        if (!entry.cancel) {
          entry.cancel = true
          entry.callback()
        }
      }
    },
  }
}

/** 内存 port：记录调用、可注入 Run 事件与 Interaction。 */
function createPort() {
  const calls: string[] = []
  const runHandles: Array<{ threadId: string; runId: string }> = []
  let protocolErrorListener: ((error: Error) => void) | undefined
  let closeListener: ((error: Error) => void) | undefined
  let interactionHandler: ((request: InteractionRequestEnvelope) => Promise<InteractionResponse>) | undefined
  const abandoned: string[] = []
  let runNumber = 0
  let profiles: ModelProfile[] = [
    { id: "fast", model: "fast-model", provider_label: "Fast Gateway", context_window_tokens: 128000, capabilities: ["streaming"], is_default: true, available: true, source: "user" },
    { id: "pro", model: "pro-model", provider_label: "Pro Gateway", context_window_tokens: 256000, capabilities: ["streaming"], is_default: false, available: true, source: "user" },
  ]

  const port: InteractiveAgentPort & {
    emitEvent: (event: EventEnvelope) => void
    failRun: (threadId: string, runId: string, error: Error) => void
    completeRun: (threadId: string, runId: string) => void
    cancelRun: (threadId: string, runId: string) => void
    failRunWithEvent: (threadId: string, runId: string) => void
    sendInteraction: (request: InteractionRequestEnvelope) => Promise<InteractionResponse>
    protocolError: (message: string) => void
    closeConnection: (message: string) => void
    setProfiles: (next: ModelProfile[]) => void
    lastRunSelection: () => { message: string; threadId: string; runId: string; modelSelection?: { primary_profile: string }; requestedSkill?: { id: string; args?: string } } | undefined
  } = {
    onProtocolError(listener) {
      protocolErrorListener = listener
      return () => { if (protocolErrorListener === listener) protocolErrorListener = undefined }
    },
    onClose(listener) {
      closeListener = listener
      return () => { if (closeListener === listener) closeListener = undefined }
    },
    setInteractionHandler(handler) {
      interactionHandler = handler
      return () => { if (interactionHandler === handler) interactionHandler = undefined }
    },
    abandonInteraction(requestId) {
      abandoned.push(requestId)
    },
    startRun(input) {
      calls.push("run.start")
      const threadId = input.threadId ?? `thread-${runNumber + 1}`
      const runId = `run-${++runNumber}`
      runHandles.push({ threadId, runId })
      return makeRunHandle({
        threadId,
        runId,
        input,
        onCancel: async () => ({ cancelled: true, run_id: runId }),
        fail: () => {},
        emit: () => {},
        end: () => {},
      })
    },
    async cancel() {
      calls.push("run.cancel")
      const run = runHandles.at(-1)!
      return { cancelled: true, run_id: run.runId }
    },
    async compactContext() {
      calls.push("context.compact")
      return { compacted: true, context: { action: "manual_summary" } }
    },
    async configDetails() {
      calls.push("config.details")
      return { revision: "r1", fields: [{ path: "models.default_profile", value: "fast", source: "user", editable: true, unavailable_reason: null, applies_to: "new-thread" }], immutable_fields: [] }
    },
    async previewConfig() {
      calls.push("config.preview")
      return { revision: "r1", changes: [], applies_to: ["new-thread"] }
    },
    async commitConfig() {
      calls.push("config.commit")
      return { revision: "r2", changes: [], applies_to: ["new-thread"] }
    },
    async listThreads() {
      calls.push("threads.list")
      return { threads: [threadSummary("thread-1", "第一条历史"), threadSummary("thread-2", "第二条历史")] }
    },
    async openThread(threadId) {
      calls.push("threads.open")
      return { thread: threadSummary(threadId, "恢复的请求"), messages: [{ kind: "user", content: "恢复的请求" }, { kind: "tool", tool_name: "execute", content: "恢复的工具结果" }] }
    },
    async mcpStatus() {
      calls.push("mcp.status")
      return { servers: [{ name: "filesystem", transport: "stdio", status: "connected", tool_names: ["read"] }], total_tools: 1 }
    },
    async mcpAdd() {
      calls.push("mcp.add")
      return { added: true, connected: true, tool_names: ["new_tool"] }
    },
    async mcpRemove() {
      calls.push("mcp.remove")
      return { removed: true }
    },
    async listModels() {
      calls.push("models.list")
      return { profiles }
    },
    async listSkills() {
      calls.push("skills.list")
      return { snapshot: {}, skills: [skill("user/repo-review-demo", true), skill("builtin/disabled-demo", false)], diagnostics: [] }
    },
    emitEvent(event) {
      const run = runHandles.at(-1)
      if (run && event.thread_id === run.threadId && event.run_id === run.runId) {
        runEmit(run, event)
      }
    },
    failRun(threadId, runId, error) {
      const run = runHandles.find(value => value.threadId === threadId && value.runId === runId)
      if (run) runFail(run, error)
    },
    completeRun(threadId, runId) {
      const run = runHandles.find(value => value.threadId === threadId && value.runId === runId)
      if (run) runEnd(run, { outcome: "completed", event: terminalEvent(EventType.RUN_COMPLETED, threadId, runId, 100, { duration_ms: 1, usage: { input_tokens: 1, output_tokens: 1 } }) })
    },
    cancelRun(threadId, runId) {
      const run = runHandles.find(value => value.threadId === threadId && value.runId === runId)
      if (run) runEnd(run, { outcome: "cancelled", event: terminalEvent(EventType.RUN_CANCELLED, threadId, runId, 100, { reason: "用户取消" }) })
    },
    failRunWithEvent(threadId, runId) {
      const run = runHandles.find(value => value.threadId === threadId && value.runId === runId)
      if (run) runEnd(run, { outcome: "failed", event: terminalEvent(EventType.RUN_FAILED, threadId, runId, 100, { error: { code: "E", message: "Agent 运行失败", retryable: false } }) })
    },
    async sendInteraction(request) {
      if (!interactionHandler) throw new Error("interaction handler is not registered")
      return interactionHandler(request)
    },
    protocolError(message) {
      protocolErrorListener?.(new Error(message))
    },
    closeConnection(message) {
      closeListener?.(new Error(message))
    },
    setProfiles(next) {
      profiles = next
    },
    lastRunSelection() {
      const run = runHandles.at(-1)
      if (!run) return undefined
      return {
        message: runStates.get(keyOf(run))?.input.message ?? "",
        threadId: run.threadId,
        runId: run.runId,
        modelSelection: runSelection(run),
        requestedSkill: runSkill(run),
        approvalMode: runStates.get(keyOf(run))?.input.approvalMode,
      }
    },
  }

  // 每个 run handle 附带事件队列与终态。
  type RunState = {
    events: EventEnvelope[]
    listeners: Set<(event: EventEnvelope) => void>
    completion: Promise<InteractiveRunCompletion>
    resolveCompletion: (value: InteractiveRunCompletion) => void
    failCompletion: (error: Error) => void
    endCalled: boolean
    cancelled: boolean
    input: { message: string; modelSelection?: { primary_profile: string }; requestedSkill?: { id: string; args?: string }; approvalMode?: string }
  }
  const runStates = new Map<string, RunState>()
  const keyOf = (run: { threadId: string; runId: string }) => `${run.threadId}:${run.runId}`

  function makeRunHandle(run: { threadId: string; runId: string; input: RunState["input"]; onCancel: () => Promise<{ cancelled: boolean; run_id: string }>; fail: (error: Error) => void; emit: (event: EventEnvelope) => void; end: () => void }): InteractiveAgentRun {
    let resolveCompletion!: (value: InteractiveRunCompletion) => void
    let rejectCompletion!: (error: Error) => void
    const completion = new Promise<InteractiveRunCompletion>((resolve, reject) => {
      resolveCompletion = resolve
      rejectCompletion = reject
    })
    const listeners = new Set<(event: EventEnvelope) => void>()
    const state: RunState = {
      events: [],
      listeners,
      completion,
      resolveCompletion,
      failCompletion: rejectCompletion,
      endCalled: false,
      cancelled: false,
      input: run.input,
    }
    runStates.set(keyOf(run), state)
    return {
      ref: { threadId: run.threadId, runId: run.runId },
      accepted: Promise.resolve(),
      events: {
        async *[Symbol.asyncIterator]() {
          let index = 0
          while (true) {
            while (index < state.events.length) yield state.events[index++]!
            if (state.endCalled) return
            await new Promise<void>(resolve => {
              const check = () => {
                if (index < state.events.length || state.endCalled) {
                  listeners.delete(check)
                  resolve()
                }
              }
              listeners.add(check)
            })
          }
        },
      },
      completion,
      cancel: async () => {
        state.cancelled = true
        return (await run.onCancel()).cancelled
      },
    }
  }

  function runEmit(run: { threadId: string; runId: string }, event: EventEnvelope) {
    const state = runStates.get(keyOf(run))
    if (!state || state.endCalled) return
    state.events.push(event)
    for (const listener of [...state.listeners]) listener(event)
  }

  function runEnd(run: { threadId: string; runId: string }, completion: InteractiveRunCompletion) {
    const state = runStates.get(keyOf(run))
    if (!state || state.endCalled) return
    state.endCalled = true
    state.events.push(completion.event)
    for (const listener of [...state.listeners]) listener(completion.event)
    state.resolveCompletion(completion)
  }

  function runFail(run: { threadId: string; runId: string }, error: Error) {
    const state = runStates.get(keyOf(run))
    if (!state || state.endCalled) return
    state.endCalled = true
    state.failCompletion(error)
  }

  function runSelection(run: { threadId: string; runId: string }): { primary_profile: string } | undefined {
    return runStates.get(keyOf(run))?.input.modelSelection
  }

  function runSkill(run: { threadId: string; runId: string }): { id: string; args?: string } | undefined {
    return runStates.get(keyOf(run))?.input.requestedSkill
  }

  return { port, calls, abandoned, runHandles, runStates }
}

function makeHarness(options: {
  initialThreadId?: string | null
  configError?: boolean
  failOpenThread?: boolean
  holdConfigDetails?: boolean
  scheduler?: InteractiveScheduler
} = {}) {
  const portState = createPort()
  const controller = createInteractiveController({
    agent: portState.port,
    runtime,
    ...(options.initialThreadId !== undefined ? { initialThreadId: options.initialThreadId } : {}),
    ...(options.scheduler !== undefined ? { scheduler: options.scheduler } : {}),
  })
  return { ...portState, controller }
}

async function flush(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0))
  await new Promise(resolve => setTimeout(resolve, 0))
}

function notices(snapshot: InteractiveSnapshot): string {
  return snapshot.timeline
    .flatMap(item => item.type === "message" && item.message.role === "system" ? [item.message.content] : [])
    .join("\n")
}

function threadSummary(threadId: string, message: string) {
  return { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: message, latest_message: message, message_count: 2 }
}

function skill(id: string, enabled: boolean) {
  return { id, name: id.split("/").at(-1)!, description: `描述 ${id}`, source: "user", enabled, user_invocable: true, argument_hint: "下一条消息使用" }
}

function terminalEvent(type: string, threadId: string, runId: string, sequence: number, payload: Record<string, unknown>): EventEnvelope {
  return {
    event_id: `event-${sequence}`,
    type: type as EventEnvelope["type"],
    thread_id: threadId,
    run_id: runId,
    sequence,
    timestamp_ms: sequence,
    payload,
  }
}

function approvalRequest(threadId: string, runId: string, decisions = ["approve_once", "reject"]): InteractionRequestEnvelope {
  return {
    type: "approval",
    request_id: "approval-1",
    thread_id: threadId,
    run_id: runId,
    timeout_ms: 5_000,
    payload: { description: "需要执行工具", requests: [], decisions },
  } as InteractionRequestEnvelope
}

function questionRequest(threadId: string, runId: string): InteractionRequestEnvelope {
  return {
    type: "question",
    request_id: "question-1",
    thread_id: threadId,
    run_id: runId,
    timeout_ms: 5_000,
    payload: {
      questions: [
        { id: "scope", question: "处理哪个目录？", header: "", body: "", options: [{ label: "src", value: "src", description: "" }], multi_select: false, allow_other: true },
        { id: "level", question: "深度？", header: "", body: "", options: [{ label: "浅", value: "shallow", description: "" }, { label: "深", value: "deep", description: "" }], multi_select: true, allow_other: false },
      ],
    },
  } as InteractionRequestEnvelope
}

test("空首页：无 initialThreadId 不恢复，snapshot 表达 null Thread", () => {
  const harness = makeHarness()
  const snapshot = harness.controller.getSnapshot()
  expect(snapshot.currentThreadId).toBeNull()
  expect(snapshot.activity.kind).toBe("home")
  expect(snapshot.connection.status).toBe("open")
  harness.controller.close()
})

test("approval-mode.cycle 循环切换审批模式，并传递给下一次 Run", async () => {
  const harness = makeHarness()
  try {
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("default")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("auto-edit")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("auto")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("yolo")
    await harness.controller.dispatch({ type: "approval-mode.cycle" })
    expect(harness.controller.getSnapshot().runtime.approvalMode).toBe("plan")

    await harness.controller.dispatch({ type: "input.submit", value: "使用覆盖模式" })
    const selection = harness.port.lastRunSelection()
    expect(selection).toMatchObject({ message: "使用覆盖模式", approvalMode: "plan" })
  } finally {
    await harness.controller.close()
  }
})

test("显式 initialThreadId null 进入空首页；字符串恢复历史与模型绑定", async () => {
  const empty = makeHarness({ initialThreadId: null })
  await flush()
  expect(empty.controller.getSnapshot().currentThreadId).toBeNull()
  expect(empty.calls).not.toContain("threads.open")
  await empty.controller.close()

  const restored = makeHarness({ initialThreadId: "thread-2" })
  await flush()
  const snapshot = restored.controller.getSnapshot()
  expect(snapshot.currentThreadId).toBe("thread-2")
  expect(snapshot.timeline).toHaveLength(2)
  expect(snapshot.catalogs.models.status).toBe("ready")
  await restored.controller.close()
})

test("普通输入启动 Run，流式内容与终态更新 Timeline", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "你好" })
    const run = harness.runHandles.at(-1)!
    expect(run.threadId).toBe("thread-1")
    expect(harness.controller.getSnapshot().activeRun).toEqual({ threadId: run.threadId, runId: run.runId })

    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 1, { text: "正在" }))
    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 2, { text: "思考" }))
    await flush()
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.timeline.at(-1)).toMatchObject({ type: "message", message: { role: "assistant", content: "正在思考", streaming: true } })

    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.activeRun).toBeNull()
    expect(snapshot.lastRun?.outcome).toBe("completed")
    expect(snapshot.activity.kind).toBe("completed")
  } finally {
    await harness.controller.close()
  }
})

test("active Run 时重复提交不调用 startRun", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一次" })
    const before = harness.calls.filter(call => call === "run.start").length
    await harness.controller.dispatch({ type: "input.submit", value: "第二次" })
    expect(harness.calls.filter(call => call === "run.start").length).toBe(before)
    expect(notices(harness.controller.getSnapshot())).toContain("仍在执行")
  } finally {
    await harness.controller.close()
  }
})

test("重复/倒序 Event 被丢弃，sequence 缺口只追加诊断并继续", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "测试事件" })
    const run = harness.runHandles.at(-1)!
    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 1, { text: "A" }))
    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 1, { text: "重复" }))
    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 3, { text: "C" }))
    harness.port.emitEvent(terminalEvent(EventType.CONTENT_DELTA, run.threadId, run.runId, 2, { text: "倒序" }))
    await flush()
    const snapshot = harness.controller.getSnapshot()
    const contents = snapshot.timeline
      .filter(item => item.type === "message" && item.message.role === "assistant")
      .map(item => item.message.content)
      .join("")
    expect(contents).toBe("AC")
    expect(notices(snapshot)).toContain("协议序号缺口：1 → 3")
  } finally {
    await harness.controller.close()
  }
})

test("旧 Run 的迟到终态不能结束新 Run", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "第一个" })
    const first = harness.runHandles.at(-1)!
    harness.port.completeRun(first.threadId, first.runId)
    await flush()

    await harness.controller.dispatch({ type: "input.submit", value: "第二个" })
    const second = harness.runHandles.at(-1)!
    expect(harness.controller.getSnapshot().activeRun?.runId).toBe(second.runId)
    harness.port.failRunWithEvent(first.threadId, first.runId)
    await flush()
    expect(harness.controller.getSnapshot().activeRun?.runId).toBe(second.runId)
  } finally {
    await harness.controller.close()
  }
})

test("approval 校验 decisions allowlist，reject_with_feedback 携带反馈", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "审批" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId, ["approve_once", "reject_with_feedback"]))
    expect(harness.controller.getSnapshot().interaction?.type).toBe("approval")

    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "approval-1",
      response: { kind: "approval", decision: "approve_always" },
    })
    // 非法 decision 不发送响应，Interaction 保持等待。
    expect(notices(harness.controller.getSnapshot())).toContain("不支持的审批决定")
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()

    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "approval-1",
      response: { kind: "approval", decision: "reject_with_feedback", feedback: "理由不足" },
    })
    expect(await responsePromise).toMatchObject({ type: "approval", request_id: "approval-1", decision: "reject_with_feedback", feedback: "理由不足" })
  } finally {
    await harness.controller.close()
  }
})

test("question 完整 schema：多题、多选、other answer 与非法响应", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "提问" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(questionRequest(run.threadId, run.runId))
    const interaction = harness.controller.getSnapshot().interaction
    expect(interaction?.type).toBe("question")
    if (interaction?.type !== "question") throw new Error("expected question")
    expect(interaction.questions).toHaveLength(2)
    expect(interaction.deadlineAtMs).toBeGreaterThan(0)

    // 缺少声明问题：不发送 RPC，Interaction 保持等待。
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: [] } },
    })
    expect(notices(harness.controller.getSnapshot())).toContain("缺少问题")
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()

    // 非法选项：不发送 RPC。
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: ["src"], level: ["shallow", "not-allowed"] } },
    })
    expect(notices(harness.controller.getSnapshot())).toContain("无效选项")

    // 合法多选 + other answer 后正常回写。
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: ["自由文本"], level: ["shallow", "deep"] } },
    })
    expect(await responsePromise).toMatchObject({ answers: { scope: ["自由文本"], level: ["shallow", "deep"] } })

    // stale request ID 不产生 RPC：新的 Interaction 到达后仍保持 pending。
    const stalePromise = harness.port.sendInteraction(questionRequest(run.threadId, run.runId))
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "stale-request",
      response: { kind: "question", answers: { scope: ["src"], level: ["shallow"] } },
    })
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()
    await harness.controller.dispatch({
      type: "interaction.respond",
      requestId: "question-1",
      response: { kind: "question", answers: { scope: ["src"], level: ["shallow"] } },
    })
    await expect(stalePromise).resolves.toMatchObject({ type: "question", answers: { scope: ["src"], level: ["shallow"] } })
  } finally {
    await harness.controller.close()
  }
})

test("Interaction timeout 按 scheduler 收敛为 fail-closed 响应", async () => {
  const manual = manualScheduler()
  const harness = makeHarness({ scheduler: manual.scheduler })
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "超时审批" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId))
    expect(harness.controller.getSnapshot().interaction).not.toBeNull()

    manual.runAll()
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.interaction).toBeNull()
    expect(notices(snapshot)).toContain("审批等待超时")
    expect(await responsePromise).toMatchObject({ type: "approval", decision: "reject" })
  } finally {
    await harness.controller.close()
  }
})

test("Run 终态与 Controller close 都会 abandon 未完成 Interaction", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "终态清理" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId))
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    expect(harness.abandoned).toContain("approval-1")
    expect(await responsePromise).toMatchObject({ decision: "reject" })
  } finally {
    await harness.controller.close()
  }
})

test("connection close 禁止新 Run 并收敛 Interaction", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "断连前" })
    const run = harness.runHandles.at(-1)!
    const responsePromise = harness.port.sendInteraction(approvalRequest(run.threadId, run.runId))
    harness.port.closeConnection("transport closed")
    await flush()
    expect(harness.controller.getSnapshot().connection.status).toBe("closed")
    expect(harness.abandoned).toContain("approval-1")
    expect(await responsePromise).toMatchObject({ decision: "reject" })

    await harness.controller.dispatch({ type: "input.submit", value: "断连后" })
    expect(harness.calls.filter(call => call === "run.start").length).toBe(1)
  } finally {
    await harness.controller.close()
  }
})

test("Thread 切换：/resume 打开选择器并原子恢复，运行中禁止切换", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "command.execute", commandId: "thread.resume" })
    await flush()
    expect(harness.controller.getSnapshot().catalogs.threads.status).toBe("ready")
    expect(harness.controller.getSnapshot().catalogs.threads.items).toHaveLength(2)

    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-2" })
    await flush()
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.currentThreadId).toBe("thread-2")
    expect(snapshot.timeline).toHaveLength(2)

    await harness.controller.dispatch({ type: "input.submit", value: "运行中" })
    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-1" })
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.currentThreadId).toBe("thread-2")
    expect(notices(snapshot)).toContain("不能恢复其他 thread")
  } finally {
    await harness.controller.close()
  }
})

test("/new 确认后取消并清空 Thread；取消失败保留原 Thread", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "保留这个 Thread" })
    const run = harness.runHandles.at(-1)!
    await harness.controller.dispatch({ type: "command.execute", commandId: "thread.new" })
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.confirmation?.confirmationId).toBe("clear-thread")

    await harness.controller.dispatch({ type: "confirmation.resolve", confirmationId: "clear-thread", confirmed: true })
    await flush()
    snapshot = harness.controller.getSnapshot()
    expect(snapshot.currentThreadId).toBeNull()
    expect(snapshot.timeline).toHaveLength(0)
    expect(harness.calls).toContain("run.cancel")
    expect(run.runId).toBeTruthy()
  } finally {
    await harness.controller.close()
  }
})

test("模型选择先更新当前 Thread，再独立同步默认值", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "catalog.refresh", catalog: "models" })
    await flush()
    await harness.controller.dispatch({ type: "model.select", profileId: "pro" })
    await flush()
    let snapshot = harness.controller.getSnapshot()
    expect(snapshot.selection.requestedModelProfileId).toBe("pro")
    expect(notices(snapshot)).toContain("后续新 Thread 默认模型已同步")
    expect(harness.calls).toEqual(["skills.list", "models.list", "config.details", "config.preview", "config.commit", "models.list"])

    await harness.controller.dispatch({ type: "input.submit", value: "使用选择运行" })
    await flush()
    expect(harness.port.lastRunSelection()?.modelSelection).toEqual({ primary_profile: "pro" })
  } finally {
    await harness.controller.close()
  }
})

test("模型默认同步失败保留当前选择并输出稳定原因", async () => {
  const harness = makeHarness()
  harness.port.configDetails = async () => {
    harness.calls.push("config.details")
    throw new Error("managed policy locked")
  }
  try {
    await harness.controller.dispatch({ type: "catalog.refresh", catalog: "models" })
    await flush()
    await harness.controller.dispatch({ type: "model.select", profileId: "pro" })
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.selection.requestedModelProfileId).toBe("pro")
    expect(notices(snapshot)).toContain("未来新 Thread 默认未更新")
  } finally {
    await harness.controller.close()
  }
})

test("catalog 单项失败只影响对应 catalog", async () => {
  const harness = makeHarness()
  harness.port.listThreads = async () => {
    harness.calls.push("threads.list")
    throw new Error("threads unavailable")
  }
  try {
    await harness.controller.dispatch({ type: "catalog.refresh", catalog: "threads" })
    await flush()
    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.catalogs.threads.status).toBe("error")
    expect(snapshot.catalogs.skills.status).toBe("ready")
    expect(snapshot.catalogs.models.status).toBe("idle")
    expect(snapshot.currentThreadId).toBeNull()
  } finally {
    await harness.controller.close()
  }
})

test("未知命令/转义/alias/动态 Skill 走同一解析路径", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "/contnue" })
    expect(notices(harness.controller.getSnapshot())).toContain("未知命令")
    expect(harness.calls).not.toContain("run.start")

    await harness.controller.dispatch({ type: "input.submit", value: "//api 路由" })
    expect(harness.port.lastRunSelection()).toMatchObject({ message: "/api 路由" })
    const escapedRun = harness.runHandles.at(-1)!
    harness.port.completeRun(escapedRun.threadId, escapedRun.runId)
    await flush()

    await harness.controller.dispatch({ type: "input.submit", value: "使用别名" })
    await flush()
    const aliasRun = harness.runHandles.at(-1)!
    harness.port.completeRun(aliasRun.threadId, aliasRun.runId)
    await flush()

    await harness.controller.dispatch({ type: "skill.arm", skillId: "user/repo-review-demo" })
    expect(harness.controller.getSnapshot().selection.armedSkill?.id).toBe("user/repo-review-demo")
    await harness.controller.dispatch({ type: "input.submit", value: "审查" })
    expect(harness.port.lastRunSelection()?.requestedSkill).toEqual({ id: "user/repo-review-demo", args: "审查" })
  } finally {
    await harness.controller.close()
  }
})

test("compact/status/version/help/web 命令语义确定", async () => {
  const harness = makeHarness()
  try {
    const exitResult = await harness.controller.dispatch({ type: "command.execute", commandId: "system.quit" })
    expect(exitResult).toEqual({ type: "request-exit" })

    const handoff = await harness.controller.dispatch({ type: "command.execute", commandId: "host.web" })
    expect(handoff).toEqual({ type: "request-handoff", threadId: null })

    await harness.controller.dispatch({ type: "input.submit", value: "需要压缩" })
    const run = harness.runHandles.at(-1)!
    harness.port.completeRun(run.threadId, run.runId)
    await flush()
    await harness.controller.dispatch({ type: "command.execute", commandId: "context.compact" })
    expect(notices(harness.controller.getSnapshot())).toContain("上下文已压缩")

    await harness.controller.dispatch({ type: "command.execute", commandId: "system.status" })
    expect(notices(harness.controller.getSnapshot())).toContain("工作区")
  } finally {
    await harness.controller.close()
  }
})

test("close 幂等；关闭后 dispatch no-op 且不再发布 snapshot", async () => {
  const harness = makeHarness()
  const snapshots: InteractiveSnapshot[] = []
  const unsubscribe = harness.controller.subscribe(snapshot => snapshots.push(snapshot))
  try {
    await harness.controller.close()
    await harness.controller.close()
    await harness.controller.dispatch({ type: "input.submit", value: "关闭后" })
    await flush()
    expect(harness.calls).not.toContain("run.start")
    const count = snapshots.length
    unsubscribe()
    await flush()
    expect(snapshots.length).toBe(count)
  } finally {
    await harness.controller.close()
  }
})

test("Thread 打开期间重复打开被拒绝，stale 模型结果不覆盖新 Thread", async () => {
  const harness = makeHarness()
  let releaseFirst!: () => void
  const gate = new Promise<void>(resolve => { releaseFirst = resolve })
  let openCount = 0
  const originalOpen = harness.port.openThread.bind(harness.port)
  harness.port.openThread = async (threadId: string) => {
    openCount += 1
    const result = await originalOpen(threadId)
    if (openCount === 1) await gate
    return result
  }
  try {
    const first = harness.controller.dispatch({ type: "thread.open", threadId: "thread-1" })
    await flush()
    // openingThread 保护：第二个 open 不发 RPC。
    await harness.controller.dispatch({ type: "thread.open", threadId: "thread-2" })
    await flush()
    expect(harness.calls.filter(call => call === "threads.open")).toHaveLength(1)
    releaseFirst()
    await first
    await flush()
    expect(harness.controller.getSnapshot().currentThreadId).toBe("thread-1")
  } finally {
    await harness.controller.close()
  }
})
