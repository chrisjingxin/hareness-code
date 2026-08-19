/**
 * PresentationCoordinator：单实例表现层接管状态机。
 *
 * 管理 TUI ↔ 内置 Web UI 的输入权切换（tui-active → opening-web → web-active →
 * returning-tui → tui-active）、UI token 签发/校验与单 renderer 门禁。状态机只
 * 决定"谁能提交可变 intent"，不迁移 Agent Host 控制权（Host 侧控制租约始终属于 owner）。
 *
 * Coordinator 不直接写业务状态帧：渲染 channel 由 WebUiGateway 独占写入，本模块
 * 只把已认证的 channel 通过 onRendererConnected 交给网关，并把生命周期事件
 * （ready/return/exit/断开/畸形帧）由网关转发为本模块的方法调用。
 */

import { randomBytes } from "node:crypto"

import type { InteractiveIntent, IntentOutcome } from "../interactive/types"
import type { DiagnosticLogger } from "../diagnostics/local-logger"
import type { WebUiServerMessage } from "./contracts"
import { isHandoffPhase, type PresentationState, type ReturnReason } from "./state"

/** Bun WebSocket 与内存测试 channel 共用的渲染通道 seam。 */
export type GatewayChannel = {
  readonly messages: AsyncIterable<unknown>
  send(message: WebUiServerMessage): Promise<void>
  close(code: number, reason: string): Promise<void>
  /**
   * 活性探测：true 表示连接仍可用。缺省视为可用（保守：不主动替换主连接）。
   * Bun 实现返回底层 ws.readyState === 1，供 attachRenderer 区分"失效主连接
   * 的同 handoff 重连"（可替换接管）与"第二窗口"（必须拒绝）。
   */
  isOpen?(): boolean
}

/** 静态 server adapter 的最小 interface；实现不暴露 Bun 对象。 */
export type PresentationServer = {
  readonly origin: string
  pathFor(handoffId: string): string
  start(): Promise<void>
  stop(): Promise<void>
}

/** 系统 Browser opener；实现由 platform 模块提供。 */
export type WebBrowserOpener = (url: string) => Promise<void>

/** 可注入的本地定时器；测试替换为手动 tick。 */
export type PresentationScheduler = {
  set(callback: () => void, ms: number): { clear(): void }
  now(): number
}

/** 生产默认 scheduler：全局 timer。 */
export function createDefaultPresentationScheduler(): PresentationScheduler {
  return {
    set: (callback, ms) => {
      const handle = setTimeout(callback, ms)
      return { clear: () => clearTimeout(handle) }
    },
    now: () => Date.now(),
  }
}

export type PresentationCoordinatorOptions = {
  server: PresentationServer
  openBrowser: WebBrowserOpener
  /** 共享 InteractiveController.dispatch 的注入点；tuiDispatch 经此执行。 */
  dispatch: (intent: InteractiveIntent) => Promise<IntentOutcome>
  /** 已认证渲染 channel 的交接点；由 CLI Composition Root 接 WebUiGateway.connectRenderer。 */
  onRendererConnected: (channel: GatewayChannel, reconnectToken: string) => void
  /** Browser 从打开到 ready 的最大等待；超时进入 returning-tui。 */
  readyTimeoutMs?: number
  /** Web-active 断线后等待同页重连的宽限；到期才收敛到 TUI。 */
  reconnectGraceMs?: number
  /** 首次 URL 中 bootstrap token 的有效期；renderer 接受后改用 handoff-scoped 轮换 token。 */
  uiTokenTtlMs?: number
  scheduler?: PresentationScheduler
  diagnostics?: DiagnosticLogger
}

