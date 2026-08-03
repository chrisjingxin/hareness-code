/** TuiController 的无渲染工作流回归：只通过 AgentClient seam 验证状态和副作用。 */

import { expect, test } from "bun:test"
import { rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { Capability, EventType, type EventEnvelope, type InteractionRequestEnvelope, type ModelProfile } from "@za38/protocol"

import { AgentClient } from "../../../src/ipc/client"
import { createTuiController, type TuiController, type TuiControllerOptions } from "../../../src/tui/application/controller"
import { parseSlashCommand } from "../../../src/tui/application/commands"
import type { TuiRuntime } from "../../../src/tui/application/model"

const runtime: TuiRuntime = {
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

test("Controller 通过小 interface 发布 snapshot，并执行 MCP 命令工作流", async () => {
  const harness = await createHarness()
  try {
    const snapshots: string[] = []
    const unsubscribe = harness.controller.subscribe(snapshot => snapshots.push(snapshot.state.status))

    await execute(harness.controller, "/mcp")
    await execute(harness.controller, "/mcp add testfs npx -y filesystem")
    await execute(harness.controller, "/mcp remove testfs")
    await execute(harness.controller, "/mcp add")

    const text = notices(harness.controller)
    expect(text).toContain("MCP 服务器状态")
    expect(text).toContain("已添加 MCP 服务器 \"testfs\"")
    expect(text).toContain("已删除 MCP 服务器 \"testfs\"")
    expect(text).toContain("用法：/mcp add")
    expect(harness.calls).toEqual(["mcp.status", "mcp.add", "mcp.remove"])
    expect(snapshots.length).toBeGreaterThan(0)
    unsubscribe()
  } finally {
    await harness.cleanup()
  }
})

test("Controller 保留 /model 的当前选择，并独立处理默认模型同步失败", async () => {
  const harness = await createHarness({ configError: true })
  try {
    await execute(harness.controller, "/model")
    await flush()
    const model = harness.controller.getSnapshot().models.items.find(value => value.id === "pro")
    expect(model).toBeDefined()

    await harness.controller.dispatch({ type: "picker-select-model", model: model! })
    expect(notices(harness.controller)).toContain("未来新 Thread 默认未更新")
    expect(harness.calls).toEqual(["models.list", "config.details"])

    await harness.controller.dispatch({ type: "submit", value: "使用当前选择运行" })
    expect(harness.runRequests.at(-1)?.modelSelection).toEqual({ primary_profile: "pro" })
  } finally {
    await harness.cleanup()
  }
})

test("Controller 的 /model 成功同步当前选择和未来默认模型", async () => {
  const harness = await createHarness()
  try {
    await execute(harness.controller, "/model")
    const model = harness.controller.getSnapshot().models.items.find(value => value.id === "pro")!
    await harness.controller.dispatch({ type: "picker-select-model", model })

    expect(notices(harness.controller)).toContain("后续新 Thread 默认模型已同步")
    expect(harness.calls).toEqual(["models.list", "config.details", "config.preview", "config.commit", "models.list"])
  } finally {
    await harness.cleanup()
  }
})

test("Controller 选择当前默认模型时不写配置，并在保存期间拒绝重复选择", async () => {
  const sameHarness = await createHarness()
  try {
    await execute(sameHarness.controller, "/model")
    const defaultModel = sameHarness.controller.getSnapshot().models.items.find(value => value.id === "fast")!
    await sameHarness.controller.dispatch({ type: "picker-select-model", model: defaultModel })
    expect(sameHarness.calls).toEqual(["models.list", "config.details", "models.list"])
  } finally {
    await sameHarness.cleanup()
  }

  const savingHarness = await createHarness({ holdConfigDetails: true })
  try {
    await execute(savingHarness.controller, "/model")
    const models = savingHarness.controller.getSnapshot().models.items
    const firstSelection = savingHarness.controller.dispatch({ type: "picker-select-model", model: models.find(value => value.id === "pro")! })
    await flush()
    expect(savingHarness.controller.getSnapshot().models.syncingDefault).toBe(true)

    await savingHarness.controller.dispatch({ type: "picker-select-model", model: models.find(value => value.id === "fast")! })
    expect(savingHarness.controller.getSnapshot().displayedModelName).toBe("pro")
    savingHarness.releaseConfigDetails()
    await firstSelection
  } finally {
    savingHarness.releaseConfigDetails()
    await savingHarness.cleanup()
  }
})

test("Controller 的 /resume 原子恢复历史，并在运行中阻止切换", async () => {
  const harness = await createHarness()
  try {
    await execute(harness.controller, "/resume")
    await flush()
    expect(harness.controller.getSnapshot().threads.items).toHaveLength(2)

    await harness.controller.dispatch({ type: "picker-search", picker: "threads", query: "第二" })
    expect(harness.controller.getSnapshot().threads.items).toHaveLength(1)
    await harness.controller.dispatch({
      type: "picker-select-thread",
      thread: harness.controller.getSnapshot().threads.items[0]!,
    })
    expect(harness.controller.getSnapshot().state.threadId).toBe("thread-2")
    expect(harness.controller.getSnapshot().state.timeline).toHaveLength(2)

    await harness.controller.dispatch({ type: "submit", value: "开始新的运行" })
    await execute(harness.controller, "/resume")
    expect(harness.controller.getSnapshot().threads.visible).toBe(false)
    expect(notices(harness.controller)).toContain("当前任务结束或交互完成后可用")
  } finally {
    await harness.cleanup()
  }
})

test("Controller 的 /new 取消失败时保留当前 Thread，并能处理 Interaction", async () => {
  const harness = await createHarness({ cancelled: false })
  try {
    await harness.controller.dispatch({ type: "submit", value: "保留这个 Thread" })
    const run = harness.runRequests.at(-1)!
    const interaction = approvalRequest(run.threadId, run.runId)
    const responsePromise = harness.sendInteraction(interaction)
    expect(harness.controller.getSnapshot().state.pendingApproval?.requestId).toBe(interaction.request_id)
    await harness.controller.dispatch({ type: "approval", decision: "reject" })
    expect(await responsePromise).toMatchObject({ decision: "reject", request_id: interaction.request_id })

    const question = questionRequest(run.threadId, run.runId)
    const questionResponsePromise = harness.sendInteraction(question)
    expect(harness.controller.getSnapshot().state.pendingQuestion?.questionId).toBe("need-context")
    await harness.controller.dispatch({ type: "question", answer: "只处理 src 目录" })
    expect(await questionResponsePromise).toMatchObject({
      type: "question",
      request_id: question.request_id,
      answers: { "need-context": ["只处理 src 目录"] },
    })

    await execute(harness.controller, "/new")
    expect(harness.controller.getSnapshot().commandDialog?.kind).toBe("confirm-new-thread")

    await harness.controller.dispatch({ type: "dialog-resolve", kind: "command", confirmed: true })
    expect(harness.controller.getSnapshot().state.threadId).toBe(run.threadId)
    expect(notices(harness.controller)).toContain("未能取消当前任务")
  } finally {
    await harness.cleanup()
  }
})

test("Controller 的 /new 确认后原子清理 Thread 和未完成 Interaction", async () => {
  const harness = await createHarness()
  try {
    await harness.controller.dispatch({ type: "submit", value: "需要清理的 Thread" })
    const run = harness.runRequests.at(-1)!
    const interaction = approvalRequest(run.threadId, run.runId)
    const responsePromise = harness.sendInteraction(interaction)

    await execute(harness.controller, "/new")
    await harness.controller.dispatch({ type: "dialog-resolve", kind: "command", confirmed: true })

    const snapshot = harness.controller.getSnapshot()
    expect(snapshot.state.threadId).toBeUndefined()
    expect(snapshot.state.timeline).toHaveLength(0)
    expect(snapshot.state.pendingApproval).toBeUndefined()
    expect(await responsePromise).toMatchObject({ decision: "reject", request_id: interaction.request_id })
  } finally {
    await harness.cleanup()
  }
})

test("Controller 接收 Agent event 后更新 reducer，并在终态清理 Interaction", async () => {
  const harness = await createHarness()
  try {
    await harness.controller.dispatch({ type: "submit", value: "触发事件" })
    const run = harness.runRequests.at(-1)!
    harness.emit({
      event_id: "event-started",
      type: EventType.RUN_STARTED,
      thread_id: run.threadId,
      run_id: run.runId,
      sequence: 1,
      timestamp_ms: 1,
      payload: { resumed: false },
    })
    const interaction = approvalRequest(run.threadId, run.runId)
    const responsePromise = harness.sendInteraction(interaction)
    expect(harness.controller.getSnapshot().state.status).toBe("等待工具审批")

    harness.emit({
      event_id: "event-cancelled",
      type: EventType.RUN_CANCELLED,
      thread_id: run.threadId,
      run_id: run.runId,
      sequence: 2,
      timestamp_ms: 2,
      payload: { reason: "用户取消" },
    })
    expect(harness.controller.getSnapshot().state.activeRun).toBeUndefined()
    expect(harness.controller.getSnapshot().state.timeline.at(-1)).toMatchObject({ type: "message" })
    expect(await responsePromise).toMatchObject({ decision: "reject" })
  } finally {
    await harness.cleanup()
  }
})

test("Controller 的 /web 把 nullable threadId 交给 openWeb 且不泄露 URL", async () => {
  const opened: Array<string | null> = []
  const harness = await createHarness({
    openWeb: async threadId => { opened.push(threadId) },
  })
  try {
    await execute(harness.controller, "/web")
    expect(opened).toEqual([null])
    expect(notices(harness.controller)).toContain("Web 会话已启动")
    expect(notices(harness.controller)).not.toContain("http://")
  } finally {
    await harness.cleanup()
  }
})

test("Controller 从 Web 归还后按 initialThreadId 恢复历史或空首页", async () => {
  const restored = await createHarness({ initialThreadId: "thread-2" })
  try {
    await flush()
    expect(restored.controller.getSnapshot().state.threadId).toBe("thread-2")
    expect(restored.controller.getSnapshot().state.timeline).toHaveLength(2)
  } finally {
    await restored.cleanup()
  }

  const empty = await createHarness({ initialThreadId: null })
  try {
    await flush()
    expect(empty.calls).not.toContain("threads.open")
    expect(empty.controller.getSnapshot().state.threadId).toBeUndefined()
  } finally {
    await empty.cleanup()
  }

  const failed = await createHarness({ initialThreadId: "thread-2", failOpenThread: true })
  try {
    await flush()
    expect(failed.controller.getSnapshot().state.threadId).toBeUndefined()
    expect(notices(failed.controller)).toContain("Web 会话恢复失败")
  } finally {
    await failed.cleanup()
  }
})

type Harness = {
  controller: TuiController
  calls: string[]
  runRequests: Array<{ threadId: string; runId: string; modelSelection?: { primary_profile: string } }>
  releaseConfigDetails: () => void
  emit: (event: EventEnvelope) => void
  sendInteraction: (request: InteractionRequestEnvelope) => Promise<unknown>
  cleanup: () => Promise<void>
}

async function createHarness(options: {
  configError?: boolean
  cancelled?: boolean
  holdConfigDetails?: boolean
  initialThreadId?: string | null
  openWeb?: (threadId: string | null) => Promise<void>
  failOpenThread?: boolean
} = {}): Promise<Harness> {
  const calls: string[] = []
  const runRequests: Harness["runRequests"] = []
  const listeners = new Map<string, Set<(...args: any[]) => void>>()
  let requestHandler: ((request: InteractionRequestEnvelope) => Promise<unknown>) | undefined
  let runNumber = 0
  let releaseConfigDetails = () => undefined
  const configDetailsGate = options.holdConfigDetails
    ? new Promise<void>(resolve => { releaseConfigDetails = resolve })
    : undefined
  const profiles = createProfiles()

  const client = {
    on(event: string, listener: (...args: any[]) => void) {
      const current = listeners.get(event) ?? new Set()
      current.add(listener)
      listeners.set(event, current)
      return client
    },
    off(event: string, listener: (...args: any[]) => void) {
      listeners.get(event)?.delete(listener)
      return client
    },
    setRequestHandler(handler: (request: InteractionRequestEnvelope) => Promise<unknown>) {
      requestHandler = handler
      return () => { if (requestHandler === handler) requestHandler = undefined }
    },
    abandonInteraction(_requestId: string) {},
    request(method: string) {
      calls.push(method)
      if (method === "skills.list") return Promise.resolve({ skills: [] })
      if (method === "context.compact") return Promise.resolve({ compacted: true, context: { action: "manual_summary" } })
      return Promise.reject(new Error(`Unexpected request: ${method}`))
    },
    listModels() {
      calls.push("models.list")
      return Promise.resolve({ profiles })
    },
    async configDetails() {
      calls.push("config.details")
      if (options.configError) return Promise.reject(new Error("managed policy locked"))
      if (configDetailsGate) await configDetailsGate
      return { revision: "r1", fields: [{ path: "models.default_profile", value: "fast", source: "user", editable: true, unavailable_reason: null, applies_to: "new-thread" }], immutable_fields: [] }
    },
    previewConfig() {
      calls.push("config.preview")
      return Promise.resolve({ revision: "r1", changes: [], applies_to: ["new-thread"] })
    },
    commitConfig() {
      calls.push("config.commit")
      return Promise.resolve({ revision: "r2", changes: [], applies_to: ["new-thread"] })
    },
    listThreads() {
      calls.push("threads.list")
      return Promise.resolve({ threads: [threadSummary("thread-1", "第一条历史"), threadSummary("thread-2", "第二条历史")] })
    },
    openThread(threadId: string) {
      calls.push("threads.open")
      if (options.failOpenThread) return Promise.reject(new Error("THREAD_NOT_FOUND"))
      return Promise.resolve({ thread: threadSummary(threadId, "恢复的请求"), messages: [{ kind: "user", content: "恢复的请求" }, { kind: "tool", tool_name: "execute", content: "恢复的工具结果" }] })
    },
    mcpStatus() {
      calls.push("mcp.status")
      return Promise.resolve({ servers: [{ name: "filesystem", transport: "stdio", status: "connected", tool_names: ["read"] }], total_tools: 1 })
    },
    mcpAdd() {
      calls.push("mcp.add")
      return Promise.resolve({ added: true, connected: true, tool_names: ["new_tool"] })
    },
    mcpRemove() {
      calls.push("mcp.remove")
      return Promise.resolve({ removed: true })
    },
    cancel() {
      calls.push("run.cancel")
      return Promise.resolve({ cancelled: options.cancelled ?? true, run_id: runRequests.at(-1)?.runId ?? "" })
    },
    startRun(input: { message: string; threadId?: string; modelSelection?: { primary_profile: string } }) {
      const threadId = input.threadId ?? `thread-${runNumber + 1}`
      const runId = `run-${++runNumber}`
      runRequests.push({ threadId, runId, modelSelection: input.modelSelection })
      return {
        ref: { threadId, runId },
        accepted: Promise.resolve(),
        events: [] as AsyncIterable<never>,
        completion: Promise.resolve({ outcome: "completed" }),
        cancel: async () => options.cancelled ?? true,
      }
    },
  } as unknown as AgentClient

  const historyFile = join(tmpdir(), `za38-controller-${crypto.randomUUID()}.jsonl`)
  const controller = createTuiController({
    client,
    runtime,
    promptHistoryFile: historyFile,
    onRequestExit: () => undefined,
    ...(options.initialThreadId !== undefined ? { initialThreadId: options.initialThreadId } : {}),
    ...(options.openWeb !== undefined ? { openWeb: options.openWeb } : {}),
  })
  await flush()
  calls.splice(0)

  return {
    controller,
    calls,
    runRequests,
    releaseConfigDetails,
    emit(event) {
      for (const listener of listeners.get("event") ?? []) listener(event)
    },
    sendInteraction(request) {
      if (!requestHandler) throw new Error("Controller interaction handler is not registered")
      return requestHandler(request)
    },
    cleanup: async () => {
      await controller.close()
      await rm(historyFile, { force: true })
    },
  }
}

async function execute(controller: TuiController, input: string): Promise<void> {
  const command = parseSlashCommand(input)
  if (!command) throw new Error(`Not a command: ${input}`)
  await controller.dispatch({ type: "execute-command", command })
  await flush()
}

function notices(controller: TuiController): string {
  return controller.getSnapshot().state.timeline
    .flatMap(item => item.type === "message" && item.message.role === "system" ? [item.message.content] : [])
    .join("\n")
}

function threadSummary(threadId: string, message: string) {
  return { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: message, latest_message: message, message_count: 2 }
}

function createProfiles(): ModelProfile[] {
  return [
    { id: "fast", model: "fast-model", provider_label: "Fast Gateway", context_window_tokens: 128000, capabilities: ["streaming"], is_default: true, available: true, source: "user" },
    { id: "pro", model: "pro-model", provider_label: "Pro Gateway", context_window_tokens: 256000, capabilities: ["streaming"], is_default: false, available: true, source: "user" },
  ]
}

function approvalRequest(threadId: string, runId: string): InteractionRequestEnvelope {
  return {
    type: "approval",
    request_id: "approval-1",
    thread_id: threadId,
    run_id: runId,
    payload: { description: "需要执行工具", requests: [] },
  } as InteractionRequestEnvelope
}

function questionRequest(threadId: string, runId: string): InteractionRequestEnvelope {
  return {
    type: "question",
    request_id: "question-1",
    thread_id: threadId,
    run_id: runId,
    payload: { questions: [{ id: "need-context", question: "处理哪个目录？", options: [] }] },
  } as InteractionRequestEnvelope
}

async function flush(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
}
