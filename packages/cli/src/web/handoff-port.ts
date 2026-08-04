/** Browser 侧 Web Handoff 窄接口：隔离 lifecycle 发送、supervisor 等待与 Host control 释放。 */

import type { BrowserConnectionSupervisor } from "./connection-supervisor"
import type { LifecycleBrowserMessage } from "./handoff-coordinator"

/** Browser 侧 Web Handoff 端口；只暴露 React Adapter 真正调用的语义动作。 */
export interface WebHandoffPort {
  /**
   * 报告最终 Thread 后发送 `ready{thread_id}`，并等待 CLI 的 `active` 确认；
   * 收到 ack 后 Adapter 才能启用 composer 与管理动作。
   */
  activate(threadId: string | null): Promise<void>
  /**
   * 上报 Thread 变化；仅在 `activate` 完成之后生效，连续相同值会去重。
   * `null` 是新建/清空后的正式状态，不被当作缺省值。
   */
  reportThread(threadId: string | null): void
  /**
   * 归还控制权：先调用注入的 release（host.control.release + owner 确认），
   * 失败时直接抛出以便调用方展示可重试错误（attachment 仍有效）。
   * 成功后再发送 `released`。
   */
  returnToTui(): Promise<void>
  /** 发送 `exit.requested`，由 CLI 完成 owner 恢复后触发 CLI 退出 handler。 */
  requestExit(): Promise<void>
  /**
   * 关闭 Browser 侧资源；幂等，不发送 `released`。
   * Coordinator 仍会按 `browser-close` 路径收敛，无需伪造 lifecycle 消息。
   */
  close(): void
}

/** lifecycle 浏览器侧消息的发送能力；Promise 化便于后续切换为异步 channel。 */
export type SendLifecycle = (message: LifecycleBrowserMessage) => Promise<void>

/**
 * Host control 释放能力：执行 `host.control.release` 并确认 owner；
 * 注入而非由本文件直接调用 Agent RPC，避免端口反向依赖 IPC。
 */
export type ReleaseHostControl = () => Promise<void>

export type WebHandoffPortOptions = {
  sendLifecycle: SendLifecycle
  supervisor: BrowserConnectionSupervisor
  release: ReleaseHostControl
}

class WebHandoffPortImpl implements WebHandoffPort {
  private readonly sendLifecycle: SendLifecycle
  private readonly supervisor: BrowserConnectionSupervisor
  private readonly release: ReleaseHostControl
  private active = false
  private closed = false
  /** 最近一次 reportThread 的值；用于连续相同值去重。 */
  private lastReportedThreadId: string | null | undefined

  constructor(options: WebHandoffPortOptions) {
    this.sendLifecycle = options.sendLifecycle
    this.supervisor = options.supervisor
    this.release = options.release
  }

  async activate(threadId: string | null): Promise<void> {
    if (this.closed) throw new Error("Web handoff port is closed")
    // 先发送最终 Thread，再等待 CLI 确认；任一步骤失败都抛错让调用方处理。
    await this.sendLifecycle({ type: "ready", thread_id: threadId })
    await this.supervisor.waitForActive()
    this.active = true
    // 首次进入 active：把"已报告"指针初始化为当前值，避免重复触发一次相同值的发送。
    this.lastReportedThreadId = threadId
  }

  reportThread(threadId: string | null): void {
    // active 之前 Thread 报告由 `ready` 帧表达，重复发送会让 CLI 进入 invalid-message。
    if (!this.active || this.closed) return
    if (this.lastReportedThreadId === threadId) return
    this.lastReportedThreadId = threadId
    void this.sendLifecycle({ type: "thread.changed", thread_id: threadId }).catch(() => {
      // lifecycle 关闭时 cleanup 由 supervisor 驱动；这里不能产生未处理 rejection。
    })
  }

  async returnToTui(): Promise<void> {
    if (this.closed) throw new Error("Web handoff port is closed")
    // release 失败直接抛：attachment 仍有效，调用方应展示可重试错误。
    await this.release()
    await this.sendLifecycle({ type: "released" })
  }

  async requestExit(): Promise<void> {
    if (this.closed) throw new Error("Web handoff port is closed")
    await this.sendLifecycle({ type: "exit.requested" })
  }

  close(): void {
    this.closed = true
  }
}

/** 创建 Browser 侧 WebHandoffPort；构造轻量，不发起任何网络动作。 */
export function createWebHandoffPort(options: WebHandoffPortOptions): WebHandoffPort {
  return new WebHandoffPortImpl(options)
}