export interface PresentationCoordinator {
  /** 创建并启动一次 Web 接管；Promise 在页面启动成功后完成，不等 Browser ready。 */
  open(): Promise<void>
  getSnapshot(): PresentationState
  subscribe(listener: (state: PresentationState) => void): () => void
  /** TUI 输入租约：仅 tui-active 受理，其余阶段拒绝（防御 WebTakeoverView 之外的残余输入）。 */
  tuiDispatch(intent: InteractiveIntent): Promise<IntentOutcome>
  /** 当前 handoff 是否仍对外提供页面/升级路径。 */
  isHandoffActive(handoffId: string): boolean
  /** WebSocket upgrade 前只读校验 token；真正消费与轮换在 renderer 被接受时完成。 */
  validateUiToken(handoffId: string, token: string, origin: string): boolean
  /** 接受 renderer 后原子消费 presentedToken、轮换下一枚单次重连 token并交给网关。 */
  attachRenderer(handoffId: string, presentedToken: string, channel: GatewayChannel): Promise<void>
  /** 渲染 channel 结束（浏览器断开/关闭）；opening 直接收敛，web-active 先进入重连宽限。 */
  notifyRendererDisconnected(reason?: ReturnReason): void
  /** 渲染帧畸形/未知：协议违规，fail-closed 收敛。 */
  notifyInvalidMessage(): void
  /** Browser ready：opening-web → web-active 的唯一入口。 */
  requestReady(): void
  /** Browser 主动归还输入权。 */
  requestReturn(): void
  /** Browser 请求退出 Harness；收敛后触发已注册的 CLI exit handler。 */
  requestExit(): void
  /** 注册 CLI 退出 handler；返回的解绑函数在 unmount/close 时调用。 */
  registerExitHandler(handler: () => void): () => void
  /** CLI 终态关闭；可重复调用，不因任何等待而挂起。 */
  close(): Promise<void>
}

const TOKEN_LENGTH_BYTES = 32

class PresentationCoordinatorImpl implements PresentationCoordinator {
  private readonly server: PresentationServer
  private readonly openBrowser: WebBrowserOpener
  private readonly dispatchIntent: (intent: InteractiveIntent) => Promise<IntentOutcome>
  private readonly onRendererConnected: (channel: GatewayChannel, reconnectToken: string) => void
  private readonly readyTimeoutMs: number
  private readonly reconnectGraceMs: number
  private readonly uiTokenTtlMs: number
  private readonly scheduler: PresentationScheduler
  private readonly diagnostics: DiagnosticLogger | undefined
  private readonly listeners = new Set<(state: PresentationState) => void>()

  private state: PresentationState = { phase: "tui-active" }
  private handoffId: string | null = null
  private uiToken: string | undefined
  private uiTokenExpiresAt = 0
  private bootstrapTokenPending = false
  private primary: GatewayChannel | undefined
  private readyTimer: { clear(): void } | undefined
  private reconnectTimer: { clear(): void } | undefined
  private cleanupPromise: Promise<void> | undefined
  private closed = false
  private exitHandler: (() => void) | undefined
  private exitHandlerPending = false

  constructor(options: PresentationCoordinatorOptions) {
    this.server = options.server
    this.openBrowser = options.openBrowser
    this.dispatchIntent = options.dispatch
    this.onRendererConnected = options.onRendererConnected
    this.readyTimeoutMs = options.readyTimeoutMs ?? 65_000
    this.reconnectGraceMs = options.reconnectGraceMs ?? 10_000
    this.uiTokenTtlMs = options.uiTokenTtlMs ?? 60_000
    this.scheduler = options.scheduler ?? createDefaultPresentationScheduler()
    this.diagnostics = options.diagnostics
  }

  getSnapshot(): PresentationState {
    return this.state
  }

