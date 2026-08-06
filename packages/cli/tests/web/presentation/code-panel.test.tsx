/** CodePanel：文件 Tab 渲染/激活/关闭、预览 ready/unsupported/error/空状态与截断提示。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act } from "react"

import { CodePanel } from "../../../src/web/presentation/context-dock/code/code-panel"
import type { ContextDockPanel, WebAdapterSnapshot, WebIntent, WorkspaceFileTab } from "../../../src/web/application/adapter"
import type { WorkspacePreviewState } from "../../../src/workspace/types"
import { makeSnapshot } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


function tabs(...paths: string[]): WorkspaceFileTab[] {
  return paths.map(path => ({ path, name: path.split("/").at(-1)!, language: path.endsWith(".ts") ? "typescript" : null }))
}

function readyPreview(path: string, content = "const x = 1\n"): Extract<WorkspacePreviewState, { status: "ready" }> {
  return {
    status: "ready",
    file: { path, name: path.split("/").at(-1)!, content, language: "typescript", sizeBytes: content.length, lineCount: 2, modifiedAtMs: 1, truncated: false, version: "1:12" },
  }
}

function mountCodePanel(
  code: WebAdapterSnapshot["contextDock"]["code"],
  intents: WebIntent[],
): RenderHandle {
  return render(
    <CodePanel
      snapshot={makeSnapshot({ contextDock: { open: true, activePanel: "code" as ContextDockPanel, widthPx: 560, code } })}
      dispatch={intent => intents.push(intent)}
    />,
  )
}

describe("CodePanel", () => {
  test("空状态：无 Tab 时显示引导文案", () => {
    const handle = mountCodePanel({ tabs: [], activePath: null, previews: {}, previewErrors: {} }, [])
    try {
      expect(handle.container.querySelector(".code-panel-empty")?.textContent).toContain("从左侧文件树打开文件")
    } finally {
      handle.unmount()
    }
  })

  test("文件 Tab 渲染：激活高亮；点击激活 / 关闭 dispatch", () => {
    const intents: WebIntent[] = []
    const previews = { "a.ts": readyPreview("a.ts"), "b.ts": readyPreview("b.ts") }
    const handle = mountCodePanel({ tabs: tabs("a.ts", "b.ts"), activePath: "a.ts", previews, previewErrors: {} }, intents)
    try {
      const fileTabs = handle.container.querySelectorAll<HTMLElement>(".file-tab")
      expect(fileTabs.length).toBe(2)
      expect(fileTabs[0]!.classList.contains("is-active")).toBe(true)
      expect(fileTabs[0]!.getAttribute("aria-selected")).toBe("true")
      // 点击未激活 Tab 激活
      act(() => { fileTabs[1]!.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })) })
      act(() => { fileTabs[1]!.click() })
      expect(intents).toContainEqual({ type: "workspace-file-tab-select", path: "b.ts" })
      // 关闭按钮
      const closeButtons = handle.container.querySelectorAll<HTMLButtonElement>(".file-tab-close")
      act(() => { closeButtons[0]!.click() })
      expect(intents).toContainEqual({ type: "workspace-file-tab-close", path: "a.ts" })
    } finally {
      handle.unmount()
    }
  })

  test("preview ready：头部显示语言/大小/行数，内容区渲染行号与高亮 span", () => {
    const handle = mountCodePanel(
      { tabs: tabs("a.ts"), activePath: "a.ts", previews: { "a.ts": readyPreview("a.ts") }, previewErrors: {} },
      [],
    )
    try {
      const meta = handle.container.querySelector(".file-preview-meta")
      expect(meta?.textContent).toContain("typescript")
      expect(meta?.textContent).toContain("行")
      const lineNumbers = handle.container.querySelector(".line-numbers")
      expect(lineNumbers?.textContent).toBe("1\n2")
      const code = handle.container.querySelector(".file-code-pre")
      expect(code).not.toBeNull()
      // 内容按行渲染（高亮 span 由 HighlightedCode 注入；plain 回退仍保留代码文本）
      expect(code?.textContent).toContain("const x = 1")
    } finally {
      handle.unmount()
    }
  })

  test("preview loading：显示加载中骨架", () => {
    const handle = mountCodePanel(
      { tabs: tabs("a.ts"), activePath: "a.ts", previews: { "a.ts": { status: "loading", path: "a.ts" } }, previewErrors: {} },
      [],
    )
    try {
      expect(handle.container.querySelector(".file-code-status")?.textContent).toContain("加载中")
    } finally {
      handle.unmount()
    }
  })

  test("preview unsupported：元信息展示而非红色错误页", () => {
    const handle = mountCodePanel(
      {
        tabs: tabs("blob.bin"),
        activePath: "blob.bin",
        previews: { "blob.bin": { status: "unsupported", path: "blob.bin", reason: "二进制文件暂不支持预览", sizeBytes: 2048 } },
        previewErrors: {},
      },
      [],
    )
    try {
      const view = handle.container.querySelector(".file-code-view-meta")
      expect(view?.textContent).toContain("blob.bin")
      expect(view?.textContent).toContain("二进制文件暂不支持预览")
      expect(view?.textContent).toContain("2.0 KiB")
      expect(view?.classList.contains("file-code-view-error")).toBe(false)
    } finally {
      handle.unmount()
    }
  })

  test("preview error：显示错误信息 + 刷新入口（头部按钮存在）", () => {
    const intents: WebIntent[] = []
    const handle = mountCodePanel(
      {
        tabs: tabs("a.ts"),
        activePath: "a.ts",
        previews: { "a.ts": { status: "error", path: "a.ts", code: "not-found", message: "文件或目录不存在" } },
        previewErrors: { "a.ts": "文件或目录不存在" },
      },
      intents,
    )
    try {
      expect(handle.container.querySelector(".file-code-view-error")?.textContent).toContain("文件或目录不存在")
      expect(handle.container.querySelector(".file-preview-error-hint")?.textContent).toContain("文件或目录不存在")
      const refresh = handle.container.querySelector<HTMLButtonElement>('button[aria-label="刷新预览"]')
      expect(refresh).not.toBeNull()
      act(() => { refresh?.click() })
      expect(intents).toContainEqual({ type: "workspace-preview-refresh", path: "a.ts" })
    } finally {
      handle.unmount()
    }
  })

  test("error 保留旧 ready 内容：内容区仍显示代码，头部显示错误提示", () => {
    const handle = mountCodePanel(
      {
        tabs: tabs("a.ts"),
        activePath: "a.ts",
        previews: { "a.ts": readyPreview("a.ts", "旧内容") },
        previewErrors: { "a.ts": "读取失败" },
      },
      [],
    )
    try {
      expect(handle.container.querySelector(".file-code-pre")?.textContent).toContain("旧内容")
      expect(handle.container.querySelector(".file-preview-error-hint")?.textContent).toContain("读取失败")
    } finally {
      handle.unmount()
    }
  })

  test("truncated 提示：头部显示截断文案", () => {
    const preview = readyPreview("big.ts")
    preview.file.truncated = true
    const handle = mountCodePanel({ tabs: tabs("big.ts"), activePath: "big.ts", previews: { "big.ts": preview }, previewErrors: {} }, [])
    try {
      expect(handle.container.querySelector(".file-preview-truncated")?.textContent).toContain("仅展示前 256 KiB")
    } finally {
      handle.unmount()
    }
  })
})
