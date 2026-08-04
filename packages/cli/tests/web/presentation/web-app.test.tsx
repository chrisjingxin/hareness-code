/** WebApp：active=false 时显示接管只读提示；active=true 时 composer 可用。 */
/** @jsxImportSource react */

import { describe, expect, test } from "bun:test"
import { act } from "react"

import { WebApp } from "../../../src/web/presentation/web-app"
import type { WebAdapterSnapshot, WebIntent, WebInteractiveAdapter } from "../../../src/web/application/adapter"
import { makeInteractive, makeSnapshot } from "./fixtures"
import { render, type RenderHandle } from "./render"

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
      expect(opening?.classList.contains("is-opening")).toBe(true)
      const handoff = handle.container.querySelector(".handoff-banner")
      expect(handoff?.textContent).toContain("等待 CLI 确认控制权")
      const statusPill = handle.container.querySelector(".status-pill.status-home")
      expect(statusPill?.textContent).toBe("正在接管")
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
      expect(opening?.classList.contains("is-active")).toBe(true)
      expect(handle.container.querySelector(".handoff-banner")).toBeNull()
      const composer = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(composer?.disabled).toBe(false)
      expect(composer?.value).toBe("hello")
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
})
