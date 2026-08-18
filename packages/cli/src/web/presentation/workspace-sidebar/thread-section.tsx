/** Thread 分区：搜索（本地状态）+ 最近 Thread 列表；迁移自旧 ThreadSidebar 的 ThreadList/filterThreads/formatUpdated。 */
/** @jsxImportSource react */

import { useState } from "react"
import { MessageCircle, Search } from "lucide-react"

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
            placeholder="搜索线程..."
            aria-label="搜索 Thread"
            onChange={event => setQuery(event.currentTarget.value)}
            disabled={disabled}
          />
        </label>
      </div>
      <ThreadList
        items={items}
        status={threads.status}
        error={threads.status === "error" ? threads.message : null}
        currentThreadId={interactive.currentThreadId}
        busy={busy}
        interaction={interactive.interaction}
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
  interaction,
  busyReason,
  disabled,
  dispatch,
}: {
  items: readonly ThreadSummary[]
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  currentThreadId: string | null
  busy: boolean
  interaction: WebAdapterSnapshot["interactive"]["interaction"]
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
              <span className="thread-item-icon" aria-hidden="true">
                <MessageCircle size={15} strokeWidth={1.8} />
              </span>
              <span className="thread-item-title">{item.first_message || item.latest_message || "（无标题）"}</span>
              <span className="thread-item-summary">{threadSubtitle(item, currentThreadId, busy, interaction)}</span>
              <span className="thread-item-meta">{formatUpdated(item.updated_at_ms)}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

/** ThreadSummary 没有历史状态字段；只对当前可观测的运行/审批展示进行中，其余显示设计稿文案。 */
function threadSubtitle(
  item: ThreadSummary,
  currentThreadId: string | null,
  busy: boolean,
  interaction: WebAdapterSnapshot["interactive"]["interaction"],
): string {
  if (item.thread_id !== currentThreadId || !busy) return "已完成"
  if (interaction?.type === "approval") return "正在审查文件变更"
  return "正在处理当前变更"
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
  const updated = new Date(updatedAtMs)
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfUpdatedDay = new Date(updated.getFullYear(), updated.getMonth(), updated.getDate()).getTime()
  const dayDelta = Math.floor((startOfToday - startOfUpdatedDay) / 86_400_000)
  if (dayDelta === 0) {
    return updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
  }
  if (dayDelta === 1) return "昨天"
  if (dayDelta > 1 && dayDelta < 7) return `${dayDelta} 天前`
  if (dayDelta >= 7 && dayDelta < 14) return "1 周前"
  return updated.toLocaleDateString([], { month: "numeric", day: "numeric" })
}
