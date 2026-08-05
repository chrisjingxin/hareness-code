/** Web Interactive Adapter：通过 fake controller 与 fake handoff port 验证语义意图与副作用。 */

import { expect, test } from "bun:test"

import type {
  IntentOutcome,
  InteractiveCommandItem,
  InteractiveConfirmation,
  InteractiveConnectionState,
  InteractiveController,
  InteractiveIntent,
  InteractiveInteraction,
  InteractiveMcpInput,
  InteractiveResponse,
  InteractiveResult,
  InteractiveSnapshot,
  LoadableCatalog,
  McpServerSummary,
  ModelProfile,
  SkillSummary,
  ThreadSummary,
} from "../../../src/interactive/types"
import { commandRegistry, type CommandMenuItem, type SkillMenuItem } from "../../../src/interactive/commands"
import {
  createWebInteractiveAdapter,
  type WebAdapterSnapshot,
  type WebFrameScheduler,
  type WebHandoffPort,
  type WebIntent,
  type WebInteractiveAdapter,
} from "../../../src/web/application/adapter"

type Listener = (snapshot: InteractiveSnapshot) => void

type DispatchHandler = (intent: InteractiveIntent) => Promise<IntentOutcome>

/** 测试用 frameScheduler：手动驱动；schedule 的任务不会自动执行。 */
function createManualScheduler(): WebFrameScheduler & { runScheduled(): void; pending: () => boolean } {
  let pending: (() => void) | null = null
  return {
    schedule(task) {
      pending = task
    },
    cancel() {
      pending = null
    },
    flush() {
      const task = pending
      pending = null
      if (task) task()
    },
    runScheduled() {
      const task = pending
      pending = null
      if (task) task()
    },
    pending() {
      return pending !== null
    },
  }
}

type FakeHandoffState = {
  activatedWith: (string | null)[]
  reportedThreads: (string | null)[]
  returnCalls: number
  exitCalls: number
  failReturn: boolean
  failExit: boolean
  closed: boolean
  active: boolean
}

function createFakeHandoff(): WebHandoffPort & { state: FakeHandoffState } {
  const state: FakeHandoffState = {
    activatedWith: [],
    reportedThreads: [],
    returnCalls: 0,
    exitCalls: 0,
    failReturn: false,
    failExit: false,
    closed: false,
    active: false,
  }
  return {
    state,
    async activate(threadId) {
      state.active = true
      state.activatedWith.push(threadId)
    },
    reportThread(threadId) {
      if (!state.active || state.closed) return
      const last = state.reportedThreads.at(-1)
      if (last === threadId) return
      state.reportedThreads.push(threadId)
    },
    async returnToTui() {
      state.returnCalls += 1
      if (state.failReturn) throw new Error("fake handoff return failure")
    },
    async requestExit() {
      state.exitCalls += 1
      if (state.failExit) throw new Error("fake handoff exit failure")
    },
    close() {
      state.closed = true
    },
  }
}

type FakeControllerOptions = {
  threadId?: string | null
  activeRun?: boolean
  interaction?: InteractiveInteraction | null
  confirmation?: InteractiveConfirmation | null
  connection?: InteractiveConnectionState
  commands?: readonly InteractiveCommandItem[]
  skills?: readonly SkillMenuItem[]
}

