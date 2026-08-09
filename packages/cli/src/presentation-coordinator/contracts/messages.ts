/**
 * CLI 内部 UI 契约：TUI 与内置 Web UI 之间的消息类型与视图载荷。
 *
 * 本契约独立于 packages/protocol 版本化，只服务 PresentationCoordinator 与
 * WebUiClient 两端；新增字段必须同步升级 UI_CONTRACT_VERSION。
 */

import { MAX_FRAME_BYTES } from "@za38/protocol"

import type { CommandView, ConversationView, InteractionView, NavigationView, RuntimeView } from "../../interactive/selectors"
import type { InteractiveIntent, IntentOutcome } from "../../interactive/types"
import type { WorkspaceIntent, WorkspaceOutcome, WorkspacePreviewState, WorkspaceTreeState } from "../../workspace/types"
import type { PresentationState } from "../state"

/** UI 契约版本：网关与 Browser 共享同一常量，消息形状变更时递增。 */
export const UI_CONTRACT_VERSION = 3

/** 单帧上限与跨进程协议一致；replace 需承载完整 Timeline，因此不用旧 lifecycle 的 16 KiB。 */
export const MAX_UI_FRAME_BYTES = MAX_FRAME_BYTES

/** requestId 是浏览器本地生成的稳定关联标识，长度上限用于防御畸形帧。 */
export const MAX_REQUEST_ID_LENGTH = 128

/** 工作区文件树视图：复用 WorkspaceExplorer 领域形状，网关只做透传。 */
export type WorkspaceTreeView = WorkspaceTreeState

/** 工作区文件预览视图：复用 WorkspaceExplorer 领域形状，网关只做透传。 */
export type WorkspacePreviewView = WorkspacePreviewState

/**
 * 首次连接 / 重同步的完整可序列化视图；七个分片恰好覆盖
 * InteractiveSnapshot 的领域事实与 WorkspaceExplorer 快照。
 */
export type WebUiState = {
  readonly conversation: ConversationView
  readonly interaction: InteractionView
  readonly navigation: NavigationView
  readonly command: CommandView
  readonly runtime: RuntimeView
  readonly workspaceTree: WorkspaceTreeView
  readonly workspacePreview: WorkspacePreviewView
}

/** 运行期增量：只携带发生变化的分片；至少一个分片存在。 */
export type WebUiPatch = {
  readonly conversation?: ConversationView
  readonly interaction?: InteractionView
  readonly navigation?: NavigationView
  readonly command?: CommandView
  readonly runtime?: RuntimeView
  readonly workspaceTree?: WorkspaceTreeView
  readonly workspacePreview?: WorkspacePreviewView
}

/**
 * Browser → 网关（CLI）消息：按 domain 分派业务 intent（interactive / workspace）
 * 与生命周期事件。主题/Dock/Tab 等纯表现状态全部留在 Browser Adapter 本地，不走契约。
 */
export type WebUiClientMessage =
  | { type: "interactive.intent"; requestId: string; revision: number; intent: InteractiveIntent }
  | { type: "workspace.intent"; requestId: string; revision: number; intent: WorkspaceIntent }
  | { type: "handoff.ready" }
  | { type: "handoff.return" }
  | { type: "handoff.exit" }

/** 网关（CLI）→ Browser 消息：状态发布、按 domain 的 intent 结果与接管状态。 */
export type WebUiServerMessage =
  | { type: "state.replace"; revision: number; state: WebUiState }
  | { type: "state.patch"; revision: number; patch: WebUiPatch }
  | { type: "intent.outcome"; requestId: string; domain: "interactive"; outcome: IntentOutcome }
  | { type: "intent.outcome"; requestId: string; domain: "workspace"; outcome: WorkspaceOutcome }
  | { type: "handoff.state"; state: PresentationState }
