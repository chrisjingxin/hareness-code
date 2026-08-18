/** Web React 工作台：组合桌面三栏布局与可访问性，不拥有 Agent 或命令业务状态。 */
/** @jsxImportSource react */

import { CircleDot, Ellipsis, Moon, Shield, Sun, Terminal, X } from "lucide-react"
import { useCallback, useEffect, useRef } from "react"
import { useSyncExternalStore } from "react"
import { selectWorkItemView } from "../../interactive/selectors"
import type { WebInteractiveAdapter, WebAdapterSnapshot, WebIntent, WebTheme } from "../application/adapter"
import { Composer } from "./composer"
import { ContextDock } from "./context-dock/context-dock"
import { DialogHost } from "./dialog"
import { InteractionForm } from "./interaction-form"
import { WorkspaceSidebar } from "./workspace-sidebar/workspace-sidebar"
import { Timeline } from "./timeline"
import { WorkItemBanner } from "./work-item-view"

/** 订阅 WebAdapterSnapshot 并转发 dispatch；页面唯一的状态来源。 */
export function WebApp(props: {
  adapter: WebInteractiveAdapter
  active: boolean
}) {
  const subscribe = useCallback(
    (listener: (snapshot: WebAdapterSnapshot) => void) => props.adapter.subscribe(listener),
    [props.adapter],
  )
  const getSnapshot = useCallback(() => props.adapter.getSnapshot(), [props.adapter])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const onIntent = useCallback(
    (intent: WebIntent) => props.adapter.dispatch(intent),
    [props.adapter],
  )
  const interactive = snapshot.interactive
  const connectionReadOnly = interactive.connection.status !== "open"
  const readOnly = !props.active || snapshot.leaving || connectionReadOnly
  const returnBlocked = Boolean(interactive.activeRun || interactive.interaction)
  const workItemView = selectWorkItemView(interactive)

  const overflowTriggerRef = useRef<HTMLButtonElement | null>(null)
  const headerMenuRef = useRef<HTMLDivElement | null>(null)
  const wasHeaderMenuOpenRef = useRef(false)

  // 打开 header menu 后焦点进入第一项；关闭后焦点返回 overflow trigger。
  useEffect(() => {
    if (snapshot.headerMenuOpen) {
      headerMenuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
    } else if (wasHeaderMenuOpenRef.current) {
      overflowTriggerRef.current?.focus()
    }
    wasHeaderMenuOpenRef.current = snapshot.headerMenuOpen
  }, [snapshot.headerMenuOpen])

  // 点击菜单外任意位置关闭 header menu。
  useEffect(() => {
    if (!snapshot.headerMenuOpen) return
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (headerMenuRef.current?.contains(target) || overflowTriggerRef.current?.contains(target)) return
      onIntent({ type: "header-menu-toggle", open: false })
    }
    document.addEventListener("pointerdown", onPointerDown)
    return () => document.removeEventListener("pointerdown", onPointerDown)
  }, [snapshot.headerMenuOpen, onIntent])

  /** Overflow 菜单按 APG menu 习惯在可用项间循环移动焦点。 */
  const handleHeaderMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'))
    if (items.length === 0) return
    const currentIndex = Math.max(0, items.indexOf(document.activeElement as HTMLButtonElement))
    let nextIndex: number | null = null
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length
    if (event.key === "Home") nextIndex = 0
    if (event.key === "End") nextIndex = items.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    items[nextIndex]?.focus()
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault()
        if (!readOnly) onIntent({ type: "command-menu-open" })
        return
      }
      if (event.key !== "Escape") return
      // Composer rail 下拉等内层浮层已 preventDefault 并自行关闭，这里不重复处理。
      if (event.defaultPrevented) return
      // Escape 关闭顺序固定：确认 Dialog → 命令菜单 → header overflow 菜单 → Context Dock → 取消 Run。
      if (interactive.confirmation) {
        // DialogHost 自己注册了 Escape handler；这里不再重复派发一次 resolve。
        return
      } else if (snapshot.commandMenuOpen) {
        event.preventDefault()
        onIntent({ type: "command-menu-close" })
      } else if (snapshot.headerMenuOpen) {
        event.preventDefault()
        onIntent({ type: "header-menu-toggle", open: false })
      } else if (snapshot.contextDock.open) {
        event.preventDefault()
        onIntent({ type: "dock-close" })
      } else if (!readOnly && interactive.activeRun) {
        event.preventDefault()
        onIntent({ type: "cancel-run" })
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [interactive.activeRun, interactive.confirmation, onIntent, readOnly, snapshot.commandMenuOpen, snapshot.contextDock.open, snapshot.headerMenuOpen])

  return (
    <div
      className="web-shell"
      data-theme={snapshot.theme}
      data-active={props.active ? "true" : "false"}
        style={{
        "--sidebar-width": `${snapshot.workspaceSidebar.widthPx}px`,
        "--dock-width": `${snapshot.contextDock.widthPx}px`,
      } as React.CSSProperties}
    >
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><Shield size={20} strokeWidth={1.9} /></span>
          <span className="brand-copy">
            <span className="brand-name">Harness Code</span>
            <span className="brand-version">{interactive.runtime.cliVersion}</span>
          </span>
          <span className="brand-online"><CircleDot aria-hidden="true" size={11} />在线</span>
        </div>
        <div className="topbar-main">
          {/* 顶栏中段不放控件：工作区标识已迁入侧栏文件分区标题，模型/审批下沉 Composer rail。 */}
          <div className="topbar-actions">
            <button type="button" className="button return-button" disabled={!props.active || snapshot.leaving || returnBlocked} title={returnBlocked ? "当前任务结束或交互完成后可返回 TUI" : "归还控制权并恢复 TUI"} onClick={() => { onIntent({ type: "return-to-tui" }) }}><Terminal aria-hidden="true" size={15} />返回 TUI</button>
            {/* 主题切换提到顶栏：单图标按钮表达下一动作（light 显示 Moon/切深色，dark 显示 Sun/切浅色），不再占用菜单项。 */}
            <button
              type="button"
              className="icon-button theme-toggle"
              aria-label={snapshot.theme === "light" ? "切换到深色主题" : "切换到浅色主题"}
              title={snapshot.theme === "light" ? "切换到深色主题" : "切换到浅色主题"}
              onClick={() => { onIntent({ type: "theme-set", theme: nextTheme(snapshot.theme) }) }}
            >
              {snapshot.theme === "light" ? <Moon aria-hidden="true" size={16} /> : <Sun aria-hidden="true" size={16} />}
            </button>
            <button
              type="button"
              ref={overflowTriggerRef}
              className="icon-button overflow-trigger"
              aria-label="更多操作"
              aria-haspopup="menu"
              aria-expanded={snapshot.headerMenuOpen}
              onClick={() => { onIntent({ type: "header-menu-toggle", open: !snapshot.headerMenuOpen }) }}
            >
              <Ellipsis aria-hidden="true" size={16} />
            </button>
            {snapshot.headerMenuOpen ? (
              <div ref={headerMenuRef} className="header-menu" role="menu" aria-label="更多操作" onKeyDown={handleHeaderMenuKeyDown}>
                <button type="button" role="menuitem" className="header-menu-item" onClick={() => { onIntent({ type: "dock-open", panel: "help" }) }}>帮助</button>
                <button type="button" role="menuitem" className="header-menu-item" disabled={!props.active || snapshot.leaving} onClick={() => { onIntent({ type: "exit-harness" }) }}>退出 Harness</button>
              </div>
            ) : null}
          </div>
        </div>
      </header>

      {snapshot.transientNotice ? <div className="notice-banner" role="status"><span>{snapshot.transientNotice}</span><button type="button" className="icon-button" aria-label="关闭通知" title="关闭通知" onClick={() => { onIntent({ type: "notice-dismiss" }) }}><X aria-hidden="true" size={14} /></button></div> : null}
      {interactive.connection.status !== "open" ? <div className="connection-banner" role="alert">{interactive.connection.message}</div> : null}
      {!props.active ? <div className="handoff-banner" role="status">浏览器正在等待 CLI 确认控制权，页面保持只读。</div> : null}

      <div className={`desktop-workspace${snapshot.contextDock.open ? " has-context-dock" : ""}`}>
        <WorkspaceSidebar snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
        <main className="conversation-column">
          <WorkItemBanner view={workItemView} />
          <div className="timeline-scroll">
            <Timeline snapshot={snapshot} dispatch={onIntent} />
          </div>
          {/* Interaction 卡固定在 composer 上方；无待处理请求时渲染空占位。 */}
          <div className="interaction-dock">
            <InteractionForm snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
          </div>
          <Composer snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
        </main>
        {snapshot.contextDock.open ? <ContextDock snapshot={snapshot} dispatch={onIntent} disabled={readOnly} /> : null}
      </div>

      <DialogHost snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
    </div>
  )
}

/** 主题切换的下一状态：浅色 ↔ 深色；顶栏图标按钮的 icon 与 aria-label 始终表达下一动作。 */
function nextTheme(theme: WebTheme): WebTheme {
  return theme === "light" ? "dark" : "light"
}