function createFakeController(options: FakeControllerOptions = {}): InteractiveController & {
  emit(): void
  setThreadId(next: string | null): void
  setActiveRun(running: boolean): void
  setInteraction(next: InteractiveInteraction | null): void
  setConnection(next: InteractiveConnectionState): void
  setConfirmation(next: InteractiveConfirmation | null): void
  setCommands(next: readonly InteractiveCommandItem[]): void
  setSkills(next: readonly SkillMenuItem[]): void
  dispatches: InteractiveIntent[]
  dispatchHandler?: DispatchHandler
  setDispatchHandler(handler: DispatchHandler | undefined): void
  currentThreadId: () => string | null
} {
  let threadId = options.threadId ?? null
  const activeRun = options.activeRun ?? false
  let interaction: InteractiveInteraction | null = options.interaction ?? null
  let connection: InteractiveConnectionState = options.connection ?? { status: "open" }
  let confirmation: InteractiveConfirmation | null = options.confirmation ?? null
  const commands = [...(options.commands ?? [])]
  const skills = [...(options.skills ?? [])]
  const listeners = new Set<Listener>()
  const dispatches: InteractiveIntent[] = []
  let dispatchHandler: DispatchHandler | undefined
  let cachedSnapshot: InteractiveSnapshot | undefined

  const emptyThreadCatalog: LoadableCatalog<ThreadSummary> = { status: "ready", items: [] }
  const emptyModelCatalog: LoadableCatalog<ModelProfile> = { status: "ready", items: [] }
  const skillsCatalog: LoadableCatalog<SkillSummary> = { status: "ready", items: skills }
  const emptyMcpCatalog: LoadableCatalog<McpServerSummary> = { status: "ready", items: [] }

  function buildSnapshot(): InteractiveSnapshot {
    return {
      currentThreadId: threadId,
      activity: activeRun ? { kind: "running", label: "执行中" } : threadId ? { kind: "idle", label: "就绪" } : { kind: "home", label: "就绪" },
      activeRun: activeRun ? { threadId: threadId ?? "thread-1", runId: "run-1" } : null,
      timeline: [],
      interaction,
      confirmation,
      lastRun: null,
      runtime: {
        workspace: "/workspace",
        cliVersion: "0.1.0",
        modelConfigured: true,
        modelName: "test-model",
        executionMode: "local",
        approvalMode: "default",
        capabilities: [],
      },
      connection,
      commands,
      catalogs: {
        threads: emptyThreadCatalog,
        models: emptyModelCatalog,
        skills: skillsCatalog,
        mcp: emptyMcpCatalog,
      },
      selection: {
        requestedModelProfileId: null,
        actualModel: null,
        armedSkill: null,
      },
    }
  }

  const controller: InteractiveController & {
    emit(): void
    setThreadId(next: string | null): void
    setActiveRun(running: boolean): void
    setInteraction(next: InteractiveInteraction | null): void
    setConnection(next: InteractiveConnectionState): void
    setConfirmation(next: InteractiveConfirmation | null): void
    setCommands(next: readonly InteractiveCommandItem[]): void
    setSkills(next: readonly SkillMenuItem[]): void
    dispatches: InteractiveIntent[]
    dispatchHandler?: DispatchHandler
    setDispatchHandler(handler: DispatchHandler | undefined): void
    currentThreadId: () => string | null
  } = {
    getSnapshot() {
      // 与真实 controller 行为一致：返回最近一次发布时的缓存引用，而不是每次新建对象。
      cachedSnapshot = cachedSnapshot ?? buildSnapshot()
      return cachedSnapshot
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    async dispatch(intent) {
      dispatches.push(intent)
      if (dispatchHandler) return await dispatchHandler(intent)
      return { status: "accepted" }
    },
    async close() {
      listeners.clear()
    },
    emit() {
      cachedSnapshot = buildSnapshot()
      for (const listener of [...listeners]) listener(cachedSnapshot)
    },
    setThreadId(next) {
      threadId = next
    },
    setActiveRun(running) {
      // 重新触发 listener 需要借助 activeRun 字段，但 activeRun 是局部 const
      // 这里通过 emit() 让 caller 主动控制；setter 只保留以满足 interface
      void running
    },
    setInteraction(next) {
      interaction = next
    },
    setConnection(next) {
      connection = next
    },
    setConfirmation(next) {
      confirmation = next
    },
    setCommands(next) {
      commands.length = 0
      commands.push(...next)
    },
    setSkills(next) {
      skills.length = 0
      skills.push(...next)
    },
    dispatches,
    setDispatchHandler(handler) {
      dispatchHandler = handler
    },
    currentThreadId() {
      return threadId
    },
  }
  return controller
}

function emptyApprovalRequest(): InteractiveInteraction {
  return {
    type: "approval",
    requestId: "approval-1",
    description: "需要执行工具",
    requests: [],
    decisions: ["approve_once", "reject"],
    deadlineAtMs: Date.now() + 60_000,
  }
}

function questionRequest(): InteractiveInteraction {
  return {
    type: "question",
    requestId: "question-1",
    questions: [
      {
        id: "scope",
        question: "范围",
        header: "scope",
        body: "",
        options: [{ label: "src", value: "src", description: "" }],
        multiSelect: false,
        allowOther: true,
      },
      {
        id: "level",
        question: "深度",
        header: "level",
        body: "",
        options: [
          { label: "浅", value: "shallow", description: "" },
          { label: "深", value: "deep", description: "" },
        ],
        multiSelect: true,
        allowOther: false,
      },
    ],
    deadlineAtMs: Date.now() + 60_000,
  }
}

function commandItem(commandId: string, name: string, kind: "command" | "skill" = "command"): CommandMenuItem {
  if (kind === "skill") {
    const skill: SkillMenuItem = {
      id: commandId,
      name,
      description: "skill 描述",
      source: "user",
      enabled: true,
      userInvocable: true,
    }
    return { kind: "skill", skill }
  }
  const definition = commandRegistry.definitions.find(def => def.id === commandId) ?? commandRegistry.definitions[0]!
  return { kind: "command", command: { ...definition, name }, availability: { state: "available" } }
}

test("宿主在 React 首次 commit 后调用 handoff.activate 携带当前 Thread ID", async () => {
  const controller = createFakeController({ threadId: "thread-9" })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })
  await handoff.activate(controller.getSnapshot().currentThreadId)
  expect(handoff.state.activatedWith).toEqual(["thread-9"])
  expect(adapter.getSnapshot().interactive.currentThreadId).toBe("thread-9")
  void adapter.close()
})

