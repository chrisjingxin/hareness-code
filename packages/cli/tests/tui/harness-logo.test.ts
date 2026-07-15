import { expect, test } from "bun:test"

import { HARNESS_WORDMARK_DIMENSIONS } from "../../src/tui/harness-logo"

test("Harness Code 字标使用固定栅格宽度，为右下角 powered by 提供稳定锚点", () => {
  expect(HARNESS_WORDMARK_DIMENSIONS.width).toBeGreaterThan(40)
  expect(HARNESS_WORDMARK_DIMENSIONS.height).toBe(10)
})
