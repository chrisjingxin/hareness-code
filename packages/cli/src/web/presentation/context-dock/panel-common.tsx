/** Context Dock 共享内件：主 tab 可见性/标签与通用工具栏/错误/空态。 */
/** @jsxImportSource react */

import { RefreshCw, Search } from "lucide-react"

import type { FeatureAvailability } from "../../../interactive/selectors"
import type { ContextDockPanel } from "../../application/adapter"

/** Dock 主 tab：Code 与 Status 恒可见；Help 只从顶栏更多菜单打开不占 tab。 */
export type DockTab = "code" | "models" | "skills" | "mcp" | "status"

export const DOCK_TABS: readonly DockTab[] = ["code", "models", "skills", "mcp", "status"]

/** 主 tab 可见性：只显示 capability 允许的 tab；Code/Status 恒可见。 */
export function tabVisible(tab: DockTab, availability: Pick<FeatureAvailability, "canOpenModelsPanel" | "canOpenSkillsPanel" | "canOpenMcpPanel">): boolean {
  switch (tab) {
    case "models": return availability.canOpenModelsPanel
    case "skills": return availability.canOpenSkillsPanel
    case "mcp": return availability.canOpenMcpPanel
    case "code":
    case "status": return true
  }
}

/** 主 tab 与 Help 面板的展示标签。 */
export function tabLabel(tab: ContextDockPanel): string {
  switch (tab) {
    case "code": return "Code"
    case "models": return "Model"
    case "skills": return "Skills"
    case "mcp": return "MCP"
    case "status": return "Status"
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
