/** Web Interactive Adapter：只拥有浏览器表现状态，通过 WebUiClient 消费共享 Core 视图并提交 intent。 */

import type { ApprovalDecision, InteractiveIntent, InteractiveMcpInput, IntentOutcome, InteractiveSnapshot, InteractiveResponse } from "../../interactive/types"
import type { CommandMenuItem } from "../../interactive/commands"
import type { PresentationState } from "../../presentation-coordinator"
import { filterCommandMenuItems } from "../../presentation-shared"
import type { WebUiClient } from "../ui-client"

/** 每帧合并表现发布的可注入调度器；rAF 不可用时由工厂实现回退到 setTimeout(16)。 */
export type WebFrameScheduler = {
  schedule(task: () => void): void
  cancel(): void
  flush(): void
}

/** Web 表现层使用的语义面板标识；null 表示当前没有主面板。 */
export type WebPanel = "threads" | "models" | "skills" | "mcp" | "status" | "help" | null

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
  /** 当前主面板与各面板局部状态。 */
  readonly activePanel: WebPanel
  readonly panelSearch: Readonly<Record<Exclude<WebPanel, null>, WebPanelSearchState>>
  /** 窄屏抽屉（移动端 sidebar）是否打开。 */
  readonly sidebarOpen: boolean
  /** 已展开的 Tool 卡片集合，按 Tool ID 维护。 */
  readonly expandedTools: ReadonlySet<string>
  /** 当前 requestId 上的 Interaction 草稿；requestId 变化时原子重置。 */
  readonly interactionDraft: WebInteractionDraft | null
  /** 正在执行 returnToTui / requestExit，期间页面只读。 */
  readonly leaving: boolean
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
  | { type: "panel-open"; panel: Exclude<WebPanel, null> }
  | { type: "panel-close" }
  | { type: "panel-search"; panel: Exclude<WebPanel, null>; query: string }
  | { type: "thread-select"; threadId: string }
  | { type: "thread-new" }
  | { type: "thread-refresh" }
  | { type: "model-select"; profileId: string }
  | { type: "skill-arm"; skillId: string }
  | { type: "skill-clear" }
  | { type: "skill-set-enabled"; skillId: string; enabled: boolean }
  | { type: "mcp-add"; input: InteractiveMcpInput }
  | { type: "mcp-remove"; name: string }
  | { type: "interaction-draft-change"; requestId: string; patch: WebInteractionDraftPatch }
  | { type: "interaction-submit"; requestId: string; response: InteractiveResponse }
  | { type: "confirmation-resolve"; confirmationId: string; confirmed: boolean }
  | { type: "tool-toggle"; runId: string; toolId: string }
  | { type: "approval-mode-cycle" }
  | { type: "cancel-run" }
  | { type: "sidebar-toggle"; open: boolean }
  | { type: "theme-set"; theme: WebTheme }
  | { type: "header-menu-toggle"; open: boolean }
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
}

/** Web Interactive Adapter：与 TUI Adapter 形状一致，便于 interface 测试。 */
export interface WebInteractiveAdapter {
  getSnapshot(): WebAdapterSnapshot
  subscribe(listener: (snapshot: WebAdapterSnapshot) => void): () => void
  dispatch(intent: WebIntent): Promise<void>
  close(): Promise<void>
}

type PanelSlot = Exclude<WebPanel, null>

type PanelState = {
  query: string
  submitting: boolean
  error: string | null
}

  /** Adapter 内部可变的整套表现状态；集中存放便于 frame batching 时整体替换。 */
class WebInteractiveAdapterImpl implements WebInteractiveAdapter {
  private readonly client: WebUiClient
  private readonly frameScheduler: WebFrameScheduler
  private readonly listeners = new Set<(snapshot: WebAdapterSnapshot) => void>()
  private readonly unsubscribeState: () => void
  private readonly unsubscribeHandoff: () => void

  private snapshot: WebAdapterSnapshot
  private draft = ""
  private composerSubmittingFlag = false
  private composerErrorStr: string | null = null
  private commandMenuOpenFlag = false
  private commandMenuIndex = 0
  private activePanel: WebPanel = null
  private readonly panelState: Record<PanelSlot, PanelState> = createEmptyPanelState()
  private sidebarOpenFlag = false
  private theme: WebTheme = "light"
  private headerMenuOpenFlag = false
  private expandedTools: Set<string> = new Set()
  private interactionDraft: WebInteractionDraft | null = null
  private leavingFlag = false
  private transientNotice: string | null = null
  private pendingScrollRequest: WebScrollRequest = null
  private webActiveRefreshSent = false
  private closed = false

