/** Web Interactive Adapter：只拥有浏览器表现状态，通过 WebUiClient 消费共享 Core 视图并提交 intent。 */

import type { ApprovalDecision, InteractiveApprovalMode, InteractiveIntent, InteractiveMcpInput, IntentOutcome, InteractiveSnapshot, InteractiveResponse } from "../../interactive/types"
import type { CommandMenuItem } from "../../interactive/commands"
import type { PresentationState, WorkspacePreviewView, WorkspaceTreeView } from "../../presentation-coordinator"
import { filterCommandMenuItems } from "../../presentation-shared"
import { fileLanguageId } from "../../workspace/file-language"
import type { WebUiClient } from "../ui-client"

/** 每帧合并表现发布的可注入调度器；rAF 不可用时由工厂实现回退到 setTimeout(16)。 */
export type WebFrameScheduler = {
  schedule(task: () => void): void
  cancel(): void
  flush(): void
}

/** Context Dock 语义面板标识；Code 面板承载文件预览，Help 只从顶栏更多菜单打开。 */
export type ContextDockPanel = "code" | "models" | "skills" | "mcp" | "agents" | "status" | "help"

/** 文件 Tab：路径 + 展示名 + canonical 语言 id（未知为 null）。 */
export type WorkspaceFileTab = {
  readonly path: string
  readonly name: string
  readonly language: string | null
}

/** Web 页面主题：纯表现状态，只属于当前 Web 接管，不持久化、不跟随系统主题。 */
export type WebTheme = "light" | "dark"

/** 面板共用的提交/搜索状态：搜索词、提交中、局部错误都属于表现层。 */
export type WebPanelSearchState = {
  readonly query: string
  readonly submitting: boolean
  readonly error: string | null
}

/** 单一请求的 Interaction 草稿：approval 反馈与 question 全量答案都按 requestId 隔离。 */
export type WebInteractionDraft = {
  /** 与 InteractiveInteraction.requestId 一一对应；变化时整组重置。 */
  readonly requestId: string
  /** approval 反馈文本，仅 reject_with_feedback 时非空。 */
  readonly feedback: string
  /** 当前 approval 选择；未选择时不提交，避免组件自行猜测默认 decision。 */
  readonly approvalDecision?: ApprovalDecision
  /** question answers：每题一组选项值；allowOther 时可携带自由文本。 */
  readonly answers: Record<string, readonly string[]>
  /** requestId 切换之前的旧 requestId 上是否曾填写过草稿，用于诊断性保留。 */
  readonly touched: boolean
}

/** 表现层滚动意图；具体 DOM scroll 位置由 React 维护。 */
export type WebScrollRequest = "new-message" | "to-bottom" | null

/** Web Adapter 发布的完整表现快照；领域事实来自共享 Core 视图（WebUiClient 缓存）。 */
export type WebAdapterSnapshot = {
  /** 由网关视图分片重组得到的 InteractiveSnapshot；与 TUI 看到的同一 Core 状态。 */
  readonly interactive: InteractiveSnapshot
  /** 当前输入草稿；提交后由 Adapter 自行清空。 */
  readonly draft: string
  /** Composer 正在提交中。 */
  readonly composerSubmitting: boolean
  /** Composer 提交或输入错误提示。 */
  readonly composerError: string | null
  /** 命令菜单可见性；`//` 与未知命令均不显示菜单。 */
  readonly commandMenuOpen: boolean
  /** 命令菜单选中索引；菜单不可见时无意义。 */
  readonly commandMenuIndex: number
  /** 命令菜单项；只通过共享 filterCommandMenuItems 计算，availability 不重算。 */
  readonly commandOptions: readonly CommandMenuItem[]
  /** Context Dock：右侧常驻面板，Code 承载文件预览；关闭时保留面板与 Tab 状态。 */
  readonly contextDock: {
    readonly open: boolean
    readonly activePanel: ContextDockPanel
    readonly widthPx: number
    readonly code: {
      readonly tabs: readonly WorkspaceFileTab[]
      readonly activePath: string | null
      /** path → 预览视图；activePath 的预览由网关推送合并进来。 */
      readonly previews: Readonly<Record<string, WorkspacePreviewView>>
      /** path → 预览错误提示：error 时保留旧 ready 内容，错误只进头部。 */
      readonly previewErrors: Readonly<Record<string, string>>
    }
  }
  /** 工作区文件树视图（来自网关分片）。 */
  readonly workspaceTree: WorkspaceTreeView
  /** 左侧 WorkspaceSidebar 的本地表现状态。 */
  readonly workspaceSidebar: {
    /** Thread 分区占侧栏高度的比例；Files 分区 = 1 - ratio。 */
    readonly threadRatio: number
    /** 用户是否拖过 Thread/Files 分隔条；未拖动时分区高度随内容自适应（比例仅作上限），避免默认截断。 */
    readonly threadRatioCustomized: boolean
    /** 文件树当前选中行；null 表示无选中。 */
    readonly selectedPath: string | null
    /** 侧栏宽度（px）；顶栏品牌列与侧栏共用，保证竖线连续对齐。 */
    readonly widthPx: number
  }
  /** 各面板局部状态（搜索词/提交/错误）。 */
  readonly panelSearch: Readonly<Record<ContextDockPanel, WebPanelSearchState>>
  /** 已展开的 Tool 卡片集合，按 Tool ID 维护。 */
  readonly expandedTools: ReadonlySet<string>
  /** 当前 requestId 上的 Interaction 草稿；requestId 变化时原子重置。 */
  readonly interactionDraft: WebInteractionDraft | null
  /** 正在执行 returnToTui / requestExit，期间页面只读。 */
  readonly leaving: boolean
  /** 「新建 Thread」提交中；按钮据此禁用防重复点击。 */
  readonly threadNewSubmitting: boolean
  /** 新建 Thread 成功后自增；Composer 监听变化把焦点还给输入框。 */
  readonly composerFocusRequest: number
  /** 脱敏临时通知；同一通知幂等替换。 */
  readonly transientNotice: string | null
  /** 表现层滚动意图；DOM 实际位置由 presentation 维护。 */
  readonly scrollRequest: WebScrollRequest
  /** 当前确认对话框的稳定 ID；用于 confirmation.resolve。 */
  readonly confirmationId: string | null
  /** 当前页面的主题；每次新 Web 接管固定从 light 开始，不读取系统主题。 */
  readonly theme: WebTheme
  /** 顶栏 overflow menu 是否打开；主题/帮助/返回/退出动作都从这里发起。 */
  readonly headerMenuOpen: boolean
}

