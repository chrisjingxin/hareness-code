/** Browser 连接监督者：lifecycle 与 Agent socket 的统一关闭与 abort 收敛。 */

/** lifecycle WebSocket 的最小形状；生产传真实 WebSocket，测试传 fake。 */
export type LifecycleSocket = {
  readonly readyState: number
  send(data: string): void
  close(code?: number, reason?: string): void
  addEventListener(type: string, listener: (event: unknown) => void): void
  removeEventListener(type: string, listener: (event: unknown) => void): void
}

export type BrowserConnectionSupervisor = {
  readonly signal: AbortSignal
  waitForAccepted(): Promise<void>
  waitForActive(): Promise<void>
  bindAgent(closeAgent: () => void): void
  abort(reason: string): void
  dispose(): void
}

/** 等待者形状：accept/active 等待共享同一 reject 语义。 */
type Waiter = {
  resolve: () => void
  reject: (error: Error) => void
}

class BrowserConnectionSupervisorImpl implements BrowserConnectionSupervisor {
  private readonly controller = new AbortController()
  private readonly socket: LifecycleSocket
  private closeAgent: (() => void) | undefined
  private agentClosed = false
  private accepted = false
  private active = false
  private abortedReason: string | undefined
  private readonly acceptedWaiters: Waiter[] = []
  private readonly activeWaiters: Waiter[] = []

  private readonly onMessage = (event: unknown) => this.handleMessage(event)
  private readonly onClose = () => this.abort("lifecycle-closed")
  private readonly onError = () => this.abort("lifecycle-error")

  constructor(socket: LifecycleSocket) {
    this.socket = socket
    // 消息监听在整个 handoff 期间保留：accepted/active 只完成等待，不卸载 shutdown 处理。
    socket.addEventListener("message", this.onMessage)
    socket.addEventListener("close", this.onClose)
    socket.addEventListener("error", this.onError)
  }

  get signal(): AbortSignal {
    return this.controller.signal
  }

  waitForAccepted(): Promise<void> {
    if (this.accepted) return Promise.resolve()
    if (this.abortedReason !== undefined) {
      return Promise.reject(new Error(`Lifecycle aborted: ${this.abortedReason}`))
    }
    return new Promise((resolve, reject) => this.acceptedWaiters.push({ resolve, reject }))
  }

  waitForActive(): Promise<void> {
    if (this.active) return Promise.resolve()
    if (this.abortedReason !== undefined) {
      return Promise.reject(new Error(`Lifecycle aborted: ${this.abortedReason}`))
    }
    return new Promise((resolve, reject) => this.activeWaiters.push({ resolve, reject }))
  }

  bindAgent(closeAgent: () => void): void {
    this.closeAgent = closeAgent
    if (this.abortedReason !== undefined) this.closeAgentNow()
  }

  abort(reason: string): void {
    if (this.abortedReason !== undefined) return
    this.abortedReason = reason
    this.controller.abort()
    try {
      this.socket.close()
    } catch {
      // socket 可能已断开；close 动作幂等。
    }
    for (const waiter of this.acceptedWaiters.splice(0)) {
      waiter.reject(new Error(`Lifecycle aborted: ${reason}`))
    }
    for (const waiter of this.activeWaiters.splice(0)) {
      waiter.reject(new Error(`Lifecycle aborted: ${reason}`))
    }
    this.closeAgentNow()
  }

  dispose(): void {
    this.socket.removeEventListener("message", this.onMessage)
    this.socket.removeEventListener("close", this.onClose)
    this.socket.removeEventListener("error", this.onError)
  }

  private closeAgentNow(): void {
    if (this.closeAgent === undefined || this.agentClosed) return
    this.agentClosed = true
    this.closeAgent()
  }

  private handleMessage(event: unknown): void {
    const data = (event as { data?: unknown }).data
    let message: unknown
    try {
      message = JSON.parse(String(data))
    } catch {
      this.abort("lifecycle-invalid")
      return
    }
    if (!isRecord(message)) {
      this.abort("lifecycle-invalid")
      return
    }
    if (message.type === "accepted") {
      // 接受后到达的 accepted 帧不重复 resolve；abort 之后则直接忽略。
      if (this.abortedReason !== undefined) return
      if (!exactFields(message, ["type"])) {
        this.abort("lifecycle-invalid")
        return
      }
      if (this.accepted) return
      this.accepted = true
      for (const waiter of this.acceptedWaiters.splice(0)) waiter.resolve()
      return
    }
    if (message.type === "active") {
      // active 必须先 accepted；生产由 server 顺序保证，乱序帧仍按协议错误收敛。
      if (this.abortedReason !== undefined) return
      if (!exactFields(message, ["type"]) || !this.accepted) {
        this.abort("lifecycle-invalid")
        return
      }
      if (this.active) return
      this.active = true
      for (const waiter of this.activeWaiters.splice(0)) waiter.resolve()
      return
    }
    if (message.type === "shutdown") {
      if (!exactFields(message, ["type", "reason"]) || typeof message.reason !== "string") {
        this.abort("lifecycle-invalid")
        return
      }
      this.abort(`shutdown:${String(message.reason)}`)
      return
    }
    this.abort("lifecycle-invalid")
  }
}

export function createBrowserConnectionSupervisor(
  socket: LifecycleSocket,
): BrowserConnectionSupervisor {
  return new BrowserConnectionSupervisorImpl(socket)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function exactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  const keys = Object.keys(value)
  return keys.length === fields.length && fields.every(field => field in value)
}
