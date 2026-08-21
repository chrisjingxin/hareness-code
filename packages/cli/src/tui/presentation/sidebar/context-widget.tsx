/** TUI 侧边栏上下文消耗与 Token 统计小部件。 */

import type { InteractiveSnapshot, TimelineItem } from "../../../interactive/types"
import { modelSelectionLabel } from "../../../presentation-shared"
import { tuiTheme } from "../theme"

export type ContextWidgetProps = {
  interactive: InteractiveSnapshot
}

/** 格式化 Token 数量为人类易读单位（k, M）。 */
export function formatTokenCount(tokens: number): string {
  if (!tokens || tokens < 0) return "0"
  if (tokens < 1000) return `${tokens}`
  if (tokens < 1_000_000) {
    const k = tokens / 1000
    return k >= 100 ? `${k.toFixed(1)}k` : `${k.toFixed(1)}k`
  }
  const m = tokens / 1_000_000
  return `${m.toFixed(1)}M`
}

/** 渲染字符进度条。 */
export function renderProgressBar(percentage: number, width = 16): string {
  const safePercentage = Math.max(0, Math.min(100, percentage))
  const filledCount = safePercentage > 0
    ? Math.max(1, Math.round((safePercentage / 100) * width))
    : 0
  const emptyCount = Math.max(0, width - filledCount)
  return "▮".repeat(filledCount) + "─".repeat(emptyCount)
}

/** 计算实时/单轮每秒输出 Token 数（TPS）。 */
export function calculateTps(outputTokens?: number, durationMs?: number): string | null {
  if (!outputTokens || outputTokens <= 0 || !durationMs || durationMs <= 0) return null
  const seconds = durationMs / 1000
  return (outputTokens / seconds).toFixed(1)
}

/** 粗略估算历史时间线消息与工具输出占用的 Token 数量。 */
export function estimateTimelineTokens(timeline: readonly TimelineItem[]): number {
  let chars = 0
  for (const item of timeline) {
    if (item.type === "message") {
      chars += item.message.content.length
    } else if (item.type === "tool") {
      chars += item.tool.output.length
    }
  }
  if (chars <= 0) return 0
  // 中英文混合与代码平均约 2.8 字符 / Token
  return Math.round(chars / 2.8)
}

export function ContextWidget(props: ContextWidgetProps) {
  const lastRun = props.interactive.lastRun
  const inputTokens = lastRun?.usage?.inputTokens ?? 0
  const outputTokens = lastRun?.usage?.outputTokens ?? 0
  const rawTokens = inputTokens + outputTokens

  // 优先读取真实 run usage；若为历史 resume 且未发生新 run，则使用 timeline 估算 tokens
  const isEstimated = rawTokens === 0 && props.interactive.timeline.length > 0
  const totalTokens = rawTokens > 0
    ? rawTokens
    : (isEstimated ? estimateTimelineTokens(props.interactive.timeline) : 0)
  
  // 上下文窗口容量（优先读取 context.inputCapTokens，默认 128k）
  const contextCap = lastRun?.context?.inputCapTokens ?? 128_000
  const usagePercentage = Math.min(100, (totalTokens / contextCap) * 100)
  const tps = calculateTps(outputTokens, lastRun?.durationMs)
  const modelName = modelSelectionLabel(props.interactive)

  const barWidth = 16
  const safePercentage = Math.max(0, Math.min(100, usagePercentage))
  const filledCount = safePercentage > 0
    ? Math.max(1, Math.round((safePercentage / 100) * barWidth))
    : 0
  const emptyCount = Math.max(0, barWidth - filledCount)

  return (
    <box flexDirection="column" paddingTop={1} paddingBottom={1} border={["bottom"]} borderColor={tuiTheme.border}>
      <text fg={tuiTheme.primary}>
        <b>上下文</b>
      </text>
      <box flexDirection="row" justifyContent="space-between" paddingTop={1}>
        <text fg={tuiTheme.text}>{modelName || "未绑定模型"}</text>
        {tps ? <text fg={tuiTheme.subtle}>{tps} tok/s</text> : null}
      </box>
      <box flexDirection="row" justifyContent="space-between" alignItems="center">
        <text fg={tuiTheme.text}>
          {formatTokenCount(totalTokens)} / {formatTokenCount(contextCap)}
        </text>
        <text fg={usagePercentage > 85 ? tuiTheme.warning : tuiTheme.muted}>
          {usagePercentage.toFixed(1)}%
        </text>
        <text>
          <span fg={usagePercentage > 85 ? tuiTheme.warning : tuiTheme.primary}>
            {"▮".repeat(filledCount)}
          </span>
          <span fg={tuiTheme.muted}>
            {"─".repeat(emptyCount)}
          </span>
        </text>
        {isEstimated ? <text fg={tuiTheme.subtle}>估算</text> : null}
      </box>
    </box>
  )
}
