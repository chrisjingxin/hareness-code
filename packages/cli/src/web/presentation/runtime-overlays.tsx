/** 执行中 BTW 旁路面板与 Goal/Plan 查看浮层。 */
/** @jsxImportSource react */

import { Copy, X } from "lucide-react"

import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"
import { Markdown } from "./markdown"

/** 执行中覆盖层：BTW 旁路问答与 Goal/Plan 只读查看，Esc 关闭不取消主 Run。 */
export function RuntimeOverlays(props: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
}): React.ReactNode {
  const { snapshot, dispatch } = props
  const { btw, inspectOverlay } = snapshot
  if (!btw.visible && !inspectOverlay.visible) return null

  return (
    <>
      {btw.visible ? (
        <div className="dialog-overlay runtime-overlay" role="dialog" aria-label="BTW 临时问答">
          <div className="dialog runtime-overlay-panel">
            <div className="runtime-overlay-header">
              <h2 className="dialog-title">BTW 临时问答</h2>
              <button type="button" className="icon-button" aria-label="关闭" onClick={() => { dispatch({ type: "btw-close" }) }}>
                <X aria-hidden="true" size={14} />
              </button>
            </div>
            <p className="runtime-overlay-question">{btw.question}</p>
            {btw.status === "loading" ? <p className="dialog-message">正在回答…</p> : null}
            {btw.status === "error" ? <p className="dialog-message">{btw.error ?? "旁路提问失败"}</p> : null}
            {btw.status === "ready" ? <div className="runtime-overlay-body-scroll"><Markdown text={btw.answer ?? ""} /></div> : null}
            <div className="dialog-actions">
              {btw.status === "ready" && btw.answer ? (
                <button type="button" className="dialog-cancel" onClick={() => { dispatch({ type: "btw-copy" }) }}>
                  <Copy aria-hidden="true" size={14} /> {btw.copied ? "已复制" : "复制"}
                </button>
              ) : null}
              <button type="button" className="dialog-confirm" onClick={() => { dispatch({ type: "btw-close" }) }}>关闭</button>
            </div>
          </div>
        </div>
      ) : null}
      {inspectOverlay.visible ? (
        <div className="dialog-overlay runtime-overlay" role="dialog" aria-label={inspectOverlay.title}>
          <div className="dialog runtime-overlay-panel">
            <div className="runtime-overlay-header">
              <h2 className="dialog-title">{inspectOverlay.title}</h2>
              <button type="button" className="icon-button" aria-label="关闭" onClick={() => { dispatch({ type: "inspect-overlay-close" }) }}>
                <X aria-hidden="true" size={14} />
              </button>
            </div>
            <div className="runtime-overlay-body-scroll"><Markdown text={inspectOverlay.body} /></div>
            <div className="dialog-actions">
              <button type="button" className="dialog-confirm" onClick={() => { dispatch({ type: "inspect-overlay-close" }) }}>关闭</button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}
