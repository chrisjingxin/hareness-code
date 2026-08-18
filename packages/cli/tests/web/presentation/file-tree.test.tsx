/** FileTree：行渲染（缩进/箭头/图标）、键盘导航 dispatch、loading/error/limited/空目录状态。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act } from "react"

import { FileTree } from "../../../src/web/presentation/workspace-sidebar/file-tree"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import type { WorkspaceTreeRow, WorkspaceTreeState } from "../../../src/workspace/types"
import { makeSnapshot } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


function row(overrides: Partial<WorkspaceTreeRow>): WorkspaceTreeRow {
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

function mountTree(tree: WorkspaceTreeState, intents: WebIntent[], selectedPath: string | null = null): RenderHandle {
  return render(
    <FileTree snapshot={makeSnapshot({ workspaceTree: tree, workspaceSidebar: { threadRatio: 0.38, selectedPath, widthPx: 280 } })} dispatch={intent => intents.push(intent)} />,
  )
}

describe("FileTree", () => {
  test("渲染行：缩进/展开箭头/图标；选中行高亮", () => {
    const tree: WorkspaceTreeState = {
      status: "ready",
      rows: [
        row({ path: "src", name: "src", expanded: true }),
        row({ path: "src/a.ts", name: "a.ts", kind: "file", depth: 1, hasChildren: false }),
        row({ path: "link", name: "link", kind: "symlink", depth: 0, hasChildren: false }),
      ],
      selectedPath: "src/a.ts",
      limited: false,
    }
    const handle = mountTree(tree, [], "src/a.ts")
    try {
      const rows = handle.container.querySelectorAll<HTMLElement>(".file-row")
      expect(rows.length).toBe(3)
      // 展开箭头：lucide chevron（SVG），与文件图标同轴对齐
      expect(rows[0]!.querySelector(".file-row-arrow svg")).not.toBeNull()
      expect(rows[0]!.getAttribute("aria-expanded")).toBe("true")
      expect(rows[0]!.getAttribute("aria-level")).toBe("1")
      const indented = rows[1]!.style.paddingInlineStart
      expect(indented).toBe("20px") // 8 + 1 * 12
      expect(rows[1]!.classList.contains("is-selected")).toBe(true)
      expect(rows[2]!.classList.contains("is-selected")).toBe(false)
    } finally {
      handle.unmount()
    }
  })

  test("目录行点击 dispatch workspace-directory-toggle；文件行 dispatch workspace-file-open", () => {
    const tree: WorkspaceTreeState = {
      status: "ready",
      rows: [row({}), row({ path: "a.ts", name: "a.ts", kind: "file", hasChildren: false })],
      selectedPath: null,
      limited: false,
    }
    const intents: WebIntent[] = []
    const handle = mountTree(tree, intents)
    try {
      const rows = handle.container.querySelectorAll<HTMLElement>(".file-row")
      act(() => { rows[0]!.click() })
      act(() => { rows[1]!.click() })
      expect(intents).toEqual([
        { type: "workspace-directory-toggle", path: "src" },
        { type: "workspace-file-open", path: "a.ts" },
      ])
    } finally {
      handle.unmount()
    }
  })

  test("键盘导航：↑↓ 移动焦点，→/← 展开收起，Enter 激活，Home/End 跳转", () => {
    const tree: WorkspaceTreeState = {
      status: "ready",
      rows: [
        row({ path: "src", name: "src" }),
        row({ path: "b.ts", name: "b.ts", kind: "file", hasChildren: false }),
        row({ path: "c.ts", name: "c.ts", kind: "file", hasChildren: false }),
      ],
      selectedPath: null,
      limited: false,
    }
    const intents: WebIntent[] = []
    const handle = mountTree(tree, intents)
    try {
      const container = handle.container.querySelector<HTMLElement>(".file-tree")!
      const rows = () => handle.container.querySelectorAll<HTMLElement>(".file-row")
      container.focus()
      // ↓ → 第二行
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true })) })
      expect(rows()[1]!.classList.contains("is-focused")).toBe(true)
      expect(container.getAttribute("aria-activedescendant")).toBe("file-row-1")
      // → 在文件行上无目录语义；先 ↓ 到第三行再 ↑ 回目录行
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true })) })
      expect(rows()[0]!.classList.contains("is-focused")).toBe(true)
      // → 展开目录
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })) })
      expect(intents).toContainEqual({ type: "workspace-directory-toggle", path: "src" })
      // ← 收起目录
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true, cancelable: true })) })
      expect(intents.filter(intent => intent.type === "workspace-directory-toggle").length).toBe(2)
      // Enter 在文件行打开
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true })) })
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })) })
      expect(intents).toContainEqual({ type: "workspace-file-open", path: "b.ts" })
      // Home / End
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true })) })
      expect(rows()[2]!.classList.contains("is-focused")).toBe(true)
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true })) })
      expect(rows()[0]!.classList.contains("is-focused")).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("键盘 ↑ 越过首行不越界；折叠目录的后代行隐藏", () => {
    const tree: WorkspaceTreeState = {
      status: "ready",
      rows: [
        row({ path: "src", name: "src", expanded: false }),
        row({ path: "src/a.ts", name: "a.ts", kind: "file", depth: 1, hasChildren: false }),
        row({ path: "top.ts", name: "top.ts", kind: "file", hasChildren: false }),
      ],
      selectedPath: null,
      limited: false,
    }
    const intents: WebIntent[] = []
    const handle = mountTree(tree, intents)
    try {
      const container = handle.container.querySelector<HTMLElement>(".file-tree")!
      const rows = handle.container.querySelectorAll<HTMLElement>(".file-row")
      // src 收起 → 其后代 src/a.ts 不可见
      expect(rows.length).toBe(2)
      expect(rows[0]!.textContent).toContain("src")
      expect(rows[1]!.textContent).toContain("top.ts")
      act(() => { container.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true })) })
      expect(rows[0]!.classList.contains("is-focused")).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("状态渲染：loading 加载中 / error 消息 / 空目录 / limited 提示", () => {
    const intents: WebIntent[] = []
    const loading = mountTree({ status: "loading", rows: [], selectedPath: null, limited: false }, intents)
    try {
      expect(loading.container.querySelector(".file-tree-status")?.textContent).toContain("加载中")
    } finally {
      loading.unmount()
    }
    const error = mountTree({ status: "error", rows: [], selectedPath: null, limited: false, message: "工作区路径不可用" }, intents)
    try {
      expect(error.container.querySelector(".file-tree-status-error")?.textContent).toBe("工作区路径不可用")
    } finally {
      error.unmount()
    }
    const empty = mountTree({ status: "ready", rows: [], selectedPath: null, limited: false }, intents)
    try {
      expect(empty.container.querySelector(".file-tree-status")?.textContent).toBe("空目录")
    } finally {
      empty.unmount()
    }
    const limited = mountTree({
      status: "ready",
      rows: [row({})],
      selectedPath: null,
      limited: true,
    }, intents)
    try {
      expect(limited.container.querySelector(".file-tree-limited")?.textContent).toContain("仅展示部分内容")
    } finally {
      limited.unmount()
    }
  })
})
