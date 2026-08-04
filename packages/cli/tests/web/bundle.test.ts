/** Web bundle 测试：验证源码开发入口可以即时构建浏览器脚本。 */

import { expect, test } from "bun:test"

import { browserBundle } from "../../src/web/bundle"

test("源码开发模式从 bundle 模块构建 Web bundle", async () => {
  const assets = await browserBundle()
  expect(assets.script.length).toBeGreaterThan(0)
  expect(assets.style.length).toBeGreaterThan(0)
})
