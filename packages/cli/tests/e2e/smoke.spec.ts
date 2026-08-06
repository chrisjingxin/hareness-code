/** E2E smoke/parity：空首页首条消息、代码高亮、失败保留输入、Thread/catalog 连续性。 */

import { expect, test } from "@playwright/test"

import { startCli, type CliHarness } from "./cli-harness"

test("空首页首条消息：发送、回显、代码高亮、草稿清空、Thread 侧栏出现", async ({ page }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    // ready 门自动上报后进入 web-active，composer 可用。
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    const composer = page.locator(".composer-textarea")
    await expect(composer).toBeVisible()

    // 发送带代码块的文本：echo Agent 原样回显，Timeline 渲染用户消息与助手回复。
    await composer.fill("用 Python 输出三行代码：\n```python\nprint(\"hello\")\n```")
    await page.keyboard.press("Enter")
    await expect(page.locator(".message-content", { hasText: "print" }).first()).toBeVisible({ timeout: 20_000 })
    // 草稿已清空。
    await expect(composer).toHaveValue("")
    // 代码块经 Shiki 高亮：存在 code-block 且带语言标记。
    await expect(page.locator(".code-block-lang", { hasText: "python" }).first()).toBeVisible({ timeout: 20_000 })
    // Thread 侧栏出现新 thread。
    await expect(page.locator(".thread-item").first()).toBeVisible({ timeout: 10_000 })

    // 往返状态连续性（A-05 核心）：同一 Controller 服务第二次 /web，Timeline 仍在。
    await page.click(".return-button")
    await expect(page.locator(".web-static-state.closed")).toBeVisible({ timeout: 15_000 })
    // TUI 恢复输入后重新执行 /web。
    await page.waitForTimeout(1_500)
    cli.writeInput("/web")
    const secondUrl = await cli.waitForNewUrl(cli.url)
    await page.goto(secondUrl)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    // 之前的消息仍在新页面 Timeline 中 —— 共享 Core 未重建。
    await expect(page.locator(".message-content", { hasText: "print" }).first()).toBeVisible({ timeout: 20_000 })
  } finally {
    await cli.stop()
  }
})

test("命令菜单与命令执行：/help 显示帮助面板文案", async ({ page }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    const composer = page.locator(".composer-textarea")
    await composer.fill("/help")
    await expect(page.locator(".command-menu")).toBeVisible({ timeout: 10_000 })
    await page.keyboard.press("Enter")
    // /help 是本地 notice，Timeline 出现系统消息。
    await expect(page.locator(".message-system").first()).toBeVisible({ timeout: 10_000 })
  } finally {
    await cli.stop()
  }
})
