/** UI 契约帧校验测试：尺寸、JSON、精确字段与类型白名单（fail-closed）。 */

import { expect, test } from "bun:test"

import {
  MAX_REQUEST_ID_LENGTH,
  MAX_UI_FRAME_BYTES,
  type WebUiClientMessage,
  type WebUiServerMessage,
  type WebUiState,
} from "../../src/presentation-coordinator/contracts/messages"
import { parseClientFrame, parseServerFrame } from "../../src/presentation-coordinator/contracts/validation"

// ---- 合法 fixture -----------------------------------------------------------

const conversation = {
  currentThreadId: "thread-1",
  activity: { kind: "idle" },
  activeRun: null,
  timeline: [],
  lastRun: null,
}
const interaction = { interaction: null, confirmation: null }
const navigation = {
  catalogs: {
    threads: { status: "idle", items: [] },
    models: { status: "idle", items: [] },
    skills: { status: "idle", items: [] },
    mcp: { status: "idle", items: [] },
  },
  availability: {
    canOpenThread: true,
    canOpenModelsPanel: true,
    canOpenSkillsPanel: true,
    canOpenMcpPanel: true,
    hasSkillManage: true,
    hasMcpManage: true,
  },
}
const command = { commands: [], availability: { canSubmit: true } }
const runtime = {
  runtime: { workspace: "/w", cliVersion: "0.1.0", modelConfigured: true, executionMode: "local", approvalMode: "default", capabilities: [] },
  connection: { status: "open" },
  selection: { requestedModelProfileId: null, actualModel: null, armedSkill: null },
  availability: { canCancelRun: false, canToggleSkill: false, canManageMcp: false, canChangeModel: false },
}

const fullState: WebUiState = { conversation, interaction, navigation, command, runtime }

const clientMessages: WebUiClientMessage[] = [
  { type: "handoff.ready" },
  { type: "handoff.return" },
  { type: "handoff.exit" },
  { type: "presentation-intent", intent: { type: "theme.set", theme: "dark" } },
  { type: "presentation-intent", intent: { type: "panel.open", panel: "threads" } },
  { type: "presentation-intent", intent: { type: "panel.close" } },
  { type: "intent", requestId: "r-1", revision: 1, intent: { type: "input.submit", value: "hello" } },
  { type: "intent", requestId: "r-2", revision: 0, intent: { type: "command.execute", commandId: "clear", argument: "" } },
  { type: "intent", requestId: "r-3", revision: 3, intent: { type: "run.cancel" } },
  { type: "intent", requestId: "r-4", revision: 1, intent: { type: "catalog.refresh", catalog: "mcp" } },
  { type: "intent", requestId: "r-5", revision: 1, intent: { type: "thread.open", threadId: "t-1" } },
  { type: "intent", requestId: "r-6", revision: 1, intent: { type: "model.select", profileId: "fast" } },
  { type: "intent", requestId: "r-7", revision: 1, intent: { type: "skill.arm", skillId: "user/demo" } },
  { type: "intent", requestId: "r-8", revision: 1, intent: { type: "skill.clear" } },
  { type: "intent", requestId: "r-9", revision: 1, intent: { type: "skill.set-enabled", skillId: "user/demo", enabled: true } },
  { type: "intent", requestId: "r-10", revision: 1, intent: { type: "mcp.add", input: { name: "filesystem" } } },
  { type: "intent", requestId: "r-11", revision: 1, intent: { type: "mcp.remove", name: "filesystem" } },
  {
    type: "intent",
    requestId: "r-12",
    revision: 1,
    intent: { type: "interaction.respond", requestId: "q-1", response: { kind: "question", answers: { scope: ["src"] } } },
  },
  {
    type: "intent",
    requestId: "r-13",
    revision: 1,
    intent: { type: "interaction.respond", requestId: "a-1", response: { kind: "approval", decision: "reject", feedback: "no" } },
  },
  { type: "intent", requestId: "r-14", revision: 1, intent: { type: "confirmation.resolve", confirmationId: "clear-thread", confirmed: false } },
  { type: "intent", requestId: "r-15", revision: 1, intent: { type: "approval-mode.cycle" } },
]

