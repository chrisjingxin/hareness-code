/** 共享 formatters 的边界测试：时长、token 用量与上下文预算格式化的确定性。 */

import { expect, test } from "bun:test"
import { formatContext, formatDuration, formatElapsed, formatUsage } from "../../src/presentation-shared/formatters"

test("formatDuration：缺失/零/亚毫秒统一返回 undefined", () => {
  expect(formatDuration(undefined)).toBeUndefined()
  expect(formatDuration(0)).toBeUndefined()
})

test("formatDuration：小于 1 秒显示毫秒", () => {
  expect(formatDuration(1)).toBe("1ms")
  expect(formatDuration(840)).toBe("840ms")
  expect(formatDuration(999)).toBe("999ms")
})

test("formatDuration：1 秒以上显示秒，短时长保留 1 位小数", () => {
  expect(formatDuration(1_000)).toBe("1.0s")
  expect(formatDuration(1_350)).toBe("1.4s")
  expect(formatDuration(9_999)).toBe("10.0s")
})

test("formatDuration：10 秒以上取整秒", () => {
  expect(formatDuration(10_000)).toBe("10s")
  expect(formatDuration(125_000)).toBe("125s")
})

test("formatElapsed：运行刚开始也显示 0s，并拒绝非法时长", () => {
  expect(formatElapsed(0)).toBe("0s")
  expect(formatElapsed(840)).toBe("0s")
  expect(formatElapsed(1_350)).toBe("1.4s")
  expect(formatElapsed(Number.NaN)).toBe("0s")
})

test("formatUsage：缺失返回 undefined，正常返回 in/out 摘要", () => {
  expect(formatUsage(undefined)).toBeUndefined()
  expect(formatUsage({ inputTokens: 500, outputTokens: 200 })).toBe("500 in · 200 out")
})

test("formatUsage：千位用量收敛为 k 单位", () => {
  expect(formatUsage({ inputTokens: 1_200, outputTokens: 35 })).toBe("1.2k in · 35 out")
  expect(formatUsage({ inputTokens: 1_200, outputTokens: 25_000 })).toBe("1.2k in · 25k out")
})

test("formatContext：缺失或零预算返回 undefined", () => {
  expect(formatContext(undefined, undefined)).toBeUndefined()
  expect(formatContext(0, 0)).toBeUndefined()
})

test("formatContext：输出 estimated/cap 预算", () => {
  expect(formatContext(120_000, 256_000)).toBe("120k/256k")
  expect(formatContext(9_500, 100_000)).toBe("9.5k/100k")
})
