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
      <box flexDirection="row" justifyContent="space-between">
        <text fg={tuiTheme.subtle}>
          <b>MCP 服务</b>
        </text>
        <text fg={tuiTheme.muted}>
          {items.length ? `${connectedCount}/${items.length}` : "0/0"}
        </text>
      </box>
      {items.length === 0 ? (
        <text fg={tuiTheme.subtle}>无配置的 MCP 服务</text>
      ) : (
        <box flexDirection="column" paddingTop={0}>
          {items.slice(0, 5).map(server => {
            const { dot, color } = mcpStatusDot(server.status)
            return (
              <box key={server.name} flexDirection="row" justifyContent="space-between">
                <box flexDirection="row" gap={1}>
                  <text fg={color}>{dot}</text>
                  <text fg={tuiTheme.text}>{server.name}</text>
                </box>
                <text fg={color}>{mcpStatusLabel(server.status)}</text>
              </box>
            )
          })}
          {items.length > 5 ? (
            <text fg={tuiTheme.subtle}>+{items.length - 5} 更多…</text>
          ) : null}
        </box>
      )}
    </box>
  )
}
