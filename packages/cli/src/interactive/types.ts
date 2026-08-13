/** Interactive Core 的共享契约：intent、result、snapshot、catalog 与 Interaction DTO。 */

import type {
  McpAddParams,
  McpServerStatus,
  ModelProfile,
  ThreadSummary,
} from "@za38/protocol"

import type { CommandMenuItem, SkillMenuItem } from "./commands"
import type { InteractiveRuntime } from "./runtime"
import type { ComposeProjection, InteractiveActivity, ActiveRun, RunProgress, RunSummary, TimelineItem, WorkItemProjection, WorkItemStatus, WorkMode } from "./state"
import type { AgentGateway, Clock, IdGenerator, Scheduler } from "./ports"

export type { ActiveRun, ComposeProjection, InteractiveActivity, InteractiveRuntime, RunProgress, RunSummary, TimelineItem, WorkItemProjection, WorkItemStatus, WorkMode }

/** 审批决定类型，与协议 ApprovalResponse.decision 保持一致。 */
export type ApprovalDecision = "approve_once" | "approve_thread" | "approve_project" | "reject" | "reject_with_feedback"

/** Skill catalog 项：与 Slash 菜单共用的最小领域视图。 */
export type SkillSummary = SkillMenuItem

/** MCP catalog 项：服务端连接的脱敏状态摘要。 */
export type McpServerSummary = McpServerStatus

/** 命令菜单项：已经过 capability、Thread、active Run 和 Interaction 可用性计算。 */
export type InteractiveCommandItem = CommandMenuItem

/** 连接状态区分正常、协议错误与关闭；错误只保留脱敏摘要。 */
export type InteractiveConnectionState =
  | { status: "open" }
  | { status: "protocol-error"; message: string }
  | { status: "closed"; message: string }

/** catalog 的 loadable 形状；单项失败只影响对应 catalog。 */
export type LoadableCatalog<T> =
  | { status: "idle"; items: readonly T[] }
  | { status: "loading"; items: readonly T[] }
  | { status: "ready"; items: readonly T[] }
  | { status: "error"; items: readonly T[]; message: string }

/** 完整问题 schema；adapter 只采集答案，校验留在共享 Controller。 */
export type InteractiveQuestion = {
  id: string
  question: string
  header: string
  body: string
  options: readonly { label: string; value: string; description: string }[]
  multiSelect: boolean
  allowOther: boolean
}

/** 挂起中的反向 Interaction；包含 deadline，adapter 不解释协议细节。 */
export type InteractiveInteraction =
  | {
      type: "approval"
      requestId: string
      description: string
      requests: unknown
      decisions: readonly ApprovalDecision[]
      deadlineAtMs: number
    }
  | {
      type: "question"
      requestId: string
      questions: readonly InteractiveQuestion[]
      deadlineAtMs: number
    }

/** adapter 提交的答案；request_id 由 Controller 用当前 request 组装。 */
export type InteractiveResponse =
  | { kind: "approval"; decision: ApprovalDecision; feedback?: string }
  | { kind: "question"; answers: Record<string, string[]> }

/** 破坏性操作的稳定确认；adapter 通过 confirmation.resolve 回写。 */
export type InteractiveConfirmation = {
  confirmationId: string
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
}

/** MCP 添加的 typed 输入；Slash 文本解析与 Web typed intent 最终调用同一路径。 */
export type InteractiveMcpInput = McpAddParams

/** 表现层必须由宿主完成的依赖副作用。 */
export type PresentationEffect =
  | { type: "present"; target: "threads" | "models" | "skills"; initialQuery?: string }
  | { type: "request-handoff"; threadId: string | null }
  | { type: "request-exit" }

/** 拒绝原因分类：涵盖从繁忙、缺少能力到通信与校验错误的稳定错误码。 */
export type RejectionCode =
  | "busy"
  | "connection-closed"
  | "stale-interaction"
  | "capability-missing"
  | "not-found"
  | "invalid-argument"
  | "agent-error"

/** Interactive Core 执行任何 Intent 的确定性处理结果。 */
export type IntentOutcome =
  | { status: "accepted"; effects?: readonly PresentationEffect[] }
  | { status: "rejected"; code: RejectionCode; message: string }

