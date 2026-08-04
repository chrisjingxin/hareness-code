/** Web bundle 测试：验证源码开发入口可以即时构建浏览器脚本。 */

import { expect, test } from "bun:test"
import { resolve } from "node:path"

import { browserBundle, resolveWebBundleLocations } from "../../src/web/bundle"

test("源码开发模式从 bundle 模块构建 Web bundle", async () => {
  const assets = await browserBundle()
  expect(assets.script.length).toBeGreaterThan(0)
  expect(assets.style.length).toBeGreaterThan(0)
  expect(assets.script).toContain("error_name")
})

test("生产 dist 入口也解析到 cli/dist 资源，不误指向 packages/dist", () => {
  const distDir = resolve(import.meta.dir, "../../dist")
  const locations = resolveWebBundleLocations(distDir)
  expect(locations.builtDirectories).toContain(distDir)
  expect(locations.sourceEntrypoints).toContain(resolve(import.meta.dir, "../../src/web/app.tsx"))
})
