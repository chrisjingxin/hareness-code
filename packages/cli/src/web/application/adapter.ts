/** Web Interactive Adapter：只拥有浏览器表现状态，把用户动作映射为 InteractiveIntent。 */

import type { ApprovalDecision, InteractiveController, InteractiveIntent, InteractiveMcpInput, InteractiveResult, InteractiveSnapshot, InteractiveResponse } from "../../interactive/types"
import { filterCommandMenuItems, type CommandMenuItem } from "../../interactive/commands"
import type { WebHandoffPort } from "../handoff-port"

/** 每帧合并表现发布的可注入调度器；rAF 不可用时由工厂实现回退到 setTimeout(16)。 */
export type WebFrameScheduler = {
  /** 在下一帧安排一次任务；同一帧内多次 schedule 只能真正执行最后一次。 */
  schedule(task: () => void): void
  /** 取消已 schedule 的任务；未调度时必须安全 no-op。 */
  cancel(): void
  /** 强制立即执行已 schedule 或下一帧任务；用于交互/连接/leaving 立即发布。 */
  flush(): void
}

/** Web 表现层使用的语义面板标识；null 表示当前没有主面板。 */
export type WebPanel = "threads" | "models" | "skills" | "mcp" | "status" | "help" | null

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

/** Web Adapter 发布的完整表现快照；领域事实来自共享 Controller。 */
export type WebAdapterSnapshot = {
  /** 共享 InteractiveSnapshot 原样引用，不复制 Timeline reducer。 */
  readonly interactive: InteractiveSnapshot
  /** 当前输入草稿；提交后由 Adapter 自行清空。 */
  readonly draft: string
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
}

