/**
 * Browser 侧 WebUiClient：WebSocket → CLI WebUiGateway。
 *
 * 职责：UI token 认证、state.replace/patch 视图缓存、interactive/workspace
 * 双域 intent 提交与 requestId 关联的 outcome 回传、handoff 状态订阅。
 * 凭据只存在于连接建立时的 URL 查询参数，永不进入 React props 或日志。
 */

import type { InteractiveIntent, IntentOutcome } from "../interactive/types"
import type { WorkspaceIntent, WorkspaceOutcome } from "../workspace/types"
import { parseServerFrame, type PresentationState, type WebUiPatch, type WebUiState } from "../presentation-coordinator"
import { isWebActive } from "../presentation-coordinator"

/** 浏览器 WebSocket 的最小形状；生产传真实 WebSocket，测试传 fake。 */
export type UiSocket = {
  readonly readyState: number
  send(data: string): void
  close(code?: number, reason?: string): void
  addEventListener(type: string, listener: (event: unknown) => void): void
  removeEventListener(type: string, listener: (event: unknown) => void): void
}

/** handoff 结束时由 closeGate 使用的稳定文案；reason 只用于日志。 */
export type WebUiCloseReason =
  | "server-close"
  | "handoff-ended"
  | "socket-error"
  | "page-hide"
  | "local-close"

export type WebUiClientOptions = {
  socket: UiSocket
  onState?: (state: WebUiState) => void
  onClosed?: (reason: WebUiCloseReason) => void
}

export interface WebUiClient {
  /** 最近一次 replace/patch 合并后的完整视图；连接建立后始终非空。 */
  getState(): WebUiState
  /** 最近一次 handoff.state；未收到前为 tui-active 的保守默认。 */
  getHandoffState(): PresentationState
  /** 订阅视图更新（replace/patch 合并后触发）；返回解绑函数。 */
  subscribeState(listener: (state: WebUiState) => void): () => void
  /** 订阅 handoff 状态变化；返回解绑函数。 */
  subscribeHandoff(listener: (state: PresentationState) => void): () => void
  /** 提交 interactive 业务 intent；Promise 恒 resolve（rejected 也走 outcome），不会 reject。 */
  submitIntent(intent: InteractiveIntent): Promise<IntentOutcome>
  /** 提交 workspace 业务 intent；Promise 恒 resolve（rejected 也走 outcome），不会 reject。 */
  workspaceIntent(intent: WorkspaceIntent): Promise<WorkspaceOutcome>
  /** Browser 已就绪（首次渲染完成）；仅 opening-web 阶段有效。 */
  ready(): void
  /** 请求归还输入权给 TUI。 */
  returnToTui(): void
  /** 请求退出 Harness。 */
  requestExit(): void
  /** 关闭本地 socket 与全部挂起 intent；幂等。 */
  close(): void
}

class WebUiClientImpl implements WebUiClient {
  private readonly socket: UiSocket
  private readonly onState: ((state: WebUiState) => void) | undefined
  private readonly onClosed: ((reason: WebUiCloseReason) => void) | undefined
  private readonly handoffListeners = new Set<(state: PresentationState) => void>()
  private readonly stateListeners = new Set<(state: WebUiState) => void>()

  private state: WebUiState
  private handoffState: PresentationState = { phase: "tui-active" }
  private lastRevision = 0
  private readonly pending = new Map<string, { resolve: (outcome: IntentOutcome | WorkspaceOutcome) => void }>()
  private closed = false

  private readonly onMessage = (event: unknown) => this.handleMessage(event)
  private readonly onClose = () => this.handleClosed("server-close")
  private readonly onError = () => this.handleClosed("socket-error")

  constructor(options: WebUiClientOptions) {
    this.socket = options.socket
    this.onState = options.onState
    this.onClosed = options.onClosed
    // 连接期间 state 必已收到 replace；构造时的空态只用于防御。
    this.state = emptyState()
    this.socket.addEventListener("message", this.onMessage)
    this.socket.addEventListener("close", this.onClose)
    this.socket.addEventListener("error", this.onError)
  }

