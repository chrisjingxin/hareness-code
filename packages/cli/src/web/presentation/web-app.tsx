/** Web React 工作台：组合布局与可访问性，不拥有 Agent 或命令业务状态。 */
/** @jsxImportSource react */

import { CircleHelp, Menu, Plug, Settings2, ShieldCheck, Terminal, X } from "lucide-react"
import { useCallback, useEffect, useState, useSyncExternalStore } from "react"
import type { ReactNode } from "react"
import { Capability } from "@za38/protocol"

import { workspaceLabel } from "../../interactive/runtime"
import type { InteractiveSnapshot } from "../../interactive/types"
import type { WebInteractiveAdapter, WebAdapterSnapshot, WebIntent } from "../application/adapter"
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

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault()
        if (!readOnly) onIntent({ type: "command-menu-open" })
        return
      }
      if (event.key !== "Escape") return
      // Escape 先关闭最上层 drawer/dialog/menu，再取消 Run。
      if (interactive.confirmation) {
        // DialogHost 自己注册了 Escape handler；这里不再重复派发一次 resolve。
        return
      } else if (snapshot.commandMenuOpen) {
        event.preventDefault()
        onIntent({ type: "command-menu-close" })
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
  }, [interactive.activeRun, interactive.confirmation, narrow, onIntent, readOnly, snapshot.activePanel, snapshot.commandMenuOpen, snapshot.sidebarOpen])

  return (
    <div className={`web-shell ${props.active ? "is-active" : "is-opening"} ${snapshot.leaving ? "is-leaving" : ""}`}>
      <header className="topbar">
        <div className="topbar-project">
          {narrow ? (
            <button type="button" className="icon-button mobile-only" aria-label="打开 Thread 导航" title="打开 Thread 导航" disabled={readOnly} onClick={() => { onIntent({ type: "sidebar-toggle", open: true }) }}>
              <Menu aria-hidden="true" size={18} />
            </button>
          ) : null}
          <div>
            <div className="project-name">{workspaceLabel(interactive.runtime.workspace)}</div>
            <div className="project-meta">{interactive.runtime.gitBranch ? `branch · ${interactive.runtime.gitBranch}` : "local workspace"}</div>
          </div>
        </div>
        <div className="topbar-status" aria-live="polite">
          <span className={`status-pill status-${interactive.activity.kind}`}><span className="status-dot" />{props.active ? interactive.activity.label : "正在接管"}</span>
          <span className="status-pill model-pill">{modelLabel(interactive)}</span>
          <span className={`status-pill connection-${interactive.connection.status}`}>{connectionLabel(interactive.connection.status)}</span>
        </div>
        <div className="topbar-actions">
          <ToolbarButton label="模型" icon={<Settings2 aria-hidden="true" size={16} />} hidden={!capabilities.has(Capability.MODELS_READ)} disabled={readOnly} onClick={() => { onIntent({ type: "panel-open", panel: "models" }) }} />
          <ToolbarButton label="Skills" icon={<ShieldCheck aria-hidden="true" size={16} />} hidden={!capabilities.has(Capability.SKILLS_READ)} disabled={readOnly} onClick={() => { onIntent({ type: "panel-open", panel: "skills" }) }} />
          <ToolbarButton label="MCP" icon={<Plug aria-hidden="true" size={16} />} hidden={!capabilities.has(Capability.MCP_READ)} disabled={readOnly} onClick={() => { onIntent({ type: "panel-open", panel: "mcp" }) }} />
          <ToolbarButton label="状态" icon={<Terminal aria-hidden="true" size={16} />} disabled={readOnly} onClick={() => { onIntent({ type: "panel-open", panel: "status" }) }} />
          <ToolbarButton label="帮助" icon={<CircleHelp aria-hidden="true" size={16} />} disabled={readOnly} onClick={() => { onIntent({ type: "panel-open", panel: "help" }) }} />
          <button type="button" className="button button-secondary return-button" disabled={!props.active || snapshot.leaving || returnBlocked} title={returnBlocked ? "当前任务结束或交互完成后可返回 TUI" : "归还控制权并恢复 TUI"} onClick={() => { onIntent({ type: "return-to-tui" }) }}>返回 TUI</button>
          <button type="button" className="button button-quiet" disabled={!props.active || snapshot.leaving} onClick={() => { onIntent({ type: "exit-harness" }) }}>退出</button>
        </div>
      </header>

      {snapshot.transientNotice ? <div className="notice-banner" role="status"><span>{snapshot.transientNotice}</span><button type="button" className="icon-button" aria-label="关闭通知" title="关闭通知" onClick={() => { onIntent({ type: "panel-close" }) }}><X aria-hidden="true" size={14} /></button></div> : null}
      {interactive.connection.status !== "open" ? <div className="connection-banner" role="alert">{interactive.connection.message}</div> : null}
      {!props.active ? <div className="handoff-banner" role="status">浏览器正在等待 CLI 确认控制权，页面保持只读。</div> : null}

      <div className={`workspace-grid ${snapshot.activePanel ? "has-utility-panel" : ""}`}>
        <ThreadSidebar snapshot={snapshot} dispatch={onIntent} narrow={narrow} disabled={readOnly} />
        <main className="conversation-column">
          <div className="mobile-thread-bar">
            <button type="button" className="button button-quiet" onClick={() => { onIntent({ type: "sidebar-toggle", open: true }) }}><Menu aria-hidden="true" size={16} /> Threads</button>
            <span>{interactive.currentThreadId ? "当前 Thread" : "New Thread"}</span>
          </div>
          <div className="timeline-scroll">
            <Timeline snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
          </div>
          {/* Interaction 卡固定在 composer 上方；无待处理请求时渲染空占位。 */}
          <div className="interaction-dock">
            <InteractionForm snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
          </div>
          <Composer snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
        </main>
        <UtilityPanels snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
      </div>

      <DialogHost snapshot={snapshot} dispatch={onIntent} disabled={readOnly} />
    </div>
  )
}

function ToolbarButton(props: { label: string; icon: ReactNode; hidden?: boolean; disabled?: boolean; onClick: () => void }) {
  if (props.hidden) return null
  return <button type="button" className="button button-quiet toolbar-button" disabled={props.disabled} onClick={props.onClick}>{props.icon}<span>{props.label}</span></button>
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

function modelLabel(snapshot: InteractiveSnapshot): string {
  const model = snapshot.selection.actualModel ?? snapshot.catalogs.models.items.find(item => item.id === snapshot.selection.requestedModelProfileId)
  return model ? `${model.provider_label} · ${model.model}` : snapshot.runtime.modelName ?? snapshot.runtime.modelProfileId ?? "未配置模型"
}

function connectionLabel(status: InteractiveSnapshot["connection"]["status"]): string {
  return status === "open" ? "连接正常" : status === "protocol-error" ? "协议错误" : "连接已断开"
}
