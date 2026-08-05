/** Web Thread 侧边栏：桌面 260px 左栏，移动端全高抽屉；只展示用户可识别的 Thread 摘要。 */
/** @jsxImportSource react */

import { useEffect, useRef } from "react"
import { Plus, RefreshCw, Search, X } from "lucide-react"

import type { ThreadSummary } from "@za38/protocol"

import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"

/**
 * 渲染 Thread 导航与移动端抽屉。
 *
 * 桌面（`narrow=false`）使用 `sidebar` 左栏，移动端（`narrow=true`）改为 `sidebar-drawer`
 * 并通过 `snapshot.sidebarOpen` 控制可见性；close 按钮 dispatch `sidebar-toggle { open:false }`。
 * 移动端抽屉打开时把焦点限制在抽屉内，关闭时由 WebApp 恢复触发器焦点。
 * 所有用户动作只回传稳定 ID 或 typed intent，不直接访问 WebUiClient 或网关，也不渲染内部 thread_id。
 */
export function ThreadSidebar({
  snapshot,
  dispatch,
  narrow,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  narrow: boolean
  disabled?: boolean
}): React.ReactElement {
  const interactive = snapshot.interactive
  const busy = Boolean(interactive.activeRun) || Boolean(interactive.interaction)
  const busyReason = busy ? "当前任务结束后可用" : null
  const threads = interactive.catalogs.threads
  const query = snapshot.panelSearch.threads.query
  const items = filterThreads(threads.items, query)
  const isDrawer = narrow
  const drawerRef = useRef<HTMLDivElement | null>(null)

  const newThread = (
    <button
      type="button"
      className="new-thread-button"
      onClick={() => dispatch({ type: "thread-new" })}
      disabled={disabled || busy}
      aria-disabled={disabled || busy}
      title={disabled ? "接管尚未完成或连接不可用" : busyReason ?? "新建 Thread"}
    >
      <Plus aria-hidden="true" />
      <span>新建 Thread</span>
    </button>
  )

  const searchRow = (
    <div className="sidebar-toolbar">
      <label className="sidebar-search">
        <Search aria-hidden="true" />
        <input
          type="search"
          value={query}
          placeholder="搜索 Thread…"
          aria-label="搜索 Thread"
          onChange={event => dispatch({
            type: "panel-search",
            panel: "threads",
            query: event.currentTarget.value,
          })}
          disabled={disabled}
        />
      </label>
      <button
        type="button"
        className="icon-button sidebar-refresh"
        onClick={() => dispatch({ type: "thread-refresh" })}
        disabled={disabled}
        aria-label="刷新 Thread 列表"
        title="刷新"
      >
        <RefreshCw aria-hidden="true" />
      </button>
    </div>
  )

  const sectionLabel = (
    <div className="sidebar-section-label">最近 Thread</div>
  )

  const list = (
    <ThreadList
      items={items}
      status={threads.status}
      error={threads.status === "error" ? threads.message : null}
      currentThreadId={interactive.currentThreadId}
      busy={busy}
      busyReason={busyReason}
      disabled={disabled}
      dispatch={dispatch}
    />
  )

  const footer = (
    <footer className="sidebar-footer">
      <span className="sidebar-footer-label">本地工作区</span>
      <span className={`sidebar-footer-status ${interactive.connection.status === "open" ? "is-open" : "is-closed"}`}>
        {interactive.connection.status === "open" ? "连接正常" : "连接异常"}
      </span>
    </footer>
  )

  // 抽屉打开时限制 Tab 焦点在抽屉内并聚焦第一项；Escape 由 WebApp 全局处理。
  useEffect(() => {
    if (!isDrawer || !snapshot.sidebarOpen) return
    const drawer = drawerRef.current
    if (!drawer) return
    const focusables = drawer.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    )
    focusables[0]?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    drawer.addEventListener("keydown", onKeyDown)
    return () => drawer.removeEventListener("keydown", onKeyDown)
  }, [isDrawer, snapshot.sidebarOpen])

  const heading = (
    <div className="sidebar-heading">
      <div>
        <span className="eyebrow">Threads</span>
        <h2>Thread</h2>
      </div>
    </div>
  )

  if (!isDrawer) {
    return (
      <aside className="sidebar" aria-label="Thread 列表">
        {newThread}
        {searchRow}
        {sectionLabel}
        {list}
        {footer}
      </aside>
    )
  }

  return (
    <>
      <div
        ref={drawerRef}
        className="sidebar-drawer"
        data-open={snapshot.sidebarOpen ? "true" : "false"}
        role="dialog"
        aria-modal="true"
        aria-label="Thread 列表"
        aria-hidden={!snapshot.sidebarOpen}
        inert={!snapshot.sidebarOpen}
      >
        <header className="sidebar-drawer-header">
          <h2>Thread</h2>
          <button
            type="button"
            className="icon-button sidebar-close"
            onClick={() => dispatch({ type: "sidebar-toggle", open: false })}
            disabled={disabled}
            aria-label="关闭 Thread 列表"
          >
            <X aria-hidden="true" />
          </button>
        </header>
        {newThread}
        {searchRow}
        {sectionLabel}
        {list}
        {footer}
      </div>
      {snapshot.sidebarOpen ? (
        <button type="button" className="drawer-scrim" aria-label="关闭 Thread 导航" onClick={() => dispatch({ type: "sidebar-toggle", open: false })} />
      ) : null}
    </>
  )
}