  getState(): WebUiState {
    return this.state
  }

  getHandoffState(): PresentationState {
    return this.handoffState
  }

  subscribeState(listener: (state: WebUiState) => void): () => void {
    this.stateListeners.add(listener)
    return () => this.stateListeners.delete(listener)
  }

  subscribeHandoff(listener: (state: PresentationState) => void): () => void {
    this.handoffListeners.add(listener)
    return () => this.handoffListeners.delete(listener)
  }

  submitIntent(intent: InteractiveIntent): Promise<IntentOutcome> {
    return this.submit("interactive.intent", intent) as Promise<IntentOutcome>
  }

  workspaceIntent(intent: WorkspaceIntent): Promise<WorkspaceOutcome> {
    return this.submit("workspace.intent", intent) as Promise<WorkspaceOutcome>
  }

  /** 双域共用提交路径：requestId 由 crypto.randomUUID 生成，outcome 按 domain 回传。 */
  private submit(type: "interactive.intent" | "workspace.intent", intent: InteractiveIntent | WorkspaceIntent): Promise<IntentOutcome | WorkspaceOutcome> {
    if (this.closed) {
      return Promise.resolve({ status: "rejected", code: "connection-closed", message: "Web 会话已结束" })
    }
    const requestId = crypto.randomUUID()
    const message = JSON.stringify({ type, requestId, revision: this.lastRevision, intent })
    if (this.socket.readyState !== 1) {
      return Promise.resolve({ status: "rejected", code: "connection-closed", message: "Web 会话连接已断开" })
    }
    return new Promise<IntentOutcome | WorkspaceOutcome>(resolve => {
      this.pending.set(requestId, { resolve })
      try {
        this.socket.send(message)
      } catch {
        this.pending.delete(requestId)
        resolve({ status: "rejected", code: "connection-closed", message: "Web 会话连接已断开" })
      }
    })
  }

  ready(): void {
    this.send({ type: "handoff.ready" })
  }

  returnToTui(): void {
    this.send({ type: "handoff.return" })
  }

  requestExit(): void {
    this.send({ type: "handoff.exit" })
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    this.socket.removeEventListener("message", this.onMessage)
    this.socket.removeEventListener("close", this.onClose)
    this.socket.removeEventListener("error", this.onError)
    try {
      this.socket.close(1000, "page-closed")
    } catch {
      // 已断开时忽略。
    }
    this.settlePending({ status: "rejected", code: "connection-closed", message: "Web 会话已结束" })
    this.handoffListeners.clear()
    this.stateListeners.clear()
  }

  private handleMessage(event: unknown): void {
    if (this.closed) return
    const raw = isRecord(event) ? event.data : undefined
    const message = parseServerFrame(raw)
    if (message === undefined) return
    switch (message.type) {
      case "state.replace":
        this.lastRevision = message.revision
        this.state = message.state
        this.notifyState()
        return
      case "state.patch":
        if (message.revision < this.lastRevision) {
          // 晚到/重复帧：防御性丢弃，以已处理序列为准。
          return
        }
        this.lastRevision = message.revision
        this.state = mergePatch(this.state, message.patch)
        this.notifyState()
        return
      case "intent.outcome": {
        const pending = this.pending.get(message.requestId)
        if (pending) {
          this.pending.delete(message.requestId)
          pending.resolve(message.outcome)
        }
        return
      }
      case "handoff.state":
        this.handoffState = message.state
        for (const listener of [...this.handoffListeners]) listener(message.state)
        // 控制权离开 web-active 后未决 intent 不再有 outcome：统一以 rejected 收敛，
        // 保留用户草稿（returning-tui 与 tui-active 都立即结算，不等 close 事件）。
        if (!isWebActive(message.state)) {
          this.settlePending({ status: "rejected", code: "busy", message: "Web 会话控制权已变化，请重试" })
        }
        return
    }
  }