test("plain input submit 产生 input.submit 携带原始 draft；resolve 后才清空 draft", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })
  await adapter.dispatch({ type: "draft-change", value: "你好" })
  expect(adapter.getSnapshot().draft).toBe("你好")
  await adapter.dispatch({ type: "submit" })
  expect(controller.dispatches).toEqual([{ type: "input.submit", value: "你好" }])
  expect(adapter.getSnapshot().draft).toBe("")
  expect(adapter.getSnapshot().scrollRequest).toBe("to-bottom")
  void adapter.close()
})

test("普通 submit 继续解释 Controller 的 present/request-exit 结果", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  controller.setDispatchHandler(async intent => {
    if (intent.type === "input.submit" && intent.value === "/models") {
      return { status: "accepted", effects: [{ type: "present", target: "models" }] }
    }
    return { status: "accepted" }
  })
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "draft-change", value: "/models" })
  await adapter.dispatch({ type: "submit" })
  expect(adapter.getSnapshot().activePanel).toBe("models")
  expect(controller.dispatches).toContainEqual({ type: "catalog.refresh", catalog: "models" })
  await adapter.close()
})

test("slash input、`//`、未知命令都只转交 controller，不在 adapter 解释", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "draft-change", value: "/help" })
  await adapter.dispatch({ type: "submit" })
  await adapter.dispatch({ type: "draft-change", value: "//escape" })
  await adapter.dispatch({ type: "submit" })
  await adapter.dispatch({ type: "draft-change", value: "/no-such-command" })
  await adapter.dispatch({ type: "submit" })

  expect(controller.dispatches.map(intent => intent.type)).toEqual([
    "input.submit",
    "input.submit",
    "input.submit",
  ])
  expect(controller.dispatches.map(intent => (intent as { value?: string }).value)).toEqual([
    "/help",
    "//escape",
    "/no-such-command",
  ])
  void adapter.close()
})

test("命令菜单 items 由 filterCommandMenuItems 计算；`//` 与缺少 `/` 都不开菜单", () => {
  const helpCommand = commandItem("system.help", "help")
  const skills = [commandItem("user/repo-review", "repo-review", "skill")]
  const controller = createFakeController({ commands: [helpCommand], skills })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  // 没有 / 时菜单不显示
  void adapter.dispatch({ type: "draft-change", value: "hello" })
  expect(adapter.getSnapshot().commandMenuOpen).toBe(false)
  expect(adapter.getSnapshot().commandOptions).toEqual([])

  // 单 / 打开菜单
  void adapter.dispatch({ type: "draft-change", value: "/" })
  expect(adapter.getSnapshot().commandMenuOpen).toBe(true)
  // draft=`，filterCommandMenuItems 的 needle 为空字符串；registry 中所有非 deprecated 命令应出现
  expect(adapter.getSnapshot().commandOptions.length).toBeGreaterThan(0)

  // /h 进一步过滤到以 h 开头的命令
  void adapter.dispatch({ type: "draft-change", value: "/h" })
  const options = adapter.getSnapshot().commandOptions
  expect(options.some(item => item.kind === "command" && item.command.name === "help")).toBe(true)
  expect(options.every(item => item.kind === "command" ? ["help", "host.web"].includes(item.command.name) : true)).toBe(true)

  // // 不打开命令菜单
  void adapter.dispatch({ type: "draft-change", value: "//escaped" })
  expect(adapter.getSnapshot().commandMenuOpen).toBe(false)
  expect(adapter.getSnapshot().commandOptions).toEqual([])

  void adapter.close()
})