/** React / DOM 事件通过这些语义意图驱动 Adapter；不允许携带 DOM event。 */
export type WebIntent =
  | { type: "draft-change"; value: string }
  | { type: "submit" }
  | { type: "composer-error-dismiss" }
  | { type: "command-menu-open" }
  | { type: "command-menu-close" }
  | { type: "command-menu-select"; item: CommandMenuItem }
  | { type: "command-menu-hover"; selectedIndex: number }
  | { type: "dock-open"; panel: ContextDockPanel }
  | { type: "dock-panel-select"; panel: ContextDockPanel }
  | { type: "dock-close" }
  | { type: "dock-width-change"; widthPx: number }
  | { type: "sidebar-thread-ratio-change"; ratio: number }
  | { type: "sidebar-width-change"; widthPx: number }
  | { type: "panel-search"; panel: ContextDockPanel; query: string }
  | { type: "thread-select"; threadId: string }
  | { type: "thread-new" }
  | { type: "thread-refresh" }
  | { type: "model-select"; profileId: string }
  | { type: "models-catalog-refresh" }
  | { type: "skill-arm"; skillId: string }
  | { type: "skill-clear" }
  | { type: "skill-set-enabled"; skillId: string; enabled: boolean }
  | { type: "mcp-add"; input: InteractiveMcpInput }
  | { type: "mcp-remove"; name: string }
  | { type: "workspace-directory-toggle"; path: string }
  | { type: "workspace-file-open"; path: string }
  | { type: "workspace-file-tab-select"; path: string }
  | { type: "workspace-file-tab-close"; path: string }
  | { type: "workspace-refresh" }
  | { type: "workspace-preview-refresh"; path: string }
  | { type: "interaction-draft-change"; requestId: string; patch: WebInteractionDraftPatch }
  | { type: "interaction-submit"; requestId: string; response: InteractiveResponse }
  | { type: "plan-view-close" }
  | { type: "confirmation-resolve"; confirmationId: string; confirmed: boolean }
  | { type: "tool-toggle"; runId: string; toolId: string }
  | { type: "approval-mode-cycle" }
  | { type: "work-mode-cycle" }
  | { type: "approval-mode-select"; mode: InteractiveApprovalMode }
  | { type: "cancel-run" }
  | { type: "notice-dismiss" }
  | { type: "theme-set"; theme: WebTheme }
  | { type: "header-menu-toggle"; open: boolean }
  | { type: "child-timeline-open"; executionId: string }
  | { type: "child-timeline-leave" }
  | { type: "return-to-tui" }
  | { type: "exit-harness" }

/** interaction-draft-change 局部更新：避免把整个 draft 都回传给 Adapter。 */
export type WebInteractionDraftPatch =
  | { kind: "feedback"; value: string }
  | { kind: "approval-decision"; value: ApprovalDecision }
  | { kind: "answer"; questionId: string; values: readonly string[] }
  | { kind: "reset"; requestId: string }

/** Adapter 工厂入参；frameScheduler 默认实现使用 requestAnimationFrame。 */
export type WebAdapterOptions = {
  client: WebUiClient
  frameScheduler?: WebFrameScheduler
  /** run 结束自动刷新的延迟定时器；测试注入手动实现。 */
  setTimeoutFn?: (callback: () => void, ms: number) => unknown
  clearTimeoutFn?: (handle: unknown) => void
  /** 视口宽度来源：侧栏/Dock 拖拽动态夹取的输入；默认读 window.innerWidth，测试注入固定值。 */
  viewportWidth?: () => number
}

/** 默认视口宽度：浏览器读 window.innerWidth；无 DOM 环境（单测未注入时）退回 1600。 */
function defaultViewportWidth(): number {
  return typeof window === "undefined" ? 1600 : window.innerWidth
}

/** Web Interactive Adapter：与 TUI Adapter 形状一致，便于 interface 测试。 */
export interface WebInteractiveAdapter {
  getSnapshot(): WebAdapterSnapshot
  subscribe(listener: (snapshot: WebAdapterSnapshot) => void): () => void
  dispatch(intent: WebIntent): Promise<void>
  close(): Promise<void>
}

type PanelState = {
  query: string
  submitting: boolean
  error: string | null
}

type ContextDockState = {
  open: boolean
  activePanel: ContextDockPanel
  widthPx: number
  code: {
    tabs: readonly WorkspaceFileTab[]
    activePath: string | null
    previews: Readonly<Record<string, WorkspacePreviewView>>
    /** 预览错误提示：error 时保留旧 ready 内容，错误只进头部（设计 14.4）。 */
    previewErrors: Readonly<Record<string, string>>
  }
}

/** 文件 Tab 上限：超出后淘汰最久未使用的非当前 tab。 */
const MAX_FILE_TABS = 12
const DOCK_WIDTH_MIN = 330
const DOCK_WIDTH_MAX = 760
const DOCK_WIDTH_INITIAL = 354
/** 左侧侧栏宽度：控制三栏工作区的第一列，并可由用户拖动调整。 */
const SIDEBAR_WIDTH_MIN = 220
const SIDEBAR_WIDTH_MAX = 480
const SIDEBAR_WIDTH_INITIAL = 296
/** 拖拽夹取的内容列下限：侧栏/Dock 拖拽都不能把中栏内容列压到该值以下（视口太窄时退回静态最小值）。 */
const CONVERSATION_CONTENT_MIN = 480
/** 三栏工作区列间距，与 styles.css 的 --workspace-gap 保持一致。 */
const WORKSPACE_GAP_PX = 16
const THREAD_RATIO_INITIAL = 0.45
/** Thread 分区比例夹取区间：下限保证 Files 可见，上限保证 Thread 可见（CSS 另有 px 级 min）。 */
const THREAD_RATIO_MIN = 0.2
const THREAD_RATIO_MAX = 0.8
/** Run 结束后延迟刷新工作区，等待文件系统写入落定。 */
const RUN_END_REFRESH_DELAY_MS = 200

/** Adapter 内部可变的整套表现状态；集中存放便于 frame batching 时整体替换。 */
class WebInteractiveAdapterImpl implements WebInteractiveAdapter {
  private readonly client: WebUiClient
  private readonly frameScheduler: WebFrameScheduler
  private readonly setTimeoutFn: (callback: () => void, ms: number) => unknown
  private readonly clearTimeoutFn: (handle: unknown) => void
  private readonly viewportWidthFn: () => number
  private readonly listeners = new Set<(snapshot: WebAdapterSnapshot) => void>()
  private readonly unsubscribeState: () => void
  private readonly unsubscribeHandoff: () => void