  private handleClosed(reason: WebUiCloseReason): void {
    if (this.closed) return
    this.closed = true
    this.settlePending({ status: "rejected", code: "connection-closed", message: "Web 会话连接已断开" })
    this.onClosed?.(reason)
  }

  private notifyState(): void {
    this.onState?.(this.state)
    for (const listener of [...this.stateListeners]) listener(this.state)
  }

  private settlePending(outcome: IntentOutcome): void {
    for (const pending of this.pending.values()) pending.resolve(outcome)
    this.pending.clear()
  }

  private send(message: { type: "handoff.ready" | "handoff.return" | "handoff.exit" }): void {
    if (this.closed || this.socket.readyState !== 1) return
    this.socket.send(JSON.stringify(message))
  }
}

/** 创建 WebUiClient；构造后立即开始消费 socket 消息。 */
export function createWebUiClient(options: WebUiClientOptions): WebUiClient {
  return new WebUiClientImpl(options)
}

/** 从 URL fragment 或 sessionStorage 读取 UI token；读取后立刻剥离 fragment。 */
export function readUiToken(handoffId: string): string | undefined {
  const storageKey = `harness-ui-token:${handoffId}`
  try {
    const stored = window.sessionStorage.getItem(storageKey)
    if (stored) return stored
  } catch {
    // sessionStorage 不可用时退回 fragment。
  }
  const fragment = window.location.hash
  const token = new URLSearchParams(fragment.startsWith("#") ? fragment.slice(1) : fragment).get("ui")
  if (typeof token === "string" && token.length > 0) {
    // token 只在连接建立前存在；剥离后不再留在地址栏。
    window.history.replaceState(null, "", window.location.pathname)
    try {
      window.sessionStorage.setItem(storageKey, token)
    } catch {
      // 存储失败不阻止本次连接。
    }
    return token
  }
  return undefined
}

/** 合并 patch 到完整视图；只覆盖存在的分片。 */
function mergePatch(state: WebUiState, patch: WebUiPatch): WebUiState {
  return {
    conversation: patch.conversation ?? state.conversation,
    interaction: patch.interaction ?? state.interaction,
    navigation: patch.navigation ?? state.navigation,
    command: patch.command ?? state.command,
    runtime: patch.runtime ?? state.runtime,
    workspaceTree: patch.workspaceTree ?? state.workspaceTree,
    workspacePreview: patch.workspacePreview ?? state.workspacePreview,
  }
}

function emptyState(): WebUiState {
  return {
    conversation: { currentThreadId: null, activity: { kind: "idle" }, activeRun: null, timeline: [], reasoningSummary: null, runProgress: null, lastRun: null },
    interaction: { interaction: null, confirmation: null },
    navigation: { catalogs: { threads: { status: "idle", items: [] }, models: { status: "idle", items: [] }, skills: { status: "idle", items: [] }, mcp: { status: "idle", items: [] } }, availability: { canOpenThread: false, canOpenModelsPanel: false, canOpenSkillsPanel: false, canOpenMcpPanel: false, hasSkillManage: false, hasMcpManage: false } },
    command: { commands: [], availability: { canSubmit: false } },
    runtime: { runtime: { workspace: "", cliVersion: "0.1.0", modelConfigured: false, executionMode: "local", approvalMode: "default", capabilities: [] }, connection: { status: "closed", message: "connecting" }, selection: { requestedModelProfileId: null, actualModel: null, armedSkill: null }, availability: { canCancelRun: false, canToggleSkill: false, canManageMcp: false, canChangeModel: false } },
    workspaceTree: { status: "idle", rows: [], selectedPath: null, limited: false },
    workspacePreview: { status: "idle" },
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}
