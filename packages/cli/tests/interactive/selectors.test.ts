/** Interactive Selector 契约测试：视图可序列化，FeatureAvailability 与 snapshot 状态一致。 */

import { expect, test } from "bun:test"
import { Capability, type ModelProfile, type ThreadSummary } from "@za38/protocol"
import type { InteractiveSnapshot } from "../../src/interactive/types"
import {
  selectCommandView,
  selectConversationView,
  selectFeatureAvailability,
  selectInteractionView,
  selectNavigationView,
  selectRuntimeView,
} from "../../src/interactive/selectors"

const CAPS = [
  Capability.THREADS_READ,
  Capability.MODELS_READ,
  Capability.MODELS_SELECT,
  Capability.SKILLS_READ,
  Capability.SKILLS_MANAGE,
  Capability.MCP_READ,
  Capability.MCP_MANAGE,
]

function snapshot(overrides: Partial<InteractiveSnapshot> = {}): InteractiveSnapshot {
  return {
    currentThreadId: "thread-1",
    activity: { kind: "idle" },
    activeRun: null,
    timeline: [],
    runProgress: null,
    interaction: null,
    confirmation: null,
    lastRun: null,
    runtime: { workspace: "/w", cliVersion: "0.1.0", modelConfigured: true, executionMode: "local", approvalMode: "default", capabilities: CAPS },
    connection: { status: "open" },
    commands: [],
    catalogs: {
      threads: { status: "idle", items: [] as readonly ThreadSummary[] },
      models: { status: "idle", items: [] as readonly ModelProfile[] },
      skills: { status: "idle", items: [] },
      mcp: { status: "idle", items: [] },
    },
    selection: { requestedModelProfileId: null, actualModel: null, armedSkill: null },
    ...overrides,
  }
}

test("FeatureAvailability：空闲且能力齐全时全部可用", () => {
  const availability = selectFeatureAvailability(snapshot())
  expect(availability.canSubmit).toBe(true)
  expect(availability.canCancelRun).toBe(false)
  expect(availability.canOpenThread).toBe(true)
  expect(availability.canToggleSkill).toBe(true)
  expect(availability.canManageMcp).toBe(true)
  expect(availability.canChangeModel).toBe(true)
  expect(availability.canOpenModelsPanel).toBe(true)
  expect(availability.canOpenSkillsPanel).toBe(true)
  expect(availability.canOpenMcpPanel).toBe(true)
})

test("FeatureAvailability：活动 Run 期间禁止切换 Thread 与变更 Skill/MCP，允许取消", () => {
  const availability = selectFeatureAvailability(snapshot({ activeRun: { threadId: "t", runId: "r" }, activity: { kind: "running" } }))
  expect(availability.canCancelRun).toBe(true)
  expect(availability.canOpenThread).toBe(false)
  expect(availability.canToggleSkill).toBe(false)
  expect(availability.canManageMcp).toBe(false)
})

test("FeatureAvailability：取消进行中不再允许重复取消", () => {
  const availability = selectFeatureAvailability(snapshot({ activeRun: { threadId: "t", runId: "r" }, activity: { kind: "cancelling" } }))
  expect(availability.canCancelRun).toBe(false)
})

test("FeatureAvailability：连接关闭禁止提交", () => {
  const availability = selectFeatureAvailability(snapshot({ connection: { status: "closed", message: "gone" } }))
  expect(availability.canSubmit).toBe(false)
})

test("FeatureAvailability：缺少能力时对应面板不可用", () => {
  const availability = selectFeatureAvailability(snapshot({ runtime: { workspace: "/w", cliVersion: "0.1.0", modelConfigured: true, executionMode: "local", approvalMode: "default", capabilities: [Capability.THREADS_READ] } }))
  expect(availability.canOpenModelsPanel).toBe(false)
  expect(availability.canOpenSkillsPanel).toBe(false)
  expect(availability.canOpenMcpPanel).toBe(false)
  expect(availability.canChangeModel).toBe(false)
  expect(availability.canToggleSkill).toBe(false)
  expect(availability.canManageMcp).toBe(false)
  expect(availability.hasSkillManage).toBe(false)
  expect(availability.hasMcpManage).toBe(false)
})

test("FeatureAvailability：纯 capability 门在 run 期间保持 true（面板可见仅禁用）", () => {
  const availability = selectFeatureAvailability(snapshot({ activeRun: { threadId: "t", runId: "r" }, activity: { kind: "running" } }))
  expect(availability.hasSkillManage).toBe(true)
  expect(availability.hasMcpManage).toBe(true)
  expect(availability.canToggleSkill).toBe(false)
  expect(availability.canManageMcp).toBe(false)
})

test("FeatureAvailability：挂起 Interaction 时禁止打开 Thread", () => {
  const availability = selectFeatureAvailability(snapshot({
    interaction: { type: "approval", requestId: "a-1", description: "", requests: null, decisions: ["reject"], deadlineAtMs: 1 },
  }))
  expect(availability.canOpenThread).toBe(false)
})

test("五个 Selector 输出只含可序列化字段（往返相等，拦截函数/Set/Map）", () => {
  const snap = snapshot({ activeRun: { threadId: "t", runId: "r" } })
  const views = [
    selectConversationView(snap),
    selectInteractionView(snap),
    selectNavigationView(snap),
    selectCommandView(snap),
    selectRuntimeView(snap),
  ]
  for (const view of views) {
    expect(JSON.parse(JSON.stringify(view))).toEqual(view)
  }
})

test("Selector 视图与 snapshot 一致：对话视图携带 timeline/activity", () => {
  const snap = snapshot({ timeline: [{ type: "message", message: { id: "m1", role: "user", content: "hi" } }] })
  const view = selectConversationView(snap)
  expect(view.currentThreadId).toBe("thread-1")
  expect(view.activity).toEqual({ kind: "idle" })
  expect(view.timeline).toHaveLength(1)
})

test("Selector 视图与 snapshot 一致：导航视图携带 catalogs 与可用性", () => {
  const view = selectNavigationView(snapshot())
  expect(view.catalogs.threads.status).toBe("idle")
  expect(view.availability.canOpenThread).toBe(true)
})

test("Selector 视图与 snapshot 一致：运行时视图携带 connection/selection", () => {
  const view = selectRuntimeView(snapshot({ selection: { requestedModelProfileId: "pro", actualModel: null, armedSkill: null } }))
  expect(view.connection.status).toBe("open")
  expect(view.selection.requestedModelProfileId).toBe("pro")
})
