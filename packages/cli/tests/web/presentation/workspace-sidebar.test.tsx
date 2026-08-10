/** WorkspaceSidebar：Thread/Files 同屏、列表 dispatch、busy 禁用、比例拖动（迁移自 thread-sidebar.test）。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act } from "react"

import { WorkspaceSidebar } from "../../../src/web/presentation/workspace-sidebar/workspace-sidebar"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import type { WorkspaceTreeRow } from "../../../src/workspace/types"
import { makeCatalog, makeInteractive, makeSnapshot, makeThread } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


function mountSidebar(snapshot: WebAdapterSnapshot, intents: WebIntent[]): RenderHandle {
  return render(
    <WorkspaceSidebar snapshot={snapshot} dispatch={intent => intents.push(intent)} />,
  )
}

function treeRow(overrides: Partial<WorkspaceTreeRow>): WorkspaceTreeRow {
  return {
    path: "src",
    name: "src",
    kind: "directory",
    depth: 0,
    expanded: false,
    loading: false,
    hasChildren: true,
    ...overrides,
  }
}

describe("WorkspaceSidebar", () => {
  test("Thread 列表与 Files 分区同屏渲染；点击 thread dispatch thread-select", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      currentThreadId: "thread-1",
      catalogs: {
        ...makeInteractive().catalogs,
        threads: makeCatalog([makeThread({ thread_id: "thread-1", first_message: "你好" }), makeThread({ thread_id: "thread-2" })]),
      },
    })
    const handle = mountSidebar(makeSnapshot({ interactive }), intents)
    try {
      const sidebar = handle.container.querySelector("aside.workspace-sidebar")
      expect(sidebar).not.toBeNull()
      const items = handle.container.querySelectorAll<HTMLButtonElement>(".thread-item")
      expect(items.length).toBe(2)
      expect(items[0]?.textContent).toContain("你好")
      expect(handle.container.textContent).not.toContain("thread-1")
      expect(handle.container.querySelector(".file-explorer")).not.toBeNull()
      expect(handle.container.querySelector(".file-tree")).not.toBeNull()
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
    const handle = mountSidebar(makeSnapshot({ interactive }), intents)
    try {
      const newButton = handle.container.querySelector<HTMLButtonElement>(".new-thread-button")
      expect(newButton?.disabled).toBe(true)
      expect(newButton?.classList.contains("button-secondary")).toBe(true)
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

  test("新建 Thread 提交中（threadNewSubmitting）按钮禁用防重复点击", () => {
    const intents: WebIntent[] = []
    const handle = mountSidebar(makeSnapshot({ threadNewSubmitting: true }), intents)
    try {
      const newButton = handle.container.querySelector<HTMLButtonElement>(".new-thread-button")
      expect(newButton?.disabled).toBe(true)
      expect(newButton?.title).toBe("正在新建…")
      act(() => { newButton?.click() })
      expect(intents).toHaveLength(0)
    } finally {
      handle.unmount()
    }
  })

  test("Thread 搜索框渲染且列表展示全部项；Files 分区同屏", () => {
    const interactive = makeInteractive({
      catalogs: {
        ...makeInteractive().catalogs,
        threads: makeCatalog([makeThread({ thread_id: "t1", first_message: "alpha" }), makeThread({ thread_id: "t2", first_message: "beta" })]),
      },
    })
    const handle = mountSidebar(makeSnapshot({ interactive }), [])
    try {
      const search = handle.container.querySelector<HTMLInputElement>(".sidebar-search input")
      expect(search).not.toBeNull()
      expect(search?.getAttribute("placeholder")).toBe("搜索 Thread…")
      const items = handle.container.querySelectorAll<HTMLButtonElement>(".thread-item")
      expect(items.length).toBe(2)
      expect(handle.container.querySelector(".file-explorer")).not.toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("垂直分隔条拖动 dispatch sidebar-thread-ratio-change（向下拖动增大比例）", () => {
    const intents: WebIntent[] = []
    const handle = mountSidebar(makeSnapshot(), intents)
    try {
      const resizeHandle = handle.container.querySelector<HTMLElement>(".vertical-resize-handle")
      expect(resizeHandle).not.toBeNull()
      act(() => { resizeHandle!.dispatchEvent(new PointerEvent("pointerdown", { clientY: 100, bubbles: true })) })
      act(() => { window.dispatchEvent(new PointerEvent("pointermove", { clientY: 160, bubbles: true })) })
      act(() => { window.dispatchEvent(new PointerEvent("pointerup", { clientY: 160, bubbles: true })) })
      const ratioIntent = intents.find(intent => intent.type === "sidebar-thread-ratio-change")
      expect(ratioIntent?.type).toBe("sidebar-thread-ratio-change")
      if (ratioIntent?.type === "sidebar-thread-ratio-change") {
        // 起始 0.38：向下拖动必须增大比例（happy-dom 高度为 0 时回退 600px）。
        expect(ratioIntent.ratio).toBeGreaterThan(0.38)
        expect(ratioIntent.ratio).toBeLessThan(0.6)
      }
    } finally {
      handle.unmount()
    }
  })

  test("侧栏右缘拖动 dispatch sidebar-width-change（向右拖动增大宽度）", () => {
    const intents: WebIntent[] = []
    const handle = mountSidebar(makeSnapshot(), intents)
    try {
      const resizeHandle = handle.container.querySelector<HTMLElement>(".sidebar-resize-handle")
      expect(resizeHandle).not.toBeNull()
      act(() => { resizeHandle!.dispatchEvent(new PointerEvent("pointerdown", { clientX: 200, bubbles: true })) })
      act(() => { window.dispatchEvent(new PointerEvent("pointermove", { clientX: 260, bubbles: true })) })
      act(() => { window.dispatchEvent(new PointerEvent("pointerup", { clientX: 260, bubbles: true })) })
      const widthIntent = intents.find(intent => intent.type === "sidebar-width-change")
      expect(widthIntent?.type).toBe("sidebar-width-change")
      if (widthIntent?.type === "sidebar-width-change") {
        // 起始 280 + (260 - 200) = 340
        expect(widthIntent.widthPx).toBe(340)
      }
    } finally {
      handle.unmount()
    }
  })

  test("Files 分区渲染文件树行；目录行点击 dispatch workspace-directory-toggle", () => {
    const intents: WebIntent[] = []
    const workspaceTree = {
      status: "ready" as const,
      rows: [treeRow({}), treeRow({ path: "a.ts", name: "a.ts", kind: "file", hasChildren: false })],
      selectedPath: null,
      limited: false,
    }
    const handle = mountSidebar(makeSnapshot({ workspaceTree }), intents)
    try {
      const rows = handle.container.querySelectorAll<HTMLElement>(".file-row")
      expect(rows.length).toBe(2)
      act(() => { rows[0]!.click() })
      expect(intents).toContainEqual({ type: "workspace-directory-toggle", path: "src" })
      act(() => { rows[1]!.click() })
      expect(intents).toContainEqual({ type: "workspace-file-open", path: "a.ts" })
    } finally {
      handle.unmount()
    }
  })
})
