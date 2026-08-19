/**
 * 接管单帧丢失回归：页面停留在 opening-web 的看门狗重载恢复。
 *
 * 背景：页面进入 web-active 依赖服务端 handoff.state(web-active) 单帧交付；该帧
 * 一旦被浏览器冻结/休眠/扩展干扰丢弃，页面会永久停在"接管尚未完成"（服务端已
 * active，连接仍开着）。本测试在服务端吞掉第一次 web-active 发布模拟该丢失，
 * 断言页面在看门狗宽限后整页重载、重连同一 handoff 并最终恢复可写。
 */

import { expect, test } from "bun:test"
import { chromium } from "@playwright/test"

import { makeHarness } from "../interactive/harness"
import { createWebServer } from "../../src/web/server"
import { webHtml } from "../../src/web/html"
import { browserBundle } from "../../src/web/bundle"
import { createPresentationCoordinator, type PresentationCoordinator } from "../../src/presentation-coordinator/coordinator"
import { createWebUiGateway, type WebUiGateway } from "../../src/presentation-coordinator/web-ui-gateway"
import type { WorkspaceExplorer } from "../../src/workspace/types"
import type { PresentationState } from "../../src/presentation-coordinator"

/** 空 explorer fake：网关构造必需，本测试不关心工作区。 */
function createFakeExplorer(): WorkspaceExplorer {
  return {
    getSnapshot: () => ({ tree: { status: "idle", rows: [], selectedPath: null, limited: false }, preview: { status: "idle" } }),
    subscribe: () => () => {},
    dispatch: async () => ({ status: "accepted" }),
    close: async () => {},
  }
}

test(
  "web-active 帧丢失后页面看门狗重载并恢复 active",
  async () => {
    const { controller } = makeHarness({ initialThreadId: "thread-1" })
    let gateway!: WebUiGateway
    let openedUrl = ""
    const dropped = { count: 0 }

    const coordinator = createPresentationCoordinator({
      server: createWebServer({
        html: webHtml,
        getAssets: browserBundle,
        isActiveHandoff: handoffId => coordinator.isHandoffActive(handoffId),
        validateUiToken: (handoffId, token, origin) => coordinator.validateUiToken(handoffId, token, origin),
        attachRenderer: (handoffId, token, channel) => coordinator.attachRenderer(handoffId, token, channel),
      }),
      openBrowser: async url => { openedUrl = url },
      dispatch: intent => controller.dispatch(intent),
      onRendererConnected: (channel, reconnectToken) => gateway.connectRenderer(channel, reconnectToken),
    })

    // 包装 subscribe：吞掉第一次 web-active 发布（在网关构造前生效），
    // 模拟该帧在浏览器侧丢失；之后的状态照常转发。
    const originalSubscribe = coordinator.subscribe.bind(coordinator)
    const wrapped: PresentationCoordinator = coordinator
    wrapped.subscribe = (listener: (state: PresentationState) => void) =>
      originalSubscribe(state => {
        if (state.phase === "web-active" && dropped.count === 0) {
          dropped.count += 1
          return
        }
        listener(state)
      })

    gateway = createWebUiGateway({ coordinator: wrapped, controller, workspaceExplorer: createFakeExplorer() })
    await coordinator.open()

    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage()
    try {
      await page.goto(openedUrl, { waitUntil: "load", timeout: 15_000 })
      await page.waitForSelector(".timeline", { timeout: 15_000 })

      // 首帧已渲染但 web-active 被吞：页面停在接管状态（复现用户症状）。
      await page.waitForSelector('.web-shell[data-active="false"]', { timeout: 5_000 })

      // 看门狗（5s 宽限）整页重载 → 重连 → 网关首帧直接下发 web-active → 恢复可写。
      await page.waitForSelector('.web-shell[data-active="true"]', { timeout: 20_000 })
      expect(dropped.count).toBe(1)
    } finally {
      await browser.close()
      await coordinator.close()
    }
  },
  { timeout: 40_000 },
)

test(
  "Web 长时间保持 active 后刷新仍轮换凭据并恢复",
  async () => {
    const { controller } = makeHarness({ initialThreadId: "thread-1" })
    let gateway!: WebUiGateway
    let openedUrl = ""
    const coordinator = createPresentationCoordinator({
      server: createWebServer({
        html: webHtml,
        getAssets: browserBundle,
        isActiveHandoff: handoffId => coordinator.isHandoffActive(handoffId),
        validateUiToken: (handoffId, token, origin) => coordinator.validateUiToken(handoffId, token, origin),
        attachRenderer: (handoffId, token, channel) => coordinator.attachRenderer(handoffId, token, channel),
      }),
      openBrowser: async url => { openedUrl = url },
      dispatch: intent => controller.dispatch(intent),
      onRendererConnected: (channel, reconnectToken) => gateway.connectRenderer(channel, reconnectToken),
      uiTokenTtlMs: 2_000,
      reconnectGraceMs: 300,
    })
    gateway = createWebUiGateway({ coordinator, controller, workspaceExplorer: createFakeExplorer() })
    await coordinator.open()

    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage()
    try {
      await page.goto(openedUrl, { waitUntil: "load", timeout: 15_000 })
      await page.waitForSelector('.web-shell[data-active="true"]', { timeout: 15_000 })

      // bootstrap token 已过期，但首个 renderer 应已获得 handoff-scoped 单次重连 token。
      await new Promise(resolve => setTimeout(resolve, 2_100))
      await page.reload({ waitUntil: "load", timeout: 15_000 })
      await page.waitForSelector('.web-shell[data-active="true"]', { timeout: 15_000 })
      expect(coordinator.getSnapshot().phase).toBe("web-active")
    } finally {
      await browser.close()
      await coordinator.close()
    }
  },
  { timeout: 40_000 },
)
