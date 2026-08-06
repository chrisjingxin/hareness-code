/** Context Dock：右侧常驻面板（Code|Model|Skills|MCP|Status）；Help 只从顶栏更多菜单打开不占 tab。 */
/** @jsxImportSource react */

import { useEffect, useRef, useState } from "react"
import { X } from "lucide-react"

import { selectNavigationView } from "../../../interactive/selectors"
import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { CodePanel } from "./code/code-panel"
import { HelpPanel } from "./help-panel"
import { McpPanel } from "./mcp-panel"
import { ModelsPanel } from "./models-panel"
import { SkillsPanel } from "./skills-panel"
import { StatusPanel } from "./status-panel"
import { DOCK_TABS, tabLabel, tabVisible } from "./panel-common"

/** Dock 关闭时不渲染；打开时按 activePanel 渲染面板 body，左缘可拖动调宽。 */
export function ContextDock({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const { open, activePanel, widthPx } = snapshot.contextDock
  if (!open) return <></>
  const interactive = snapshot.interactive
  const busy = Boolean(interactive.activeRun) || Boolean(interactive.interaction)
  const busyReason = busy ? "当前任务结束后可用" : null
  const { availability } = selectNavigationView(interactive)
  const isMainTab = activePanel !== "help"

  return (
    <aside className="context-dock" style={{ width: widthPx }} aria-label="Context Dock">
      <DockResizeHandle widthPx={widthPx} dispatch={dispatch} disabled={disabled} />
      <header className="context-dock-header">
        <h2 className="context-dock-title">{activePanel === "help" ? "帮助" : "Context Dock"}</h2>
        <button
          type="button"
          className="icon-button panel-close"
          onClick={() => dispatch({ type: "dock-close" })}
          disabled={disabled}
          aria-label="关闭 Dock"
        >
          <X aria-hidden="true" />
        </button>
      </header>
      {isMainTab ? (
        <div className="context-dock-tabs" role="tablist" aria-label="Context Dock 面板" onKeyDown={event => handleTabListKeyDown(event, dispatch)}>
          {DOCK_TABS.filter(tab => tabVisible(tab, availability)).map(tab => (
            <button
              type="button"
              key={tab}
              role="tab"
              id={`dock-tab-${tab}`}
              aria-selected={activePanel === tab}
              aria-controls="context-dock-panel"
              data-panel={tab}
              className={activePanel === tab ? "dock-tab is-selected" : "dock-tab"}
              disabled={disabled}
              onClick={() => dispatch({ type: "dock-panel-select", panel: tab })}
            >
              {tabLabel(tab)}
            </button>
          ))}
        </div>
      ) : null}
      <div className="context-dock-body" id="context-dock-panel" role="tabpanel">
        {activePanel === "code" ? <CodePanel snapshot={snapshot} dispatch={dispatch} disabled={disabled} /> : null}
        {activePanel === "models" ? <ModelsPanel snapshot={snapshot} busyReason={busyReason} disabled={disabled} dispatch={dispatch} /> : null}
        {activePanel === "skills" ? <SkillsPanel snapshot={snapshot} dispatch={dispatch} disabled={disabled} /> : null}
        {activePanel === "mcp" ? <McpPanel snapshot={snapshot} dispatch={dispatch} disabled={disabled} /> : null}
        {activePanel === "status" ? <StatusPanel snapshot={snapshot} /> : null}
        {activePanel === "help" ? <HelpPanel snapshot={snapshot} /> : null}
      </div>
    </aside>
  )
}

/** tablist 内使用方向键/Home/End 循环移动并立即激活对应面板。 */
function handleTabListKeyDown(event: React.KeyboardEvent<HTMLDivElement>, dispatch: (intent: WebIntent) => void): void {
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled)'))
  if (tabs.length === 0) return
  const currentIndex = Math.max(0, tabs.indexOf(document.activeElement as HTMLButtonElement))
  let nextIndex: number | null = null
  if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length
  if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length
  if (event.key === "Home") nextIndex = 0
  if (event.key === "End") nextIndex = tabs.length - 1
  if (nextIndex === null) return
  event.preventDefault()
  const next = tabs[nextIndex]
  next?.focus()
  const nextPanel = next?.dataset.panel as "code" | "models" | "skills" | "mcp" | "status" | undefined
  if (nextPanel) dispatch({ type: "dock-panel-select", panel: nextPanel })
}

/** Dock 左缘拖动条：宽度 = 起始宽度 - 水平位移，dispatch dock-width-change（Adapter 夹取 400-760）。 */
function DockResizeHandle({
  widthPx,
  dispatch,
  disabled,
}: {
  widthPx: number
  dispatch: (intent: WebIntent) => void
  disabled: boolean
}): React.ReactElement {
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (disabled) return
    event.preventDefault()
    dragRef.current = { startX: event.clientX, startWidth: widthPx }
    setDragging(true)
  }

  useEffect(() => {
    if (!dragging) return
    const onMove = (event: PointerEvent) => {
      const drag = dragRef.current
      if (!drag) return
      dispatch({ type: "dock-width-change", widthPx: drag.startWidth - (event.clientX - drag.startX) })
    }
    const onUp = () => {
      dragRef.current = null
      setDragging(false)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onUp)
    return () => {
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onUp)
    }
  }, [dragging, dispatch])

  return (
    <div
      className={`dock-resize-handle${dragging ? " is-dragging" : ""}`}
      role="separator"
      aria-orientation="vertical"
      aria-label="调整 Dock 宽度"
      onPointerDown={onPointerDown}
    />
  )
}
