/** E2E lifecycle：第二窗口拒绝、刷新重连重同步、CLI 退出收敛。 */

import { expect, test } from "@playwright/test"

import { startCli, type CliHarness } from "./cli-harness"

test("第二窗口被拒：单 renderer 门禁关闭后页面进入只读结束态", async ({ page, browser }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })

    const second = await browser.newPage()
    await second.goto(cli.url)
    // 第二个渲染连接被 already-open 拒绝：页面展示结束态，不进入可交互工作台。
    await expect(second.locator(".web-static-state.closed")).toBeVisible({ timeout: 20_000 })
    await expect(second.locator(".composer-textarea")).not.toBeVisible()
    // 第一个窗口不受影响。
    await expect(page.locator(".composer-textarea")).toBeVisible()
    await second.close()
  } finally {
    await cli.stop()
  }
})

test("刷新重连：宽限内 state.replace 重同步，Timeline 完整", async ({ page }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    const composer = page.locator(".composer-textarea")
    await composer.fill("刷新前的消息内容")
    await page.keyboard.press("Enter")
    await expect(page.locator(".message-content", { hasText: "刷新前的消息内容" }).first()).toBeVisible({ timeout: 20_000 })

    await page.reload()
    // 重连后仍 active，且 Timeline 完整（state.replace 重同步，A-05）。
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    await expect(page.locator(".message-content", { hasText: "刷新前的消息内容" }).first()).toBeVisible({ timeout: 20_000 })
  } finally {
    await cli.stop()
  }
})

test("退出 Harness：浏览器请求后 CLI 进程收敛退出", async ({ page }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    // 顶栏更多菜单 → 退出 Harness。
    await page.click(".overflow-trigger")
    await page.click(".header-menu-item:has-text(\"退出 Harness\")")
    const exitCode = await Promise.race([
      cli.exited,
      new Promise<number | null>(resolveTimer => setTimeout(() => resolveTimer(null), 20_000)),
    ])
    expect(exitCode).not.toBeNull()
  } finally {
    await cli.stop()
  }
})
