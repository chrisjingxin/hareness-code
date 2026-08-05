/** 共享 Tool 输出策略的边界测试：折叠阈值、参数摘要与截断的确定性。 */

import { expect, test } from "bun:test"
import {
  DEFAULT_ARGUMENT_SUMMARY_MAX,
  collapseToolOutput,
  toolArgumentSummary,
} from "../../src/presentation-shared/tool-output-policy"

test("collapseToolOutput：未超限时原样返回且不标记溢出", () => {
  const result = collapseToolOutput("a\nb", 4, 360)
  expect(result).toEqual({ output: "a\nb", overflow: false })
})

test("collapseToolOutput：超过行数限制时截断到前 N 行并追加省略行", () => {
  const result = collapseToolOutput("1\n2\n3\n4\n5", 4, 360)
  expect(result.overflow).toBe(true)
  expect(result.output).toBe("1\n2\n3\n4\n…")
})

test("collapseToolOutput：单行超长时按字符数截断并追加省略号", () => {
  const long = "x".repeat(500)
  const result = collapseToolOutput(long, 4, 100)
  expect(result.overflow).toBe(true)
  expect(result.output).toHaveLength(100)
  expect(result.output.endsWith("…")).toBe(true)
})

test("collapseToolOutput：空输出原样返回", () => {
  expect(collapseToolOutput("", 4, 360)).toEqual({ output: "", overflow: false })
})

test("collapseToolOutput：Unicode 字符按码点计数，不截断代理对", () => {
  const emoji = "👍".repeat(60)
  const result = collapseToolOutput(emoji, 4, 50)
  expect(result.output).toBe(`${"👍".repeat(49)}…`)
})

test("toolArgumentSummary：空/空白参数返回 null", () => {
  expect(toolArgumentSummary(undefined)).toBeNull()
  expect(toolArgumentSummary("")).toBeNull()
  expect(toolArgumentSummary("   ")).toBeNull()
})

test("toolArgumentSummary：JSON 参数收敛为 key: value 摘要", () => {
  const summary = toolArgumentSummary('{"path": "src/main.ts", "mode": "write"}')
  expect(summary).toBe("path: src/main.ts · mode: write")
})

test("toolArgumentSummary：超长摘要按默认阈值截断", () => {
  const longValue = "v".repeat(100)
  const summary = toolArgumentSummary(`{"key": "${longValue}"}`)
  expect(summary).not.toBeNull()
  expect(summary!.length).toBeLessThanOrEqual(DEFAULT_ARGUMENT_SUMMARY_MAX)
  expect(summary!.endsWith("…")).toBe(true)
})

test("toolArgumentSummary：非 JSON 文本做空白归一化后截断", () => {
  const summary = toolArgumentSummary("  a   b   c  ")
  expect(summary).toBe("a b c")
})

test("toolArgumentSummary：嵌套值只保留第一层规模", () => {
  const summary = toolArgumentSummary('{"items": [1,2,3], "meta": {"a":1}}')
  expect(summary).toBe("items: {3} · meta: {1}")
})