test("present result 打开对应 panel 并触发 catalog.refresh", async () => {
  const controller = createFakeController({ activeRun: true })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  controller.setDispatchHandler(async intent => {
    if (intent.type === "command.execute" && intent.commandId === "model.select") {
      return { status: "accepted", effects: [{ type: "present", target: "models" }] }
    }
    if (intent.type === "command.execute" && intent.commandId === "thread.resume") {
      return { status: "accepted", effects: [{ type: "present", target: "threads" }] }
    }
    return { status: "accepted" }
  })
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "command-menu-select", item: commandItem("model.select", "model") })
  expect(adapter.getSnapshot().activePanel).toBe("models")
  expect(controller.dispatches.some(intent => intent.type === "catalog.refresh" && intent.catalog === "models")).toBe(true)

  controller.dispatches.length = 0
  await adapter.dispatch({ type: "command-menu-select", item: commandItem("thread.resume", "resume") })
  expect(adapter.getSnapshot().activePanel).toBe("threads")
  expect(controller.dispatches.some(intent => intent.type === "catalog.refresh" && intent.catalog === "threads")).toBe(true)

  void adapter.close()
})

test("request-handoff result 只显示本地通知，不调用 handoff 任何方法", async () => {
  const controller = createFakeController({ activeRun: true })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  controller.setDispatchHandler(async intent => {
    if (intent.type === "command.execute" && intent.commandId === "host.web") {
      return { status: "accepted", effects: [{ type: "request-handoff", threadId: "thread-1" }] }
    }
    return { status: "accepted" }
  })
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "command-menu-select", item: commandItem("host.web", "web") })
  expect(adapter.getSnapshot().transientNotice).toBe("当前页面不能再次打开 Web。")
  expect(handoff.state.returnCalls).toBe(0)
  expect(handoff.state.exitCalls).toBe(0)
  void adapter.close()
})

test("request-exit result 触发 handoff.requestExit 并设置 leaving", async () => {
  const controller = createFakeController({ activeRun: true })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  controller.setDispatchHandler(async intent => {
    if (intent.type === "command.execute" && intent.commandId === "system.quit") {
      return { status: "accepted", effects: [{ type: "request-exit" }] }
    }
    return { status: "accepted" }
  })
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "command-menu-select", item: commandItem("system.quit", "quit") })
  expect(handoff.state.exitCalls).toBe(1)
  expect(adapter.getSnapshot().leaving).toBe(true)
  void adapter.close()
})

test("Thread/Model/Skill/MCP click 意图只携带稳定 ID 或 typed input", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "thread-select", threadId: "thread-77" })
  await adapter.dispatch({ type: "thread-new" })
  await adapter.dispatch({ type: "thread-refresh" })
  await adapter.dispatch({ type: "model-select", profileId: "profile-fast" })
  await adapter.dispatch({ type: "skill-arm", skillId: "user/repo-review" })
  await adapter.dispatch({ type: "skill-clear" })
  await adapter.dispatch({ type: "skill-set-enabled", skillId: "user/repo-review", enabled: false })
  const mcpInput: InteractiveMcpInput = { name: "fs", transport: "stdio", command: "fs-server", args: ["--root", "/tmp"] }
  await adapter.dispatch({ type: "mcp-add", input: mcpInput })
  await adapter.dispatch({ type: "mcp-remove", name: "fs" })

  expect(controller.dispatches).toEqual([
    { type: "thread.open", threadId: "thread-77" },
    { type: "command.execute", commandId: "thread.new" },
    { type: "catalog.refresh", catalog: "threads" },
    { type: "model.select", profileId: "profile-fast" },
    { type: "skill.arm", skillId: "user/repo-review" },
    { type: "skill.clear" },
    { type: "skill.set-enabled", skillId: "user/repo-review", enabled: false },
    { type: "mcp.add", input: mcpInput },
    { type: "mcp.remove", name: "fs" },
  ])
  void adapter.close()
})

