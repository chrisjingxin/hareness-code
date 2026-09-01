/** Status 运行状态仪表盘浮层组件（OpenTUI）。 */

import type { ReactNode } from "react"
import type { InteractiveSnapshot, TimelineItem } from "../../interactive/types"
import type { SidebarState } from "../application/adapter"
import { modeAccent, tuiTheme } from "./theme"
import { OverlayShell } from "./overlays"
import { workspaceLabel, executionStatusLabel, approvalModeLabel } from "../../interactive/runtime"
import { gitWorkspaceLabel } from "../../presentation-shared"

export type StatusModalProps = {
  visible: boolean
  interactive: InteractiveSnapshot
  sidebar?: SidebarState
  terminalWidth: number
  terminalHeight: number
  onClose: () => void
}

/** 格式化 Token 数量为易读单位 (k, M) */
function formatTokenCount(tokens: number): string {
  if (!tokens || tokens < 0) return "0"
  if (tokens < 1000) return `${tokens}`
  if (tokens < 1_000_000) {
    const k = tokens / 1000
    return `${k >= 10 ? Math.round(k) : k.toFixed(1)}k`
  }
  const m = tokens / 1_000_000
  return `${m.toFixed(1)}M`
}

/** 辅助格式化 Token 水位条 */
function renderTokenBar(used: number, max: number, width = 10): string {
  if (max <= 0) return ""
  const ratio = Math.min(1, Math.max(0, used / max))
  const filled = Math.round(ratio * width)
  const empty = Math.max(0, width - filled)
  return `[${"█".repeat(filled)}${"░".repeat(empty)}] ${Math.round(ratio * 100)}%`
}

/** 计算 Timeline 中的消息和工具调用统计 */
function countTimelineStats(timeline: readonly TimelineItem[]): { messages: number; toolCalls: number } {
  let messages = 0
  let toolCalls = 0
  for (const item of timeline) {
    if (item.type === "message") messages++
    else if (item.type === "tool") toolCalls++
  }
  return { messages, toolCalls }
}

/** 粗略估算时间线中的 Token 占用（平均 ~2.8 字符 / token） */
function estimateTimelineTokens(timeline: readonly TimelineItem[]): number {
  let chars = 0
  for (const item of timeline) {
    if (item.type === "message") chars += item.message.content.length
    else if (item.type === "tool") chars += item.tool.output.length
  }
  return chars > 0 ? Math.round(chars / 2.8) : 0
}

/** 格式化 Git 变更统计 */
function describeGitChanges(sidebar?: SidebarState): { text: string; clean: boolean } {
  const files = sidebar?.workspaceChangedFiles
  if (!files || files.length === 0) {
    return { text: "工作区整洁 (Clean)", clean: true }
  }
  let totalAdded = 0
  let totalRemoved = 0
  for (const f of files) {
    totalAdded += f.addedLines ?? 0
    totalRemoved += f.removedLines ?? 0
  }
  const diffPart = (totalAdded > 0 || totalRemoved > 0) ? ` (+${totalAdded} -${totalRemoved})` : ""
  return {
    text: `${files.length} 个文件修改${diffPart}`,
    clean: false,
  }
}

/**
 * 运行状态仪表盘：结构化展示 4 大卡片模块（工作区环境、运行模式、上下文健康度、扩展生态），
 * 支持自适应双列/单列排版，按 Esc / Enter / q 快速关闭。
 */
