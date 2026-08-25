/** Agents 面板：只读浏览可派发 Agent，不切换当前会话 Agent。 */
/** @jsxImportSource react */

import { Bot } from "lucide-react"

import { agentBrowsePurpose, agentKindLabel, filterAgents } from "../../../presentation-shared/agent-catalog"

import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { PanelEmpty, PanelError, PanelToolbar } from "./panel-common"

/** 只读 Agent 目录：搜索、刷新；选中不 arm、不提交 task。 */
export function AgentsPanel({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const catalog = snapshot.interactive.catalogs.agents
  const query = snapshot.panelSearch.agents.query
  const items = filterAgents(catalog.items, query)
  const isLoading = catalog.status === "loading" && catalog.items.length === 0
  return (
    <div className="panel panel-agents">
      <PanelToolbar
        query={query}
        placeholder="搜索 Agent…"
        onSearch={value => dispatch({ type: "panel-search", panel: "agents", query: value })}
        onRefresh={() => dispatch({ type: "dock-panel-select", panel: "agents" })}
        disabled={disabled}
      />
      {catalog.status === "error" ? (
        <PanelError
          message={catalog.message}
          onRetry={() => dispatch({ type: "dock-panel-select", panel: "agents" })}
          disabled={disabled}
        />
      ) : isLoading ? (
        <p className="panel-status">正在读取 Agent…</p>
      ) : items.length === 0 ? (
        <PanelEmpty message="没有匹配的 Agent" />
      ) : (
        <ul className="panel-list" role="list">
          {items.map(agent => (
            <li key={agent.id}>
              <div className="panel-item">
                <span className="panel-item-title">
                  <Bot aria-hidden="true" />
                  {agent.id}
                </span>
                <span className="panel-item-sub">
                  {agentKindLabel(agent.kind)} · {agentBrowsePurpose(agent)}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="panel-empty">派出请在对话里让主 Agent 使用 task，这里只浏览目录。</p>
    </div>
  )
}