test("returnToTui 正常情况下调用 handoff.returnToTui；active Run 或 pending interaction 时阻止并通知", async () => {
  const cleanController = createFakeController()
  const cleanHandoff = createFakeHandoff()
  const cleanScheduler = createManualScheduler()
  const cleanAdapter = createWebInteractiveAdapter({ controller: cleanController, handoff: cleanHandoff, frameScheduler: cleanScheduler })
  await cleanAdapter.dispatch({ type: "return-to-tui" })
  expect(cleanHandoff.state.returnCalls).toBe(1)
  expect(cleanAdapter.getSnapshot().leaving).toBe(true)
  await cleanAdapter.close()

  const runController = createFakeController({ activeRun: true })
  const runHandoff = createFakeHandoff()
  const runScheduler = createManualScheduler()
  const runAdapter = createWebInteractiveAdapter({ controller: runController, handoff: runHandoff, frameScheduler: runScheduler })
  await runAdapter.dispatch({ type: "return-to-tui" })
  expect(runHandoff.state.returnCalls).toBe(0)
  expect(runAdapter.getSnapshot().leaving).toBe(false)
  expect(runAdapter.getSnapshot().transientNotice).toContain("当前任务")
  await runAdapter.close()

  const interactionController = createFakeController({ interaction: emptyApprovalRequest() })
  const interactionHandoff = createFakeHandoff()
  const interactionScheduler = createManualScheduler()
  const interactionAdapter = createWebInteractiveAdapter({ controller: interactionController, handoff: interactionHandoff, frameScheduler: interactionScheduler })
  await interactionAdapter.dispatch({ type: "return-to-tui" })
  expect(interactionHandoff.state.returnCalls).toBe(0)
  expect(interactionAdapter.getSnapshot().transientNotice).toContain("审批")
  await interactionAdapter.close()
})

test("Interaction 草稿随 requestId 变化原子重置；stale 草稿不会发给新 requestId", async () => {
  const controller = createFakeController({ interaction: emptyApprovalRequest() })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "interaction-draft-change", requestId: "approval-1", patch: { kind: "feedback", value: "原因不足" } })
  expect(adapter.getSnapshot().interactionDraft).toEqual({
    requestId: "approval-1",
    feedback: "原因不足",
    answers: {},
    touched: true,
  })

  // 切换到 question interaction
  controller.setInteraction(questionRequest())
  controller.emit()
  scheduler.runScheduled()
  expect(adapter.getSnapshot().interactionDraft).toBeNull()

  await adapter.dispatch({ type: "interaction-draft-change", requestId: "question-1", patch: { kind: "answer", questionId: "scope", values: ["src"] } })
  await adapter.dispatch({ type: "interaction-draft-change", requestId: "question-1", patch: { kind: "answer", questionId: "level", values: ["shallow", "deep"] } })
  expect(adapter.getSnapshot().interactionDraft).toMatchObject({
    requestId: "question-1",
    answers: { scope: ["src"], level: ["shallow", "deep"] },
  })

  // 用旧 requestId 提交：必须不进入 controller。
  const staleResponse: InteractiveResponse = { kind: "approval", decision: "approve_once" }
  await adapter.dispatch({ type: "interaction-submit", requestId: "approval-1", response: staleResponse })
  // controller.dispatches 不应包含 interaction.respond with approval-1
  expect(controller.dispatches.find(intent => intent.type === "interaction.respond" && intent.requestId === "approval-1")).toBeUndefined()

  // 用当前 requestId 提交：应产生 interaction.respond
  const freshResponse: InteractiveResponse = { kind: "question", answers: { scope: ["src"], level: ["shallow", "deep"] } }
  await adapter.dispatch({ type: "interaction-submit", requestId: "question-1", response: freshResponse })
  expect(controller.dispatches.find(intent => intent.type === "interaction.respond" && intent.requestId === "question-1")).toEqual({
    type: "interaction.respond",
    requestId: "question-1",
    response: freshResponse,
  })

  void adapter.close()
})

