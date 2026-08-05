/** Web Handoff 深模块：单实例接管状态机、lifecycle 校验与 owner 恢复收敛。 */

import { randomBytes } from "node:crypto"

import type {
  ControlStatus,
  HostAttachmentCreateResult,
  HostAttachmentRevokeResult,
} from "@za38/protocol"
import type { DiagnosticLogger } from "../diagnostics/local-logger"

export type WebBootstrapStage =
  | "lifecycle.accepted"
  | "attachment.auth"
  | "agent.initialize"
  | "host.control.acquire"
  | "thread.restore"
  | "react.mount"

/** Browser 归还控制权的可观察原因；只用于状态展示，不参与业务分支。 */
export type WebReturnReason =
  | "released"
  | "browser-close"
  | "ready-timeout"
  | "invalid-message"
  | "opener-failed"
  | "host-control-failed"
  | "cli-exit"
  | "exit-requested"

/** lifecycle shutdown 的稳定原因；Browser 只按 type 处理。 */
export type LifecycleShutdownReason =
  | "already-open"
  | "invalid-handoff"
  | "invalid-message"
  | "ready-timeout"
  | "returning"
  | "host-control-failed"
  | "cli-exit"

export type LifecycleServerMessage =
  | { type: "accepted" }
  | { type: "active" }
  | { type: "shutdown"; reason: LifecycleShutdownReason }

export type LifecycleBrowserMessage =
  | { type: "ready"; thread_id: string | null }
  | { type: "thread.changed"; thread_id: string | null }
  | { type: "diagnostic"; stage: WebBootstrapStage; error_name: string; error_message: string }
  | { type: "released" }
  | { type: "exit.requested" }

/** Bun WebSocket 与内存测试 adapter 共用的 lifecycle channel seam。 */
export type LifecycleChannel = {
  readonly messages: AsyncIterable<unknown>
  send(message: LifecycleServerMessage): Promise<void>
  close(code: number, reason: string): Promise<void>
}

/** 不包含凭据的只读接管状态；TUI 根层只消费这个快照。 */
export type WebHandoffSnapshot =
  | {
      phase: "idle"
      tuiLocked: false
      threadId: string | null
      handoffVersion: number
      error?: string
    }
  | {
      phase: "opening"
      tuiLocked: false
      handoffId: string
      threadId: string | null
      handoffVersion: number
    }
  | {
      phase: "active"
      tuiLocked: true
      handoffId: string
      threadId: string | null
      handoffVersion: number
    }
  | {
      phase: "returning"
      tuiLocked: boolean
      handoffId: string
      threadId: string | null
      reason: WebReturnReason
      handoffVersion: number
      error?: string
    }

/** Coordinator 对 owner AgentClient 的最小 Host 控制面。 */
export type WebHostControl = {
  createAttachment(origin: string): Promise<HostAttachmentCreateResult>
  revokeAttachment(attachmentId: string): Promise<HostAttachmentRevokeResult>
  controlStatus(): Promise<ControlStatus>
}

/** 静态 server adapter 的最小 interface；实现不暴露 Bun 对象。 */
export type WebHandoffServer = {
  readonly origin: string
  pathFor(handoffId: string): string
  start(): Promise<void>
  stop(): Promise<void>
}

export type WebBrowserOpener = (url: string) => Promise<void>

export type WebTimer = { clear(): void }

export type WebScheduler = {
  set(callback: () => void, ms: number): WebTimer
  now(): number
  sleep?(ms: number): Promise<void>
}

/** 可注入 scheduler 的默认实现；生产使用全局 timer，测试可替换为手动 tick。 */
export function createDefaultScheduler(): WebScheduler {
  return {
    set: (callback, ms) => {
      const handle = setTimeout(callback, ms)
      return { clear: () => clearTimeout(handle) }
    },
    now: () => Date.now(),
    sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
  }
}

export type WebHandoffCoordinatorOptions = {
  host: WebHostControl
  server: WebHandoffServer
  openBrowser: WebBrowserOpener
  readyTimeoutMs?: number
  ownerPollMs?: number
  ownerWaitMs?: number
  scheduler?: WebScheduler
  diagnostics?: DiagnosticLogger
}

export interface WebHandoffCoordinator {
  open(initialThreadId: string | null): Promise<void>
  getSnapshot(): WebHandoffSnapshot
  subscribe(listener: (snapshot: WebHandoffSnapshot) => void): () => void
  attachLifecycle(handoffId: string, channel: LifecycleChannel): Promise<void>
  registerExitHandler(handler: () => void): () => void
  close(): Promise<void>
}

