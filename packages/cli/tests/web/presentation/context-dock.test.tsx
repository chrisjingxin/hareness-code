/** ContextDock：各面板内容、主 tab 语义、capability 门禁、dock-close / dock-width-change（迁移自 panels.test）。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act } from "react"

import { ContextDock } from "../../../src/web/presentation/context-dock/context-dock"
import type { ContextDockPanel, WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import { makeCatalog, makeInteractive, makeMcp, makeModel, makeRuntime, makeSkill, makeSnapshot } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


function mountDock(snapshot: WebAdapterSnapshot, intents: WebIntent[]): RenderHandle {
  return render(
    <ContextDock snapshot={snapshot} dispatch={intent => intents.push(intent)} />,
  )
}

function dockSnapshot(activePanel: ContextDockPanel, overrides: Partial<WebAdapterSnapshot> = {}): WebAdapterSnapshot {
  return makeSnapshot({
    contextDock: { open: true, activePanel, widthPx: 560, code: { tabs: [], activePath: null, previews: {}, previewErrors: {} } },
    ...overrides,
  })
}

describe("ContextDock", () => {
  test("dock 关闭时不渲染任何内容", () => {
    const intents: WebIntent[] = []
    const handle = mountDock(makeSnapshot(), intents)
    try {
      expect(handle.container.querySelector(".context-dock")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=models 列出 Profile 并 dispatch model-select", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      catalogs: {
        ...makeInteractive().catalogs,
        models: makeCatalog([makeModel({ id: "p1", model: "gpt-x", provider_label: "Prov" }), makeModel({ id: "p2", model: "gpt-y", provider_label: "Prov" })]),
      },
    })
    const handle = mountDock(dockSnapshot("models", { interactive }), intents)
    try {
      const items = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".panel-item"))
      expect(items.length).toBe(2)
      act(() => { items[1]?.click() })
      expect(intents).toContainEqual({ type: "model-select", profileId: "p2" })
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=skills 无 skills.manage capability 时不渲染启停开关", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      runtime: makeRuntime({ capabilities: ["skills.read"] }),
      catalogs: {
        ...makeInteractive().catalogs,
        skills: makeCatalog([makeSkill({ id: "s1", name: "Skill 1", enabled: true, userInvocable: true })]),
      },
    })
    const handle = mountDock(dockSnapshot("skills", { interactive }), intents)
    try {
      const items = handle.container.querySelectorAll(".panel-item")
      expect(items.length).toBe(1)
      expect(handle.container.querySelector(".skill-toggle")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=skills 具备 skills.manage 时显示启停开关；切换 dispatch skill-set-enabled", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      runtime: makeRuntime({ capabilities: ["skills.read", "skills.manage"] }),
      catalogs: {
        ...makeInteractive().catalogs,
        skills: makeCatalog([makeSkill({ id: "s1", name: "Skill 1", enabled: true, userInvocable: true })]),
      },
    })
    const handle = mountDock(dockSnapshot("skills", { interactive }), intents)
    try {
      const toggle = handle.container.querySelector<HTMLInputElement>(".skill-toggle input[type=checkbox]")
      expect(toggle).not.toBeNull()
      expect(toggle?.checked).toBe(true)
      act(() => { toggle?.click() })
      expect(intents).toContainEqual({ type: "skill-set-enabled", skillId: "s1", enabled: false })
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=skills 列表中 disabled Skill 不可被 arm", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      runtime: makeRuntime({ capabilities: ["skills.read", "skills.manage"] }),
      catalogs: {
        ...makeInteractive().catalogs,
        skills: makeCatalog([
          makeSkill({ id: "s1", name: "已停用", enabled: false, userInvocable: true }),
          makeSkill({ id: "s2", name: "可用", enabled: true, userInvocable: true }),
        ]),
      },
    })
    const handle = mountDock(dockSnapshot("skills", { interactive }), intents)
    try {
      const items = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".panel-item"))
      const disabled = items.find(button => button.getAttribute("data-disabled") === "true")
      expect(disabled?.textContent).toContain("已停用")
      act(() => { disabled?.click() })
      expect(intents.find(intent => intent.type === "skill-arm" && intent.skillId === "s1")).toBeUndefined()
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=mcp 列出服务器；具备 mcp.manage 时显示删除按钮并 dispatch mcp-remove", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      runtime: makeRuntime({ capabilities: ["mcp.read", "mcp.manage"] }),
      catalogs: {
        ...makeInteractive().catalogs,
        mcp: makeCatalog([makeMcp({ name: "server-a" }), makeMcp({ name: "server-b" })]),
      },
    })
    const handle = mountDock(dockSnapshot("mcp", { interactive }), intents)
    try {
      const items = handle.container.querySelectorAll(".panel-list li")
      expect(items.length).toBe(2)
      const removeButtons = handle.container.querySelectorAll<HTMLButtonElement>(".icon-button[aria-label^='移除']")
      expect(removeButtons.length).toBe(2)
      act(() => { removeButtons[0]?.click() })
      expect(intents).toContainEqual({ type: "mcp-remove", name: "server-a" })
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=status 只读展示 runtime 与连接信息；内部不出现业务操作按钮", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      runtime: makeRuntime({ workspace: "/workspace", modelName: "gpt-x", gitWorkspace: { kind: "branch", branch: "main", root: "/workspace" } }),
      connection: { status: "open" },
    })
    const handle = mountDock(dockSnapshot("status", { interactive }), intents)
    try {
      const status = handle.container.querySelector(".status-view")
      expect(status).not.toBeNull()
      expect(status?.textContent).toContain("workspace")
      expect(status?.textContent).toContain("gpt-x")
      expect(status?.textContent).toContain("main")
      const businessButtons = status?.querySelectorAll("button")
      expect(businessButtons?.length ?? 0).toBe(0)
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=models 加载错误时点击重试 dispatch dock-panel-select models", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      catalogs: {
        ...makeInteractive().catalogs,
        models: makeCatalog([], "error", "请求失败"),
      },
    })
    const handle = mountDock(dockSnapshot("models", { interactive }), intents)
    try {
      const retry = handle.container.querySelector<HTMLButtonElement>(".panel-error button")
      expect(retry).not.toBeNull()
      act(() => { retry?.click() })
      expect(intents).toContainEqual({ type: "dock-panel-select", panel: "models" })
    } finally {
      handle.unmount()
    }
  })

  test("dock 关闭按钮 dispatch dock-close", () => {
    const intents: WebIntent[] = []
    const handle = mountDock(dockSnapshot("status"), intents)
    try {
      const close = handle.container.querySelector<HTMLButtonElement>(".panel-close")
      expect(close).not.toBeNull()
      act(() => { close?.click() })
      expect(intents).toContainEqual({ type: "dock-close" })
    } finally {
      handle.unmount()
    }
  })

  test("主 tab 使用 tablist/tab 语义：Code 恒可见；点击 dispatch dock-panel-select", () => {
    const intents: WebIntent[] = []
    const handle = mountDock(dockSnapshot("models"), intents)
    try {
      const tablist = handle.container.querySelector<HTMLElement>('[role="tablist"]')
      expect(tablist).not.toBeNull()
      const header = handle.container.querySelector<HTMLElement>(".context-dock-header")
      expect(header?.querySelector('[role="tablist"]')).toBe(tablist)
      expect(header?.querySelector(".context-dock-title")).toBeNull()
      expect(header?.querySelector(".panel-close")).not.toBeNull()
      expect(handle.container.querySelectorAll(".context-dock-header")).toHaveLength(1)
      const tabs = Array.from(handle.container.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
      expect(tabs.length).toBe(5) // Code|Model|Skills|MCP|Status
      const modelTab = tabs.find(tab => tab.textContent === "Model")
      const codeTab = tabs.find(tab => tab.textContent === "Code")
      expect(modelTab?.getAttribute("aria-selected")).toBe("true")
      expect(codeTab?.getAttribute("aria-selected")).toBe("false")
      act(() => { codeTab?.click() })
      expect(intents).toContainEqual({ type: "dock-panel-select", panel: "code" })
      const panel = handle.container.querySelector('[role="tabpanel"]')
      expect(panel?.getAttribute("id")).toBe("context-dock-panel")
    } finally {
      handle.unmount()
    }
  })

  test("主 tab 支持 Arrow/Home/End：移动焦点并派发对应 dock-panel-select", () => {
    const intents: WebIntent[] = []
    const handle = mountDock(dockSnapshot("models"), intents)
    try {
      const tablist = handle.container.querySelector<HTMLElement>('[role="tablist"]')
      const tabs = Array.from(handle.container.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
      tabs[0]?.focus() // Code 是第一个 tab
      act(() => { tablist?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })) })
      expect(document.activeElement).toBe(tabs[1])
      expect(intents).toContainEqual({ type: "dock-panel-select", panel: "models" })
      act(() => { tablist?.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true })) })
      expect(document.activeElement).toBe(tabs[tabs.length - 1])
      expect(intents).toContainEqual({ type: "dock-panel-select", panel: "status" })
    } finally {
      handle.unmount()
    }
  })

  test("主 tab 受 capability 过滤：无 MODELS_READ/SKILLS_READ/MCP_READ 时只显示 Code + Status", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      runtime: makeRuntime({ capabilities: ["status.read"] }),
    })
    const handle = mountDock(dockSnapshot("status", { interactive }), intents)
    try {
      const tabs = Array.from(handle.container.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
      expect(tabs.map(tab => tab.textContent)).toEqual(["Code", "Status"])
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=help 时标题为「帮助」且不渲染主 tab；内容列出命令", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({ commands: [] })
    const handle = mountDock(dockSnapshot("help", { interactive }), intents)
    try {
      const title = handle.container.querySelector(".context-dock-title")
      expect(title?.textContent).toBe("帮助")
      expect(handle.container.querySelector('[role="tablist"]')).toBeNull()
      expect(handle.container.querySelector(".help-view")).not.toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("Dock 宽度：左缘拖动 dispatch dock-width-change（宽度随位移递减）", () => {
    const intents: WebIntent[] = []
    const handle = mountDock(dockSnapshot("code"), intents)
    try {
      const resizeHandle = handle.container.querySelector<HTMLElement>(".dock-resize-handle")
      expect(resizeHandle).not.toBeNull()
      act(() => { resizeHandle!.dispatchEvent(new PointerEvent("pointerdown", { clientX: 500, bubbles: true })) })
      act(() => { window.dispatchEvent(new PointerEvent("pointermove", { clientX: 450, bubbles: true })) })
      act(() => { window.dispatchEvent(new PointerEvent("pointerup", { clientX: 450, bubbles: true })) })
      const widthIntent = intents.find(intent => intent.type === "dock-width-change")
      expect(widthIntent?.type).toBe("dock-width-change")
      if (widthIntent?.type === "dock-width-change") {
        // 起始 560 - (450 - 500) = 610（向左拖变宽）
        expect(widthIntent.widthPx).toBe(610)
      }
    } finally {
      handle.unmount()
    }
  })
})
