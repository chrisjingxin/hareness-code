/**
 * CLI 内部 UI 契约：TUI 与内置 Web UI 之间的消息类型与视图载荷。
 *
 * 本契约独立于 packages/protocol 版本化，只服务 PresentationCoordinator 与
 * WebUiClient 两端；新增字段必须同步升级 UI_CONTRACT_VERSION。
 */

import { MAX_FRAME_BYTES } from "@za38/protocol"

import type { CommandView, ConversationView, InteractionView, NavigationView, RuntimeView } from "../../interactive/selectors"
import type { InteractiveIntent, IntentOutcome } from "../../interactive/types"
import type { PresentationState } from "../state"

/** UI 契约版本：网关与 Browser 共享同一常量，消息形状变更时递增。 */
export const UI_CONTRACT_VERSION = 1

/** 单帧上限与跨进程协议一致；replace 需承载完整 Timeline，因此不用旧 lifecycle 的 16 KiB。 */
export const MAX_UI_FRAME_BYTES = MAX_FRAME_BYTES

/** requestId 是浏览器本地生成的稳定关联标识，长度上限用于防御畸形帧。 */
export const MAX_REQUEST_ID_LENGTH = 128

/** 首次连接 / 重同步的完整可序列化视图；五个分片恰好覆盖 InteractiveSnapshot 的领域事实。 */
export type WebUiState = {
  readonly conversation: ConversationView
  readonly interaction: InteractionView
  readonly navigation: NavigationView
  readonly command: CommandView
  readonly runtime: RuntimeView
}

/** 运行期增量：只携带发生变化的分片；至少一个分片存在。 */
export type WebUiPatch = {
  readonly conversation?: ConversationView
  readonly interaction?: InteractionView
  readonly navigation?: NavigationView
  readonly command?: CommandView
  readonly runtime?: RuntimeView
}

/**
 * Web 表现层本地动作（主题/面板/焦点等）；默认只在 Browser 本地消费，不进 Core。
 * 契约保留该类型，网关对收到它的渲染连接做无害 no-op。
 */
export type WebPresentationIntent =
  | { type: "theme.set"; theme: "light" | "dark" }
  | { type: "panel.open"; panel: string }
  | { type: "panel.close" }

/** Browser → 网关（CLI）消息：业务 intent、本地动作与生命周期事件。 */
export type WebUiClientMessage =
  | { type: "intent"; requestId: string; revision: number; intent: InteractiveIntent }
  | { type: "presentation-intent"; intent: WebPresentationIntent }
  | { type: "handoff.ready" }
  | { type: "handoff.return" }
  | { type: "handoff.exit" }

/** 网关（CLI）→ Browser 消息：状态发布、intent 结果与接管状态。 */
export type WebUiServerMessage =
  | { type: "state.replace"; revision: number; state: WebUiState }
  | { type: "state.patch"; revision: number; patch: WebUiPatch }
  | { type: "intent.outcome"; requestId: string; outcome: IntentOutcome }
  | { type: "handoff.state"; state: PresentationState }