  private snapshot: WebAdapterSnapshot
  private draft = ""
  private composerSubmittingFlag = false
  private composerErrorStr: string | null = null
  private commandMenuOpenFlag = false
  private commandMenuIndex = 0
  private readonly panelState: Record<ContextDockPanel, PanelState> = createEmptyPanelState()
  private contextDock: ContextDockState = {
    open: false,
    activePanel: "code",
    widthPx: DOCK_WIDTH_INITIAL,
    code: { tabs: [], activePath: null, previews: {}, previewErrors: {} },
  }
  private threadRatio = THREAD_RATIO_INITIAL
  private threadRatioCustomized = false
  private sidebarWidthPx = SIDEBAR_WIDTH_INITIAL
  private workspaceSelectedPath: string | null = null
  private theme: WebTheme = "light"
  private headerMenuOpenFlag = false
  private expandedTools: Set<string> = new Set()
  private interactionDraft: WebInteractionDraft | null = null
  private leavingFlag = false
  private transientNotice: string | null = null
  private pendingScrollRequest: WebScrollRequest = null
  private webActiveRefreshSent = false
  /** 上次观测到的 activeRun.runId；用于检测 run 结束（非空 → null）。 */
  private lastActiveRun: string | null = null
  private runEndTimer: unknown = null
  /** 「新建 Thread」提交中：按钮禁用防重复点击。 */
  private threadNewSubmittingFlag = false
  /** 新建 Thread 成功后自增；Composer 监听变化把焦点还给输入框。 */
  private composerFocusRequestValue = 0
  private closed = false

  constructor(options: WebAdapterOptions) {
    this.client = options.client
    this.frameScheduler = options.frameScheduler ?? createDefaultFrameScheduler()
    this.setTimeoutFn = options.setTimeoutFn ?? ((callback, ms) => setTimeout(callback, ms))
    this.clearTimeoutFn = options.clearTimeoutFn ?? (handle => clearTimeout(handle as ReturnType<typeof setTimeout>))
    this.viewportWidthFn = options.viewportWidth ?? defaultViewportWidth
    this.snapshot = this.buildSnapshot()
    this.unsubscribeState = this.client.subscribeState(() => this.onViewUpdate())
    this.unsubscribeHandoff = this.client.subscribeHandoff(state => this.onHandoffState(state))
    // 重连时 handoff.state(web-active) 可能在 Adapter 创建前就已到达，构造时补一次检查。
    this.onHandoffState(this.client.getHandoffState())
  }

  getSnapshot(): WebAdapterSnapshot {
    return this.snapshot
  }

