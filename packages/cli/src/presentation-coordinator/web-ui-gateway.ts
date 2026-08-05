/**
 * WebUiGateway：把共享 InteractiveController 的 snapshot 发布为 UI 契约状态帧，
 * 并把 Browser 提交的 intent 送入 Controller、按 requestId 回传 outcome。
 *
 * 本模块是渲染 channel 的唯一写者（handoff.state 也经它转发），保证服务器到
 * Browser 的消息顺序单一。Coordinator 负责生命周期与输入租约，本模块只做视图
 * 序列化、revision 门禁与 intent 受理。
 */

import {
  selectCommandView,
  selectConversationView,
  selectInteractionView,
  selectNavigationView,
  selectRuntimeView,
} from "../interactive/selectors"
import type { InteractiveController, InteractiveIntent, InteractiveSnapshot, IntentOutcome } from "../interactive/types"
import type { DiagnosticLogger } from "../diagnostics/local-logger"
import type { GatewayChannel, PresentationCoordinator } from "./coordinator"
import { parseClientFrame, type WebUiClientMessage, type WebUiPatch, type WebUiServerMessage, type WebUiState } from "./contracts"
import { isWebActive, type PresentationState } from "./state"

const OUTCOME_CACHE_LIMIT = 256
const BASE_REVISION = 1

/** replace 之后每个补丁递增的 revision 基准；连接重建时从 replace 重新计数。 */

export type WebUiGatewayOptions = {
  coordinator: PresentationCoordinator
  controller: InteractiveController
  diagnostics?: DiagnosticLogger
}

export interface WebUiGateway {
  /** 接管一个已完成认证的渲染 channel：发送首帧 replace 与 handoff.state，并开始消费。 */
  connectRenderer(channel: GatewayChannel): void
  close(): Promise<void>
}

class WebUiGatewayImpl implements WebUiGateway {
  private readonly coordinator: PresentationCoordinator
  private readonly controller: InteractiveController
  private readonly diagnostics: DiagnosticLogger | undefined
  private readonly unsubscribeController: () => void
  private readonly unsubscribeCoordinator: () => void

  private channel: GatewayChannel | null = null
  private lastState: WebUiState | null = null
  private revision = 0
  private readonly outcomeCache = new Map<string, IntentOutcome>()

  constructor(options: WebUiGatewayOptions) {
    this.coordinator = options.coordinator
    this.controller = options.controller
    this.diagnostics = options.diagnostics
    this.unsubscribeController = this.controller.subscribe(() => this.onControllerPublish())
    this.unsubscribeCoordinator = this.coordinator.subscribe(state => this.onCoordinatorPublish(state))
  }

  connectRenderer(channel: GatewayChannel): void {
    this.channel = channel
    this.revision = BASE_REVISION
    this.lastState = buildWebUiState(this.controller.getSnapshot())
    this.diagnostics?.info("web.gateway.connected")
    void this.send({ type: "state.replace", revision: this.revision, state: this.lastState })
    void this.send({ type: "handoff.state", state: this.coordinator.getSnapshot() })
    void this.consume(channel)
  }

  async close(): Promise<void> {
    this.unsubscribeController()
    this.unsubscribeCoordinator()
    this.channel = null
    this.outcomeCache.clear()
  }

  /** 消费渲染 channel 的全部客户端帧；畸形帧由 Coordinator fail-closed 收敛。 */
  private async consume(channel: GatewayChannel): Promise<void> {
    try {
      for await (const raw of channel.messages) {
        if (this.channel !== channel) return
        const message = parseClientFrame(raw)
        if (message === undefined) {
          this.coordinator.notifyInvalidMessage()
          return
        }
        await this.handleClientMessage(message)
      }
    } catch {
      // channel fail/close 与 for await 结束走同一个断开收敛路径。
    }
    if (this.channel === channel) {
      this.channel = null
      this.coordinator.notifyRendererDisconnected("browser-close")
    }
  }

  private async handleClientMessage(message: WebUiClientMessage): Promise<void> {
    switch (message.type) {
      case "presentation-intent":
        // 主题/面板/焦点等本地动作默认不进 Core；契约保留类型供未来扩展。
        return
      case "handoff.ready":
        this.coordinator.requestReady()
        return
      case "handoff.return":
        this.coordinator.requestReturn()
        return
      case "handoff.exit":
        this.coordinator.requestExit()
        return
      case "intent":
        await this.handleIntent(message)
        return
    }
  }