export function StatusModal(props: StatusModalProps): ReactNode {
  if (!props.visible) return null

  const { interactive, sidebar, terminalWidth, terminalHeight, onClose } = props
  const runtime = interactive.runtime
  const accent = modeAccent(interactive.workMode)
  const stats = countTimelineStats(interactive.timeline)
  const gitChanges = describeGitChanges(sidebar)
  const gitBranch = gitWorkspaceLabel(runtime.gitWorkspace)

  // 模型展示
  const modelName = interactive.selection.actualModel?.model ?? runtime.modelName ?? "未配置"
  const modelProfile = interactive.selection.requestedModelProfileId ?? runtime.modelProfileId

  // MCP 统计
  const mcpItems = interactive.catalogs.mcp.items
  let totalMcpTools = 0
  for (const s of mcpItems) {
    totalMcpTools += s.tool_names?.length ?? 0
  }
  const mcpSummaryText = mcpItems.length > 0
    ? `${mcpItems.length} 个服务在线 (${totalMcpTools} 个工具)`
    : (runtime.mcpSummary ?? "未配置")

  // Skills 与 Agents 统计
  const skillsCount = interactive.catalogs.skills.items.length
  const agentsCount = interactive.catalogs.agents.items.length

  // Token 与上下文用量计算
  const lastRun = interactive.lastRun
  const inputTokens = lastRun?.usage?.inputTokens ?? 0
  const outputTokens = lastRun?.usage?.outputTokens ?? 0
  const rawTokens = inputTokens + outputTokens
  const isEstimated = rawTokens === 0 && interactive.timeline.length > 0
  const totalTokens = rawTokens > 0 ? rawTokens : (isEstimated ? estimateTimelineTokens(interactive.timeline) : 0)
  const maxTokens = lastRun?.context?.inputCapTokens ?? 128_000
  const cachedTokens = lastRun?.usage?.cachedTokens
  const cacheHitRate = cachedTokens !== undefined && inputTokens > 0
    ? Math.min(100, Math.max(0, Math.round((cachedTokens / inputTokens) * 100)))
    : null

  const isWide = terminalWidth >= 86 && terminalHeight >= 22

  return (
    <OverlayShell terminalWidth={terminalWidth} terminalHeight={terminalHeight} placement="dialog" zIndex={106}>
      {({ width }: { width: number }) => (
        <box
          width={Math.max(width, isWide ? 88 : 54)}
          maxWidth="100%"
          backgroundColor={tuiTheme.menu}
          flexDirection="column"
          zIndex={1}
          paddingLeft={3}
          paddingRight={3}
          paddingTop={1}
          paddingBottom={1}
        >
          {/* Header */}
          <box flexDirection="row" justifyContent="space-between" alignItems="center" paddingBottom={1}>
            <text fg={tuiTheme.text}>
              <strong>📊 运行状态仪表盘 (Status)</strong>
            </text>
            <box backgroundColor={tuiTheme.panel} paddingLeft={1} paddingRight={1}>
              <text fg={accent}>
                <strong>{interactive.workMode.toUpperCase()} 模式</strong>
              </text>
            </box>
          </box>

          {/* 4 Cards Container */}
          <box flexDirection={isWide ? "row" : "column"} gap={isWide ? 2 : 1}>
            {/* Card 1 & 3: Left Column */}
            <box flexDirection="column" flexGrow={1} gap={1}>
              {/* Card 1: 工作区与环境 */}
              <box backgroundColor={tuiTheme.panel} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} flexDirection="column">
                <text fg={tuiTheme.primary}>
                  <strong>[ 工作区与环境 ]</strong>
                </text>
                <box paddingTop={1} flexDirection="column" gap={0}>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>目录: </span>{workspaceLabel(runtime.workspace)}
                  </text>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>Git:  </span>
                    {gitBranch ? `${gitBranch} · ` : ""}
                    <span style={{ fg: gitChanges.clean ? tuiTheme.success : tuiTheme.warning }}>{gitChanges.text}</span>
                  </text>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>版本: </span>za38-cli {runtime.cliVersion ?? "0.1.0"}
                  </text>
                </box>
              </box>

              {/* Card 3: 会话与上下文 */}
              <box backgroundColor={tuiTheme.panel} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} flexDirection="column">
                <text fg={tuiTheme.primary}>
                  <strong>[ 会话与上下文 ]</strong>
                </text>
                <box paddingTop={1} flexDirection="column" gap={0}>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>会话: </span>
                    {interactive.currentThreadId ? `${interactive.currentThreadId.slice(0, 12)}…` : "新会话"}
                    <span style={{ fg: tuiTheme.muted }}> ({stats.messages} 消息 · {stats.toolCalls} 工具)</span>
                  </text>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>用量: </span>
                    {totalTokens > 0 ? (
                      <>
                        <span style={{ fg: totalTokens / maxTokens > 0.8 ? tuiTheme.danger : tuiTheme.success }}>
                          {renderTokenBar(totalTokens, maxTokens, 10)}
                        </span>
                        <span style={{ fg: tuiTheme.muted }}> ({formatTokenCount(totalTokens)} / {formatTokenCount(maxTokens)}{isEstimated ? " 估算" : ""})</span>
                      </>
                    ) : (
                      <span style={{ fg: tuiTheme.muted }}>0 tokens (新会话)</span>
                    )}
                  </text>
                  {cacheHitRate !== null ? (
                    <text fg={tuiTheme.text}>
                      <span style={{ fg: tuiTheme.muted }}>缓存: </span>
                      <span style={{ fg: tuiTheme.success }}>{cacheHitRate}% 命中</span>
                      <span style={{ fg: tuiTheme.muted }}> ({formatTokenCount(cachedTokens ?? 0)} tokens)</span>
                    </text>
                  ) : null}
                </box>
              </box>
            </box>

            {/* Card 2 & 4: Right Column */}
            <box flexDirection="column" flexGrow={1} gap={1}>
              {/* Card 2: 运行模式与模型 */}
              <box backgroundColor={tuiTheme.panel} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} flexDirection="column">
                <text fg={tuiTheme.primary}>
                  <strong>[ 运行模式与模型 ]</strong>
                </text>
                <box paddingTop={1} flexDirection="column" gap={0}>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>模型: </span>
                    {modelProfile ? <span style={{ fg: accent }}>{modelProfile} · </span> : null}
                    {modelName}
                  </text>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>执行: </span>{executionStatusLabel(runtime)}
                  </text>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>审批: </span>
                    <span style={{ fg: runtime.approvalMode === "yolo" ? tuiTheme.danger : runtime.approvalMode === "plan" ? tuiTheme.primary : tuiTheme.success }}>
                      [{approvalModeLabel(runtime)}]
                    </span>
                    {runtime.approvalModeWarning ? (
                      <span style={{ fg: tuiTheme.warning }}> ({runtime.approvalModeWarning})</span>
                    ) : null}
                  </text>
                </box>
              </box>

              {/* Card 4: 扩展生态与连接 */}
              <box backgroundColor={tuiTheme.panel} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1} flexDirection="column">
                <text fg={tuiTheme.primary}>
                  <strong>[ 扩展生态与连接 ]</strong>
                </text>
                <box paddingTop={1} flexDirection="column" gap={0}>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>连接: </span>
                    <span style={{ fg: interactive.connection.status === "open" ? tuiTheme.success : tuiTheme.danger }}>
                      {interactive.connection.status === "open" ? "🟢 正常" : "🔴 异常"}
                    </span>
                  </text>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>MCP:  </span>{mcpSummaryText}
                  </text>
                  <text fg={tuiTheme.text}>
                    <span style={{ fg: tuiTheme.muted }}>技能: </span>{skillsCount} 个 Skill · {agentsCount} 个 Agent
                  </text>
                </box>
              </box>
            </box>
          </box>

          {/* Footer Action */}
          <box paddingTop={1} flexDirection="row" justifyContent="flex-end" alignItems="center">
            <box backgroundColor={tuiTheme.panel} paddingLeft={2} paddingRight={2} onMouseUp={onClose}>
              <text fg={tuiTheme.text}>Esc / Enter / q 关闭</text>
            </box>
          </box>
        </box>
      )}
    </OverlayShell>
  )
}
