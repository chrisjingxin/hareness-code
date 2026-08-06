/** E2E visual/a11y：双视口 × 双主题基线截图、无横向溢出、首帧 light。 */

import { expect, test, type Page } from "@playwright/test"
import { resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { startCli, type CliHarness } from "./cli-harness"

const screenshotDir = resolve(fileURLToPath(new URL(".", import.meta.url)), "screenshots")

async function assertNoHorizontalOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
}

test("1440×900：首帧 light，菜单切换 dark，四组截图之一", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    // 首帧固定 light。
    await expect(page.locator(".web-shell")).toHaveAttribute("data-theme", "light")
    await assertNoHorizontalOverflow(page)
    await page.screenshot({ path: resolve(screenshotDir, "desktop-light.png"), fullPage: false })

    // 顶栏更多菜单 → 使用深色主题。
    await page.click(".overflow-trigger")
    await page.click(".header-menu-item:has-text(\"使用深色主题\")")
    await expect(page.locator(".web-shell")).toHaveAttribute("data-theme", "dark")
    await assertNoHorizontalOverflow(page)
    await page.screenshot({ path: resolve(screenshotDir, "desktop-dark.png"), fullPage: false })
  } finally {
    await cli.stop()
  }
})

test("390×844：移动端 drawer 布局，light/dark 无横向溢出", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    await expect(page.locator(".web-shell")).toHaveAttribute("data-theme", "light")
    await assertNoHorizontalOverflow(page)
    await page.screenshot({ path: resolve(screenshotDir, "mobile-light.png"), fullPage: false })

    await page.click(".overflow-trigger")
    await page.click(".header-menu-item:has-text(\"使用深色主题\")")
    await expect(page.locator(".web-shell")).toHaveAttribute("data-theme", "dark")
    await assertNoHorizontalOverflow(page)
    await page.screenshot({ path: resolve(screenshotDir, "mobile-dark.png"), fullPage: false })
  } finally {
    await cli.stop()
  }
})
