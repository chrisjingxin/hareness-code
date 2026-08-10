/** Web React 工作台：组合桌面三栏布局与可访问性，不拥有 Agent 或命令业务状态。 */
/** @jsxImportSource react */

import { Activity, Cpu, Ellipsis, ShieldCheck, X } from "lucide-react"
import { useCallback, useEffect, useRef } from "react"
import { useSyncExternalStore } from "react"
import { activityLabel, gitWorkspaceLabel, modelSelectionLabel } from "../../presentation-shared"
import { selectNavigationView } from "../../interactive/selectors"
import { approvalModeLabel, workspaceLabel } from "../../interactive/runtime"
import type { InteractiveSnapshot } from "../../interactive/types"
import type { WebInteractiveAdapter, WebAdapterSnapshot, WebIntent, WebTheme } from "../application/adapter"
import { Composer } from "./composer"
import { ContextDock } from "./context-dock/context-dock"
import { DialogHost } from "./dialog"
import { InteractionForm } from "./interaction-form"
import { WorkspaceSidebar } from "./workspace-sidebar/workspace-sidebar"
import { Timeline } from "./timeline"

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
  const { availability } = selectNavigationView(interactive)

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
      style={{ "--sidebar-width": `${snapshot.workspaceSidebar.widthPx}px` } as React.CSSProperties}
    >
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">H</span>
          <span className="brand-name">Harness Code</span>
          <span className="brand-version">{interactive.runtime.cliVersion}</span>
        </div>
        <div className="topbar-main">
          <div className="topbar-project">
            <div className="topbar-project-copy">
              <span className="project-name">{workspaceLabel(interactive.runtime.workspace)}</span>
              {gitWorkspaceLabel(interactive.runtime.gitWorkspace) ? <span className="project-meta">{gitWorkspaceLabel(interactive.runtime.gitWorkspace)}</span> : null}
            </div>
          </div>
          <div className="topbar-meta">
            <button type="button" className="meta-chip" hidden={!availability.canOpenModelsPanel} disabled={readOnly} onClick={() => { onIntent({ type: "dock-open", panel: "models" }) }} title="模型设置">
              <Cpu aria-hidden="true" size={16} />
              <span className="sr-only">模型</span>
              <span className="meta-chip-value">{modelLabel(interactive)}</span>
            </button>
            <span className="meta-chip" title="审批模式">
              <ShieldCheck aria-hidden="true" size={16} />
              <span className="sr-only">审批</span>
              <span className="meta-chip-value">{approvalModeLabel(interactive.runtime)}</span>
            </span>
            <button type="button" className="meta-chip meta-chip-run" disabled={readOnly} onClick={() => { onIntent({ type: "dock-open", panel: "status" }) }} title="连接与活动状态">
              <Activity aria-hidden="true" size={16} />
              <span className={`status-dot status-dot-${interactive.activity.kind}`} aria-hidden="true" />
              <span className="meta-chip-value">{props.active ? activityLabel(interactive.activity.kind) : "正在接管"}{interactive.connection.status !== "open" ? ` · ${connectionLabel(interactive.connection.status)}` : ""}</span>
            </button>
          </div>
          <div className="topbar-actions">
            <button type="button" className="button button-secondary return-button" disabled={!props.active || snapshot.leaving || returnBlocked} title={returnBlocked ? "当前任务结束或交互完成后可返回 TUI" : "归还控制权并恢复 TUI"} onClick={() => { onIntent({ type: "return-to-tui" }) }}>返回 TUI</button>
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
                <button type="button" role="menuitem" className="header-menu-item" onClick={() => { onIntent({ type: "theme-set", theme: nextTheme(snapshot.theme) }) }}>
                  {snapshot.theme === "light" ? "使用深色主题" : "使用浅色主题"}
                </button>
                <button type="button" role="menuitem" className="header-menu-item" onClick={() => { onIntent({ type: "approval-mode-cycle" }) }}>
                  切换审批模式（当前：{approvalModeLabel(interactive.runtime)}）
                </button>
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

/** 主题切换的下一状态：浅色 ↔ 深色，菜单文案始终表达下一动作。 */
function nextTheme(theme: WebTheme): WebTheme {
  return theme === "light" ? "dark" : "light"
}

function modelLabel(snapshot: InteractiveSnapshot): string {
  // 与 TUI 共用同一展示策略：选择优先，回退握手运行时。
  return modelSelectionLabel(snapshot)
}

function connectionLabel(status: InteractiveSnapshot["connection"]["status"]): string {
  return status === "open" ? "连接正常" : status === "protocol-error" ? "协议错误" : "连接已断开"
}
