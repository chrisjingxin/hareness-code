/**
 * 适配自 OpenCode 的 `packages/tui/src/util/collapse-tool-output.ts`。
 * 上游提交：05c3e40a4e641732b991499000ca479e5dad4b02（MIT）。
 * 仅保留与本项目协议无关的纯输出折叠逻辑，具体工具视图仍由 Harness Code 维护。
 */
/** 按行数和 Unicode 字符数生成工具输出预览，并返回是否存在溢出。 */
export function collapseToolOutput(output: string, maxLines: number, maxChars: number) {
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
