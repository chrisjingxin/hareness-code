/** WebApp：active=false 时显示接管只读提示；active=true 时 composer 可用。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act } from "react"

import { WebApp } from "../../../src/web/presentation/web-app"
import type { WebAdapterSnapshot, WebIntent, WebInteractiveAdapter } from "../../../src/web/application/adapter"
import { makeInteractive, makeSnapshot } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


type AdapterHarness = WebInteractiveAdapter & {
  emit(next: WebAdapterSnapshot): void
  intentLog: WebIntent[]
  closeCount: number
  listeners: Set<(snapshot: WebAdapterSnapshot) => void>
  storedSnapshot: WebAdapterSnapshot
}

function createFakeAdapter(initial: WebAdapterSnapshot): AdapterHarness {
  const listeners = new Set<(snapshot: WebAdapterSnapshot) => void>()
  const harness: AdapterHarness = {
    storedSnapshot: initial,
    intentLog: [],
    closeCount: 0,
    listeners,
    getSnapshot() { return this.storedSnapshot },
    subscribe(listener) {
      this.listeners.add(listener)
      return () => { this.listeners.delete(listener) }
    },
    async dispatch(intent) {
      this.intentLog.push(intent)
    },
    async close() {
      this.closeCount += 1
    },
    emit(next) {
      this.storedSnapshot = next
      for (const listener of [...this.listeners]) listener(next)
    },
  }
  return harness
}

function mountWebApp(active: boolean, snapshot: WebAdapterSnapshot): { adapter: AdapterHarness; handle: RenderHandle } {
  const adapter = createFakeAdapter(snapshot)
  const handle = render(<WebApp adapter={adapter} active={active} />)
  return { adapter, handle }
}

describe("WebApp", () => {
  test("active=false 时显示「正在接管」只读提示，且 composer disabled", () => {
    const { adapter, handle } = mountWebApp(false, makeSnapshot())
    try {
      const opening = handle.container.querySelector(".web-shell")
      expect(opening?.getAttribute("data-active")).toBe("false")
      expect(opening?.getAttribute("data-theme")).toBe("light")
      const handoff = handle.container.querySelector(".handoff-banner")
      expect(handoff?.textContent).toContain("等待 CLI 确认控制权")
      expect(handle.container.querySelector(".meta-chip-run")).toBeNull()
      const composer = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(composer?.disabled).toBe(true)
      expect(adapter.intentLog.length).toBe(0)
    } finally {
      handle.unmount()
    }
  })

  test("active=true 时「正在接管」提示消失，composer 接收用户输入", () => {
    const { handle } = mountWebApp(true, makeSnapshot({ draft: "hello" }))
    try {
      const opening = handle.container.querySelector(".web-shell")
      expect(opening?.getAttribute("data-active")).toBe("true")
      expect(handle.container.querySelector(".handoff-banner")).toBeNull()
      const composer = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(composer?.disabled).toBe(false)
      expect(composer?.value).toBe("hello")
    } finally {
      handle.unmount()
    }
  })

  test("Run 状态不再渲染顶栏活动状态入口", () => {
    const { handle } = mountWebApp(true, makeSnapshot({
      interactive: makeInteractive({ activity: { kind: "running" }, activeRun: { threadId: "t1", runId: "r1" } }),
    }))
    try {
      expect(handle.container.querySelector(".meta-chip-run")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("顶栏只读展示工作区，并移除分支与活动状态入口", () => {
    const { handle } = mountWebApp(true, makeSnapshot())
    try {
      expect(handle.container.querySelector(".brand-name")?.textContent).toBe("Harness Code")
      expect(handle.container.querySelector(".topbar-project .project-name")?.textContent).toBe("workspace")
      expect(handle.container.querySelector(".topbar-project .topbar-segment-chevron")).toBeNull()
      expect(handle.container.querySelector(".topbar-branch")).toBeNull()
      expect(handle.container.querySelector(".meta-chip-run")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("模型段不带下拉 chevron（点击是展开右侧 Dock 而非菜单）；审批模式保留下拉指示", () => {
    const { handle } = mountWebApp(true, makeSnapshot())
    try {
      // 断言转成 boolean：toBeNull 失败时会序列化整个 SVG 节点，输出过大导致测试超时。
      expect(handle.container.querySelector(".topbar-model .topbar-segment-chevron") === null).toBe(true)
      expect(handle.container.querySelector(".topbar-approval-mode .topbar-segment-chevron") !== null).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("active Run 时「返回 TUI」按钮被禁用并展示 reason", () => {
    const interactive = makeInteractive({
      activeRun: { threadId: "t1", runId: "r1" },
    })
    const { handle } = mountWebApp(true, makeSnapshot({ interactive }))
    try {
      const returnButton = handle.container.querySelector<HTMLButtonElement>(".return-button")
      expect(returnButton?.disabled).toBe(true)
      expect(returnButton?.title).toBe("当前任务结束或交互完成后可返回 TUI")
    } finally {
      handle.unmount()
    }
  })

  test("active 切换时 WebApp 重新读取 adapter.getSnapshot 并重新渲染", () => {
    const adapter = createFakeAdapter(makeSnapshot({ draft: "first" }))
    const handle = render(<WebApp adapter={adapter} active={true} />)
    try {
      const composer = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(composer?.value).toBe("first")
      act(() => {
        adapter.emit(makeSnapshot({ draft: "second" }))
      })
      const updated = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(updated?.value).toBe("second")
    } finally {
      handle.unmount()
    }
  })

  test("header menu 不再提供主题与审批模式入口（均已在顶栏）：只保留帮助与退出", () => {
    const { adapter, handle } = mountWebApp(true, makeSnapshot())
    try {
      act(() => {
        adapter.emit(makeSnapshot({ headerMenuOpen: true }))
      })
      const items = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".header-menu-item"))
      expect(items.map(item => item.textContent)).toEqual(["帮助", "退出 Harness"])
    } finally {
      handle.unmount()
    }
  })

  test("顶栏审批模式控件打开下拉并直接选择模式，忙碌时禁用", () => {
    const { adapter, handle } = mountWebApp(true, makeSnapshot())
    try {
      const control = handle.container.querySelector<HTMLButtonElement>(".topbar-approval-mode")
      expect(control).not.toBeNull()
      expect(control?.textContent).toContain("default")
      expect(control?.getAttribute("aria-label")).toBe("选择审批模式，当前：default")
      expect(control?.title).toContain("plan、default、auto-edit、auto、yolo")
      expect(control?.getAttribute("aria-haspopup")).toBe("menu")
      expect(control?.getAttribute("aria-expanded")).toBe("false")
      expect(control?.disabled).toBe(false)

      act(() => { control?.click() })
      expect(control?.getAttribute("aria-expanded")).toBe("true")
      const options = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".approval-mode-option"))
      expect(options.map(option => option.textContent?.replace("✓", "").trim())).toEqual(["plan", "default", "auto-edit", "auto", "yolo"])

      const autoOption = options.find(option => option.textContent?.includes("auto") && !option.textContent?.includes("auto-edit"))
      act(() => { autoOption?.click() })
      expect(adapter.intentLog).toContainEqual({ type: "approval-mode-select", mode: "auto" })
    } finally {
      handle.unmount()
    }

    const busy = makeInteractive({ activeRun: { threadId: "t1", runId: "r1" } })
    const busyMount = mountWebApp(true, makeSnapshot({ interactive: busy }))
    try {
      expect(busyMount.handle.container.querySelector<HTMLButtonElement>(".topbar-approval-mode")?.disabled).toBe(true)
    } finally {
      busyMount.handle.unmount()
    }
  })

  test("连接断开（只读）时 Tool 仍可展开：本地表现不受连接状态阻断", () => {
    const interactive = makeInteractive({
      connection: { status: "closed", message: "连接已断开" },
      timeline: [
        {
          type: "tool",
          tool: { id: "t1", runId: "run-1", name: "exec", arguments: "", output: "结果", status: "completed" },
        },
      ],
    })
    const { adapter, handle } = mountWebApp(true, makeSnapshot({ interactive }))
    try {
      const header = handle.container.querySelector<HTMLButtonElement>(".tool-row-header")
      expect(header).not.toBeNull()
      act(() => { header?.click() })
      expect(adapter.intentLog).toContainEqual({ type: "tool-toggle", runId: "run-1", toolId: "t1" })
    } finally {
      handle.unmount()
    }
  })

  test("data-theme 跟随 snapshot.theme 变化；theme-set 派发到 adapter", () => {
    const adapter = createFakeAdapter(makeSnapshot())
    const handle = render(<WebApp adapter={adapter} active={true} />)
    try {
      const shell = handle.container.querySelector(".web-shell")
      expect(shell?.getAttribute("data-theme")).toBe("light")
      act(() => {
        adapter.emit(makeSnapshot({ theme: "dark" }))
      })
      expect(shell?.getAttribute("data-theme")).toBe("dark")
    } finally {
      handle.unmount()
    }
  })

  test("主题切换在顶栏图标按钮：icon 与 aria-label 表达下一动作；overflow trigger 具备 aria-haspopup/aria-expanded", () => {
    const adapter = createFakeAdapter(makeSnapshot())
    const handle = render(<WebApp adapter={adapter} active={true} />)
    try {
      const trigger = handle.container.querySelector<HTMLButtonElement>(".overflow-trigger")
      expect(trigger?.getAttribute("aria-haspopup")).toBe("menu")
      expect(trigger?.getAttribute("aria-expanded")).toBe("false")
      expect(handle.container.querySelector(".header-menu")).toBeNull()

      // light → 展示 Moon（点击切深色）；dark → 展示 Sun（点击切浅色）。
      const toggle = handle.container.querySelector<HTMLButtonElement>(".theme-toggle")
      expect(toggle?.getAttribute("aria-label")).toBe("切换到深色主题")
      expect(toggle?.querySelector("svg.lucide-moon")).not.toBeNull()

      act(() => { toggle?.click() })
      expect(adapter.intentLog).toContainEqual({ type: "theme-set", theme: "dark" })

      act(() => {
        adapter.emit(makeSnapshot({ theme: "dark" }))
      })
      expect(toggle?.getAttribute("aria-label")).toBe("切换到浅色主题")
      expect(toggle?.querySelector("svg.lucide-sun")).not.toBeNull()

      act(() => { trigger?.click() })
      expect(adapter.intentLog).toContainEqual({ type: "header-menu-toggle", open: true })
    } finally {
      handle.unmount()
    }
  })

  test("overflow menu 支持 Arrow/Home/End 焦点导航", () => {
    const adapter = createFakeAdapter(makeSnapshot({ headerMenuOpen: true }))
    const handle = render(<WebApp adapter={adapter} active={true} />)
    try {
      const menu = handle.container.querySelector<HTMLElement>(".header-menu")
      const items = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".header-menu-item"))
      items[0]?.focus()
      act(() => { menu?.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true })) })
      expect(document.activeElement).toBe(items[items.length - 1])
      act(() => { menu?.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true })) })
      expect(document.activeElement).toBe(items[0])
    } finally {
      handle.unmount()
    }
  })

  test("Escape 优先级：header menu 打开时先关闭菜单而非关闭 Dock", () => {
    const adapter = createFakeAdapter(makeSnapshot({
      headerMenuOpen: true,
      contextDock: { open: true, activePanel: "status", widthPx: 560, code: { tabs: [], activePath: null, previews: {}, previewErrors: {} } },
    }))
    const handle = render(<WebApp adapter={adapter} active={true} />)
    try {
      const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
      window.dispatchEvent(event)
      expect(adapter.intentLog).toContainEqual({ type: "header-menu-toggle", open: false })
      expect(adapter.intentLog.find(intent => intent.type === "dock-close")).toBeUndefined()
    } finally {
      handle.unmount()
    }
  })

  test("Dock 打开时渲染 .context-dock，且中间 Conversation（composer）不被预览替换", () => {
    const adapter = createFakeAdapter(makeSnapshot({
      contextDock: { open: true, activePanel: "code", widthPx: 560, code: { tabs: [], activePath: null, previews: {}, previewErrors: {} } },
    }))
    const handle = render(<WebApp adapter={adapter} active={true} />)
    try {
      expect(handle.container.querySelector(".context-dock")).not.toBeNull()
      expect(handle.container.querySelector(".desktop-workspace")?.classList.contains("has-context-dock")).toBe(true)
      const composer = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(composer).not.toBeNull() // Conversation 永不被文件预览替换
    } finally {
      handle.unmount()
    }
  })

  test("Dock 关闭时 .context-dock 不渲染，workspace 恢复两列", () => {
    const adapter = createFakeAdapter(makeSnapshot())
    const handle = render(<WebApp adapter={adapter} active={true} />)
    try {
      expect(handle.container.querySelector(".context-dock")).toBeNull()
      expect(handle.container.querySelector(".desktop-workspace")?.classList.contains("has-context-dock")).toBe(false)
    } finally {
      handle.unmount()
    }
  })
})