const MAX_LIFECYCLE_FRAME_BYTES = 16 * 1024
const textEncoder = new TextEncoder()

class WebHandoffCoordinatorImpl implements WebHandoffCoordinator {
  private readonly host: WebHostControl
  private readonly server: WebHandoffServer
  private readonly openBrowser: WebBrowserOpener
  private readonly readyTimeoutMs: number
  private readonly ownerPollMs: number
  private readonly ownerWaitMs: number
  private readonly scheduler: WebScheduler
  private readonly diagnostics: DiagnosticLogger | undefined
  private readonly listeners = new Set<(snapshot: WebHandoffSnapshot) => void>()

  private phase: "idle" | "opening" | "active" | "returning" = "idle"
  private handoffVersion = 0
  private handoffId: string | null = null
  private threadId: string | null = null
  private tuiLocked = false
  private returnReason: WebReturnReason | undefined
  private error: string | undefined
  private attachmentId: string | undefined
  private primary: LifecycleChannel | undefined
  private readyTimer: WebTimer | undefined
  private cleanupPromise: Promise<void> | undefined
  private recoveryTimer: WebTimer | undefined
  private closed = false
  private exitHandler: (() => void) | undefined
  /** 当前 handoff 是否还有未触发的 exit handler；open 时清零、handler 触发后清零。 */
  private exitHandlerPending = false
  private snapshot: WebHandoffSnapshot
  private readonly sleep: (ms: number) => Promise<void>

  constructor(options: WebHandoffCoordinatorOptions) {
    this.host = options.host
    this.server = options.server
    this.openBrowser = options.openBrowser
    this.readyTimeoutMs = options.readyTimeoutMs ?? 65_000
    this.ownerPollMs = options.ownerPollMs ?? 100
    this.ownerWaitMs = options.ownerWaitMs ?? 30_000
    this.scheduler = options.scheduler ?? createDefaultScheduler()
    this.diagnostics = options.diagnostics
    this.sleep = options.scheduler?.sleep
      ?? (ms => new Promise(resolve => setTimeout(resolve, ms)))
    this.snapshot = this.buildSnapshot()
  }

  /** 创建并启动一次 handoff；Promise 在页面启动成功后完成，不等 Browser ready。 */
  async open(initialThreadId: string | null): Promise<void> {
    if (this.closed) throw new Error("Web handoff coordinator is closed")
    if (this.phase !== "idle") throw new Error("Web handoff is already active")
    this.handoffId = randomBytes(32).toString("base64url")
    this.threadId = initialThreadId
    this.phase = "opening"
    this.tuiLocked = false
    this.returnReason = undefined
    this.error = undefined
    this.primary = undefined
    this.attachmentId = undefined
    // 新 handoff 重置 exit handler 触发门：上一轮 exit-requested 已停止 pending，
    // 重新进入 opening 时未注册的 handler 不能被新 handoff 自动消费。
    this.exitHandlerPending = false
    this.diagnostics?.info("web.handoff.opening")
    this.publish()
    try {
      await this.server.start()
      if (this.closed || this.phase !== "opening") return
      const attachment = await this.host.createAttachment(this.server.origin)
      if (this.closed || this.phase !== "opening") {
        // close() 的 cleanup 发生在 attachment 创建之前，这里需要手动撤销。
        try {
          await this.host.revokeAttachment(attachment.attachment_id)
        } catch {
          // 撤销失败不阻止 open 中止；后续 status 查询仍可收敛。
        }
        return
      }
      this.attachmentId = attachment.attachment_id
      const fragment = new URLSearchParams({
        endpoint: attachment.endpoint,
        token: attachment.token,
        attachment: attachment.attachment_id,
      })
      if (this.threadId !== null) fragment.set("thread", this.threadId)
      const url = `${this.server.origin}${this.server.pathFor(this.handoffId)}#${fragment}`
      // 先 arm ready timer 再启动浏览器：若 openBrowser 期间 handoff 已
      // 进入 active（confirmReady 已清 timer），这里不能再 arm 新的 timer。
      this.readyTimer = this.scheduler.set(
        () => void this.cleanup("ready-timeout"),
        this.readyTimeoutMs,
      )
      await this.openBrowser(url)
      if (this.phase !== "opening") {
        this.readyTimer?.clear()
        this.readyTimer = undefined
      }
    } catch (error) {
      await this.cleanup("opener-failed", error)
      throw error
    }
  }

  getSnapshot(): WebHandoffSnapshot {
    return this.snapshot
  }