function ThreadList({
  items,
  status,
  error,
  currentThreadId,
  busy,
  busyReason,
  disabled,
  dispatch,
}: {
  items: readonly ThreadSummary[]
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  currentThreadId: string | null
  busy: boolean
  busyReason: string | null
  disabled: boolean
  dispatch: (intent: WebIntent) => void
}): React.ReactElement {
  if (status === "loading" && items.length === 0) {
    return <p className="sidebar-status">正在读取 Thread…</p>
  }
  if (status === "error") {
    return (
      <div className="sidebar-status sidebar-status-error" role="alert">
        <p>{error ?? "Thread 列表加载失败"}</p>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => dispatch({ type: "thread-refresh" })}
          disabled={disabled}
        >
          重试
        </button>
      </div>
    )
  }
  if (items.length === 0) {
    return <p className="sidebar-status">没有匹配的 Thread</p>
  }
  return (
    <ul className="sidebar-list" role="list">
      {items.map(item => {
        const isActive = item.thread_id === currentThreadId
        const itemDisabled = disabled || (busy && !isActive)
        return (
          <li key={item.thread_id}>
            <button
              type="button"
              className="thread-item"
              data-active={isActive ? "true" : "false"}
              data-disabled={itemDisabled ? "true" : "false"}
              disabled={itemDisabled}
              aria-current={isActive ? "true" : undefined}
              title={itemDisabled ? (disabled ? "接管尚未完成或连接不可用" : busyReason ?? undefined) : undefined}
              onClick={() => dispatch({ type: "thread-select", threadId: item.thread_id })}
            >
              <span className="thread-item-title">{item.first_message || item.latest_message || "（无标题）"}</span>
              {item.latest_message && item.latest_message !== item.first_message ? (
                <span className="thread-item-summary">{item.latest_message}</span>
              ) : null}
              <span className="thread-item-meta">{`${formatUpdated(item.updated_at_ms)} · ${item.message_count} 条消息`}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

/** 轻量级本地过滤：与适配器 panel-search 状态对齐，避免再次引入 RPC。 */
function filterThreads(
  items: readonly ThreadSummary[],
  query: string,
): readonly ThreadSummary[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(item => {
    const haystack = `${item.first_message}\n${item.latest_message}`.toLowerCase()
    return haystack.includes(needle)
  })
}

/** 把更新时间折叠成短标签，与 TUI Picker 的展示口径保持一致。 */
function formatUpdated(updatedAtMs: number): string {
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - updatedAtMs) / 60_000))
  if (elapsedMinutes < 1) return "刚刚"
  if (elapsedMinutes < 60) return `${elapsedMinutes} 分钟前`
  const elapsedHours = Math.floor(elapsedMinutes / 60)
  if (elapsedHours < 24) return `${elapsedHours} 小时前`
  return `${Math.floor(elapsedHours / 24)} 天前`
}