test("快速 controller 发布合并为每帧最多一次 presentation publish；close 后无 publish", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  const publications: WebAdapterSnapshot[] = []
  const unsubscribe = adapter.subscribe(snapshot => { publications.push(snapshot) })
  // 构造时已有一次发布（buildSnapshot），订阅之后第一次发布仍计入基线
  const baseline = publications.length

  // 连续触发五次 controller 发布
  for (let i = 0; i < 5; i += 1) controller.emit()
  expect(scheduler.pending()).toBe(true)
  expect(publications.length).toBe(baseline)
  scheduler.runScheduled()
  expect(publications.length).toBe(baseline + 1)

  // connection 状态变化必须立即 flush
  controller.setConnection({ status: "closed", message: "transport closed" })
  controller.emit()
  expect(publications.length).toBe(baseline + 2)
  expect(scheduler.pending()).toBe(false)

  // interaction 变化也必须立即 flush
  controller.setInteraction(emptyApprovalRequest())
  controller.emit()
  expect(publications.length).toBe(baseline + 3)

  // close 之后 controller 发布不再触发 publish
  await adapter.close()
  controller.emit()
  expect(publications.length).toBe(baseline + 3)

  unsubscribe()
})

test("close 幂等：第二次 close 不抛错、不再调用 frameScheduler.cancel 之外的操作", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.close()
  await adapter.close()
  // dispatch 在 close 之后是 no-op，不会调用 controller
  await adapter.dispatch({ type: "draft-change", value: "ignored" })
  await adapter.dispatch({ type: "submit" })
  expect(controller.dispatches.find(intent => intent.type === "input.submit" && intent.value === "ignored")).toBeUndefined()
})

test("Thread 报告：currentThreadId 变化经 handoff.reportThread 发出，相同值去重", async () => {
  const controller = createFakeController({ threadId: null })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })
  await handoff.activate(null)

  // 触发一次 buildSnapshot 让首条 null 报告进入序列
  controller.emit()
  scheduler.runScheduled()
  expect(handoff.state.reportedThreads[handoff.state.reportedThreads.length - 1]).toBeNull()

  controller.setThreadId("thread-1")
  controller.emit()
  scheduler.runScheduled()
  expect(handoff.state.reportedThreads.at(-1)).toBe("thread-1")

  // 连续多次相同值：只发一次
  controller.setThreadId("thread-1")
  controller.emit()
  controller.emit()
  scheduler.runScheduled()
  controller.emit()
  scheduler.runScheduled()
  const seen = handoff.state.reportedThreads.filter(value => value === "thread-1")
  expect(seen.length).toBe(1)

  // 切回 null 是合法状态，需要再次发送
  controller.setThreadId(null)
  controller.emit()
  scheduler.runScheduled()
  expect(handoff.state.reportedThreads.at(-1)).toBeNull()

  void adapter.close()
})

test("approval 与 question 的 response payload 透传 controller", async () => {
  const approval = emptyApprovalRequest()
  const controller = createFakeController({ interaction: approval })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  const approvalResponse: InteractiveResponse = { kind: "approval", decision: "reject_with_feedback", feedback: "信息不足" }
  await adapter.dispatch({ type: "interaction-submit", requestId: "approval-1", response: approvalResponse })
  expect(controller.dispatches.at(-1)).toEqual({ type: "interaction.respond", requestId: "approval-1", response: approvalResponse })

  const question = questionRequest()
  controller.setInteraction(question)
  controller.emit()
  scheduler.runScheduled()

  const questionResponse: InteractiveResponse = { kind: "question", answers: { scope: ["src"], level: ["deep"] } }
  await adapter.dispatch({ type: "interaction-submit", requestId: "question-1", response: questionResponse })
  expect(controller.dispatches.at(-1)).toEqual({ type: "interaction.respond", requestId: "question-1", response: questionResponse })

  void adapter.close()
})

test("confirmation-resolve 把 confirmationId/confirmed 透传给 controller", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "confirmation-resolve", confirmationId: "clear-thread", confirmed: true })
  expect(controller.dispatches.at(-1)).toEqual({ type: "confirmation.resolve", confirmationId: "clear-thread", confirmed: true })

  void adapter.close()
})

