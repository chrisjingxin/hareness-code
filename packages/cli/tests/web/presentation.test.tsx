/** Web presentation DOM 契约：安全 Markdown、Tool 折叠和动态 Interaction 不越过 Adapter seam。 */
/** @jsxImportSource react */

import { afterAll, expect, test } from "bun:test"
import { act, createElement } from "react"

import type { WebAdapterSnapshot, WebIntent } from "../../src/web/application/adapter"
import { InteractionForm } from "../../src/web/presentation/interaction-form"
import { Markdown } from "../../src/web/presentation/markdown"
import { Timeline } from "../../src/web/presentation/timeline"
import type { InteractiveSnapshot } from "../../src/interactive/types"
import { registerTestDom, render, setControlledValue } from "./presentation/render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())

function snapshot(overrides: Partial<InteractiveSnapshot> = {}): InteractiveSnapshot {
  const base: InteractiveSnapshot = {
    currentThreadId: "thread-1",
    activity: { kind: "idle", label: "就绪" },
    activeRun: null,
    timeline: [],
    interaction: null,
    confirmation: null,
    lastRun: null,
    runtime: {
      workspace: "/workspace",
      cliVersion: "0.1.0",
      modelConfigured: true,
      modelName: "test-model",
      executionMode: "local",
      approvalMode: "default",
      capabilities: ["threads.read", "models.read", "skills.read", "mcp.read"],
    },
    connection: { status: "open" },
    commands: [],
    catalogs: {
      threads: { status: "ready", items: [] },
      models: { status: "ready", items: [] },
      skills: { status: "ready", items: [] },
      mcp: { status: "ready", items: [] },
    },
    selection: { requestedModelProfileId: null, actualModel: null, armedSkill: null },
  }
  return { ...base, ...overrides }
}

function adapterSnapshot(interactive: InteractiveSnapshot, overrides: Partial<WebAdapterSnapshot> = {}): WebAdapterSnapshot {
  return {
    interactive,
    draft: "",
    commandMenuOpen: false,
    commandMenuIndex: 0,
    commandOptions: [],
    contextDock: {
      open: false,
      activePanel: "code",
      widthPx: 560,
      code: { tabs: [], activePath: null, previews: {}, previewErrors: {} },
    },
    workspaceTree: { status: "idle", rows: [], selectedPath: null, limited: false },
    workspaceSidebar: { threadRatio: 0.38, selectedPath: null, widthPx: 280 },
    panelSearch: {
      code: { query: "", submitting: false, error: null },
      models: { query: "", submitting: false, error: null },
      skills: { query: "", submitting: false, error: null },
      mcp: { query: "", submitting: false, error: null },
      status: { query: "", submitting: false, error: null },
      help: { query: "", submitting: false, error: null },
    },
    expandedTools: new Set<string>(),
    interactionDraft: null,
    leaving: false,
    threadNewSubmitting: false,
    composerFocusRequest: 0,
    transientNotice: null,
    scrollRequest: null,
    confirmationId: null,
    ...overrides,
  }
}

test("Markdown 将 raw HTML、非法 scheme 和图片降级为文本", async () => {
  const { container, unmount } = render(createElement(Markdown, {
    text: "<script>alert(1)</script>",
  }))
  await act(async () => {})
  expect(container.querySelector("script")).toBeNull()
  expect(container.querySelector("img")).toBeNull()
  expect(container.textContent).toContain("<script>")
  unmount()

  const links = render(createElement(Markdown, {
    text: "[x](javascript:alert(1)) ![secret](https://example.com/a.png) [ok](https://example.com)",
  }))
  await act(async () => {})
  expect(links.container.querySelector("img")).toBeNull()
  expect(links.container.querySelector('a[href^="javascript:"]')).toBeNull()
  expect(links.container.querySelector('a[href^="https://"]')).not.toBeNull()
  links.unmount()
})

test("Timeline Tool 默认独立渲染，展开只发送 tool-toggle intent", async () => {
  const interactive = snapshot({
    timeline: [{ type: "tool", tool: { id: "tool-1", runId: "run-1", name: "read_file", arguments: "{\"path\":\"x\"}", output: "result", status: "completed" } }],
  })
  const adapter = adapterSnapshot(interactive)
  const intents: WebIntent[] = []
  const { container, unmount } = render(createElement(Timeline, { snapshot: adapter, dispatch: intent => { intents.push(intent) } }))
  await act(async () => {})
  const header = container.querySelector<HTMLButtonElement>(".tool-card-header")
  expect(header).not.toBeNull()
  expect(container.querySelector(".tool-details")).toBeNull()
  await act(async () => { header!.click() })
  expect(intents).toEqual([{ type: "tool-toggle", runId: "run-1", toolId: "tool-1" }])
  unmount()
})

test("Approval 只展示服务端 decisions，并把反馈和提交转成 typed intent", async () => {
  const interactive = snapshot({
    interaction: {
      type: "approval",
      requestId: "request-1",
      description: "执行写操作",
      requests: [{ tool: "write_file" }],
      decisions: ["approve_once", "reject_with_feedback"],
      deadlineAtMs: Date.now() + 60_000,
    },
  })
  const adapter = adapterSnapshot(interactive)
  const intents: WebIntent[] = []
  const { container, unmount } = render(createElement(InteractionForm, { snapshot: adapter, dispatch: intent => { intents.push(intent) } }))
  await act(async () => {})
  expect(container.textContent).toContain("允许一次")
  expect(container.textContent).toContain("拒绝并反馈")
  expect(container.textContent).not.toContain("允许本 Thread")
  const reject = [...container.querySelectorAll("button")].find(button => button.textContent?.includes("拒绝并反馈")) as HTMLButtonElement
  await act(async () => { reject.click() })
  const feedback = container.querySelector<HTMLTextAreaElement>(".interaction-feedback-input")
  expect(feedback?.disabled).toBe(false)
  act(() => { setControlledValue(feedback!, "缺少必要说明") })
  const submit = [...container.querySelectorAll("button")].find(button => button.textContent?.includes("提交")) as HTMLButtonElement
  await act(async () => { submit.click() })
  expect(intents.some(intent => intent.type === "interaction-draft-change" && intent.patch.kind === "approval-decision")).toBe(true)
  expect(intents.some(intent => intent.type === "interaction-submit" && intent.requestId === "request-1")).toBe(true)
  unmount()
})
