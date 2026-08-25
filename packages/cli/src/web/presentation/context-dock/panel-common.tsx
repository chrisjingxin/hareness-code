/** Context Dock 共享内件：主 tab 可见性/标签与通用工具栏/错误/空态。 */
/** @jsxImportSource react */

import { RefreshCw, Search } from "lucide-react"

import type { FeatureAvailability } from "../../../interactive/selectors"
import type { ContextDockPanel } from "../../application/adapter"

/** Dock 主 tab：设计稿中的所有面板均在同一组标签内切换。 */
export type DockTab = "code" | "models" | "skills" | "mcp" | "agents" | "status" | "help"

export const DOCK_TABS: readonly DockTab[] = ["code", "models", "skills", "mcp", "agents", "status", "help"]

/** 主 tab 可见性：只显示 capability 允许的 tab；Code/Status/Help 恒可见。 */
export function tabVisible(tab: DockTab, availability: Pick<FeatureAvailability, "canOpenModelsPanel" | "canOpenSkillsPanel" | "canOpenMcpPanel" | "canOpenAgentsPanel">): boolean {
  switch (tab) {
    case "models": return availability.canOpenModelsPanel
    case "skills": return availability.canOpenSkillsPanel
    case "mcp": return availability.canOpenMcpPanel
    case "agents": return availability.canOpenAgentsPanel
    case "code":
    case "status": return true
    case "help": return true
  }
}

/** 主 tab 与 Help 面板的展示标签。 */
export function tabLabel(tab: ContextDockPanel): string {
  switch (tab) {
    case "code": return "代码"
    case "models": return "模型"
    case "skills": return "技能"
    case "mcp": return "MCP"
    case "agents": return "Agent"
    case "status": return "状态"
    case "help": return "帮助"
  }
}

/** 面板通用工具栏：搜索框 + 刷新按钮。 */
export function PanelToolbar({
  query,
  placeholder,
  onSearch,
  onRefresh,
  disabled = false,
}: {
  query: string
  placeholder: string
  onSearch: (value: string) => void
  onRefresh: () => void
  disabled?: boolean
}): React.ReactElement {
  return (
    <div className="panel-toolbar">
      <label className="panel-search-input">
        <Search aria-hidden="true" />
        <input
          type="search"
          value={query}
          placeholder={placeholder}
          aria-label={placeholder}
          onChange={event => onSearch(event.currentTarget.value)}
          disabled={disabled}
        />
      </label>
      <button
        type="button"
        className="icon-button"
        onClick={onRefresh}
        disabled={disabled}
        aria-label="刷新"
        title="刷新"
      >
        <RefreshCw aria-hidden="true" />
      </button>
    </div>
  )
}

/** 面板错误块：message + 重试按钮。 */
export function PanelError({
  message,
  onRetry,
  disabled = false,
}: {
  message: string
  onRetry: () => void
  disabled?: boolean
}): React.ReactElement {
  return (
    <div className="panel-error" role="alert">
      <p>{message}</p>
      <button
        type="button"
        className="button button-secondary"
        onClick={onRetry}
        disabled={disabled}
      >
        重试
      </button>
    </div>
  )
}

/** 面板空态文案。 */
export function PanelEmpty({ message }: { message: string }): React.ReactElement {
  return <p className="panel-empty">{message}</p>
}