  constructor(options: WebAdapterOptions) {
    this.client = options.client
    this.frameScheduler = options.frameScheduler ?? createDefaultFrameScheduler()
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
  private refreshSnapshot(): void {
    if (this.closed) return
    this.snapshot = this.buildSnapshot()
  }

  /** 用户意图统一入口；表现层动作留在 Adapter，领域动作通过 InteractiveIntent 转发。 */
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
      case "panel-open":
        this.openPanel(intent.panel)
        return
      case "panel-close":
        this.closePanel()
        return
      case "panel-search":
        this.updatePanelSearch(intent.panel, intent.query)
        return
      case "thread-select":
        await this.selectThread(intent.threadId)
        return
      case "thread-new":
        await this.executeCoreIntent({ type: "command.execute", commandId: "thread.new" })
        return
      case "thread-refresh":
        await this.client.submitIntent({ type: "catalog.refresh", catalog: "threads" })
        return
      case "model-select":
        await this.selectModel(intent.profileId)
        return
      case "skill-arm":
        await this.client.submitIntent({ type: "skill.arm", skillId: intent.skillId })
        return
      case "skill-clear":
        await this.client.submitIntent({ type: "skill.clear" })
        return
      case "skill-set-enabled":
        await this.client.submitIntent({ type: "skill.set-enabled", skillId: intent.skillId, enabled: intent.enabled })
        return
      case "mcp-add":
        await this.client.submitIntent({ type: "mcp.add", input: intent.input })
        return
      case "mcp-remove":
        await this.client.submitIntent({ type: "mcp.remove", name: intent.name })
        return
      case "interaction-draft-change":
        this.updateInteractionDraft(intent.requestId, intent.patch)
        return
      case "interaction-submit":
        await this.submitInteraction(intent.requestId, intent.response)
        return
      case "confirmation-resolve":
        await this.client.submitIntent({ type: "confirmation.resolve", confirmationId: intent.confirmationId, confirmed: intent.confirmed })
        return
      case "tool-toggle":
        this.toggleTool(intent.runId, intent.toolId)
        return
      case "approval-mode-cycle":
        await this.client.submitIntent({ type: "approval-mode.cycle" })
        return
      case "cancel-run":
        await this.client.submitIntent({ type: "run.cancel" })
        return
      case "sidebar-toggle":
        this.sidebarOpenFlag = intent.open
        // 移动端 Thread 抽屉与 Utility 面板互斥：打开抽屉时收起右侧面板。
        if (intent.open) this.activePanel = null
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
      case "exit-harness":
        await this.exitHarness()
    }
  }

  /** 关闭 Adapter 自己的订阅；WebUiClient 由 bootstrap 宿主关闭。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.unsubscribeState()
    this.unsubscribeHandoff()
    this.frameScheduler.cancel()
  }

  /** Web 接管成功后预取 Thread catalog；每个 Adapter 实例只触发一次（含重连时已 web-active）。 */
  private onHandoffState(state: PresentationState): void {
    if (this.webActiveRefreshSent) return
    if (state.phase !== "web-active") return
    this.webActiveRefreshSent = true
    void this.client.submitIntent({ type: "catalog.refresh", catalog: "threads" })
  }

  /** 视图更新（replace/patch 合并后）→ 与本地状态一起重发布。 */
  private onViewUpdate(): void {
    if (this.closed) return
    const previous = this.snapshot.interactive
    const next = this.getInteractive()
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
      activePanel: this.activePanel,
      panelSearch: freezePanelState(this.panelState),
      sidebarOpen: this.sidebarOpenFlag,
      expandedTools: new Set(this.expandedTools),
      interactionDraft: this.interactionDraft ? cloneInteractionDraft(this.interactionDraft) : null,
      leaving: this.leavingFlag,
      transientNotice: this.transientNotice,
      scrollRequest: this.pendingScrollRequest,
      confirmationId: interactive.confirmation?.confirmationId ?? null,
      theme: this.theme,
      headerMenuOpen: this.headerMenuOpenFlag,
    }
  }

  /** 从网关视图缓存重组 InteractiveSnapshot；五个 Selector 分片覆盖全部领域事实。 */
  private getInteractive(): InteractiveSnapshot {
    const view = this.client.getState()
    return {
      currentThreadId: view.conversation.currentThreadId,
      activity: view.conversation.activity,
      activeRun: view.conversation.activeRun,
      timeline: view.conversation.timeline,
      lastRun: view.conversation.lastRun,
      interaction: view.interaction.interaction,
      confirmation: view.interaction.confirmation,
      catalogs: view.navigation.catalogs,
      commands: view.command.commands,
      runtime: view.runtime.runtime,
      connection: view.runtime.connection,
      selection: view.runtime.selection,
    }
  }

  /** draft 变化：只有 `/` 前缀且未进入参数区、未转义时才打开命令菜单。 */
  private updateDraft(value: string): void {
    this.draft = value
    this.composerErrorStr = null
    const query = value.trimStart()
    const shouldShowMenu = query.startsWith("/") && !query.startsWith("//") && !query.slice(1).match(/\s/)
    this.commandMenuOpenFlag = shouldShowMenu
    if (shouldShowMenu) this.commandMenuIndex = 0
    this.publishNow()
  }

  /** 把当前 draft 提交给共享 Core；从 Adapter 当前 draft 读取，不信任外部传入值。 */
  private async submit(): Promise<void> {
    const submittedDraft = this.draft
    const value = submittedDraft.trim()
    const interactive = this.getInteractive()
    if (!value || this.composerSubmittingFlag || this.leavingFlag || interactive.connection.status !== "open" || Boolean(interactive.activeRun)) {
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

  /** 打开某个面板：先重置该面板的局部状态，再 dispatch catalog.refresh。 */
  private openPanel(panel: PanelSlot): void {
    this.activePanel = panel
    this.closeHeaderMenu()
    // 移动端 Utility 抽屉与 Thread 抽屉互斥：打开面板时收起左侧抽屉。
    this.sidebarOpenFlag = false
    this.panelState[panel] = { query: "", submitting: false, error: null }
    this.schedulePublish()
    const catalog = panel === "status" || panel === "help" ? null : panel
    if (catalog === "threads" || catalog === "models" || catalog === "skills" || catalog === "mcp") {
      void this.client.submitIntent({ type: "catalog.refresh", catalog })
    }
  }

  /** 关闭当前面板；保留搜索词以便再次打开时恢复。 */
  private closePanel(): void {
    if (this.activePanel === null) return
    this.activePanel = null
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
  private updatePanelSearch(panel: PanelSlot, query: string): void {
    this.panelState[panel] = { ...this.panelState[panel], query, error: null }
    this.schedulePublish()
  }

  /** 切换 Thread：依赖共享 Core 做 generation 校验；rejected 时保留选择并提示。 */
  private async selectThread(threadId: string): Promise<void> {
    this.activePanel = null
    // 移动端选中 Thread 后自动收起抽屉；桌面端该 flag 始终为 false，无副作用。
    this.sidebarOpenFlag = false
    this.publishNow()
    await this.executeCoreIntent({ type: "thread.open", threadId })
  }

  /** 切换模型：能力门禁与不可用性都交由共享 Core 处理；rejected 不关闭面板并显示错误。 */
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
      this.activePanel = null
      this.panelState.models = { ...this.panelState.models, submitting: false }
      this.publishNow()
    } catch (error) {
      this.panelState.models = { ...this.panelState.models, submitting: false, error: errorMessage(error) }
      this.publishNow()
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
    await this.client.submitIntent({ type: "interaction.respond", requestId, response })
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
          if (effect.target === "threads") this.openPanel("threads")
          else if (effect.target === "models") this.openPanel("models")
          else this.openPanel("skills")
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

function createEmptyPanelState(): Record<PanelSlot, PanelState> {
  return {
    threads: { query: "", submitting: false, error: null },
    models: { query: "", submitting: false, error: null },
    skills: { query: "", submitting: false, error: null },
    mcp: { query: "", submitting: false, error: null },
    status: { query: "", submitting: false, error: null },
    help: { query: "", submitting: false, error: null },
  }
}

function freezePanelState(state: Record<PanelSlot, PanelState>): Readonly<Record<PanelSlot, WebPanelSearchState>> {
  const frozen: Record<PanelSlot, WebPanelSearchState> = {
    threads: { ...state.threads },
    models: { ...state.models },
    skills: { ...state.skills },
    mcp: { ...state.mcp },
    status: { ...state.status },
    help: { ...state.help },
  }
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
