/** ThreadSidebar：桌面侧栏 / 移动抽屉 / busy 禁用 / thread-select dispatch。 */
/** @jsxImportSource react */

import { describe, expect, test } from "bun:test"
import { act } from "react"

import { ThreadSidebar } from "../../../src/web/presentation/thread-sidebar"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import { makeCatalog, makeInteractive, makeSnapshot, makeThread } from "./fixtures"
import { render, type RenderHandle } from "./render"

function mountSidebar(
  snapshot: WebAdapterSnapshot,
  intents: WebIntent[],
  narrow: boolean,
): RenderHandle {
  return render(
    <ThreadSidebar snapshot={snapshot} dispatch={intent => intents.push(intent)} narrow={narrow} />,
  )
}

describe("ThreadSidebar", () => {
  test("桌面侧栏（narrow=false）渲染 thread 列表；点击 dispatch thread-select", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      currentThreadId: "thread-1",
      catalogs: {
        ...makeInteractive().catalogs,
        threads: makeCatalog([makeThread({ thread_id: "thread-1", first_message: "你好" }), makeThread({ thread_id: "thread-2" })]),
      },
    })
    const handle = mountSidebar(makeSnapshot({ interactive }), intents, false)
    try {
      const sidebar = handle.container.querySelector("aside.sidebar")
      expect(sidebar).not.toBeNull()
      const items = handle.container.querySelectorAll<HTMLButtonElement>(".thread-item")
      expect(items.length).toBe(2)
      expect(items[0]?.textContent).toContain("你好")
      expect(handle.container.textContent).not.toContain("thread-1")
      act(() => { items[1]?.click() })
      expect(intents).toContainEqual({ type: "thread-select", threadId: "thread-2" })
    } finally {
      handle.unmount()
    }
  })

  test("activeRun 存在时新建 Thread 与列表项被禁用，并展示 reason", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      currentThreadId: "thread-1",
      activeRun: { threadId: "thread-1", runId: "run-1" },
      catalogs: {
        ...makeInteractive().catalogs,
        threads: makeCatalog([makeThread({ thread_id: "thread-1" }), makeThread({ thread_id: "thread-2" })]),
      },
    })
    const handle = mountSidebar(makeSnapshot({ interactive }), intents, false)
    try {
      const newButton = handle.container.querySelector<HTMLButtonElement>(".new-thread-button")
      expect(newButton?.disabled).toBe(true)
      expect(newButton?.title).toBe("当前任务结束后可用")
      const items = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".thread-item"))
      const inactive = items.find(button => button.getAttribute("data-active") === "false")
      expect(inactive?.disabled).toBe(true)
      const active = items.find(button => button.getAttribute("data-active") === "true")
      expect(active?.disabled).toBe(false)
    } finally {
      handle.unmount()
    }
  })

  test("移动端（narrow=true）渲染 drawer；sidebarOpen=false 时不可见", () => {
    const intents: WebIntent[] = []
    const handle = mountSidebar(makeSnapshot({ sidebarOpen: false }), intents, true)
    try {
      const drawer = handle.container.querySelector<HTMLElement>(".sidebar-drawer")
      expect(drawer).not.toBeNull()
      expect(drawer?.getAttribute("data-open")).toBe("false")
      expect(drawer?.getAttribute("aria-hidden")).toBe("true")
      expect(drawer?.hasAttribute("inert")).toBe(true)
      expect(handle.container.querySelector(".drawer-scrim")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("sidebarOpen=true 时 drawer data-open=true；关闭按钮 dispatch sidebar-toggle open:false", () => {
    const intents: WebIntent[] = []
    const handle = mountSidebar(makeSnapshot({ sidebarOpen: true }), intents, true)
    try {
      const drawer = handle.container.querySelector<HTMLElement>(".sidebar-drawer")
      expect(drawer?.getAttribute("data-open")).toBe("true")
      expect(drawer?.getAttribute("aria-hidden")).toBe("false")
      expect(drawer?.hasAttribute("inert")).toBe(false)
      expect(handle.container.querySelector(".drawer-scrim")).not.toBeNull()
      const close = handle.container.querySelector<HTMLButtonElement>(".sidebar-close")
      act(() => { close?.click() })
      expect(intents).toContainEqual({ type: "sidebar-toggle", open: false })
    } finally {
      handle.unmount()
    }
  })

  test("搜索框是受控输入，查询值来自 snapshot", () => {
    const handle = mountSidebar(makeSnapshot({
      panelSearch: {
        threads: { query: "query", loading: false, error: null },
        models: { query: "", loading: false, error: null },
        skills: { query: "", loading: false, error: null },
        mcp: { query: "", loading: false, error: null },
      },
    }), [], false)
    try {
      const search = handle.container.querySelector<HTMLInputElement>(".sidebar-search input")
      expect(search).not.toBeNull()
      expect(search?.value).toBe("query")
    } finally {
      handle.unmount()
    }
  })
})
