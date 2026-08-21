/** TUI 侧边栏 MCP 服务器状态小部件。 */

import type { LoadableCatalog, McpServerSummary } from "../../../interactive/types"
import { tuiTheme } from "../theme"

export type McpWidgetProps = {
  mcp: LoadableCatalog<McpServerSummary>
}

function mcpStatusDot(status: McpServerSummary["status"]): { dot: string; color: string } {
  switch (status) {
    case "connected":
      return { dot: "●", color: tuiTheme.success }
    case "failed":
      return { dot: "●", color: tuiTheme.danger }
    case "skipped":
    default:
      return { dot: "○", color: tuiTheme.muted }
  }
}

function mcpStatusLabel(status: McpServerSummary["status"]): string {
  switch (status) {
    case "connected":
      return "已连接"
    case "failed":
      return "失败"
    case "skipped":
      return "已跳过"
    default:
      return status
  }
}

export function McpWidget(props: McpWidgetProps) {
  const items = props.mcp.items
  const connectedCount = items.filter(s => s.status === "connected").length

  return (
    <box flexDirection="column" paddingTop={1} paddingBottom={1} border={["bottom"]} borderColor={tuiTheme.border}>
      <text fg={tuiTheme.primary}>
        <b>MCP 服务</b>
      </text>
      {items.length === 0 ? (
        <box paddingTop={1}>
          <text fg={tuiTheme.subtle}>0/0 · 无配置的 MCP 服务</text>
        </box>
      ) : (
        <box flexDirection="column" paddingTop={1}>
          <text fg={tuiTheme.text}>{connectedCount}/{items.length} 已连接</text>
          <box flexDirection="row" flexWrap="wrap" gap={2} paddingTop={1}>
          {items.slice(0, 5).map(server => {
            const { dot, color } = mcpStatusDot(server.status)
            return (
              <box key={server.name} flexDirection="row" gap={1}>
                <text fg={color}>{dot}</text>
                <text fg={tuiTheme.text}>{server.name}</text>
                <text fg={color}>{mcpStatusLabel(server.status)}</text>
              </box>
            )
          })}
          {items.length > 5 ? (
            <text fg={tuiTheme.subtle}>+{items.length - 5} 更多…</text>
          ) : null}
          </box>
        </box>
      )}
    </box>
  )
}
