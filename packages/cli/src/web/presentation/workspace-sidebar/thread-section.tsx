/** Thread 分区：搜索（本地状态）+ 最近 Thread 列表；迁移自旧 ThreadSidebar 的 ThreadList/filterThreads/formatUpdated。 */
/** @jsxImportSource react */

import { useState } from "react"
import { Search } from "lucide-react"

import type { ThreadSummary } from "@za38/protocol"

import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"

/** 渲染 Thread 搜索与列表；搜索词是分区本地状态，不进 Adapter（桌面侧栏纯表现行为）。 */
export function ThreadSection({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const [query, setQuery] = useState("")
  const interactive = snapshot.interactive
  const busy = Boolean(interactive.activeRun) || Boolean(interactive.interaction)
  const busyReason = busy ? "当前任务结束后可用" : null
  const threads = interactive.catalogs.threads
  const items = filterThreads(threads.items, query)

  return (
    <section className="thread-section" aria-label="Thread 列表">
      <div className="sidebar-toolbar">
        <label className="sidebar-search">
          <Search aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder="搜索 Thread…"
            aria-label="搜索 Thread"
            onChange={event => setQuery(event.currentTarget.value)}
            disabled={disabled}
          />
        </label>
      </div>
      <div className="sidebar-section-label">最近 Thread</div>
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
    </section>
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

/** 轻量级本地过滤：搜索词在 ThreadSection 内部维护，不再经 Adapter 往返。 */
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