const serverMessages: WebUiServerMessage[] = [
  { type: "state.replace", revision: 1, state: fullState },
  { type: "state.patch", revision: 2, patch: { runtime } },
  { type: "intent.outcome", requestId: "r-1", outcome: { status: "accepted" } },
  { type: "intent.outcome", requestId: "r-2", outcome: { status: "accepted", effects: [] } },
  { type: "intent.outcome", requestId: "r-3", outcome: { status: "rejected", code: "busy", message: "busy" } },
  { type: "handoff.state", state: { phase: "tui-active" } },
  { type: "handoff.state", state: { phase: "opening-web", handoffId: "h-1" } },
  { type: "handoff.state", state: { phase: "web-active", handoffId: "h-1" } },
  { type: "handoff.state", state: { phase: "returning-tui", handoffId: "h-1", reason: "returned" } },
]

test("parseClientFrame：全部合法帧 JSON 往返后形状不变", () => {
  for (const message of clientMessages) {
    expect(parseClientFrame(JSON.stringify(message))).toEqual(message)
  }
})

test("parseServerFrame：全部合法帧 JSON 往返后形状不变", () => {
  for (const message of serverMessages) {
    expect(parseServerFrame(JSON.stringify(message))).toEqual(message)
  }
})

test("未知 type / 越界 type 一律拒绝", () => {
  expect(parseClientFrame(JSON.stringify({ type: "state.replace" }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ type: "handoff.dance" }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ type: "" }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "intent", requestId: "r", revision: 1 }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "unknown" }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ type: 42 }))).toBeUndefined()
})

test("非 JSON / 非对象输入拒绝", () => {
  expect(parseClientFrame("not json")).toBeUndefined()
  expect(parseClientFrame("")).toBeUndefined()
  expect(parseClientFrame("123")).toBeUndefined()
  expect(parseClientFrame("null")).toBeUndefined()
  expect(parseClientFrame('"hello"')).toBeUndefined()
  expect(parseClientFrame("[1,2,3]")).toBeUndefined()
  expect(parseServerFrame("not json")).toBeUndefined()
  expect(parseServerFrame("[]")).toBeUndefined()
})

test("缺少/多余字段拒绝（exactFields）", () => {
  // intent 缺 requestId / revision / intent
  expect(parseClientFrame(JSON.stringify({ type: "intent", revision: 1, intent: { type: "run.cancel" } }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ type: "intent", requestId: "r", intent: { type: "run.cancel" } }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ type: "intent", requestId: "r", revision: 1 }))).toBeUndefined()
  // intent 多余字段
  expect(parseClientFrame(JSON.stringify({ type: "intent", requestId: "r", revision: 1, intent: { type: "run.cancel" }, extra: 1 }))).toBeUndefined()
  // handoff.* 带多余字段
  expect(parseClientFrame(JSON.stringify({ type: "handoff.ready", extra: 1 }))).toBeUndefined()
  // presentation-intent 带多余字段 / 缺 intent
  expect(parseClientFrame(JSON.stringify({ type: "presentation-intent", intent: { type: "panel.close" }, extra: 1 }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ type: "presentation-intent" }))).toBeUndefined()
  // 服务端帧同理
  expect(parseServerFrame(JSON.stringify({ type: "state.replace", revision: 1, state: fullState, extra: 1 }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "handoff.state" }))).toBeUndefined()
})

test("requestId：空 / 超长拒绝", () => {
  const base = { type: "intent", revision: 1, intent: { type: "run.cancel" } }
  expect(parseClientFrame(JSON.stringify({ ...base, requestId: "" }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ ...base, requestId: "x".repeat(MAX_REQUEST_ID_LENGTH + 1) }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ ...base, requestId: 42 }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ ...base, requestId: "r-ok" }))).toEqual({ ...base, requestId: "r-ok" })
  // 服务端 intent.outcome 的 requestId 同样受限
  expect(parseServerFrame(JSON.stringify({ type: "intent.outcome", requestId: "", outcome: { status: "accepted" } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "intent.outcome", requestId: "x".repeat(MAX_REQUEST_ID_LENGTH + 1), outcome: { status: "accepted" } }))).toBeUndefined()
})

test("revision：负数 / 非整数 / 非 number 拒绝", () => {
  const base = { type: "intent", requestId: "r", intent: { type: "run.cancel" } }
  expect(parseClientFrame(JSON.stringify({ ...base, revision: -1 }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ ...base, revision: 1.5 }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ ...base, revision: "1" }))).toBeUndefined()
  expect(parseClientFrame(JSON.stringify({ ...base, revision: 0 }))).toEqual({ ...base, revision: 0 })
  // 服务端 replace/patch 的 revision 同样受限
  expect(parseServerFrame(JSON.stringify({ type: "state.replace", revision: -1, state: fullState }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.replace", revision: 1.5, state: fullState }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.patch", revision: -1, patch: { runtime } }))).toBeUndefined()
})