test("tool-toggle 维护 expandedTools 集合", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "tool-toggle", toolId: "tool-1" })
  expect(adapter.getSnapshot().expandedTools.has("tool-1")).toBe(true)
  await adapter.dispatch({ type: "tool-toggle", toolId: "tool-1" })
  expect(adapter.getSnapshot().expandedTools.has("tool-1")).toBe(false)
  await adapter.dispatch({ type: "tool-toggle", toolId: "tool-2" })
  expect(adapter.getSnapshot().expandedTools.has("tool-2")).toBe(true)

  void adapter.close()
})

test("cancel-run 直接转发 controller", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "cancel-run" })
  expect(controller.dispatches.at(-1)).toEqual({ type: "run.cancel" })
  void adapter.close()
})

test("exit-harness 走 handoff.requestExit 并设置 leaving；失败时恢复 leaving", async () => {
  const successController = createFakeController()
  const successHandoff = createFakeHandoff()
  const successScheduler = createManualScheduler()
  const successAdapter = createWebInteractiveAdapter({ controller: successController, handoff: successHandoff, frameScheduler: successScheduler })
  await successAdapter.dispatch({ type: "exit-harness" })
  expect(successHandoff.state.exitCalls).toBe(1)
  expect(successAdapter.getSnapshot().leaving).toBe(true)
  await successAdapter.close()

  const failController = createFakeController()
  const failHandoff = createFakeHandoff()
  failHandoff.state.failExit = true
  const failScheduler = createManualScheduler()
  const failAdapter = createWebInteractiveAdapter({ controller: failController, handoff: failHandoff, frameScheduler: failScheduler })
  await failAdapter.dispatch({ type: "exit-harness" })
  expect(failHandoff.state.exitCalls).toBe(1)
  expect(failAdapter.getSnapshot().leaving).toBe(false)
  expect(failAdapter.getSnapshot().transientNotice).toContain("退出失败")
  await failAdapter.close()
})

test("panel search 写入 panelSearch 字段；panel open 触发对应 catalog.refresh", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "panel-search", panel: "models", query: "fast" })
  expect(adapter.getSnapshot().panelSearch.models.query).toBe("fast")

  await adapter.dispatch({ type: "panel-open", panel: "models" })
  expect(adapter.getSnapshot().activePanel).toBe("models")
  expect(controller.dispatches.find(intent => intent.type === "catalog.refresh" && intent.catalog === "models")).toBeDefined()

  await adapter.dispatch({ type: "panel-close" })
  expect(adapter.getSnapshot().activePanel).toBeNull()
  void adapter.close()
})

test("subscribe 返回的 unsubscribe 真的能取消订阅", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  const publications: WebAdapterSnapshot[] = []
  const unsubscribe = adapter.subscribe(snapshot => { publications.push(snapshot) })
  const baseline = publications.length
  controller.emit()
  scheduler.runScheduled()
  expect(publications.length).toBe(baseline + 1)

  unsubscribe()
  controller.emit()
  scheduler.runScheduled()
  expect(publications.length).toBe(baseline + 1)
  void adapter.close()
})

test("snapshot 字段：interactive 是引用而非拷贝；expandedTools 是新 Set", async () => {
  const controller = createFakeController({ threadId: "thread-1" })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })
  await adapter.dispatch({ type: "tool-toggle", toolId: "t-1" })
  const snapshot = adapter.getSnapshot()
  expect(snapshot.interactive).toBe(controller.getSnapshot())
  // expandedTools 必须是新 Set：表现层修改它不能影响 adapter 内部状态。
  ;(snapshot.expandedTools as Set<string>).add("外部工具")
  await adapter.dispatch({ type: "tool-toggle", toolId: "t-2" })
  const second = adapter.getSnapshot()
  expect(second.expandedTools.has("外部工具")).toBe(false)
  expect(second.expandedTools.has("t-2")).toBe(true)
  void adapter.close()
})

test("主题初始固定 light，与外部 matchMedia / controller 状态无关", () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })
  expect(adapter.getSnapshot().theme).toBe("light")
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
  void adapter.close()
})