  subscribe(listener: (state: PresentationState) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** 创建并启动一次 Web 接管；URL 只携带 UI token，不含任何 Agent 凭据。 */
  async open(): Promise<void> {
    if (this.closed) throw new Error("Presentation coordinator is closed")
    if (this.state.phase !== "tui-active") throw new Error("Web session is already active")
    this.handoffId = randomBytes(TOKEN_LENGTH_BYTES).toString("base64url")
    this.uiToken = randomBytes(TOKEN_LENGTH_BYTES).toString("base64url")
    this.uiTokenExpiresAt = 0
    this.bootstrapTokenPending = true
    this.state = { phase: "opening-web", handoffId: this.handoffId }
    this.exitHandlerPending = false
    this.diagnostics?.info("web.handoff.opening")
    this.publish()
    try {
      await this.server.start()
      if (this.closed || this.state.phase !== "opening-web") return
      // TTL 从生成 URL 前一刻起算：server.start()/getAssets 的耗时不占用 token 有效期。
      this.uiTokenExpiresAt = this.scheduler.now() + this.uiTokenTtlMs
      const url = `${this.server.origin}${this.server.pathFor(this.handoffId)}#ui=${this.uiToken}`
      // 先 arm ready timer 再启动浏览器：若 openBrowser 期间已进入 active，这里不再补 arm。
      this.readyTimer = this.scheduler.set(
        () => void this.cleanup("ready-timeout"),
        this.readyTimeoutMs,
      )
      await this.openBrowser(url)
      if (this.state.phase !== "opening-web") {
        this.readyTimer?.clear()
        this.readyTimer = undefined
      }
    } catch (error) {
      await this.cleanup("opener-failed", error)
      throw error
    }
  }

  /** TUI 输入租约：只有 tui-active 阶段允许提交可变 intent。 */
  async tuiDispatch(intent: InteractiveIntent): Promise<IntentOutcome> {
    if (this.closed) return { status: "rejected", code: "connection-closed", message: "Coordinator is closed" }
    if (this.state.phase !== "tui-active") {
      return { status: "rejected", code: "busy", message: "Web session has taken over; input resumes after returning to TUI" }
    }
    return this.dispatchIntent(intent)
  }

  isHandoffActive(handoffId: string): boolean {
    return isHandoffPhase(this.state) && this.state.handoffId === handoffId
  }

  /**
   * 只读校验升级请求。首次 URL 的 bootstrap token 受 TTL 约束；renderer 被接受后
   * 换发 handoff-scoped 单次重连 token，其寿命由 handoff 决定、每次成功连接立即轮换。
   */
  validateUiToken(handoffId: string, token: string, origin: string): boolean {
    if (this.closed) return false
    if (this.handoffId !== handoffId) return false
    if (this.state.phase !== "opening-web" && this.state.phase !== "web-active") return false
    if (this.uiToken === undefined || token !== this.uiToken) return false
    if (this.bootstrapTokenPending && this.scheduler.now() > this.uiTokenExpiresAt) return false
    if (origin !== this.server.origin) return false
    return true
  }

  /** 处理一个已完成 upgrade 的渲染 channel；生命周期或凭据判定失败时直接 shutdown。 */
  async attachRenderer(handoffId: string, presentedToken: string, channel: GatewayChannel): Promise<void> {
    if (this.closed || this.handoffId !== handoffId || this.state.phase === "tui-active") {
      await channel.send({ type: "handoff.state", state: this.state })
      await channel.close(1008, "invalid-handoff")
      return
    }
    if (this.state.phase === "returning-tui") {
      await channel.send({ type: "handoff.state", state: this.state })
      await channel.close(1008, "returning")
      return
    }
    if (this.primary !== undefined) {
      // 主连接已失效（如整页重载重连时旧 socket 关闭事件还没驱动到 consume 循环
      // 清理、primary 尚未释放）时允许同 handoff 的新连接替换接管；仍活跃的
      // 主连接才视为第二窗口拒绝，保持单窗口 invariant。
      if (this.primary.isOpen?.() !== false) {
        await channel.send({ type: "handoff.state", state: this.state })
        await channel.close(1008, "already-open")
        return
      }
      const stale = this.primary
      this.primary = undefined
      void stale.close(1001, "superseded")
    }
    // upgrade 校验与 open 回调之间可能发生并发连接；接受前必须再次校验并在同一
    // 事件循环 turn 内轮换，确保旧 token 只能成功建立一个 renderer。
    if (!this.validateUiToken(handoffId, presentedToken, this.server.origin)) {
      await channel.send({ type: "handoff.state", state: this.state })
      await channel.close(1008, "invalid-token")
      return
    }
    const reconnectToken = randomBytes(TOKEN_LENGTH_BYTES).toString("base64url")
    this.uiToken = reconnectToken
    this.uiTokenExpiresAt = 0
    this.bootstrapTokenPending = false
    this.primary = channel
    this.reconnectTimer?.clear()
    this.reconnectTimer = undefined
    this.diagnostics?.info("web.renderer.accepted")
    this.onRendererConnected(channel, reconnectToken)
  }

  /** 渲染 channel 结束：opening 阶段直接收敛；web-active 先给同页重连宽限。 */
  notifyRendererDisconnected(reason: ReturnReason = "browser-close"): void {
    if (this.closed || this.state.phase !== "opening-web" && this.state.phase !== "web-active") return
    if (this.state.phase === "opening-web") {
      void this.cleanup("browser-close")
      return
    }
    if (this.reconnectTimer !== undefined) return
    // 先释放 primary，宽限期内同页重连才能通过单 renderer 门禁重新 attach。
    this.primary = undefined
    this.reconnectTimer = this.scheduler.set(() => {
      this.reconnectTimer = undefined
      if (this.closed || this.state.phase !== "web-active") return
      void this.cleanup(reason)
    }, this.reconnectGraceMs)
  }

  /** 畸形/未知渲染帧：协议违规，fail-closed 收敛整个会话。 */
  notifyInvalidMessage(): void {
    if (this.closed || this.state.phase !== "opening-web" && this.state.phase !== "web-active") return
    void this.cleanup("invalid-message")
  }

  /** Browser ready：opening-web → web-active；其他阶段视为协议违规。 */
  requestReady(): void {
    if (this.closed || this.state.phase !== "opening-web") {
      this.notifyInvalidMessage()
      return
    }
    this.readyTimer?.clear()
    this.readyTimer = undefined
    this.state = { phase: "web-active", handoffId: this.state.handoffId }
    this.diagnostics?.info("web.handoff.active")
    this.publish()
  }

  /** Browser 主动归还输入权。 */
  requestReturn(): void {
    if (this.closed || this.state.phase !== "web-active") {
      this.notifyInvalidMessage()
      return
    }
    void this.cleanup("returned")
  }

  /** Browser 请求退出 Harness：收敛后触发 CLI exit handler。 */
  requestExit(): void {
    if (this.closed || this.state.phase !== "web-active") {
      this.notifyInvalidMessage()
      return
    }
    this.exitHandlerPending = true
    void this.cleanup("exit-requested")
  }

  registerExitHandler(handler: () => void): () => void {
    this.exitHandler = handler
    return () => {
      if (this.exitHandler === handler) this.exitHandler = undefined
    }
  }

  /** CLI 终态关闭；可重复调用。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.exitHandlerPending = false
    this.reconnectTimer?.clear()
    this.reconnectTimer = undefined
    if (this.state.phase !== "tui-active") {
      await this.cleanup("cli-exit")
    }
    await this.server.stop()
  }

  /** 统一收敛：进入 returning-tui 后立即回到 tui-active（无 Host 依赖，无需等待）。 */
  private cleanup(reason: ReturnReason, error?: unknown): Promise<void> {
    if (this.cleanupPromise !== undefined) return this.cleanupPromise
    // 先捕获 promise 再赋值：performCleanup 的异步体同步执行到返回，期间会清空
    // cleanupPromise；若先执行右侧赋值，会把已收敛的 promise 写回字段造成楔住。
    const promise = this.performCleanup(reason, error)
    this.cleanupPromise = promise
    void promise.finally(() => {
      if (this.cleanupPromise === promise) this.cleanupPromise = undefined
    })
    return promise
  }

  private async performCleanup(reason: ReturnReason, error?: unknown): Promise<void> {
    this.diagnostics?.info("web.handoff.returning", { reason })
    this.readyTimer?.clear()
    this.readyTimer = undefined
    this.reconnectTimer?.clear()
    this.reconnectTimer = undefined
    if (isHandoffPhase(this.state)) {
      this.state = { phase: "returning-tui", handoffId: this.state.handoffId, reason }
      this.publish()
    }
    if (this.state.phase === "returning-tui") {
      // close() 先置 closed 再走 cleanup，此处不检查 closed：close 期间回退到
      // tui-active 正是期望终态（publish 无副作用，exit handler 已被 close 清空）。
      this.handoffId = null
      this.uiToken = undefined
      this.uiTokenExpiresAt = 0
      this.bootstrapTokenPending = false
      this.primary = undefined
      this.state = { phase: "tui-active" }
      this.publish()
      if (this.exitHandlerPending) {
        this.exitHandlerPending = false
        const handler = this.exitHandler
        if (handler !== undefined) {
          try {
            handler()
          } catch {
            // handler 异常不阻止收敛；exit 由调用方实现自己处理。
          }
        }
      }
    }
    void error
  }

  private publish(): void {
    for (const listener of [...this.listeners]) listener(this.state)
  }
}

/** 创建 PresentationCoordinator 的唯一工厂；状态与测试入口都收敛在这里。 */
export function createPresentationCoordinator(
  options: PresentationCoordinatorOptions,
): PresentationCoordinator {
  return new PresentationCoordinatorImpl(options)
}
