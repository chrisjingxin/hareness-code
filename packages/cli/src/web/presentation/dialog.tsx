/** 确认对话框：渲染共享 InteractiveConfirmation 并管理焦点。 */
/** @jsxImportSource react */

import { useEffect, useRef } from "react"

import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"

/**
 * 渲染 snapshot.interactive.confirmation 对应的模态确认框。
 *
 * - 关闭/取消时 dispatch confirmation-resolve { confirmationId, confirmed: false }。
 * - 按 Esc 同样取消，遮罩点击也取消。
 * - 打开时把焦点放到确认按钮；关闭时把焦点还回之前拥有焦点的元素。
 */
export function DialogHost(props: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactNode {
  const { snapshot, dispatch } = props
  const confirmation = snapshot.interactive.confirmation
  const confirmRef = useRef<HTMLButtonElement | null>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!confirmation) return
    const active = document.activeElement
    previousFocusRef.current = active instanceof HTMLElement ? active : null
    confirmRef.current?.focus()
    return () => {
      const previous = previousFocusRef.current
      if (previous && document.contains(previous)) {
        previous.focus()
      }
      previousFocusRef.current = null
    }
  }, [confirmation?.confirmationId])

  useEffect(() => {
    if (!confirmation) return
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        if (!props.disabled) dispatch({ type: "confirmation-resolve", confirmationId: confirmation.confirmationId, confirmed: false })
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [confirmation, dispatch, props.disabled])

  if (!confirmation) return null

  const cancel = () => {
    if (!props.disabled) dispatch({ type: "confirmation-resolve", confirmationId: confirmation.confirmationId, confirmed: false })
  }
  const confirm = () => {
    if (!props.disabled) dispatch({ type: "confirmation-resolve", confirmationId: confirmation.confirmationId, confirmed: true })
  }

  return (
    <div
      className="dialog-overlay"
      role="presentation"
      onClick={event => {
        if (event.target === event.currentTarget) cancel()
      }}
    >
      <section
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        aria-describedby="dialog-message"
      >
        <h2 id="dialog-title" className="dialog-title">{confirmation.title}</h2>
        <p id="dialog-message" className="dialog-message">{confirmation.message}</p>
        <div className="dialog-actions">
          <button
            type="button"
            className="dialog-cancel"
            onClick={cancel}
            disabled={snapshot.leaving || props.disabled}
            aria-label={confirmation.cancelLabel ?? "取消"}
          >
            {confirmation.cancelLabel ?? "取消"}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className="dialog-confirm"
            onClick={confirm}
            disabled={snapshot.leaving || props.disabled}
            aria-label={confirmation.confirmLabel ?? "确认"}
          >
            {confirmation.confirmLabel ?? "确认"}
          </button>
        </div>
      </section>
    </div>
  )
}
