/** Web 工作区侧栏：新建 Thread + Thread 分区 + 垂直分隔条 + Files 分区（桌面常驻，无移动端抽屉）。 */
/** @jsxImportSource react */

import { useEffect, useRef, useState } from "react"
import { Plus } from "lucide-react"

import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { ThreadSection } from "./thread-section"
import { FileExplorer } from "./file-explorer"

/** 垂直分隔条高度；Thread 分区高度 = (100% - 分隔条) * threadRatio。 */
const RESIZE_HANDLE_PX = 5
const THREAD_MIN_PX = 160
const FILES_MIN_PX = 220

/**
 * 渲染桌面端左栏：新建 Thread 独立成行（不混入目录信息），下方 Thread 列表与
 * 文件树上下分区、中间可拖动调整比例；右缘可拖动调整侧栏宽度（顶栏品牌列同步）。
 * 工作区名与 Git 分支只在顶栏展示，此处不重复。
 */
export function WorkspaceSidebar({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const busy = Boolean(snapshot.interactive.activeRun) || Boolean(snapshot.interactive.interaction)
  return (
    <aside className="workspace-sidebar" aria-label="工作区导航">
      <SidebarResizeHandle widthPx={snapshot.workspaceSidebar.widthPx} dispatch={dispatch} disabled={disabled} />
      <button
        type="button"
        className="new-thread-button"
        onClick={() => dispatch({ type: "thread-new" })}
        disabled={disabled || busy || snapshot.threadNewSubmitting}
        aria-disabled={disabled || busy || snapshot.threadNewSubmitting}
        title={disabled ? "接管尚未完成或连接不可用" : busy ? "当前任务结束后可用" : snapshot.threadNewSubmitting ? "正在新建…" : "新建 Thread"}
      >
        <Plus aria-hidden="true" size={14} />
        <span>新建 Thread</span>
      </button>
      <div
        className="workspace-sidebar-thread"
        style={{
          height: `calc((100% - ${RESIZE_HANDLE_PX}px) * ${snapshot.workspaceSidebar.threadRatio})`,
          minHeight: THREAD_MIN_PX,
        }}
      >
        <ThreadSection snapshot={snapshot} dispatch={dispatch} disabled={disabled} />
      </div>
      <VerticalResizeHandle threadRatio={snapshot.workspaceSidebar.threadRatio} dispatch={dispatch} disabled={disabled} />
      <div className="workspace-sidebar-files" style={{ minHeight: FILES_MIN_PX }}>
        <FileExplorer snapshot={snapshot} dispatch={dispatch} disabled={disabled} />
      </div>
    </aside>
  )
}

/** 侧栏右缘拖动条：宽度 = 起始宽度 + 水平位移，dispatch sidebar-width-change（Adapter 夹取 220-480）。 */
function SidebarResizeHandle({
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
      dispatch({ type: "sidebar-width-change", widthPx: drag.startWidth + (event.clientX - drag.startX) })
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
      className={`sidebar-resize-handle${dragging ? " is-dragging" : ""}`}
      role="separator"
      aria-orientation="vertical"
      aria-label="调整侧栏宽度"
      onPointerDown={onPointerDown}
    />
  )
}

/** 垂直分隔条：拖动时按侧栏高度换算 Thread 比例，dispatch sidebar-thread-ratio-change。 */
function VerticalResizeHandle({
  threadRatio,
  dispatch,
  disabled,
}: {
  threadRatio: number
  dispatch: (intent: WebIntent) => void
  disabled: boolean
}): React.ReactElement {
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef<{ startY: number; startRatio: number; height: number } | null>(null)

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (disabled) return
    event.preventDefault()
    const sidebar = event.currentTarget.closest<HTMLElement>(".workspace-sidebar")
    // happy-dom / 未布局环境返回 0：回退 600px 保证拖动比例可计算。
    const height = sidebar?.getBoundingClientRect().height || 600
    dragRef.current = { startY: event.clientY, startRatio: threadRatio, height }
    setDragging(true)
  }

  // 拖动期间在 window 上监听；松手后本次拖动会话结束。
  useEffect(() => {
    if (!dragging) return
    const onMove = (event: PointerEvent) => {
      const drag = dragRef.current
      if (!drag) return
      const ratio = Math.min(0.95, Math.max(0.05, drag.startRatio + (event.clientY - drag.startY) / drag.height))
      dispatch({ type: "sidebar-thread-ratio-change", ratio })
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
      className={`vertical-resize-handle${dragging ? " is-dragging" : ""}`}
      role="separator"
      aria-orientation="horizontal"
      aria-label="调整 Thread 分区高度"
      onPointerDown={onPointerDown}
    />
  )
}