test("intent：各 type 的最小字段校验", () => {
  const frame = (intent: unknown) => JSON.stringify({ type: "intent", requestId: "r", revision: 1, intent })
  // 未知 intent type 拒绝
  expect(parseClientFrame(frame({ type: "thread.delete" }))).toBeUndefined()
  // thread.open 缺 threadId / 空 / 超长
  expect(parseClientFrame(frame({ type: "thread.open" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "thread.open", threadId: "" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "thread.open", threadId: "x".repeat(257) }))).toBeUndefined()
  // input.submit value 非 string / 缺失
  expect(parseClientFrame(frame({ type: "input.submit", value: 42 }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "input.submit" }))).toBeUndefined()
  // command.execute 缺 commandId / 缺 argument 键 / argument 非 string
  expect(parseClientFrame(frame({ type: "command.execute", commandId: "clear" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "command.execute", argument: "" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "command.execute", commandId: "clear", argument: 42 }))).toBeUndefined()
  // run.cancel 带多余字段
  expect(parseClientFrame(frame({ type: "run.cancel", extra: 1 }))).toBeUndefined()
  // catalog.refresh 未知 catalog
  expect(parseClientFrame(frame({ type: "catalog.refresh", catalog: "unknown" }))).toBeUndefined()
  // model.select / skill.arm 缺 id
  expect(parseClientFrame(frame({ type: "model.select" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "skill.arm" }))).toBeUndefined()
  // skill.set-enabled enabled 非 boolean
  expect(parseClientFrame(frame({ type: "skill.set-enabled", skillId: "user/demo", enabled: "yes" }))).toBeUndefined()
  // mcp.add 缺 input / input 缺 name / name 为空
  expect(parseClientFrame(frame({ type: "mcp.add" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "mcp.add", input: {} }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "mcp.add", input: { name: "" } }))).toBeUndefined()
  // mcp.remove 缺 name
  expect(parseClientFrame(frame({ type: "mcp.remove" }))).toBeUndefined()
  // interaction.respond 缺 response / 未知 kind
  expect(parseClientFrame(frame({ type: "interaction.respond", requestId: "q-1" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "interaction.respond", requestId: "q-1", response: { kind: "nope" } }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "interaction.respond", requestId: "q-1", response: { kind: "question", answers: { scope: [42] } } }))).toBeUndefined()
  // confirmation.resolve confirmed 非 boolean
  expect(parseClientFrame(frame({ type: "confirmation.resolve", confirmationId: "clear-thread", confirmed: "yes" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "confirmation.resolve", confirmed: true }))).toBeUndefined()
  // approval-mode.cycle 带多余字段
  expect(parseClientFrame(frame({ type: "approval-mode.cycle", extra: 1 }))).toBeUndefined()
  // skill.clear 带多余字段
  expect(parseClientFrame(frame({ type: "skill.clear", extra: 1 }))).toBeUndefined()
})

test("presentation-intent：theme/panel 枚举与长度校验", () => {
  const frame = (intent: unknown) => JSON.stringify({ type: "presentation-intent", intent })
  expect(parseClientFrame(frame({ type: "theme.set", theme: "light" }))).toEqual({ type: "presentation-intent", intent: { type: "theme.set", theme: "light" } })
  expect(parseClientFrame(frame({ type: "theme.set", theme: "blue" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "theme.set" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "panel.open" }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "panel.open", panel: "x".repeat(65) }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "panel.close", extra: 1 }))).toBeUndefined()
  expect(parseClientFrame(frame({ type: "panel.minimize" }))).toBeUndefined()
  expect(parseClientFrame(frame("not-an-object"))).toBeUndefined()
})

