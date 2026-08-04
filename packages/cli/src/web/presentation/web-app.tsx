/** Web React 工作台：组合蓝色三栏布局与可访问性，不拥有 Agent 或命令业务状态。 */
/** @jsxImportSource react */

import { Ellipsis, Menu, X } from "lucide-react"
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react"
import { Capability } from "@za38/protocol"

import { approvalModeLabel, workspaceLabel } from "../../interactive/runtime"
import type { InteractiveSnapshot } from "../../interactive/types"
import type { WebInteractiveAdapter, WebAdapterSnapshot, WebIntent, WebTheme } from "../application/adapter"
import { Composer } from "./composer"
import { DialogHost } from "./dialog"
import { InteractionForm } from "./interaction-form"
import { UtilityPanels } from "./panels"
import { ThreadSidebar } from "./thread-sidebar"
import { Timeline } from "./timeline"

/** 窄屏断点：与 styles.css 的移动端布局保持一致，抽屉代替侧栏。 */
const NARROW_QUERY = "(max-width: 899px)"

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
    (intent: WebIntent) => {
      // 移动端抽屉/面板打开前记录当前焦点元素，关闭时用于恢复触发器焦点。
      if ((intent.type === "sidebar-toggle" && intent.open) || intent.type === "panel-open") {
        drawerTriggerRef.current = document.activeElement as HTMLElement | null
      }
      return props.adapter.dispatch(intent)
    },
    [props.adapter],
  )
  const narrow = useMediaQuery(NARROW_QUERY)
  const interactive = snapshot.interactive
  const connectionReadOnly = interactive.connection.status !== "open"
  const readOnly = !props.active || snapshot.leaving || connectionReadOnly
  const returnBlocked = Boolean(interactive.activeRun || interactive.interaction)
  const capabilities = new Set(interactive.runtime.capabilities ?? [])

  const overflowTriggerRef = useRef<HTMLButtonElement | null>(null)
  const headerMenuRef = useRef<HTMLDivElement | null>(null)
  const wasHeaderMenuOpenRef = useRef(false)
  const drawerTriggerRef = useRef<HTMLElement | null>(null)
  const wasSidebarOpenRef = useRef(false)
  const wasPanelOpenRef = useRef(false)

  // 打开 header menu 后焦点进入第一项；关闭后焦点返回 overflow trigger。
  useEffect(() => {
    if (snapshot.headerMenuOpen) {
      headerMenuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
    } else if (wasHeaderMenuOpenRef.current) {
      overflowTriggerRef.current?.focus()
    }
    wasHeaderMenuOpenRef.current = snapshot.headerMenuOpen
  }, [snapshot.headerMenuOpen])

  // 移动端抽屉/面板关闭后恢复打开前焦点；打开动作在 onIntent 中记录触发器。
  useEffect(() => {
    if (snapshot.sidebarOpen === false && wasSidebarOpenRef.current) {
      drawerTriggerRef.current?.focus()
    }
    wasSidebarOpenRef.current = snapshot.sidebarOpen
  }, [snapshot.sidebarOpen])

  useEffect(() => {
    const panelOpen = snapshot.activePanel !== null
    if (panelOpen === false && wasPanelOpenRef.current) {
      drawerTriggerRef.current?.focus()
    }
    wasPanelOpenRef.current = panelOpen
  }, [snapshot.activePanel])

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

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault()
        if (!readOnly) onIntent({ type: "command-menu-open" })
        return
      }
      if (event.key !== "Escape") return
      // Escape 关闭顺序固定：确认 Dialog → 命令菜单 → header overflow 菜单 → Utility 面板 → Thread 抽屉 → 取消 Run。
      if (interactive.confirmation) {
        // DialogHost 自己注册了 Escape handler；这里不再重复派发一次 resolve。
        return
      } else if (snapshot.commandMenuOpen) {
        event.preventDefault()
        onIntent({ type: "command-menu-close" })
      } else if (snapshot.headerMenuOpen) {
        event.preventDefault()
        onIntent({ type: "header-menu-toggle", open: false })
      } else if (snapshot.activePanel) {
        event.preventDefault()
        onIntent({ type: "panel-close" })
      } else if (narrow && snapshot.sidebarOpen) {
        event.preventDefault()
        onIntent({ type: "sidebar-toggle", open: false })
      } else if (!readOnly && interactive.activeRun) {
        event.preventDefault()
        onIntent({ type: "cancel-run" })
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [interactive.activeRun, interactive.confirmation, narrow, onIntent, readOnly, snapshot.activePanel, snapshot.commandMenuOpen, snapshot.headerMenuOpen, snapshot.sidebarOpen])

  return (
    <div className="web-shell" data-theme={snapshot.theme} data-active={props.active ? "true" : "false"}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">H</span>
          <span className="brand-name">Harness Code</span>
          <span className="brand-version">{interactive.runtime.cliVersion}</span>
        </div>
        <div className="topbar-main">
          <div className="topbar-project">
            {narrow ? (
              <button type="button" className="icon-button mobile-only" aria-label="打开 Thread 导航" title="打开 Thread 导航" disabled={readOnly} onClick={() => { onIntent({ type: "sidebar-toggle", open: true }) }}>
                <Menu aria-hidden="true" size={18} />
              </button>
            ) : null}
            <div className="topbar-project-copy">
              <span className="project-name">{workspaceLabel(interactive.runtime.workspace)}</span>
              {interactive.runtime.gitBranch ? <span className="project-meta">{interactive.runtime.gitBranch}</span> : null}
            </div>
          </div>
          <div className="topbar-meta">
            <button type="button" className="meta-chip" hidden={!capabilities.has(Capability.MODELS_READ)} disabled={readOnly} onClick={() => { onIntent({ type: "panel-open", panel: "models" }) }} title="模型设置">
              <span className="meta-chip-label">模型</span>
              <span className="meta-chip-value">{modelLabel(interactive)}</span>
            </button>
            <span className="meta-chip" title="审批模式">
              <span className="meta-chip-label">审批</span>
              <span className="meta-chip-value">{approvalModeLabel(interactive.runtime)}</span>
            </span>
            <button type="button" className="meta-chip" disabled={readOnly} onClick={() => { onIntent({ type: "panel-open", panel: "status" }) }} title="连接与活动状态">
              <span className={`status-dot status-dot-${interactive.activity.kind}`} />
              <span className="meta-chip-value">{props.active ? interactive.activity.label : "正在接管"}{interactive.connection.status !== "open" ? ` · ${connectionLabel(interactive.connection.status)}` : ""}</span>
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
              <Ellipsis aria-hidden="true" size={18} />
            </button>
            {snapshot.headerMenuOpen ? (
              <div ref={headerMenuRef} className="header-menu" role="menu" aria-label="更多操作">
                <button type="button" role="menuitem" className="header-menu-item" onClick={() => { onIntent({ type: "theme-set", theme: nextTheme(snapshot.theme) }) }}>
                  {snapshot.theme === "light" ? "使用深色主题" : "使用浅色主题"}
                </button>
                <button type="button" role="menuitem" className="header-menu-item" onClick={() => { onIntent({ type: "panel-open", panel: "help" }) }}>帮助</button>
                {narrow ? (
                  <button type="button" role="menuitem" className="header-menu-item" disabled={!props.active || snapshot.leaving || returnBlocked} title={returnBlocked ? "当前任务结束或交互完成后可返回 TUI" : "归还控制权并恢复 TUI"} onClick={() => { onIntent({ type: "return-to-tui" }) }}>返回 TUI</button>
                ) : null}
                <button type="button" role="menuitem" className="header-menu-item" disabled={!props.active || snapshot.leaving} onClick={() => { onIntent({ type: "exit-harness" }) }}>退出 Harness</button>
              </div>
            ) : null}
          </div>
        </div>
      </header>

      {snapshot.transientNotice ? <div className="notice-banner" role="status"><span>{snapshot.transientNotice}</span><button type="button" className="icon-button" aria-label="关闭通知" title="关闭通知" onClick={() => { onIntent({ type: "panel-close" }) }}><X aria-hidden="true" size={14} /></button></div> : null}
      {interactive.connection.status !== "open" ? <div className="connection-banner" role="alert">{interactive.connection.message}</div> : null}
      {!props.active ? <div className="handoff-banner" role="status">浏览器正在等待 CLI 确认控制权，页面保持只读。</div> : null}

      <div className={`workspace-grid ${snapshot.activePanel ? "has-utility-panel" : ""}`}>
        <ThreadSidebar snapshot={snapshot} dispatch={onIntent} narrow={narrow} disabled={readOnly} />
        <main className="conversation-column">
          <div className="timeline-scroll">
            <Timeline snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
          </div>
          {/* Interaction 卡固定在 composer 上方；无待处理请求时渲染空占位。 */}
          <div className="interaction-dock">
            <InteractionForm snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
          </div>
          <Composer snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
        </main>
        <UtilityPanels snapshot={snapshot} dispatch={onIntent} narrow={narrow} disabled={readOnly} />
      </div>

      <DialogHost snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
    </div>
  )
}

/** 订阅窄屏媒体查询；仅在 web 环境（matchMedia 存在）时生效。 */
function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false)
  useEffect(() => {
    if (typeof window === "undefined") return
    const media = window.matchMedia(query)
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches)
    media.addEventListener("change", onChange)
    setMatches(media.matches)
    return () => media.removeEventListener("change", onChange)
  }, [query])
  return matches
}

/** 主题切换的下一状态：浅色 ↔ 深色，菜单文案始终表达下一动作。 */
function nextTheme(theme: WebTheme): WebTheme {
  return theme === "light" ? "dark" : "light"
}

function modelLabel(snapshot: InteractiveSnapshot): string {
  const model = snapshot.selection.actualModel ?? snapshot.catalogs.models.items.find(item => item.id === snapshot.selection.requestedModelProfileId)
  return model ? `${model.provider_label} · ${model.model}` : snapshot.runtime.modelName ?? snapshot.runtime.modelProfileId ?? "未配置模型"
}

function connectionLabel(status: InteractiveSnapshot["connection"]["status"]): string {
  return status === "open" ? "连接正常" : status === "protocol-error" ? "协议错误" : "连接已断开"
}
