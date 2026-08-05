/** 跨端共享格式化策略：时长、token 用量与上下文预算的确定性展示。 */

/** 将毫秒耗时格式化为紧凑的毫秒或秒显示；缺失/零/亚毫秒不展示。 */
export function formatDuration(durationMs: number | undefined): string | undefined {
  if (!durationMs || durationMs < 1) return undefined
  if (durationMs < 1000) return `${durationMs}ms`
  return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`
}

/** 将 token 用量格式化为 in/out 摘要；缺失不展示。 */
export function formatUsage(usage: { inputTokens: number; outputTokens: number } | undefined): string | undefined {
  if (!usage) return undefined
  return `${compactNumber(usage.inputTokens)} in · ${compactNumber(usage.outputTokens)} out`
}

/** 将上下文预算格式化为 estimated/cap 摘要；缺失或零预算不展示。 */
export function formatContext(estimatedTokens: number | undefined, inputCapTokens: number | undefined): string | undefined {
  if (!estimatedTokens || !inputCapTokens) return undefined
  return `${compactNumber(estimatedTokens)}/${compactNumber(inputCapTokens)}`
}

/** 将大数字转换为 k 单位，避免窄终端/窄卡片换行。 */
function compactNumber(value: number): string {
  if (value < 1000) return String(value)
  return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}k`
}