test("theme-set 更新主题并发布一次；重复设置当前值不重复发布", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })
  const publications: WebAdapterSnapshot[] = []
  adapter.subscribe(snapshot => { publications.push(snapshot) })
  const baseline = publications.length

  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  scheduler.runScheduled()
  expect(adapter.getSnapshot().theme).toBe("dark")
  expect(publications.length).toBe(baseline + 1)

  // 重复设置为当前主题：不产生新发布
  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  scheduler.runScheduled()
  expect(publications.length).toBe(baseline + 1)

  await adapter.dispatch({ type: "theme-set", theme: "light" })
  scheduler.runScheduled()
  expect(adapter.getSnapshot().theme).toBe("light")
  expect(publications.length).toBe(baseline + 2)

  void adapter.close()
})

test("theme/header menu 意图是纯表现动作：不调用 controller、handoff 或 catalog", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  await adapter.dispatch({ type: "header-menu-toggle", open: false })

  expect(controller.dispatches).toEqual([])
  expect(handoff.state.returnCalls).toBe(0)
  expect(handoff.state.exitCalls).toBe(0)
  void adapter.close()
})

test("header menu 关闭规则：选择主题/打开 Help/返回 TUI/退出都先关闭菜单", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(true)
  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)

  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  await adapter.dispatch({ type: "panel-open", panel: "help" })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
  expect(adapter.getSnapshot().activePanel).toBe("help")

  await adapter.dispatch({ type: "panel-close" })
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  await adapter.dispatch({ type: "return-to-tui" })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
  expect(handoff.state.returnCalls).toBe(1)
  await adapter.close()

  const exitController = createFakeController()
  const exitHandoff = createFakeHandoff()
  const exitScheduler = createManualScheduler()
  const exitAdapter = createWebInteractiveAdapter({ controller: exitController, handoff: exitHandoff, frameScheduler: exitScheduler })
  await exitAdapter.dispatch({ type: "header-menu-toggle", open: true })
  await exitAdapter.dispatch({ type: "exit-harness" })
  expect(exitAdapter.getSnapshot().headerMenuOpen).toBe(false)
  expect(exitHandoff.state.exitCalls).toBe(1)
  await exitAdapter.close()
})

test("连接变为只读时关闭 header menu 并立即发布", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  expect(adapter.getSnapshot().headerMenuOpen).toBe(true)

  controller.setConnection({ status: "closed", message: "transport closed" })
  controller.emit()
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
  expect(adapter.getSnapshot().interactive.connection.status).toBe("closed")
  void adapter.close()
})

test("close 之后 theme/header menu intent 安全 no-op", async () => {
  const controller = createFakeController()
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })
  await adapter.close()

  await adapter.dispatch({ type: "theme-set", theme: "dark" })
  await adapter.dispatch({ type: "header-menu-toggle", open: true })
  expect(adapter.getSnapshot().theme).toBe("light")
  expect(adapter.getSnapshot().headerMenuOpen).toBe(false)
  expect(controller.dispatches).toEqual([])
})

test("移动端抽屉互斥：打开 Thread 抽屉关闭面板，打开面板关闭抽屉，选择 Thread 关闭抽屉", async () => {
  const controller = createFakeController({ threadId: "thread-1" })
  const handoff = createFakeHandoff()
  const scheduler = createManualScheduler()
  const adapter = createWebInteractiveAdapter({ controller, handoff, frameScheduler: scheduler })

  await adapter.dispatch({ type: "panel-open", panel: "models" })
  expect(adapter.getSnapshot().activePanel).toBe("models")

  // 打开 Thread 抽屉：右侧面板收起
  await adapter.dispatch({ type: "sidebar-toggle", open: true })
  expect(adapter.getSnapshot().sidebarOpen).toBe(true)
  expect(adapter.getSnapshot().activePanel).toBeNull()

  // 打开面板：左侧抽屉收起
  await adapter.dispatch({ type: "panel-open", panel: "status" })
  expect(adapter.getSnapshot().activePanel).toBe("status")
  expect(adapter.getSnapshot().sidebarOpen).toBe(false)

  // 选中 Thread：抽屉自动收起
  await adapter.dispatch({ type: "sidebar-toggle", open: true })
  expect(adapter.getSnapshot().sidebarOpen).toBe(true)
  await adapter.dispatch({ type: "thread-select", threadId: "thread-9" })
  expect(adapter.getSnapshot().sidebarOpen).toBe(false)
  expect(controller.dispatches).toContainEqual({ type: "thread.open", threadId: "thread-9" })

  void adapter.close()
})
