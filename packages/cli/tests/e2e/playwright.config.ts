/** Browser E2E 配置：单 worker 串行（每个用例独立拉起 CLI），headless。 */

import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    headless: true,
  },
  outputDir: "test-results",
})
