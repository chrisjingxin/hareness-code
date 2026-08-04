/** UtilityPanels：activePanel 切换、各面板内容、retry 行为、Skill 启停受 capability 约束。 */
/** @jsxImportSource react */

import { describe, expect, test } from "bun:test"
import { act } from "react"

import { UtilityPanels } from "../../../src/web/presentation/panels"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import { makeCatalog, makeInteractive, makeMcp, makeModel, makeRuntime, makeSkill, makeSnapshot } from "./fixtures"
import { render, type RenderHandle } from "./render"

function mountPanel(snapshot: WebAdapterSnapshot, intents: WebIntent[]): RenderHandle {
  return render(
    <UtilityPanels snapshot={snapshot} dispatch={intent => intents.push(intent)} />,
  )
}

describe("UtilityPanels", () => {
  test("activePanel 为 null 时不渲染任何内容", () => {
    const intents: WebIntent[] = []
    const handle = mountPanel(makeSnapshot(), intents)
    try {
      expect(handle.container.querySelector(".utility-drawer")).toBeNull()
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
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "models" }), intents)
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
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "skills" }), intents)
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
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "skills" }), intents)
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
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "skills" }), intents)
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
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "mcp" }), intents)
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
      runtime: makeRuntime({ workspace: "/workspace", modelName: "gpt-x", gitBranch: "main" }),
      connection: { status: "open" },
    })
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "status" }), intents)
    try {
      const status = handle.container.querySelector(".status-view")
      expect(status).not.toBeNull()
      expect(status?.textContent).toContain("workspace")
      expect(status?.textContent).toContain("gpt-x")
      expect(status?.textContent).toContain("main")
      const view = handle.container.querySelector(".status-view")
      const businessButtons = view?.querySelectorAll("button")
      expect(businessButtons?.length ?? 0).toBe(0)
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=threads 加载错误时显示重试，点击 dispatch thread-refresh", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      catalogs: {
        ...makeInteractive().catalogs,
        threads: makeCatalog([], "error", "网络异常"),
      },
    })
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "threads" }), intents)
    try {
      const error = handle.container.querySelector(".panel-error")
      expect(error?.textContent).toContain("网络异常")
      const retry = error?.querySelector<HTMLButtonElement>("button")
      expect(retry).not.toBeNull()
      act(() => { retry?.click() })
      expect(intents).toContainEqual({ type: "thread-refresh" })
    } finally {
      handle.unmount()
    }
  })

  test("activePanel=models 加载错误时点击重试 dispatch panel-open models", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      catalogs: {
        ...makeInteractive().catalogs,
        models: makeCatalog([], "error", "请求失败"),
      },
    })
    const handle = mountPanel(makeSnapshot({ interactive, activePanel: "models" }), intents)
    try {
      const retry = handle.container.querySelector<HTMLButtonElement>(".panel-error button")
      expect(retry).not.toBeNull()
      act(() => { retry?.click() })
      expect(intents).toContainEqual({ type: "panel-open", panel: "models" })
    } finally {
      handle.unmount()
    }
  })

  test("面板关闭按钮 dispatch panel-close", () => {
    const intents: WebIntent[] = []
    const handle = mountPanel(makeSnapshot({ activePanel: "status" }), intents)
    try {
      const close = handle.container.querySelector<HTMLButtonElement>(".panel-close")
      expect(close).not.toBeNull()
      act(() => { close?.click() })
      expect(intents).toContainEqual({ type: "panel-close" })
    } finally {
      handle.unmount()
    }
  })
})
