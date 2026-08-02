/** Web launcher 测试：验证源码开发入口可以即时构建浏览器脚本。 */

import { expect, test } from "bun:test"

import { browserBundle } from "../../src/web/launcher"

test("源码开发模式从 launcher 同目录构建 Web bundle", async () => {
  const script = await browserBundle()

  expect(script.length).toBeGreaterThan(0)
})