test("state.replace：缺分片 / 非 record 分片 / 多余字段拒绝", () => {
  const { conversation: _dropped, ...withoutConversation } = fullState
  expect(parseServerFrame(JSON.stringify({ type: "state.replace", revision: 1, state: withoutConversation }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.replace", revision: 1, state: { ...fullState, extra: 1 } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.replace", revision: 1, state: { ...fullState, conversation: 42 } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.replace", revision: 1, state: { ...fullState, navigation: [] } }))).toBeUndefined()
})

test("state.patch：空对象 / 未知分片 / 非 record 分片拒绝", () => {
  expect(parseServerFrame(JSON.stringify({ type: "state.patch", revision: 2, patch: {} }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.patch", revision: 2, patch: { unknown: {} } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.patch", revision: 2, patch: { runtime: 42 } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "state.patch", revision: 2, patch: { conversation: { ok: true }, runtime } }))).toEqual({
    type: "state.patch",
    revision: 2,
    patch: { conversation: { ok: true }, runtime },
  })
})

test("intent.outcome：rejected 缺 code/message、accepted effects 非数组拒绝", () => {
  expect(parseServerFrame(JSON.stringify({ type: "intent.outcome", requestId: "r", outcome: { status: "rejected", code: "busy" } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "intent.outcome", requestId: "r", outcome: { status: "rejected", message: "x" } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "intent.outcome", requestId: "r", outcome: { status: "rejected", code: "x".repeat(65), message: "x" } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "intent.outcome", requestId: "r", outcome: { status: "accepted", effects: "no" } }))).toBeUndefined()
  expect(parseServerFrame(JSON.stringify({ type: "intent.outcome", requestId: "r", outcome: { status: "weird" } }))).toBeUndefined()
})

test("handoff.state：各 phase 合法/非法", () => {
  const frame = (state: unknown) => JSON.stringify({ type: "handoff.state", state })
  // tui-active 精确字段
  expect(parseServerFrame(frame({ phase: "tui-active" }))).toEqual({ type: "handoff.state", state: { phase: "tui-active" } })
  expect(parseServerFrame(frame({ phase: "tui-active", extra: 1 }))).toBeUndefined()
  // opening-web / web-active 必须带非空 handoffId
  expect(parseServerFrame(frame({ phase: "opening-web", handoffId: "h" }))).toEqual({ type: "handoff.state", state: { phase: "opening-web", handoffId: "h" } })
  expect(parseServerFrame(frame({ phase: "web-active", handoffId: "h" }))).toEqual({ type: "handoff.state", state: { phase: "web-active", handoffId: "h" } })
  expect(parseServerFrame(frame({ phase: "opening-web" }))).toBeUndefined()
  expect(parseServerFrame(frame({ phase: "web-active", handoffId: "" }))).toBeUndefined()
  expect(parseServerFrame(frame({ phase: "opening-web", handoffId: "h", extra: 1 }))).toBeUndefined()
  expect(parseServerFrame(frame({ phase: "web-active", handoffId: "x".repeat(257) }))).toBeUndefined()
  // returning-tui 必须带 handoffId + 白名单 reason
  expect(parseServerFrame(frame({ phase: "returning-tui", handoffId: "h", reason: "exit-requested" }))).toEqual({
    type: "handoff.state",
    state: { phase: "returning-tui", handoffId: "h", reason: "exit-requested" },
  })
  expect(parseServerFrame(frame({ phase: "returning-tui", handoffId: "h" }))).toBeUndefined()
  expect(parseServerFrame(frame({ phase: "returning-tui", handoffId: "h", reason: "nope" }))).toBeUndefined()
  expect(parseServerFrame(frame({ phase: "returning-tui", reason: "returned" }))).toBeUndefined()
  // 未知 phase
  expect(parseServerFrame(frame({ phase: "handoff-started" }))).toBeUndefined()
})

test("超大帧：超过 MAX_UI_FRAME_BYTES 的输入拒绝（fail-closed）", () => {
  const oversized = "x".repeat(MAX_UI_FRAME_BYTES + 1)
  expect(parseClientFrame(oversized)).toBeUndefined()
  expect(parseServerFrame(oversized)).toBeUndefined()

  // 合法 JSON 恰好压线（等长空白填充）→ 通过；超 1 字节 → 拒绝
  const base = JSON.stringify({ type: "handoff.ready" })
  const exactlyAtLimit = base + " ".repeat(MAX_UI_FRAME_BYTES - base.length)
  expect(new TextEncoder().encode(exactlyAtLimit).byteLength).toBe(MAX_UI_FRAME_BYTES)
  expect(parseClientFrame(exactlyAtLimit)).toEqual({ type: "handoff.ready" })
  const oneOverLimit = base + " ".repeat(MAX_UI_FRAME_BYTES - base.length + 1)
  expect(new TextEncoder().encode(oneOverLimit).byteLength).toBe(MAX_UI_FRAME_BYTES + 1)
  expect(parseClientFrame(oneOverLimit)).toBeUndefined()
  expect(parseServerFrame(oneOverLimit)).toBeUndefined()
})

test("契约版本与帧上限为共享常量", () => {
  expect(MAX_UI_FRAME_BYTES).toBeGreaterThan(0)
  expect(MAX_REQUEST_ID_LENGTH).toBeGreaterThan(0)
  expect(JSON.parse(JSON.stringify(fullState))).toBeTruthy()
})
