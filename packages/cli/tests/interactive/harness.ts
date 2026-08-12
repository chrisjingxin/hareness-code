/** Interactive Core 的 interface contract：只通过公开 interface 和内存 port 观察。 */

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

export const runtime: InteractiveRuntime = {
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
  ],
}

/** 手动 scheduler：测试直接驱动 timeout 回调。 */
export function manualScheduler() {
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
  // 模拟服务端持久化的线程模型选择（最近一次 Run 的 requested_selection）。
  let threadSelection: string | null = null
  let skillsList: { snapshot: Record<string, never>; skills: ReturnType<typeof skill>[]; diagnostics: string[] } = {
    snapshot: {},
    skills: [skill("user/repo-review-demo", true), skill("builtin/disabled-demo", false)],
    diagnostics: [],
  }
  let setSkillEnabledImpl: (skillId: string, enabled: boolean) => Promise<Record<string, never>> = async () => ({})
  let compactContextImpl: () => Promise<{ compacted: boolean; context: { action: string } }> = async () => ({
    compacted: true,
    context: { action: "manual_summary" },
  })

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
    setThreadSelection: (next: string | null) => void
    setSkillsList: (next: { skills: ReturnType<typeof skill>[] }) => void
    setSkillEnabledImpl: (impl: (skillId: string, enabled: boolean) => Promise<Record<string, never>>) => void
    setCompactContextImpl: (impl: () => Promise<{ compacted: boolean; context: { action: string } }>) => void
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
      return compactContextImpl()
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
      return {
        profiles,
        ...(threadSelection !== null ? { thread_selection: { primary_profile: threadSelection } } : {}),
      }
    },
    async listSkills(includeDisabled: boolean) {
      calls.push(`skills.list(${includeDisabled})`)
      return skillsList
    },
    async setSkillEnabled(skillId: string, enabled: boolean) {
      calls.push(`skills.set_enabled(${skillId},${enabled})`)
      return setSkillEnabledImpl(skillId, enabled)
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
    setThreadSelection(next) {
      threadSelection = next
    },
    setSkillsList(next) {
      skillsList = { snapshot: {}, skills: next.skills, diagnostics: [] }
    },
    setSkillEnabledImpl(impl) {
      setSkillEnabledImpl = impl
    },
    setCompactContextImpl(impl) {
      compactContextImpl = impl
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

export function makeHarness(options: {
  initialThreadId?: string | null
  configError?: boolean
  failOpenThread?: boolean
  holdConfigDetails?: boolean
  scheduler?: InteractiveScheduler
  capabilities?: Capability[]
} = {}) {
  const portState = createPort()
  const runtimeOverride: InteractiveRuntime = options.capabilities
    ? { ...runtime, capabilities: options.capabilities }
    : runtime
  const controller = createInteractiveController({
    agent: portState.port,
    runtime: runtimeOverride,
    ...(options.initialThreadId !== undefined ? { initialThreadId: options.initialThreadId } : {}),
    ...(options.scheduler !== undefined ? { scheduler: options.scheduler } : {}),
  })
  return { ...portState, controller }
}

export async function flush(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0))
  await new Promise(resolve => setTimeout(resolve, 0))
}

export function notices(snapshot: InteractiveSnapshot): string {
  return snapshot.timeline
    .flatMap(item => item.type === "message" && item.message.role === "system" ? [item.message.content] : [])
    .join("\n")
}

export function threadSummary(threadId: string, message: string) {
  return { thread_id: threadId, created_at_ms: 1, updated_at_ms: 2, first_message: message, latest_message: message, message_count: 2 }
}

export function skill(id: string, enabled: boolean) {
  return { id, name: id.split("/").at(-1)!, description: `描述 ${id}`, source: "user", enabled, user_invocable: true, argument_hint: "下一条消息使用" }
}

export function terminalEvent(type: string, threadId: string, runId: string, sequence: number, payload: Record<string, unknown>): EventEnvelope {
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

export function approvalRequest(threadId: string, runId: string, decisions = ["approve_once", "reject"]): InteractionRequestEnvelope {
  return {
    type: "approval",
    request_id: "approval-1",
    thread_id: threadId,
    run_id: runId,
    timeout_ms: 5_000,
    payload: { description: "需要执行工具", requests: [], decisions },
  } as InteractionRequestEnvelope
}

export function questionRequest(threadId: string, runId: string): InteractionRequestEnvelope {
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
