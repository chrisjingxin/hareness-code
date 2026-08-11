/** Selector 输出类型：可序列化视图与 FeatureAvailability。 */

import type { CommandMenuItem } from "../commands"
import type {
  ActiveRun,
  InteractiveActivity,
  InteractiveConfirmation,
  InteractiveConnectionState,
  InteractiveInteraction,
  InteractiveRuntime,
  InteractiveSnapshot,
  RunSummary,
  TimelineItem,
} from "../types"

/** 从 snapshot 推导的展示可用性；全布尔、可 JSON 序列化，presentation 不再直接判断协议 Capability。 */
export type FeatureAvailability = {
  readonly canSubmit: boolean
  readonly canCancelRun: boolean
  readonly canOpenThread: boolean
  readonly canToggleSkill: boolean
  readonly canManageMcp: boolean
  readonly canChangeModel: boolean
  readonly canOpenModelsPanel: boolean
  readonly canOpenSkillsPanel: boolean
  readonly canOpenMcpPanel: boolean
  /** 纯 capability 门：是否具备对应协议能力；不折叠 busy 状态，面板在 run 期间保持可见仅禁用。 */
  readonly hasSkillManage: boolean
  readonly hasMcpManage: boolean
}

/** 在 snapshot 层做 capability 判断的合法白名单；其余能力判断必须下沉到 Core。 */
export const CAPABILITY_GATE = {
  toggleSkill: "skills.manage",
  manageMcp: "mcp.manage",
  changeModel: "models.select",
  openModelsPanel: "models.read",
  openSkillsPanel: "skills.read",
  openMcpPanel: "mcp.read",
} as const

/** 对话视图：时间线与当前运行状态；是 UI 契约中高频发布的 conversation 分片。 */
export type ConversationView = {
  readonly currentThreadId: string | null
  readonly activity: InteractiveActivity
  readonly activeRun: ActiveRun | null
  readonly timeline: readonly TimelineItem[]
  readonly runProgress: InteractiveSnapshot["runProgress"]
  readonly lastRun: RunSummary | null
}

/** 交互视图：挂起 Interaction 与破坏性确认；UI 契约 interaction 分片。 */
export type InteractionView = {
  readonly interaction: InteractiveInteraction | null
  readonly confirmation: InteractiveConfirmation | null
}

/** 导航视图：四类 catalog 与打开/变更可用性；UI 契约 navigation 分片。 */
export type NavigationView = {
  readonly catalogs: InteractiveSnapshot["catalogs"]
  readonly availability: {
    readonly canOpenThread: boolean
    readonly canOpenModelsPanel: boolean
    readonly canOpenSkillsPanel: boolean
    readonly canOpenMcpPanel: boolean
    readonly hasSkillManage: boolean
    readonly hasMcpManage: boolean
  }
}

/** 命令视图：可用命令与提交可用性；UI 契约 command 分片。 */
export type CommandView = {
  readonly commands: readonly CommandMenuItem[]
  readonly availability: {
    readonly canSubmit: boolean
  }
}

/** 运行时视图：runtime/connection/selection 与运行控制可用性；UI 契约 runtime 分片。 */
export type RuntimeView = {
  readonly runtime: InteractiveRuntime
  readonly connection: InteractiveConnectionState
  readonly selection: InteractiveSnapshot["selection"]
  readonly workMode: InteractiveSnapshot["workMode"]
  readonly composeState: InteractiveSnapshot["composeState"]
  readonly availability: {
    readonly canCancelRun: boolean
    readonly canToggleSkill: boolean
    readonly canManageMcp: boolean
    readonly canChangeModel: boolean
  }
}
