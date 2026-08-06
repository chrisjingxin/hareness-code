/** E2E security：错误 Origin 拒绝、归还后旧 token 失效、页面无 Agent 凭据泄漏。 */

import { expect, test } from "@playwright/test"

import { startCli, uiSocketUrl, type CliHarness } from "./cli-harness"

/** 用原生 WebSocket 探测 /ui 升级：返回是否被拒绝（未升级/关闭）。 */
function probeSocket(url: string, origin: string): Promise<"refused" | "connected" | "closed"> {
  return new Promise(resolveTimer => {
    try {
      const socket = new WebSocket(url, { headers: { origin } })
      const settled = (state: "refused" | "connected" | "closed") => {
        try { socket.close() } catch { /* 已关闭 */ }
        resolveTimer(state)
      }
      socket.onopen = () => settled("connected")
      socket.onerror = () => settled("refused")
      socket.onclose = () => settled("closed")
      setTimeout(() => settled("refused"), 5_000)
    } catch {
      resolveTimer("refused")
    }
  })
}

test("错误 Origin 的 /ui 升级被拒绝", async ({ page }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    const state = await probeSocket(uiSocketUrl(cli.url), "http://127.0.0.1:9999")
    expect(state).toBe("refused")
  } finally {
    await cli.stop()
  }
})

test("归还 TUI 后旧 UI token 失效：重连被拒", async ({ page }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    // 合法 Origin 首次可连。
    expect(await probeSocket(uiSocketUrl(cli.url), new URL(cli.url).origin)).not.toBe("refused")

    await page.click(".return-button")
    await expect(page.locator(".web-static-state.closed")).toBeVisible({ timeout: 15_000 })
    // 会话回到 tui-active：旧 token 不再被 consumeUiToken 受理。
    const state = await probeSocket(uiSocketUrl(cli.url), new URL(cli.url).origin)
    expect(state).toBe("refused")
  } finally {
    await cli.stop()
  }
})

test("页面不携带 Agent endpoint / attachment / host.control 信息", async ({ page }) => {
  const cli: CliHarness = await startCli()
  try {
    await page.goto(cli.url)
    await expect(page.locator(".web-shell[data-active=\"true\"]")).toBeVisible({ timeout: 20_000 })
    const content = await page.content()
    expect(content).not.toContain("attachment")
    expect(content).not.toContain("host.control")
    expect(content).not.toContain("agent-endpoint")
  } finally {
    await cli.stop()
  }
})
