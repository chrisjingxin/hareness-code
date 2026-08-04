/** DialogHost：confirmation 模态焦点、确认/取消 dispatch、Escape 行为。 */
/** @jsxImportSource react */

import { describe, expect, test } from "bun:test"
import { act } from "react"

import { DialogHost } from "../../../src/web/presentation/dialog"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import { makeConfirmation, makeInteractive, makeSnapshot } from "./fixtures"
import { render, type RenderHandle } from "./render"

function mountDialog(snapshot: WebAdapterSnapshot, intents: WebIntent[]): RenderHandle {
  return render(
    <DialogHost snapshot={snapshot} dispatch={intent => intents.push(intent)} />,
  )
}

describe("DialogHost", () => {
  test("confirmation 渲染 overlay + dialog，含 title 与 message", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      confirmation: makeConfirmation({ title: "删除 Thread", message: "此操作不可撤销" }),
    })
    const handle = mountDialog(makeSnapshot({ interactive }), intents)
    try {
      expect(handle.container.querySelector(".dialog-overlay")).not.toBeNull()
      const dialog = handle.container.querySelector(".dialog")
      expect(dialog?.getAttribute("role")).toBe("dialog")
      expect(dialog?.getAttribute("aria-modal")).toBe("true")
      expect(handle.container.querySelector(".dialog-title")?.textContent).toBe("删除 Thread")
      expect(handle.container.querySelector(".dialog-message")?.textContent).toBe("此操作不可撤销")
    } finally {
      handle.unmount()
    }
  })

  test("打开时确认按钮获得焦点", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      confirmation: makeConfirmation(),
    })
    const handle = mountDialog(makeSnapshot({ interactive }), intents)
    try {
      const confirm = handle.container.querySelector<HTMLButtonElement>(".dialog-confirm")
      expect(confirm).not.toBeNull()
      act(() => { confirm?.focus() })
      expect(document.activeElement).toBe(confirm)
    } finally {
      handle.unmount()
    }
  })

  test("Escape 在窗口按键时 dispatch confirmation-resolve confirmed:false", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      confirmation: makeConfirmation({ confirmationId: "conf-9" }),
    })
    const handle = mountDialog(makeSnapshot({ interactive }), intents)
    try {
      act(() => {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }))
      })
      const resolve = intents.find(intent => intent.type === "confirmation-resolve")
      expect(resolve).toBeDefined()
      if (resolve && resolve.type === "confirmation-resolve") {
        expect(resolve.confirmed).toBe(false)
        expect(resolve.confirmationId).toBe("conf-9")
      }
    } finally {
      handle.unmount()
    }
  })

  test("点击确认 dispatch confirmed:true；点击取消 dispatch confirmed:false", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      confirmation: makeConfirmation({ confirmationId: "conf-c" }),
    })
    const handle = mountDialog(makeSnapshot({ interactive }), intents)
    try {
      const confirm = handle.container.querySelector<HTMLButtonElement>(".dialog-confirm")
      act(() => { confirm?.click() })
      expect(intents).toContainEqual({ type: "confirmation-resolve", confirmationId: "conf-c", confirmed: true })
    } finally {
      handle.unmount()
    }

    intents.length = 0
    const handle2 = mountDialog(makeSnapshot({ interactive }), intents)
    try {
      const cancel = handle2.container.querySelector<HTMLButtonElement>(".dialog-cancel")
      act(() => { cancel?.click() })
      expect(intents).toContainEqual({ type: "confirmation-resolve", confirmationId: "conf-c", confirmed: false })
    } finally {
      handle2.unmount()
    }
  })

  test("无 confirmation 时不渲染任何内容", () => {
    const intents: WebIntent[] = []
    const handle = mountDialog(makeSnapshot(), intents)
    try {
      expect(handle.container.querySelector(".dialog")).toBeNull()
      expect(handle.container.querySelector(".dialog-overlay")).toBeNull()
    } finally {
      handle.unmount()
    }
  })
})