  subscribe(listener: (snapshot: WebAdapterSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /**
   * 同步刷新内部 snapshot 缓存；保持 getSnapshot() 永远返回最新数据，
   * 但 listener 通知仍走 publishNow / schedulePublish 的批处理。
   */
  /**
   * Dock 拖拽动态上限：视口 − 当前侧栏 − 双侧间距 − 内容列下限；
   * Dock 关闭时内容列也按 Dock 宽度预留居中，所以打开/关闭状态共用同一上限。
   */
  private dockWidthMax(): number {
    const available = this.viewportWidthFn() - this.sidebarWidthPx - 2 * WORKSPACE_GAP_PX - CONVERSATION_CONTENT_MIN
    return Math.max(DOCK_WIDTH_MIN, Math.min(DOCK_WIDTH_MAX, available))
  }

  /** 侧栏拖拽动态上限：无论 Dock 开关都预留 Dock 当前宽度（理由同上）。 */
  private sidebarWidthMax(): number {
    const available = this.viewportWidthFn() - this.contextDock.widthPx - 2 * WORKSPACE_GAP_PX - CONVERSATION_CONTENT_MIN
    return Math.max(SIDEBAR_WIDTH_MIN, Math.min(SIDEBAR_WIDTH_MAX, available))
  }

  private refreshSnapshot(): void {
    if (this.closed) return
    this.snapshot = this.buildSnapshot()
  }

  /** 用户意图统一入口；表现层动作留在 Adapter，领域动作通过 InteractiveIntent / WorkspaceIntent 转发。 */
  async dispatch(intent: WebIntent): Promise<void> {
    if (this.closed) return
    switch (intent.type) {
      case "draft-change":
        this.updateDraft(intent.value)
        return
      case "submit":
        await this.submit()
        return
      case "composer-error-dismiss":
        this.composerErrorStr = null
        this.publishNow()
        return
      case "command-menu-open":
        this.openCommandMenu()
        return
      case "command-menu-close":
        this.closeCommandMenu()
        return
      case "command-menu-select":
        await this.selectCommandMenuItem(intent.item)
        return
      case "command-menu-hover":
        this.commandMenuIndex = intent.selectedIndex
        this.schedulePublish()
        return
      case "dock-open":
        this.dockOpen(intent.panel)
        return
      case "dock-panel-select":
        this.selectDockPanel(intent.panel)
        return
      case "dock-close":
        this.closeDock()
        return
      case "dock-width-change":
        this.contextDock = { ...this.contextDock, widthPx: clamp(intent.widthPx, DOCK_WIDTH_MIN, this.dockWidthMax()) }
        this.schedulePublish()
        return
      case "sidebar-thread-ratio-change":
        this.threadRatio = clamp(intent.ratio, THREAD_RATIO_MIN, THREAD_RATIO_MAX)
        this.threadRatioCustomized = true
        this.schedulePublish()
        return
      case "sidebar-width-change":
        this.sidebarWidthPx = clamp(intent.widthPx, SIDEBAR_WIDTH_MIN, this.sidebarWidthMax())
        this.schedulePublish()
        return
      case "panel-search":
        this.updatePanelSearch(intent.panel, intent.query)
        return
      case "thread-select":
        await this.selectThread(intent.threadId)
        return
      case "thread-new":
        await this.createNewThread()
        return
      case "thread-refresh":
        await this.client.submitIntent({ type: "catalog.refresh", catalog: "threads" })
        return
      case "model-select":
        await this.selectModel(intent.profileId)
        return
      case "models-catalog-refresh":
        // 顶栏模型下拉打开时刷新目录；只拉数据，不改变 Dock 开合与当前面板。
        await this.client.submitIntent({ type: "catalog.refresh", catalog: "models" })
        return
      case "skill-arm":
        await this.executeCoreIntent({ type: "skill.arm", skillId: intent.skillId })
        return
      case "skill-clear":
        await this.executeCoreIntent({ type: "skill.clear" })
        return
      case "skill-set-enabled":
        await this.executeCoreIntent({ type: "skill.set-enabled", skillId: intent.skillId, enabled: intent.enabled })
        return
      case "mcp-add":
        await this.executeCoreIntent(
          { type: "mcp.add", input: intent.input },
          {
            onRejected: message => {
              this.panelState.mcp = { ...this.panelState.mcp, error: message }
              this.publishNow()
            },
          },
        )
        return
      case "mcp-remove":
        await this.executeCoreIntent({ type: "mcp.remove", name: intent.name })
        return
      case "workspace-directory-toggle":
        this.workspaceSelectedPath = intent.path
        this.schedulePublish()
        void this.client.workspaceIntent({ type: "workspace.toggle-directory", path: intent.path })
        return
      case "workspace-file-open":
        this.openFileTab(intent.path)
        return
      case "workspace-file-tab-select":
        this.selectFileTab(intent.path)
        return
      case "workspace-file-tab-close":
        this.closeFileTab(intent.path)
        return
      case "workspace-refresh":
        void this.client.workspaceIntent({ type: "workspace.refresh" })
        return
      case "workspace-preview-refresh":
        void this.client.workspaceIntent({ type: "workspace.refresh-preview", path: intent.path })
        return
      case "interaction-draft-change":
        this.updateInteractionDraft(intent.requestId, intent.patch)
        return
      case "interaction-submit":
        await this.submitInteraction(intent.requestId, intent.response)
        return
      case "plan-view-close":
        await this.executeCoreIntent({ type: "plan-view.close" })
        return
      case "confirmation-resolve":
        await this.executeCoreIntent({ type: "confirmation.resolve", confirmationId: intent.confirmationId, confirmed: intent.confirmed })
        return
      case "tool-toggle":
        this.toggleTool(intent.runId, intent.toolId)
        return
      case "approval-mode-cycle":
        await this.executeCoreIntent({ type: "approval-mode.cycle" })
        return
      case "work-mode-cycle":
        await this.executeCoreIntent({ type: "work-mode.cycle" })
        return
      case "approval-mode-select":
        await this.executeCoreIntent({ type: "approval-mode.set", mode: intent.mode })
        return
      case "cancel-run":
        await this.executeCoreIntent({ type: "run.cancel" })
        return
      case "notice-dismiss":
        this.transientNotice = null
        this.schedulePublish()
        return
      case "theme-set":
        this.setTheme(intent.theme)
        return
      case "header-menu-toggle":
        this.setHeaderMenuOpen(intent.open)
        return
      case "return-to-tui":
        await this.returnToTui()
        return
      case "child-timeline-open":
        await this.dispatchInteractive({ type: "child-timeline.open", executionId: intent.executionId })
        return
      case "child-timeline-leave":
        await this.dispatchInteractive({ type: "child-timeline.leave" })
        return
      case "exit-harness":
        await this.exitHarness()
    }
  }

  /** 关闭 Adapter 自己的订阅；WebUiClient 由 bootstrap 宿主关闭。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    if (this.runEndTimer !== null) {
      this.clearTimeoutFn(this.runEndTimer)
      this.runEndTimer = null
    }
    this.unsubscribeState()
    this.unsubscribeHandoff()
    this.frameScheduler.cancel()
  }

  /** Web 接管成功后预取 Thread catalog 与工作区文件树；每个 Adapter 实例只触发一次（含重连时已 web-active）。 */
  private onHandoffState(state: PresentationState): void {
    if (this.webActiveRefreshSent) return
    if (state.phase !== "web-active") return
    this.webActiveRefreshSent = true
    void this.client.submitIntent({ type: "catalog.refresh", catalog: "threads" })
    // 首次加载文件树：不预取则左侧 Files 保持 idle 空态，必须手动刷新才出现文件。
    void this.client.workspaceIntent({ type: "workspace.load" })
  }

  /** 视图更新（replace/patch 合并后）→ 合并工作区预览、检测 run 结束 → 与本地状态一起重发布。 */
  private onViewUpdate(): void {
    if (this.closed) return
    const previous = this.snapshot.interactive
    const next = this.getInteractive()
    this.mergeWorkspacePreview()
    this.detectRunEnd(next)
    if (previous.activity.kind !== "compacting" && next.activity.kind === "compacting") {
      this.resetDraftState()
      this.publishNow()
      return
    }
    // Interaction 变化（包含 requestId 变化）必须立即 flush：草稿原子重置与表单状态都依赖该通知。
    if (previous.interaction?.requestId !== next.interaction?.requestId) {
      this.interactionDraft = null
      this.publishNow()
      return
    }
    if (connectionChanged(previous.connection, next.connection)) {
      this.closeHeaderMenu()
      this.publishNow()
      return
    }
    this.schedulePublish()
  }

  /** 把网关推送的 workspacePreview 合并进 Code 面板；只接受当前 activePath 的结果（设计 14.4）。 */
  private mergeWorkspacePreview(): void {
    const preview = this.client.getState().workspacePreview
    if (preview.status === "idle") return
    const previewPath = preview.status === "ready" ? preview.file.path : preview.path
    const code = this.contextDock.code
    if (code.activePath === null || previewPath !== code.activePath) {
      // 旧请求的晚到结果不切回旧文件：直接丢弃，Tab 重新激活时会再触发读取。
      return
    }
    const existing = code.previews[previewPath]
    const previewErrors = { ...code.previewErrors }
    if (preview.status === "loading" && existing?.status === "ready") {
      // 刷新在飞：保留旧 ready 内容，loading 不覆盖（explorer 每次结果前必推 loading）。
      return
    }
    if (preview.status === "error" && existing?.status === "ready") {
      // 保留旧内容：错误只进头部提示，刷新入口由头部按钮承担（设计 14.4）。
      previewErrors[previewPath] = preview.message
      this.contextDock = { ...this.contextDock, code: { ...code, previewErrors } }
      return
    }
    if (preview.status === "ready") delete previewErrors[previewPath]
    const previews = { ...code.previews, [previewPath]: preview }
    this.contextDock = { ...this.contextDock, code: { ...code, tabs: code.tabs, activePath: code.activePath, previews, previewErrors } }
  }

  /** 检测 activeRun 非空 → null：延迟 200ms 后刷新文件树与当前预览（设计 15）。 */
  private detectRunEnd(next: InteractiveSnapshot): void {
    const runId = next.activeRun ? next.activeRun.runId : null
    if (this.lastActiveRun !== null && runId === null) {
      this.scheduleRunEndRefresh()
    }
    this.lastActiveRun = runId
  }

  private scheduleRunEndRefresh(): void {
    if (this.runEndTimer !== null) return
    this.runEndTimer = this.setTimeoutFn(() => {
      this.runEndTimer = null
      if (this.closed) return
      // run 结束：文件树、当前预览与 Thread 列表一起刷新（消息数/时间戳已变化）。
      void this.client.workspaceIntent({ type: "workspace.refresh" })
      void this.client.submitIntent({ type: "catalog.refresh", catalog: "threads" })
      const activePath = this.contextDock.code.activePath
      if (activePath !== null) {
        void this.client.workspaceIntent({ type: "workspace.refresh-preview", path: activePath })
      }
    }, RUN_END_REFRESH_DELAY_MS)
  }

  /** 同步刷新缓存并安排一帧后通知监听者；同一帧内多次调用只发一次。 */
  private schedulePublish(): void {
    if (this.closed) return
    this.refreshSnapshot()
    this.frameScheduler.schedule(() => this.publishFromFrame())
  }

  /** 取消任何已 schedule 的通知，立即把当前状态推给所有监听者。 */
  private publishNow(): void {
    if (this.closed) return
    this.frameScheduler.cancel()
    this.snapshot = this.buildSnapshot()
    this.snapshot = { ...this.snapshot, scrollRequest: this.pendingScrollRequest }
    this.pendingScrollRequest = null
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }

  /** 帧触发时调用：把当前缓存 + 滚动意图推给监听者，并清空滚动意图。 */
  private publishFromFrame(): void {
    if (this.closed) return
    // 缓存可能在 schedule 与 frame 之间被更新过（多次 refreshSnapshot），需要先重读。
    this.snapshot = this.buildSnapshot()
    this.snapshot = { ...this.snapshot, scrollRequest: this.pendingScrollRequest }
    this.pendingScrollRequest = null
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }

  /** 生成对外快照；面板状态以 record 形式发布，调用方按需读取。 */
  private buildSnapshot(): WebAdapterSnapshot {
    const interactive = this.getInteractive()
    return {
      interactive,
      draft: this.draft,
      composerSubmitting: this.composerSubmittingFlag,
      composerError: this.composerErrorStr,
      commandMenuOpen: this.commandMenuOpenFlag,
      commandMenuIndex: this.commandMenuIndex,
      commandOptions: filterCommandMenuItems(webRenderableCommands(interactive.commands), this.draft),
      contextDock: {
        open: this.contextDock.open,
        activePanel: this.contextDock.activePanel,
        widthPx: this.contextDock.widthPx,
        code: {
          tabs: [...this.contextDock.code.tabs],
          activePath: this.contextDock.code.activePath,
          previews: { ...this.contextDock.code.previews },
          previewErrors: { ...this.contextDock.code.previewErrors },
        },
      },
      workspaceTree: this.client.getState().workspaceTree,
      workspaceSidebar: { threadRatio: this.threadRatio, threadRatioCustomized: this.threadRatioCustomized, selectedPath: this.workspaceSelectedPath, widthPx: this.sidebarWidthPx },
      panelSearch: freezePanelState(this.panelState),
      expandedTools: new Set(this.expandedTools),
      interactionDraft: this.interactionDraft ? cloneInteractionDraft(this.interactionDraft) : null,
      leaving: this.leavingFlag,
      threadNewSubmitting: this.threadNewSubmittingFlag,
      composerFocusRequest: this.composerFocusRequestValue,
      transientNotice: this.transientNotice,
      scrollRequest: this.pendingScrollRequest,
      confirmationId: interactive.confirmation?.confirmationId ?? null,
      theme: this.theme,
      headerMenuOpen: this.headerMenuOpenFlag,
    }
  }

  /** 从网关视图缓存重组 InteractiveSnapshot；六个 Selector 分片覆盖全部领域事实。 */
  private getInteractive(): InteractiveSnapshot {
    const view = this.client.getState()
    return {
      currentThreadId: view.conversation.currentThreadId,
      activity: view.conversation.activity,
      activeRun: view.conversation.activeRun,
      timeline: view.conversation.timeline,
      runProgress: view.conversation.runProgress,
      lastRun: view.conversation.lastRun,
      interaction: view.interaction.interaction,
      confirmation: view.interaction.confirmation,
      catalogs: view.navigation.catalogs,
      commands: view.command.commands,
      runtime: view.runtime.runtime,
      connection: view.runtime.connection,
      selection: view.runtime.selection,
      workMode: view.runtime.workMode,
      composeState: view.runtime.composeState,
      workItem: view.workItem.workItem,
      threadMode: view.workItem.threadMode,
      childTimelineExecutionId: view.conversation.childTimelineExecutionId,
      isReverted: false,
      revertedTurnId: null,
    }
  }

  /** draft 变化：只有 `/` 前缀且未进入参数区、未转义时才打开命令菜单。 */
  private updateDraft(value: string): void {
    if (this.getInteractive().activity.kind === "compacting") return
    this.draft = value
    this.composerErrorStr = null
    const query = value.trimStart()
    const shouldShowMenu = query.startsWith("/") && !query.startsWith("//") && !query.slice(1).match(/\s/)
    this.commandMenuOpenFlag = shouldShowMenu
    if (shouldShowMenu) this.commandMenuIndex = 0
    this.publishNow()
  }

  /** 操作进入等待态时清除旧输入，避免已提交的 Slash 命令继续停留在 Composer。 */
  private resetDraftState(): void {
    this.draft = ""
    this.commandMenuOpenFlag = false
    this.commandMenuIndex = 0
    this.composerErrorStr = null
  }

  /** 把当前 draft 提交给共享 Core；从 Adapter 当前 draft 读取，不信任外部传入值。 */
  private async submit(): Promise<void> {
    const submittedDraft = this.draft
    const value = submittedDraft.trim()
    const interactive = this.getInteractive()
    if (!value || this.composerSubmittingFlag || this.leavingFlag || interactive.connection.status !== "open" || Boolean(interactive.activeRun) || interactive.activity.kind === "compacting") {
      return
    }
    this.composerSubmittingFlag = true
    this.composerErrorStr = null
    this.publishNow()

    try {
      const outcome = await this.client.submitIntent({ type: "input.submit", value: submittedDraft })
      await this.handleInteractiveResult(outcome)
      if (this.closed) return
      if (outcome.status === "accepted") {
        if (this.draft === submittedDraft) {
          this.draft = ""
        }
        this.commandMenuOpenFlag = false
        this.commandMenuIndex = 0
        this.pendingScrollRequest = "to-bottom"
      } else {
        this.composerErrorStr = outcome.message
      }
    } catch (error) {
      if (this.closed) return
      this.composerErrorStr = errorMessage(error)
    } finally {
      if (!this.closed) {
        this.composerSubmittingFlag = false
        this.publishNow()
      }
    }
  }

  /** 打开命令菜单并保留当前输入语义；与 TUI Adapter 行为保持一致。 */
  private openCommandMenu(): void {
    if (this.getInteractive().activity.kind === "compacting") return
    const value = this.draft.trimStart()
    if (!value.startsWith("/") || value.slice(1).match(/\s/)) this.updateDraft("/")
    this.commandMenuOpenFlag = true
    this.commandMenuIndex = 0
    this.schedulePublish()
  }

  /** 关闭命令菜单并保留 draft；TUI 的 dismissedValue 在 Web 上不必要。 */
  private closeCommandMenu(): void {
    this.commandMenuOpenFlag = false
    this.schedulePublish()
  }

  /** 命令/Skill 选中：按 menu item 类型分别处理，命令通过 command.execute 转发。 */
  private async selectCommandMenuItem(item: CommandMenuItem): Promise<void> {
    if (this.getInteractive().activity.kind === "compacting") {
      this.commandMenuOpenFlag = false
      this.showTransientNotice("上下文正在压缩；完成前不能选择新命令或 Skill。")
      return
    }
    if (item.kind === "skill") {
      await this.client.submitIntent({ type: "skill.arm", skillId: item.skill.id })
      this.draft = ""
      this.commandMenuOpenFlag = false
      this.publishNow()
      return
    }
    if (item.availability.state === "disabled") {
      this.showTransientNotice(`/${item.command.name} 暂不可用：${item.availability.reason}。`)
      return
    }
    const interactive = this.getInteractive()
    if (interactive.activeRun) {
      // active run 中命令直接走共享 dispatcher，结果由 handleInteractiveResult 解释。
      this.draft = ""
      this.commandMenuOpenFlag = false
      this.publishNow()
      await this.dispatchInteractive({ type: "command.execute", commandId: item.command.id })
      return
    }
    this.draft = `/${item.command.name}`
    this.commandMenuOpenFlag = false
    this.publishNow()
  }

  /** 打开 Dock 到指定面板；打开 models/skills/mcp 时触发对应 catalog.refresh。 */
  private dockOpen(panel: ContextDockPanel): void {
    this.contextDock = { ...this.contextDock, open: true, activePanel: panel }
    this.closeHeaderMenu()
    this.schedulePublish()
    this.refreshDockCatalog(panel)
  }

  /** 在已打开的 Dock 内切换面板；不改变 open（已开则仅切面板）。 */
  private selectDockPanel(panel: ContextDockPanel): void {
    this.contextDock = { ...this.contextDock, activePanel: panel }
    this.schedulePublish()
    this.refreshDockCatalog(panel)
  }

  /** Code/Status/Help 没有可刷新的 catalog；models/skills/mcp/agents 打开时刷新。 */
  private refreshDockCatalog(panel: ContextDockPanel): void {
    if (panel === "models" || panel === "skills" || panel === "mcp" || panel === "agents") {
      void this.client.submitIntent({ type: "catalog.refresh", catalog: panel })
    }
  }

  /** 关闭 Dock；保留 activePanel，重新打开时恢复上次面板（设计 4.2）。 */
  private closeDock(): void {
    if (!this.contextDock.open) return
    this.contextDock = { ...this.contextDock, open: false }
    this.schedulePublish()
  }

  /** 显式设置主题；与当前主题相同时不重复发布，设置后总是先关闭 header menu。 */
  private setTheme(theme: WebTheme): void {
    if (this.theme === theme) return
    this.theme = theme
    this.closeHeaderMenu()
    this.schedulePublish()
  }

  /** 打开/关闭顶栏 overflow menu；菜单状态属于本次接管的纯表现状态。 */
  private setHeaderMenuOpen(open: boolean): void {
    if (this.headerMenuOpenFlag === open) return
    this.headerMenuOpenFlag = open
    this.schedulePublish()
  }

  /** 菜单关闭规则：选择主题、打开面板、开始 leaving 或连接变化时统一关闭。 */
  private closeHeaderMenu(): void {
    this.setHeaderMenuOpen(false)
  }

  /** 写入面板搜索词；adapter 只记录表现状态，不直接驱动 Core。 */
  private updatePanelSearch(panel: ContextDockPanel, query: string): void {
    this.panelState[panel] = { ...this.panelState[panel], query, error: null }
    this.schedulePublish()
  }

  /** 切换 Thread：依赖共享 Core 做 generation 校验；rejected 时保留选择并提示。 */
  private async selectThread(threadId: string): Promise<void> {
    this.publishNow()
    await this.executeCoreIntent({ type: "thread.open", threadId })
  }

  /**
   * 新建 Thread：提交期间禁用按钮防重复；成功后清空 Web 本地 Thread 级表现状态，
   * 并递增 composerFocusRequest 让 Composer 把焦点还给输入框。领域语义（clear-thread /
   * 保留全局 Catalog）由共享 Core 的 beginNewThread 执行，这里只做本地表现收尾。
   */
  private async createNewThread(): Promise<void> {
    if (this.threadNewSubmittingFlag) return
    this.threadNewSubmittingFlag = true
    this.publishNow()
    const outcome = await this.executeCoreIntent({ type: "command.execute", commandId: "thread.new" })
    this.threadNewSubmittingFlag = false
    if (outcome.status === "accepted") {
      this.draft = ""
      this.composerErrorStr = null
      this.commandMenuOpenFlag = false
      this.interactionDraft = null
      this.expandedTools = new Set()
      this.composerFocusRequestValue += 1
    }
    this.publishNow()
  }

  /** 切换模型：能力门禁与不可用性都交由共享 Core 处理；rejected 不关闭 Dock 并显示错误。 */
  private async selectModel(profileId: string): Promise<void> {
    this.panelState.models = { ...this.panelState.models, submitting: true, error: null }
    this.publishNow()
    try {
      const outcome = await this.executeCoreIntent(
        { type: "model.select", profileId },
        {
          onRejected: message => {
            this.panelState.models = { ...this.panelState.models, submitting: false, error: message }
            this.publishNow()
          },
        },
      )
      if (outcome.status === "rejected") return
      this.contextDock = { ...this.contextDock, open: false }
      this.panelState.models = { ...this.panelState.models, submitting: false }
      this.publishNow()
    } catch (error) {
      this.panelState.models = { ...this.panelState.models, submitting: false, error: errorMessage(error) }
      this.publishNow()
    }
  }

  /** 构造 loading 预览视图；字面量需显式标注，避免 status 被拓宽为 string。 */
  private static loadingPreview(filePath: string): WorkspacePreviewView {
    return { status: "loading", path: filePath }
  }

  /** 打开文件：新建/激活 Tab → Dock 切到 Code → 触发预览读取（MRU 排序，超出 12 个淘汰最旧）。 */
  private openFileTab(filePath: string): void {
    const code = this.contextDock.code
    const existing = code.tabs.find(tab => tab.path === filePath)
    let tabs: readonly WorkspaceFileTab[]
    let evictedPaths: readonly string[] = []
    if (existing) {
      tabs = [existing, ...code.tabs.filter(tab => tab.path !== filePath)]
    } else {
      tabs = [{ path: filePath, name: fileBasename(filePath), language: fileLanguageId(filePath) }, ...code.tabs]
      if (tabs.length > MAX_FILE_TABS) {
        // 淘汰最久未使用（末尾）：同步清理其预览缓存，避免会话内无界累积。
        evictedPaths = tabs.slice(MAX_FILE_TABS).map(tab => tab.path)
        tabs = tabs.slice(0, MAX_FILE_TABS)
      }
    }
    const previews = { ...code.previews, [filePath]: WebInteractiveAdapterImpl.loadingPreview(filePath) }
    const previewErrors = { ...code.previewErrors }
    delete previewErrors[filePath]
    for (const evictedPath of evictedPaths) {
      delete previews[evictedPath]
      delete previewErrors[evictedPath]
    }
    this.contextDock = {
      ...this.contextDock,
      open: true,
      activePanel: "code",
      code: {
        ...code,
        tabs,
        activePath: filePath,
        previews,
        previewErrors,
      },
    }
    this.workspaceSelectedPath = filePath
    this.schedulePublish()
    void this.client.workspaceIntent({ type: "workspace.preview-file", path: filePath })
  }

  /** 激活已有 Tab；无缓存预览时重新触发读取（Tab 可能因淘汰或关闭而无 previews 条目）。 */
  private selectFileTab(filePath: string): void {
    const code = this.contextDock.code
    const tab = code.tabs.find(candidate => candidate.path === filePath)
    if (!tab) return
    const tabs = [tab, ...code.tabs.filter(candidate => candidate.path !== filePath)]
    const previews = code.previews[filePath]
      ? code.previews
      : { ...code.previews, [filePath]: WebInteractiveAdapterImpl.loadingPreview(filePath) }
    this.contextDock = { ...this.contextDock, code: { ...code, tabs, activePath: filePath, previews } }
    this.workspaceSelectedPath = filePath
    this.schedulePublish()
    if (!code.previews[filePath]) {
      void this.client.workspaceIntent({ type: "workspace.preview-file", path: filePath })
    }
  }

  /** 关闭 Tab：激活相邻（右侧优先，无则左侧）；关最后一个保留 Dock 打开并显示空状态（设计 6.3）。 */
  private closeFileTab(filePath: string): void {
    const code = this.contextDock.code
    const index = code.tabs.findIndex(tab => tab.path === filePath)
    if (index === -1) return
    const tabs = code.tabs.filter(tab => tab.path !== filePath)
    const previews = { ...code.previews }
    delete previews[filePath]
    const previewErrors = { ...code.previewErrors }
    delete previewErrors[filePath]
    let activePath = code.activePath
    if (activePath === filePath) {
      const neighbor = tabs[Math.min(index, tabs.length - 1)] ?? tabs[Math.max(index - 1, 0)] ?? null
      activePath = neighbor?.path ?? null
    }
    this.contextDock = { ...this.contextDock, code: { ...code, tabs, activePath, previews, previewErrors } }
    this.schedulePublish()
    if (activePath !== null && !previews[activePath]) {
      void this.client.workspaceIntent({ type: "workspace.preview-file", path: activePath })
    }
  }

  /** 草稿合并：feedback、answer、reset 都只对当前 requestId 生效。 */
  private updateInteractionDraft(requestId: string, patch: WebInteractionDraftPatch): void {
    const interactive = this.getInteractive()
    if (interactive.interaction?.requestId !== requestId) {
      // 旧 requestId 上的草稿必须原子清空，绝不能跨 requestId 提交。
      if (this.interactionDraft && this.interactionDraft.requestId !== requestId) {
        this.interactionDraft = null
        this.publishNow()
      }
      return
    }
    const current = this.interactionDraft && this.interactionDraft.requestId === requestId
      ? this.interactionDraft
      : { requestId, feedback: "", answers: {}, touched: false }
    if (patch.kind === "reset") {
      this.interactionDraft = { requestId, feedback: "", answers: {}, touched: false }
      this.publishNow()
      return
    }
    if (patch.kind === "feedback") {
      this.interactionDraft = { ...current, feedback: patch.value, touched: true }
      this.publishNow()
      return
    }
    if (patch.kind === "approval-decision") {
      this.interactionDraft = { ...current, approvalDecision: patch.value, touched: true }
      this.publishNow()
      return
    }
    this.interactionDraft = {
      ...current,
      answers: { ...current.answers, [patch.questionId]: patch.values.slice() },
      touched: true,
    }
    this.publishNow()
  }

  /** 提交 Interaction 答案：依赖共享 Core 校验 requestId 仍有效。 */
  private async submitInteraction(requestId: string, response: InteractiveResponse): Promise<void> {
    const interactive = this.getInteractive()
    if (interactive.interaction?.requestId !== requestId) {
      this.interactionDraft = null
      this.publishNow()
      return
    }
    if (this.interactionDraft?.requestId === requestId) {
      this.interactionDraft = { ...this.interactionDraft, touched: false }
    }
    this.publishNow()
    await this.executeCoreIntent({ type: "interaction.respond", requestId, response })
  }

  /** 折叠/展开单个 Tool 卡片；展开状态只属于表现层，使用 runId+toolId 复合键。 */
  private toggleTool(runId: string, toolId: string): void {
    const key = toolKey(runId, toolId)
    const next = new Set(this.expandedTools)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    this.expandedTools = next
    this.schedulePublish()
  }

  /** 显示临时通知；adapter 的表现状态，不进入共享 Timeline。 */
  private showTransientNotice(message: string): void {
    this.transientNotice = message
    this.schedulePublish()
  }

  /** 归还控制权：active Run/Interaction 必须在本地阻止，再发送 handoff.return。 */
  private async returnToTui(): Promise<void> {
    this.closeHeaderMenu()
    const interactive = this.getInteractive()
    if (interactive.activeRun) {
      this.showTransientNotice("当前任务结束或交互完成后可返回 TUI。")
      return
    }
    if (interactive.interaction) {
      this.showTransientNotice("请先完成当前审批或问题，再返回 TUI。")
      return
    }
    this.leavingFlag = true
    this.publishNow()
    try {
      this.client.returnToTui()
    } catch (error) {
      this.leavingFlag = false
      this.showTransientNotice(`返回 TUI 失败：${errorMessage(error)}`)
      this.publishNow()
    }
  }

  /** 退出 Harness：只发送 handoff.exit，不调用 process.exit。 */
  private async exitHarness(): Promise<void> {
    this.closeHeaderMenu()
    this.leavingFlag = true
    this.publishNow()
    try {
      this.client.requestExit()
    } catch (error) {
      this.leavingFlag = false
      this.showTransientNotice(`退出失败：${errorMessage(error)}`)
      this.publishNow()
    }
  }

  /**
   * 统一业务意图执行器：submitIntent 恒 resolve（rejected 也是 outcome），
   * 因此所有业务动作必须显式检查 outcome，不能依赖 try/catch。
   * rejected 默认显示 transient notice；需要面板内错误时可注入 onRejected。
   */
  private async executeCoreIntent(
    intent: InteractiveIntent,
    options: { onRejected?: (message: string) => void } = {},
  ): Promise<IntentOutcome> {
    const outcome = await this.client.submitIntent(intent)
    if (outcome.status === "rejected") {
      if (options.onRejected) options.onRejected(outcome.message)
      else this.showTransientNotice(outcome.message)
    }
    return outcome
  }

  /** 解释 IntentOutcome：根据 PresentationEffect 执行本地呈现层副作用。 */
  private async handleInteractiveResult(outcome: IntentOutcome): Promise<void> {
    if (outcome.status === "rejected") {
      this.showTransientNotice(outcome.message)
      return
    }
    if (!outcome.effects) return
    for (const effect of outcome.effects) {
      switch (effect.type) {
        case "present":
          if (effect.target === "models") this.dockOpen("models")
          else if (effect.target === "skills") this.dockOpen("skills")
          else if (effect.target === "agents") this.dockOpen("agents")
          else if (effect.target === "status") this.dockOpen("status")
          // threads：Thread 常驻左侧栏，无对应 Dock 面板可开。
          break
        case "request-handoff":
          this.showTransientNotice("当前页面不能再次打开 Web。")
          break
        case "request-exit":
          await this.exitHarness()
          break
      }
    }
  }

  /** 派发 InteractiveIntent 并按 outcome 决定是否再触发本地副作用。 */
  private async dispatchInteractive(intent: InteractiveIntent): Promise<IntentOutcome> {
    const outcome = await this.client.submitIntent(intent)
    await this.handleInteractiveResult(outcome)
    return outcome
  }
}

/** 创建 Web Interactive Adapter；bootstrap 在收到首帧 state.replace 后创建。 */
export function createWebInteractiveAdapter(options: WebAdapterOptions): WebInteractiveAdapter {
  return new WebInteractiveAdapterImpl(options)
}

/** Tool 展开状态的复合键：runId + toolId，跨 Run 相同 toolId 不冲突。 */
export function toolKey(runId: string, toolId: string): string {
  return `${runId}:${toolId}`
}

/** 创建默认 frameScheduler：使用 requestAnimationFrame，回退到 setTimeout(16)。 */
export function createDefaultFrameScheduler(): WebFrameScheduler {
  if (typeof globalThis.requestAnimationFrame === "function") {
    // 直接传函数引用会在调用时丢失 receiver（window 上的 rAF/cAF 是宿主方法），
    // 触发 "Illegal invocation" 导致 schedulePublish 静默失败；必须用箭头包装保持 this。
    return new RafFrameScheduler(
      callback => globalThis.requestAnimationFrame(callback),
      handle => globalThis.cancelAnimationFrame(handle),
    )
  }
  return new TimeoutFrameScheduler(16)
}

class RafFrameScheduler implements WebFrameScheduler {
  private readonly raf: (cb: FrameRequestCallback) => number
  private readonly caf: (handle: number) => void
  private handle: number | null = null
  private pending: (() => void) | null = null

  constructor(raf: (cb: FrameRequestCallback) => number, caf: (handle: number) => void) {
    this.raf = raf
    this.caf = caf
  }

  schedule(task: () => void): void {
    this.pending = task
    if (this.handle !== null) return
    this.handle = this.raf(() => {
      const task = this.pending
      this.handle = null
      this.pending = null
      if (task) task()
    })
  }

  cancel(): void {
    if (this.handle !== null) {
      this.caf(this.handle)
      this.handle = null
    }
    this.pending = null
  }

  flush(): void {
    if (this.handle !== null) {
      this.caf(this.handle)
      this.handle = null
    }
    const task = this.pending
    this.pending = null
    if (task) task()
  }
}

class TimeoutFrameScheduler implements WebFrameScheduler {
  private readonly intervalMs: number
  private readonly setTimeoutFn: (cb: () => void, ms: number) => unknown
  private readonly clearTimeoutFn: (handle: unknown) => void
  private handle: unknown = null
  private pending: (() => void) | null = null

  constructor(intervalMs: number, setTimeoutFn?: (cb: () => void, ms: number) => unknown, clearTimeoutFn?: (handle: unknown) => void) {
    this.intervalMs = intervalMs
    this.setTimeoutFn = setTimeoutFn ?? ((cb, ms) => setTimeout(cb, ms))
    this.clearTimeoutFn = clearTimeoutFn ?? (handle => clearTimeout(handle as ReturnType<typeof setTimeout>))
  }

  schedule(task: () => void): void {
    this.pending = task
    if (this.handle !== null) return
    this.handle = this.setTimeoutFn(() => {
      const task = this.pending
      this.handle = null
      this.pending = null
      if (task) task()
    }, this.intervalMs)
  }

  cancel(): void {
    if (this.handle !== null) {
      this.clearTimeoutFn(this.handle)
      this.handle = null
    }
    this.pending = null
  }

  flush(): void {
    if (this.handle !== null) {
      this.clearTimeoutFn(this.handle)
      this.handle = null
    }
    const task = this.pending
    this.pending = null
    if (task) task()
  }
}

/** 比较 connection 字段，决定是否必须立即 flush。 */
function connectionChanged(previous: InteractiveSnapshot["connection"], next: InteractiveSnapshot["connection"]): boolean {
  if (previous.status !== next.status) return true
  if (previous.status !== "open" && next.status !== "open") {
    return previous.message !== next.message
  }
  return false
}

function createEmptyPanelState(): Record<ContextDockPanel, PanelState> {
  return {
    code: { query: "", submitting: false, error: null },
    models: { query: "", submitting: false, error: null },
    skills: { query: "", submitting: false, error: null },
    mcp: { query: "", submitting: false, error: null },
    agents: { query: "", submitting: false, error: null },
    status: { query: "", submitting: false, error: null },
    help: { query: "", submitting: false, error: null },
  }
}

const PANEL_SLOTS: readonly ContextDockPanel[] = ["code", "models", "skills", "mcp", "agents", "status", "help"]

function freezePanelState(state: Record<ContextDockPanel, PanelState>): Readonly<Record<ContextDockPanel, WebPanelSearchState>> {
  const frozen: Record<ContextDockPanel, WebPanelSearchState> = {} as Record<ContextDockPanel, WebPanelSearchState>
  for (const slot of PANEL_SLOTS) frozen[slot] = { ...state[slot] }
  return frozen
}

function cloneInteractionDraft(draft: WebInteractionDraft): WebInteractionDraft {
  const answers: Record<string, readonly string[]> = {}
  for (const [key, values] of Object.entries(draft.answers)) answers[key] = values.slice()
  return {
    requestId: draft.requestId,
    feedback: draft.feedback,
    ...(draft.approvalDecision ? { approvalDecision: draft.approvalDecision } : {}),
    answers,
    touched: draft.touched,
  }
}

/** 命令菜单数据来自共享 snapshot，只按 draft 做显示过滤，不重新计算可用性。 */
function webRenderableCommands(items: readonly CommandMenuItem[]): readonly CommandMenuItem[] {
  // host.web 是 TUI 入口；共享 Core 下 Web 页面不能嵌套接管，从命令菜单隐藏。
  return items.filter(item => item.kind !== "command" || item.command.id !== "host.web")
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function fileBasename(filePath: string): string {
  return filePath.split("/").at(-1) ?? filePath
}
