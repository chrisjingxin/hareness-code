/** 跨端共享 Tool 输出策略：折叠阈值、参数摘要与单行截断的唯一实现。 */

/** 折叠头参数摘要的默认最大长度；两端 Tool 卡共用，避免各自漂移。 */
export const DEFAULT_ARGUMENT_SUMMARY_MAX = 80

/** 按行数和 Unicode 码点生成工具输出预览，并返回是否存在溢出。 */
export function collapseToolOutput(output: string, maxLines: number, maxChars: number): { output: string; overflow: boolean } {
  const lines = output.split("\n")
  if (lines.length <= maxLines && Array.from(output).length <= maxChars) {
    return { output, overflow: false }
  }

  const preview = lines.slice(0, maxLines).join("\n")
  if (Array.from(preview).length > maxChars) {
    return {
      output: `${Array.from(preview).slice(0, Math.max(0, maxChars - 1)).join("")}…`,
      overflow: true,
    }
  }

  return { output: [...lines.slice(0, maxLines), "…"].join("\n"), overflow: true }
}

/**
 * 折叠头参数摘要：只在该字段能安全收敛为单行文本时返回。
 * arguments 是 JSON 字符串时取 key: value 摘要；否则做空白归一化并截断。
 */
export function toolArgumentSummary(argumentsText: string | undefined, maxChars: number = DEFAULT_ARGUMENT_SUMMARY_MAX): string | null {
  if (!argumentsText) return null
  const trimmed = argumentsText.trim()
  if (!trimmed) return null
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>
    const entries = Object.entries(parsed)
    if (entries.length === 0) return null
    const summary = entries.map(([key, value]) => `${key}: ${stringifySummaryValue(value)}`).join(" · ")
    return truncateSingleLine(summary, maxChars)
  } catch {
    return truncateSingleLine(trimmed, maxChars)
  }
}

/** 把 JSON 标量/嵌套值收敛为短字符串；对象与数组只保留第一层规模。 */
function stringifySummaryValue(value: unknown): string {
  if (value === null) return "null"
  if (typeof value === "string") return value.length > 24 ? `${value.slice(0, 21)}…` : value
  if (typeof value === "object") {
    const size = Array.isArray(value) ? value.length : Object.keys(value as Record<string, unknown>).length
    return `{${size}}`
  }
  return String(value)
}

/** 空白归一化为单行并截断；超长追加省略号。 */
function truncateSingleLine(text: string, maxChars: number): string {
  const singleLine = text.replace(/\s+/g, " ").trim()
  if (singleLine.length <= maxChars) return singleLine
  return `${singleLine.slice(0, maxChars - 1)}…`
}
