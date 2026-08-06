/** Models 面板：选择当前 Thread 下一次运行的 Model Profile（迁移自 panels.tsx，dispatch 语义不变）。 */
/** @jsxImportSource react */

import { Cpu } from "lucide-react"

import type { ModelProfile } from "@za38/protocol"

import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { PanelEmpty, PanelError, PanelToolbar } from "./panel-common"

/** Models 面板：搜索 + 选择 Profile；rejected 错误显示在面板内。 */
export function ModelsPanel({
  snapshot,
  busyReason,
  disabled = false,
  dispatch,
}: {
  snapshot: WebAdapterSnapshot
  busyReason: string | null
  disabled?: boolean
  dispatch: (intent: WebIntent) => void
}): React.ReactElement {
  const catalog = snapshot.interactive.catalogs.models
  const query = snapshot.panelSearch.models.query
  const panelError = snapshot.panelSearch.models.error
  const items = filterModels(catalog.items, query)
  const selectedId = snapshot.interactive.selection.requestedModelProfileId
    ?? snapshot.interactive.selection.actualModel?.id
    ?? null
  const isLoading = catalog.status === "loading" && catalog.items.length === 0
  return (
    <div className="panel panel-models">
      <PanelToolbar
        query={query}
        placeholder="搜索 Model…"
        onSearch={value => dispatch({ type: "panel-search", panel: "models", query: value })}
        onRefresh={() => dispatch({ type: "dock-panel-select", panel: "models" })}
        disabled={disabled}
      />
      {panelError ? (
        <PanelError
          message={panelError}
          onRetry={() => dispatch({ type: "dock-panel-select", panel: "models" })}
          disabled={disabled}
        />
      ) : catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "dock-panel-select", panel: "models" })}
          disabled={disabled}
        />
      ) : isLoading ? (
        <p className="panel-status">正在读取 Model…</p>
      ) : items.length === 0 ? (
        <PanelEmpty message="没有匹配的 Model" />
      ) : (
        <ul className="panel-list" role="list">
          {items.map(profile => {
            const isCurrent = profile.id === selectedId
            const itemDisabled = disabled || Boolean(busyReason)
            const unavailReason = !profile.available
              ? (profile.unavailable_reason ?? "当前不可用")
              : null
            return (
              <li key={profile.id}>
                <button
                  type="button"
                  className="panel-item"
                  data-active={isCurrent ? "true" : "false"}
                  data-disabled={itemDisabled ? "true" : "false"}
                  disabled={itemDisabled}
                  aria-pressed={isCurrent}
                  title={busyReason ?? unavailReason ?? undefined}
                  onClick={() => dispatch({ type: "model-select", profileId: profile.id })}
                >
                  <span className="panel-item-title">
                    <Cpu aria-hidden="true" />
                    {profile.id}
                  </span>
                  <span className="panel-item-sub">
                    {profile.provider_label} · {profile.model}
                  </span>
                  {unavailReason ? (
                    <span className="panel-item-note">{unavailReason}</span>
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function filterModels(
  items: readonly ModelProfile[],
  query: string,
): readonly ModelProfile[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(profile => {
    const haystack = `${profile.id} ${profile.model} ${profile.provider_label}`.toLowerCase()
    return haystack.includes(needle)
  })
}