/** 兼容别名（暂保留） */
export type InteractiveResult = PresentationEffect

/** 表现层唯一的输入入口；不携带选中行、DOM event 或 OpenTUI key。 */
export type InteractiveIntent =
  | { type: "input.submit"; value: string }
  | { type: "command.execute"; commandId: string; argument?: string }
  | { type: "run.cancel" }
  | { type: "catalog.refresh"; catalog: "threads" | "models" | "skills" | "mcp" }
  | { type: "thread.open"; threadId: string }
  | { type: "model.select"; profileId: string }
  | { type: "skill.arm"; skillId: string }
  | { type: "skill.clear" }
  | { type: "skill.set-enabled"; skillId: string; enabled: boolean }
  | { type: "mcp.add"; input: InteractiveMcpInput }
  | { type: "mcp.remove"; name: string }
  | { type: "interaction.respond"; requestId: string; response: InteractiveResponse }
  | { type: "confirmation.resolve"; confirmationId: string; confirmed: boolean }
  | { type: "approval-mode.cycle" }
  | { type: "work-mode.cycle" }

/** 两个 adapter 都需要的领域事实；不包含终端尺寸、DOM、颜色或组件状态。 */
export type InteractiveSnapshot = {
  readonly currentThreadId: string | null
  readonly activity: InteractiveActivity
  readonly activeRun: ActiveRun | null
  readonly timeline: readonly TimelineItem[]
  readonly runProgress: RunProgress | null
  readonly interaction: InteractiveInteraction | null
  readonly confirmation: InteractiveConfirmation | null
  readonly lastRun: RunSummary | null
  readonly runtime: InteractiveRuntime
  readonly connection: InteractiveConnectionState
  readonly commands: readonly InteractiveCommandItem[]
  readonly catalogs: {
    readonly threads: LoadableCatalog<ThreadSummary>
    readonly models: LoadableCatalog<ModelProfile>
    readonly skills: LoadableCatalog<SkillSummary>
    readonly mcp: LoadableCatalog<McpServerSummary>
  }
  readonly selection: {
    readonly requestedModelProfileId: string | null
    readonly actualModel: ModelProfile | null
    readonly armedSkill: SkillSummary | null
  }
  /** 当前 Thread 下一次 Run 的工作模式；与 Approval Mode 分字段保存。 */
  readonly workMode: WorkMode
  /** 当前 active Run 的 Compose 投影；null 表示非 Compose 或未开始。 */
  readonly composeState: ComposeProjection | null
  /** 当前 Thread 的持久 Work Item 投影；null 表示无未终结项或 Build Thread。 */
  readonly workItem: WorkItemProjection | null
  /** Thread 首条有效消息后冻结的持久工作模式；未冻结为 null。 */
  readonly threadMode: WorkMode | null
}

/** Interactive Core 的唯一业务入口；实现细节不泄漏 React、DOM 或 transport。 */
export interface InteractiveController {
  /** 同步返回最近一次发布的不可变 snapshot。 */
  getSnapshot(): InteractiveSnapshot
  /** 订阅 snapshot 发布；listener 在回调中取消订阅不影响当前发布。 */
  subscribe(listener: (snapshot: InteractiveSnapshot) => void): () => void
  /** 执行一个 intent 及其必要的 Agent effect；返回确定性 IntentOutcome。 */
  dispatch(intent: InteractiveIntent): Promise<IntentOutcome>
  /** 停止接收 intent、使 generation 失效、卸载 Agent listener，但不关闭外层 transport。 */
  close(): Promise<void>
}

/** 兼容导出：可注入的本地定时器。 */
export type InteractiveScheduler = Scheduler

/** 创建 InteractiveController 的依赖与一次性恢复输入。 */
export type InteractiveControllerOptions = {
  gateway?: AgentGateway
  /** 兼容别名属性：AgentGateway 传入 */
  agent?: AgentGateway
  runtime?: InteractiveRuntime
  baseRuntime?: InteractiveRuntime
  /** 缺省表示不做启动恢复；显式 null 进入空首页；字符串调用 canonical threads.open。 */
  initialThreadId?: string | null
  clock?: Clock
  scheduler?: Scheduler
  idGenerator?: IdGenerator
}