/** React / DOM 事件通过这些语义意图驱动 Adapter；不允许携带 DOM event。 */
export type WebIntent =
  | { type: "draft-change"; value: string }
  | { type: "submit"; value: string }
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
  | { type: "tool-toggle"; toolId: string }
  | { type: "cancel-run" }
  | { type: "sidebar-toggle"; open: boolean }
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
  controller: InteractiveController
  handoff: WebHandoffPort
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
  private readonly controller: InteractiveController
  private readonly handoff: WebHandoffPort
  private readonly frameScheduler: WebFrameScheduler
  private readonly listeners = new Set<(snapshot: WebAdapterSnapshot) => void>()
  private readonly unsubscribeInteractive: () => void

  private snapshot: WebAdapterSnapshot
  private draft = ""
  private commandMenuOpenFlag = false
  private commandMenuIndex = 0
  private activePanel: WebPanel = null
  private readonly panelState: Record<PanelSlot, PanelState> = createEmptyPanelState()
  private sidebarOpenFlag = false
  private expandedTools: Set<string> = new Set()
  private interactionDraft: WebInteractionDraft | null = null
  private leavingFlag = false
  private transientNotice: string | null = null
  private pendingScrollRequest: WebScrollRequest = null
  private closed = false

  constructor(options: WebAdapterOptions) {
    this.controller = options.controller
    this.handoff = options.handoff
    this.frameScheduler = options.frameScheduler ?? createDefaultFrameScheduler()
    this.snapshot = this.buildSnapshot()
    this.unsubscribeInteractive = this.controller.subscribe(() => this.onControllerPublish())
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
        await this.submit(intent.value)
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
        await this.controller.dispatch({ type: "command.execute", commandId: "thread.new" })
        return
      case "thread-refresh":
        await this.controller.dispatch({ type: "catalog.refresh", catalog: "threads" })
        return
      case "model-select":
        await this.selectModel(intent.profileId)
        return
      case "skill-arm":
        await this.controller.dispatch({ type: "skill.arm", skillId: intent.skillId })
        return
      case "skill-clear":
        await this.controller.dispatch({ type: "skill.clear" })
        return
      case "skill-set-enabled":
        await this.controller.dispatch({ type: "skill.set-enabled", skillId: intent.skillId, enabled: intent.enabled })
        return
      case "mcp-add":
        await this.controller.dispatch({ type: "mcp.add", input: intent.input })
        return
      case "mcp-remove":
        await this.controller.dispatch({ type: "mcp.remove", name: intent.name })
        return
      case "interaction-draft-change":
        this.updateInteractionDraft(intent.requestId, intent.patch)
        return
      case "interaction-submit":
        await this.submitInteraction(intent.requestId, intent.response)
        return
      case "confirmation-resolve":
        await this.controller.dispatch({ type: "confirmation.resolve", confirmationId: intent.confirmationId, confirmed: intent.confirmed })
        return
      case "tool-toggle":
        this.toggleTool(intent.toolId)
        return
      case "cancel-run":
        await this.controller.dispatch({ type: "run.cancel" })
        return
      case "sidebar-toggle":
        this.sidebarOpenFlag = intent.open
        this.schedulePublish()
        return
      case "return-to-tui":
        await this.returnToTui()
        return
      case "exit-harness":
        await this.exitHarness()
    }
  }

  /** 关闭 Adapter 自己的订阅；Controller 与 handoff port 由宿主关闭。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.unsubscribeInteractive()
    this.frameScheduler.cancel()
  }

  /** 处理 Controller 的 snapshot 发布，按类别决定是批处理还是立即 flush。 */
  private onControllerPublish(): void {
    if (this.closed) return
    const previous = this.snapshot.interactive
    const next = this.controller.getSnapshot()
    // Interaction 变化（包含 requestId 变化）必须立即 flush：草稿原子重置与表单状态都依赖该通知。
    if (previous.interaction?.requestId !== next.interaction?.requestId) {
      this.interactionDraft = null
      this.publishNow()
      return
    }
    if (connectionChanged(previous.connection, next.connection)) {
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
    this.reportThreadIfChanged()
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }

  /** 帧触发时调用：把当前缓存 + 滚动意图推给监听者，并清空滚动意图。 */
  private publishFromFrame(): void {
    if (this.closed) return
    // 缓存可能在 schedule 与 frame 之间被更新过（多次 refreshSnapshot），需要先重读。
    this.snapshot = this.buildSnapshot()
    this.snapshot = { ...this.snapshot, scrollRequest: this.pendingScrollRequest }
    this.pendingScrollRequest = null
    this.reportThreadIfChanged()
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }

  /** 生成对外快照；面板状态以 record 形式发布，调用方按需读取。 */
  private buildSnapshot(): WebAdapterSnapshot {
    const interactive = this.controller.getSnapshot()
    return {
      interactive,
      draft: this.draft,
      commandMenuOpen: this.commandMenuOpenFlag,
      commandMenuIndex: this.commandMenuIndex,
      commandOptions: filterCommandMenuItems(interactive.commands, this.draft),
      activePanel: this.activePanel,
      panelSearch: freezePanelState(this.panelState),
      sidebarOpen: this.sidebarOpenFlag,
      expandedTools: new Set(this.expandedTools),
      interactionDraft: this.interactionDraft ? cloneInteractionDraft(this.interactionDraft) : null,
      leaving: this.leavingFlag,
      transientNotice: this.transientNotice,
      scrollRequest: this.pendingScrollRequest,
      confirmationId: interactive.confirmation?.confirmationId ?? null,
    }
  }

  /** draft 变化：只有 `/` 前缀且未进入参数区、未转义时才打开命令菜单。 */
  private updateDraft(value: string): void {
    this.draft = value
    const query = value.trimStart()
    const shouldShowMenu = query.startsWith("/") && !query.startsWith("//") && !query.slice(1).match(/\s/)
    this.commandMenuOpenFlag = shouldShowMenu
    if (shouldShowMenu) this.commandMenuIndex = 0
    this.schedulePublish()
  }

  /** 把当前 draft 提交给 Controller；只有成功发出 intent 后才清空 draft。 */
  private async submit(rawValue: string): Promise<void> {
    const value = rawValue.trim()
    if (!value) return
    const previousDraft = this.draft
    // 先发出 intent，Controller 统一解释 Slash/转义/未知命令/普通消息；不重写 draft。
    const result = await this.controller.dispatch({ type: "input.submit", value: rawValue })
    await this.handleInteractiveResult(result)
    if (this.closed) return
    // 成功后才清空 draft，避免 Controller 拒绝/通知时用户输入丢失。
    if (this.draft === previousDraft) this.draft = ""
    this.commandMenuOpenFlag = false
    this.commandMenuIndex = 0
    this.pendingScrollRequest = "to-bottom"
    this.publishNow()
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
      await this.controller.dispatch({ type: "skill.arm", skillId: item.skill.id })
      this.draft = ""
      this.commandMenuOpenFlag = false
      this.publishNow()
      return
    }
    if (item.availability.state === "disabled") {
      this.showTransientNotice(`/${item.command.name} 暂不可用：${item.availability.reason}。`)
      return
    }
    const interactive = this.controller.getSnapshot()
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
    this.panelState[panel] = { query: "", submitting: false, error: null }
    this.schedulePublish()
    const catalog = panel === "status" || panel === "help" ? null : panel
    if (catalog === "threads" || catalog === "models" || catalog === "skills" || catalog === "mcp") {
      void this.controller.dispatch({ type: "catalog.refresh", catalog })
    }
  }

  /** 关闭当前面板；保留搜索词以便再次打开时恢复。 */
  private closePanel(): void {
    if (this.activePanel === null) return
    this.activePanel = null
    this.schedulePublish()
  }

  /** 写入面板搜索词；adapter 只记录表现状态，不直接驱动 Controller。 */
  private updatePanelSearch(panel: PanelSlot, query: string): void {
    this.panelState[panel] = { ...this.panelState[panel], query, error: null }
    this.schedulePublish()
  }

  /** 切换 Thread：依赖 Controller 做 generation 校验。 */
  private async selectThread(threadId: string): Promise<void> {
    this.activePanel = null
    this.publishNow()
    await this.controller.dispatch({ type: "thread.open", threadId })
  }

  /** 切换模型：能力门禁与不可用性都交由 Controller 处理。 */
  private async selectModel(profileId: string): Promise<void> {
    this.panelState.models = { ...this.panelState.models, submitting: true, error: null }
    this.publishNow()
    try {
      await this.controller.dispatch({ type: "model.select", profileId })
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
    const interactive = this.controller.getSnapshot()
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

  /** 提交 Interaction 答案：依赖 Controller 校验 requestId 仍有效。 */
  private async submitInteraction(requestId: string, response: InteractiveResponse): Promise<void> {
    const interactive = this.controller.getSnapshot()
    if (interactive.interaction?.requestId !== requestId) {
      this.interactionDraft = null
      this.publishNow()
      return
    }
    if (this.interactionDraft?.requestId === requestId) {
      this.interactionDraft = { ...this.interactionDraft, touched: false }
    }
    this.publishNow()
    await this.controller.dispatch({ type: "interaction.respond", requestId, response })
  }

  /** 折叠/展开单个 Tool 卡片。 */
  private toggleTool(toolId: string): void {
    const next = new Set(this.expandedTools)
    if (next.has(toolId)) next.delete(toolId)
    else next.add(toolId)
    this.expandedTools = next
    this.schedulePublish()
  }

  /** 显示临时通知；adapter 的表现状态，不进入共享 Timeline。 */
  private showTransientNotice(message: string): void {
    this.transientNotice = message
    this.schedulePublish()
  }

  /** 归还控制权：active Run/Interaction 必须在本地阻止，不调用 handoff。 */
  private async returnToTui(): Promise<void> {
    const interactive = this.controller.getSnapshot()
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
      await this.handoff.returnToTui()
    } catch (error) {
      this.leavingFlag = false
      this.showTransientNotice(`返回 TUI 失败：${errorMessage(error)}`)
      this.publishNow()
    }
  }

  /** 退出 Harness：只走 handoff.requestExit，不调用 process.exit。 */
  private async exitHarness(): Promise<void> {
    this.leavingFlag = true
    this.publishNow()
    try {
      await this.handoff.requestExit()
    } catch (error) {
      this.leavingFlag = false
      this.showTransientNotice(`退出失败：${errorMessage(error)}`)
      this.publishNow()
    }
  }

  /** 解释 InteractiveResult：present/request-handoff/request-exit 三类都映射到本地副作用。 */
  private async handleInteractiveResult(result: InteractiveResult | void): Promise<void> {
    if (!result) return
    switch (result.type) {
      case "present":
        if (result.target === "threads") this.openPanel("threads")
        else if (result.target === "models") this.openPanel("models")
        else this.openPanel("skills")
        return
      case "request-handoff":
        // Browser 内嵌套 handoff 是禁止行为；只展示本地通知，不再次调用 WebHandoffPort。
        this.showTransientNotice("当前页面不能再次打开 Web。")
        return
      case "request-exit":
        await this.exitHarness()
    }
  }

  /** 派发 InteractiveIntent 并按 result 决定是否再触发本地副作用。 */
  private async dispatchInteractive(intent: InteractiveIntent): Promise<InteractiveResult | void> {
    const result = await this.controller.dispatch(intent)
    await this.handleInteractiveResult(result)
    return result
  }

  /** Thread 报告只在 currentThreadId 真正变化时调用 handoff.reportThread。 */
  private reportThreadIfChanged(): void {
    const currentThreadId = this.snapshot.interactive.currentThreadId
    this.handoff.reportThread(currentThreadId)
  }
}

/** 创建 Web Interactive Adapter；宿主在 React 首次 commit 后显式调用 handoff.activate。 */
export function createWebInteractiveAdapter(options: WebAdapterOptions): WebInteractiveAdapter {
  return new WebInteractiveAdapterImpl(options)
}

/** 创建默认 frameScheduler：使用 requestAnimationFrame，回退到 setTimeout(16)。 */
export function createDefaultFrameScheduler(): WebFrameScheduler {
  if (typeof globalThis.requestAnimationFrame === "function") {
    return new RafFrameScheduler(globalThis.requestAnimationFrame, globalThis.cancelAnimationFrame)
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