  private async handleIntent(message: Extract<WebUiClientMessage, { type: "intent" }>): Promise<void> {
    const { requestId, revision, intent } = message
    // 非 web-active：不发 outcome，只推当前 handoff.state；Browser 据此保留草稿并进入只读等待。
    if (!isWebActive(this.coordinator.getSnapshot())) {
      await this.send({ type: "handoff.state", state: this.coordinator.getSnapshot() })
      return
    }
    // revision 门禁：Browser 视图必须已覆盖最近一次 replace（>=1），且不超前于网关发布序列。
    if (revision < BASE_REVISION || revision > this.revision) {
      await this.sendOutcome(requestId, {
        status: "rejected",
        code: "invalid-argument",
        message: "Invalid revision; resync from state.replace",
      })
      return
    }
    // 重放去重：同一 requestId 幂等返回已缓存的 outcome，避免传输重试造成重复执行。
    // （消费循环串行处理帧，同 requestId 不可能并发在飞，无需 in-flight 去重。）
    const cached = this.outcomeCache.get(requestId)
    if (cached !== undefined) {
      await this.sendOutcome(requestId, cached)
      return
    }
    const promise = this.controller.dispatch(intent)
    let outcome: IntentOutcome
    try {
      outcome = await promise
    } catch (error) {
      // dispatch 设计上返回 IntentOutcome，但远端异常仍需收敛为可回传的 rejected。
      outcome = {
        status: "rejected",
        code: "agent-error",
        message: error instanceof Error ? error.message : String(error),
      }
    }
    this.cacheOutcome(requestId, outcome)
    await this.sendOutcome(requestId, outcome)
  }

  /** Controller snapshot 发布 → 分片 diff → state.patch（revision 单调）。 */
  private onControllerPublish(): void {
    const next = buildWebUiState(this.controller.getSnapshot())
    const previous = this.lastState ?? next
    this.lastState = next
    if (this.channel === null) return
    const changed: Record<string, unknown> = {}
    if (!sameSlice(previous.conversation, next.conversation)) changed.conversation = next.conversation
    if (!sameSlice(previous.interaction, next.interaction)) changed.interaction = next.interaction
    if (!sameSlice(previous.navigation, next.navigation)) changed.navigation = next.navigation
    if (!sameSlice(previous.command, next.command)) changed.command = next.command
    if (!sameSlice(previous.runtime, next.runtime)) changed.runtime = next.runtime
    if (Object.keys(changed).length === 0) return
    this.revision += 1
    void this.send({ type: "state.patch", revision: this.revision, patch: changed as unknown as WebUiPatch })
  }

  /** Coordinator 状态变化 → 转发 handoff.state；会话结束时关闭渲染 channel。 */
  private onCoordinatorPublish(state: PresentationState): void {
    if (this.channel === null) return
    void this.send({ type: "handoff.state", state })
    if (state.phase === "returning-tui" || state.phase === "tui-active") {
      const channel = this.channel
      this.channel = null
      void channel.close(1000, "handoff-converged")
    }
  }

  private async sendOutcome(requestId: string, outcome: IntentOutcome): Promise<void> {
    await this.send({ type: "intent.outcome", requestId, outcome })
  }

  private async send(message: WebUiServerMessage): Promise<void> {
    const channel = this.channel
    if (channel === null) return
    await channel.send(message)
  }

  private cacheOutcome(requestId: string, outcome: IntentOutcome): void {
    this.outcomeCache.set(requestId, outcome)
    if (this.outcomeCache.size > OUTCOME_CACHE_LIMIT) {
      const oldest = this.outcomeCache.keys().next()
      if (!oldest.done) this.outcomeCache.delete(oldest.value)
    }
  }
}

/** 创建 WebUiGateway 的唯一工厂。 */
export function createWebUiGateway(options: WebUiGatewayOptions): WebUiGateway {
  return new WebUiGatewayImpl(options)
}

/** 把 Controller snapshot 收敛为五个 Selector 分片的完整视图。 */
export function buildWebUiState(snapshot: InteractiveSnapshot): WebUiState {
  return {
    conversation: selectConversationView(snapshot),
    interaction: selectInteractionView(snapshot),
    navigation: selectNavigationView(snapshot),
    command: selectCommandView(snapshot),
    runtime: selectRuntimeView(snapshot),
  }
}

/** 分片结构性比较：Selector 输出均为纯 JSON，序列化比较即可。 */
function sameSlice(previous: unknown, next: unknown): boolean {
  return JSON.stringify(previous) === JSON.stringify(next)
}