  private buildSnapshot(): WebHandoffSnapshot {
    switch (this.phase) {
      case "opening":
        return {
          phase: "opening",
          tuiLocked: false,
          handoffId: this.handoffId!,
          threadId: this.threadId,
          handoffVersion: this.handoffVersion,
        }
      case "active":
        return {
          phase: "active",
          tuiLocked: true,
          handoffId: this.handoffId!,
          threadId: this.threadId,
          handoffVersion: this.handoffVersion,
        }
      case "returning":
        return {
          phase: "returning",
          tuiLocked: this.tuiLocked,
          handoffId: this.handoffId!,
          threadId: this.threadId,
          reason: this.returnReason ?? "browser-close",
          handoffVersion: this.handoffVersion,
          ...(this.error !== undefined ? { error: this.error } : {}),
        }
      default:
        return {
          phase: "idle",
          tuiLocked: false,
          threadId: this.threadId,
          handoffVersion: this.handoffVersion,
          ...(this.error !== undefined ? { error: this.error } : {}),
        }
    }
  }

  subscribe(listener: (snapshot: WebHandoffSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** 注册 CLI 退出 handler；返回的函数在 unmount/close 时调用以解绑。 */
  registerExitHandler(handler: () => void): () => void {
    this.exitHandler = handler
    return () => {
      if (this.exitHandler === handler) this.exitHandler = undefined
    }
  }

  /** 消费一个已经完成 upgrade 的 lifecycle channel，并完成首连接竞争。 */
  async attachLifecycle(handoffId: string, channel: LifecycleChannel): Promise<void> {
    if (this.closed || this.handoffId !== handoffId || this.phase === "idle") {
      await channel.send({ type: "shutdown", reason: "invalid-handoff" })
      await channel.close(1008, "invalid-handoff")
      return
    }
    if (this.phase === "returning") {
      await channel.send({ type: "shutdown", reason: "returning" })
      await channel.close(1008, "returning")
      return
    }
    if (this.primary !== undefined) {
      await channel.send({ type: "shutdown", reason: "already-open" })
      await channel.close(1008, "already-open")
      return
    }
    try {
      await channel.send({ type: "accepted" })
    } catch {
      // send 失败说明 socket 已失效；收敛本 handoff，避免 primary 残留。
      await this.cleanup("browser-close")
      return
    }
    this.primary = channel
    this.diagnostics?.info("web.lifecycle.accepted")
    void this.consumeLifecycle(channel)
  }

  /** CLI 终态关闭；可重复调用，不因 owner 恢复超时而无限挂起。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.recoveryTimer?.clear()
    this.recoveryTimer = undefined
    // close 由 CLI 自身触发，不需要再回调 exit handler；清理悬挂标记。
    this.exitHandlerPending = false
    if (this.phase !== "idle") {
      await this.cleanup("cli-exit")
    }
    await this.server.stop()
  }

  /** 消费 primary 的 lifecycle 消息；任何结束路径都进入统一 cleanup。 */
  private async consumeLifecycle(channel: LifecycleChannel): Promise<void> {
    try {
      for await (const raw of channel.messages) {
        const message = parseLifecycleMessage(raw)
        if (message === undefined) {
          await this.cleanup("invalid-message")
          return
        }
        if (message.type === "diagnostic") {
          if (this.phase !== "opening") {
            await this.cleanup("invalid-message")
            return
          }
          this.diagnostics?.error(
            "web.bootstrap.failed",
            { stage: message.stage, error_name: message.error_name },
            { error_message: message.error_message },
          )
          continue
        }
        if (message.type === "ready") {
          // opening 只接受一次 ready；ready 携带的 thread_id 是 Browser
          // 恢复/创建后的最终值，覆盖 open() 时的初值。
          if (this.phase !== "opening") {
            await this.cleanup("invalid-message")
            return
          }
          this.threadId = message.thread_id
          this.publish()
          await this.confirmReady()
          continue
        }
        if (message.type === "thread.changed") {
          if (this.phase === "active") {
            this.threadId = message.thread_id
            this.publish()
          } else if (this.phase === "opening") {
            await this.cleanup("invalid-message")
            return
          }
          continue
        }
        if (message.type === "released") {
          if (this.phase === "active") {
            await this.cleanup("released")
            return
          }
          if (this.phase === "opening") {
            await this.cleanup("invalid-message")
            return
          }
        }
        if (message.type === "exit.requested") {
          if (this.phase === "active") {
            this.exitHandlerPending = true
            await this.cleanup("exit-requested")
            return
          }
          // opening 阶段还没有 host holder 也没锁定 TUI，exit 视为
          // 非法流程；fail-closed 进入 invalid-message 收敛。
          if (this.phase === "opening") {
            await this.cleanup("invalid-message")
            return
          }
        }
      }
    } catch {
      // channel fail/close 与 for await 结束走同一个收敛路径。
    }
    if (
      this.primary === channel
      && this.phase !== "idle"
      && this.phase !== "returning"
    ) {
      await this.cleanup("browser-close")
    }
  }

  /** ready 后通过 owner 查询 Host；只有 matching attached holder 才进入 active。 */
  private async confirmReady(): Promise<void> {
    if (this.phase !== "opening" || this.attachmentId === undefined) return
    let status: ControlStatus
    try {
      status = await this.host.controlStatus()
    } catch (error) {
      await this.cleanup("host-control-failed", error)
      return
    }
    if (
      status.state === "attached"
      && status.holder.attachment_id === this.attachmentId
    ) {
      this.phase = "active"
      this.tuiLocked = true
      this.readyTimer?.clear()
      this.readyTimer = undefined
      this.publish()
      this.diagnostics?.info("web.handoff.active")
      // Browser 收到 active 才允许启用输入；send 失败由 channel close 收敛。
      if (this.primary !== undefined) {
        try {
          await this.primary.send({ type: "active" })
        } catch {
          // Browser 已断；不动 cleanup，由 channel close 触发 browser-close。
        }
      }
    } else {
      await this.cleanup("host-control-failed")
    }
  }

  /** 统一 cleanup：拒绝新请求、撤销 attachment、等待 owner，最后发布 idle。 */
  private cleanup(reason: WebReturnReason, error?: unknown): Promise<void> {
    if (this.cleanupPromise !== undefined) return this.cleanupPromise
    this.cleanupPromise = this.performCleanup(reason, error)
    return this.cleanupPromise
  }

  private async performCleanup(
    reason: WebReturnReason,
    error?: unknown,
  ): Promise<void> {
    this.diagnostics?.info("web.handoff.returning", { reason })
    const attachmentId = this.attachmentId
    const primary = this.primary
    if (this.phase !== "returning") {
      this.returnReason = reason
      this.error = error !== undefined ? sanitizeError(error) : undefined
      this.phase = "returning"
      // tuiLocked 保留进入回收前的锁定事实：active 过的一直锁定到 owner 确认，
      // 未完成 acquire 的 opening 失败保持 false，不伪造 Web holder。
    }
    this.readyTimer?.clear()
    this.readyTimer = undefined
    this.publish()

    if (primary !== undefined) {
      try {
        await primary.send({ type: "shutdown", reason: shutdownReason(reason) })
      } catch {
        // primary 可能已断开；cleanup 继续。
      }
    }
    // 静态 server 是 session-scoped 且跨 handoff 复用（设计约定），只在
    // Coordinator.close() 时停止；opener 失败后下一次 open 会复用同一实例。

    let ownerConfirmed = false
    if (attachmentId !== undefined) {
      try {
        const result = await this.host.revokeAttachment(attachmentId)
        ownerConfirmed = result.control.state === "owner"
      } catch (revokeError) {
        if (this.error === undefined) this.error = sanitizeError(revokeError)
        this.publish()
      }
    }
    const deadline = this.scheduler.now() + this.ownerWaitMs
    while (!ownerConfirmed && !this.closed && this.scheduler.now() < deadline) {
      await this.sleep(this.ownerPollMs)
      try {
        const status = await this.host.controlStatus()
        ownerConfirmed = status.state === "owner"
      } catch (statusError) {
        if (this.error === undefined) this.error = sanitizeError(statusError)
        this.publish()
      }
    }

    if (primary !== undefined) {
      try {
        await primary.close(1000, "handoff-converged")
      } catch {
        // 已断开时忽略。
      }
    }
    if (ownerConfirmed) {
      this.invokeExitHandlerIfPending()
      this.restoreToIdle()
    } else if (!this.closed) {
      // owner 在窗口内未确认：保持 returning，但继续后台轮询，owner 一旦
      // 恢复仍会回到 idle，不会永久楔住 TUI。
      this.scheduleOwnerRecovery()
      this.publish()
    } else {
      this.publish()
    }
    this.cleanupPromise = undefined
    this.publish()
  }

  private publish(): void {
    this.snapshot = this.buildSnapshot()
    for (const listener of this.listeners) listener(this.snapshot)
  }

  /** 后台轮询 owner 恢复；close 或回到 idle 时停止。 */
  private scheduleOwnerRecovery(): void {
    if (this.recoveryTimer !== undefined) return
    this.recoveryTimer = this.scheduler.set(() => {
      void this.pollOwnerRecovery()
    }, this.ownerPollMs)
  }

  private async pollOwnerRecovery(): Promise<void> {
    this.recoveryTimer = undefined
    if (this.closed || this.phase !== "returning") return
    try {
      const status = await this.host.controlStatus()
      if (status.state === "owner") {
        this.invokeExitHandlerIfPending()
        this.restoreToIdle()
        return
      }
    } catch {
      // 查询失败继续轮询。
    }
    this.scheduleOwnerRecovery()
  }

  /**
   * 触发注册的 CLI exit handler；必须在 owner 确认之后调用，避免
   * Browser 主动退出时直接杀掉 TUI / Python sidecar。
   */
  private invokeExitHandlerIfPending(): void {
    if (!this.exitHandlerPending) return
    this.exitHandlerPending = false
    const handler = this.exitHandler
    if (handler === undefined) return
    try {
      handler()
    } catch {
      // handler 异常不阻止 coordinator 收敛；exit 由调用方实现自己处理。
    }
  }

  private restoreToIdle(): void {
    this.recoveryTimer?.clear()
    this.recoveryTimer = undefined
    this.handoffVersion += 1
    this.phase = "idle"
    this.handoffId = null
    this.attachmentId = undefined
    this.primary = undefined
    this.tuiLocked = false
    this.returnReason = undefined
    this.error = undefined
    this.publish()
  }
}

/** 创建 WebHandoffCoordinator 的唯一工厂；状态与测试入口都收敛在这里。 */
export function createWebHandoffCoordinator(
  options: WebHandoffCoordinatorOptions,
): WebHandoffCoordinator {
  return new WebHandoffCoordinatorImpl(options)
}

/** 校验一条 lifecycle 帧；畸形、超限或未知字段返回 undefined。 */
export function parseLifecycleMessage(value: unknown): LifecycleBrowserMessage | undefined {
  if (typeof value !== "string") return undefined
  if (textEncoder.encode(value).byteLength > MAX_LIFECYCLE_FRAME_BYTES) {
    return undefined
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(value)
  } catch {
    return undefined
  }
  if (!isRecord(parsed)) return undefined
  const type = parsed.type
  if (type === "ready") {
    if (!exactFields(parsed, ["type", "thread_id"])) return undefined
    const threadId = parsed.thread_id
    if (
      threadId !== null
      && (typeof threadId !== "string"
        || threadId.length === 0
        || threadId.length > 256)
    ) {
      return undefined
    }
    return { type: "ready", thread_id: threadId }
  }
  if (type === "released") {
    return exactFields(parsed, ["type"]) ? { type: "released" } : undefined
  }
  if (type === "exit.requested") {
    return exactFields(parsed, ["type"]) ? { type: "exit.requested" } : undefined
  }
  if (type === "thread.changed") {
    if (!exactFields(parsed, ["type", "thread_id"])) return undefined
    const threadId = parsed.thread_id
    if (
      threadId !== null
      && (typeof threadId !== "string"
        || threadId.length === 0
        || threadId.length > 256)
    ) {
      return undefined
    }
    return { type: "thread.changed", thread_id: threadId }
  }
  if (type === "diagnostic") {
    if (!exactFields(parsed, ["type", "stage", "error_name", "error_message"])) return undefined
    if (!isBootstrapStage(parsed.stage)) return undefined
    if (typeof parsed.error_name !== "string" || parsed.error_name.length === 0 || parsed.error_name.length > 80) {
      return undefined
    }
    if (typeof parsed.error_message !== "string" || parsed.error_message.length > 200) return undefined
    return {
      type: "diagnostic",
      stage: parsed.stage,
      error_name: parsed.error_name,
      error_message: parsed.error_message,
    }
  }
  return undefined
}

function isBootstrapStage(value: unknown): value is WebBootstrapStage {
  return typeof value === "string" && [
    "lifecycle.accepted",
    "attachment.auth",
    "agent.initialize",
    "host.control.acquire",
    "thread.restore",
    "react.mount",
  ].includes(value)
}

/** 把归还原因映射为 lifecycle shutdown 稳定原因。 */
function shutdownReason(reason: WebReturnReason): LifecycleShutdownReason {
  switch (reason) {
    case "ready-timeout":
      return "ready-timeout"
    case "invalid-message":
      return "invalid-message"
    case "host-control-failed":
      return "host-control-failed"
    case "cli-exit":
      return "cli-exit"
    case "exit-requested":
      // Browser 主动退出仍走 returning；shutdown reason 用于 Browser 侧
      // 收敛状态展示，不影响命令面板或 Thread 切换。
      return "returning"
    default:
      return "returning"
  }
}

function exactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  const keys = Object.keys(value)
  return keys.length === fields.length && fields.every(field => field in value)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function sanitizeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.length > 200 ? `${message.slice(0, 200)}…` : message
}
