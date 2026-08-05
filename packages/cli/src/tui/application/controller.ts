/** TUI 工作流 Controller：集中状态转换、命令副作用和 Agent 事件，隔离 React 表现层。 */

import {
  Capability,
  EventType,
  isClientMethod,
  type EventEnvelope,
  type InteractionRequestEnvelope,
  type InteractionResponse,
  type McpAddResult,
  type McpStatusResult,
  type ModelProfile,
  type ApprovalResponse,
  type RequestedSkill,
  type ThreadMessage,
} from "@za38/protocol"

import { AgentClient, JsonRpcRemoteError } from "../../ipc/client"
import {
  createCommandRegistry,
  defaultCommandContext,
  findCommandMenuItems,
  parseSlashCommand,
  resolveSlashCommand,
  unknownCommandNotice,
  type CommandMenuItem,
  type CommandRegistry,
  type SkillMenuItem,
  type SlashCommand,
} from "./commands"
import { dispatchSlashCommand, type CommandResult } from "./command-dispatcher"
import { nextApprovalMode, runtimeStatusSummary, type TuiApprovalMode, type TuiRuntime } from "./model"
import {
  loadPromptHistory,
  movePromptHistory,
  persistPromptHistory,
  rememberPrompt,
  type PromptHistoryCursor,
} from "./prompt-history"
import type { ShortcutAction } from "./shortcuts"
import {
  appendNotice,
  applyAgentEvent,
  applyInteractionRequest,
  clearPendingInteraction,
  clearThread,
  createInitialState,
  finishContextCompaction,
  markCancelling,
  markRunFailed,
  restoreThread,
  startContextCompaction,
  startRun,
  type TuiState,
} from "./state"

/** 审批决定类型，与协议 ApprovalResponse.decision 保持一致。 */
export type ApprovalDecision = ApprovalResponse["decision"]

export type CommandMenuState = {
  visible: boolean
  selectedIndex: number
}

/** 用户从选择器或 Slash 菜单选中的一次性 Skill 上下文。 */
export type SelectedSkill = SkillMenuItem

/** 恢复选择器使用的 thread 摘要；内部 thread_id 绝不直接渲染。 */
export type ThreadPickerItem = {
  threadId: string
  createdAtMs: number
  updatedAtMs: number
  firstMessage: string
  latestMessage: string
  messageCount: number
}

/** 三类业务选择器共用的稳定标识。 */
export type PickerKind = "skills" | "threads" | "models"

/** 选择器向 React 暴露的只读快照；items 已按 query 过滤。 */
export type PickerSnapshot<T> = {
  readonly visible: boolean
  readonly loading: boolean
  readonly query: string
  readonly selectedIndex: number
  readonly error?: string
  readonly syncingDefault?: boolean
  readonly items: readonly T[]
}

/** Controller 向表现层发布的完整 TUI 状态。 */
export type TuiSnapshot = {
  readonly state: TuiState
  readonly runtime: TuiRuntime
  readonly displayedModelName?: string
  readonly draft: string
  readonly draftCursor?: "start" | "end"
  readonly commandMenu: CommandMenuState
  readonly commandOptions: readonly CommandMenuItem[]
  readonly selectedSkill?: SelectedSkill
  readonly skills: PickerSnapshot<SkillMenuItem>
  readonly threads: PickerSnapshot<ThreadPickerItem>
  readonly models: PickerSnapshot<ModelProfile>
  readonly commandDialog?: {
    readonly kind: "confirm-new-thread"
    readonly title: string
    readonly message: string
  }
  readonly modelBindingDialog?: {
    readonly title: string
    readonly message: string
  }
  readonly showToolDetails: boolean
  readonly expandedTools: ReadonlySet<string>
  /** 递增后由 React adapter 滚动到最新内容。 */
  readonly scrollRequest: number
}

/** React、快捷键和鼠标只能通过这些语义意图驱动 Controller。 */
export type TuiIntent =
  | { type: "draft-input"; value: string }
  | { type: "submit"; value: string }
  | { type: "history"; direction: "previous" | "next" }
  | { type: "execute-command"; command: SlashCommand }
  | { type: "shortcut"; action: ShortcutAction }
  | { type: "command-menu-select"; item: CommandMenuItem }
  | { type: "command-menu-hover"; selectedIndex: number }
  | { type: "picker-search"; picker: PickerKind; query: string }
  | { type: "picker-hover"; picker: PickerKind; selectedIndex: number }
  | { type: "picker-select-skill"; skill: SkillMenuItem }
  | { type: "picker-select-thread"; thread: ThreadPickerItem }
  | { type: "picker-select-model"; model: ModelProfile }
  | { type: "picker-close"; picker: PickerKind }
  | { type: "dialog-resolve"; kind: "command" | "model-binding"; confirmed: boolean }
  | { type: "clear-selected-skill" }
  | { type: "approval"; decision: ApprovalDecision }
  | { type: "question"; answer: string }
  | { type: "tool-toggle"; toolId: string }

/** Controller 的最小 external interface；实现细节不泄漏 React 或 transport。 */
export interface TuiController {
  /** 返回最近一次发布的稳定 snapshot。 */
  getSnapshot(): TuiSnapshot
  /** 订阅状态发布，返回卸载函数。 */
  subscribe(listener: (snapshot: TuiSnapshot) => void): () => void
  /** 执行一个用户意图及其必要的 AgentClient effect。 */
  dispatch(intent: TuiIntent): Promise<void>
  /** 接收已经通过 AgentClient 校验的 Agent event。 */
  applyAgentEvent(event: EventEnvelope): void
  /** 清理事件订阅和未完成 Interaction，但不关闭外层持有的 AgentClient。 */
  close(): Promise<void>
}

/** 创建一次 TUI 生命周期对应的 Controller。 */
export type TuiControllerOptions = {
  client: AgentClient
  runtime: TuiRuntime
  resume?: boolean
  promptHistoryFile?: string
  onRequestExit: () => void
  openWeb?: (threadId: string) => Promise<string>
}

type InternalPicker<T> = {
  visible: boolean
  loading: boolean
  query: string
  selectedIndex: number
  error?: string
  syncingDefault?: boolean
}

type PendingInteraction = {
  request: InteractionRequestEnvelope
  resolve: (response: InteractionResponse) => void
}

/** Controller 的具体实现；所有可变状态都集中在这个 module 内。 */
class TuiControllerImpl implements TuiController {
  private readonly client: AgentClient
  private readonly baseRuntime: TuiRuntime
  private readonly commandRegistry: CommandRegistry
  private readonly promptHistoryFile: string | undefined
  private readonly onRequestExit: () => void
  private readonly openWeb?: (threadId: string) => Promise<string>
  private readonly listeners = new Set<(snapshot: TuiSnapshot) => void>()
  private readonly pendingInteractions = new Map<string, PendingInteraction>()
  private readonly pickerEpoch: Record<PickerKind, number> = { skills: 0, threads: 0, models: 0 }
  private readonly eventListener = (event: EventEnvelope) => this.applyAgentEvent(event)
  private readonly protocolErrorListener = (error: Error) => this.commit(current => appendNotice(current, `协议错误：${error.message}`))
  private readonly closeListener = (error: Error) => this.commit(current => appendNotice(current, `Agent 连接已关闭：${error.message}`))
  private readonly clearRequestHandler: () => void

  private state: TuiState
  private snapshot: TuiSnapshot
  private draft = ""
  private draftCursor: "start" | "end" | undefined
  private commandMenu: CommandMenuState = { visible: false, selectedIndex: 0 }
  private commandMenuDismissedValue: string | undefined
  private skills: readonly SkillMenuItem[] = []
  private threads: readonly ThreadPickerItem[] = []
  private models: readonly ModelProfile[] = []
  private skillPicker: InternalPicker<SkillMenuItem> = emptyPicker()
  private threadPicker: InternalPicker<ThreadPickerItem> = emptyPicker()
  private modelPicker: InternalPicker<ModelProfile> = emptyPicker()
  private selectedSkill: SelectedSkill | undefined
  private threadModelSelection: string | undefined
  private approvalModeOverride: TuiApprovalMode | undefined
  private actualModelProfile: ModelProfile | undefined
  private commandDialog: TuiSnapshot["commandDialog"]
  private modelBindingDialog: TuiSnapshot["modelBindingDialog"]
  private showToolDetails = false
  private expandedTools: ReadonlySet<string> = new Set()
  private promptHistory: string[] = []
  private promptHistoryCursor: PromptHistoryCursor | undefined
  private historyApplyValue: string | undefined
  private openingThread = false
  private scrollRequest = 0
  private closed = false

  constructor(options: TuiControllerOptions) {
    this.client = options.client
    this.baseRuntime = options.runtime
    this.commandRegistry = createCommandRegistry(options.runtime.agentCommands)
    this.promptHistoryFile = options.promptHistoryFile
    this.onRequestExit = options.onRequestExit
    this.openWeb = options.openWeb
    this.state = createInitialState()
    this.snapshot = this.buildSnapshot()

    this.client.on("event", this.eventListener)
    this.client.on("protocolError", this.protocolErrorListener)
    this.client.on("close", this.closeListener)
    this.clearRequestHandler = this.client.setRequestHandler(request => this.handleInteractionRequest(request))

    void loadPromptHistory(this.promptHistoryFile).then(history => {
      if (!this.closed) this.promptHistory = history
    })
    void this.refreshSkillCatalog()
    if (options.resume) this.openThreadPicker()
  }

  /** 创建默认的空状态；保留所有公开方法的同步 snapshot 语义。 */
  getSnapshot(): TuiSnapshot {
    return this.snapshot
  }

  /** 注册 snapshot listener；同一 listener 不会重复登记。 */
  subscribe(listener: (snapshot: TuiSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** 执行用户意图；滚动等纯终端动作由 adapter 自己处理。 */
  async dispatch(intent: TuiIntent): Promise<void> {
    if (this.closed) return
    switch (intent.type) {
      case "draft-input":
        if (this.state.pendingOperation) return
        this.updateDraft(intent.value)
        return
      case "submit":
        await this.submit(intent.value)
        return
      case "history":
        this.navigatePromptHistory(intent.direction)
        return
      case "execute-command":
        await this.executeSlashCommand(intent.command)
        return
      case "shortcut":
        await this.handleShortcut(intent.action)
        return
      case "command-menu-select":
        await this.selectCommandMenuItem(intent.item)
        return
      case "command-menu-hover":
        this.commandMenu = { ...this.commandMenu, selectedIndex: intent.selectedIndex }
        this.publish()
        return
      case "picker-search":
        this.updatePickerQuery(intent.picker, intent.query)
        return
      case "picker-hover":
        this.updatePickerIndex(intent.picker, intent.selectedIndex)
        return
      case "picker-select-skill":
        this.selectSkill(intent.skill)
        return
      case "picker-select-thread":
        await this.selectThread(intent.thread)
        return
      case "picker-select-model":
        await this.selectModel(intent.model)
        return
      case "picker-close":
        this.closePicker(intent.picker)
        return
      case "dialog-resolve":
        await this.resolveDialog(intent.kind, intent.confirmed)
        return
      case "clear-selected-skill":
        this.selectedSkill = undefined
        this.publish()
        return
      case "approval":
        this.respondApproval(intent.decision)
        return
      case "question":
        this.respondQuestion(intent.answer)
        return
      case "tool-toggle":
        this.toggleTool(intent.toolId)
    }
  }

  /** 将 Agent event 交给现有 reducer，并同步实际运行模型。 */
  applyAgentEvent(event: EventEnvelope): void {
    if (this.closed) return
    if (event.type === EventType.RUN_STARTED && event.thread_id === this.state.threadId) {
      const actual = modelProfileFromRunStarted(event.payload)
      if (actual) this.actualModelProfile = actual
    }
    if (TERMINAL_EVENT_TYPES.has(event.type)) {
      const requestId = this.state.pendingApproval?.requestId ?? this.state.pendingQuestion?.requestId
      if (requestId) this.settleAbandonedInteraction(requestId)
    }
    this.commit(current => applyAgentEvent(current, event))
  }

  /** 清理 Controller 自己的订阅和 Interaction resolver，不触碰外层 AgentClient。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.pickerEpoch.skills += 1
    this.pickerEpoch.threads += 1
    this.pickerEpoch.models += 1
    this.client.off("event", this.eventListener)
    this.client.off("protocolError", this.protocolErrorListener)
    this.client.off("close", this.closeListener)
    this.clearRequestHandler()
    for (const [requestId, pending] of this.pendingInteractions) {
      this.client.abandonInteraction(requestId)
      pending.resolve(interactionCancellation(pending.request))
    }
    this.pendingInteractions.clear()
  }

  /** 把 state reducer 转换和非 reducer 工作流状态一起发布为新 snapshot。 */
  private commit(transition: (current: TuiState) => TuiState): void {
    if (this.closed) return
    this.state = transition(this.state)
    this.publish()
  }

  /** 生成稳定的只读快照；数组和 Set 均不复用外部可变容器。 */
  private buildSnapshot(): TuiSnapshot {
    const selectedModel = this.models.find(model => model.id === this.threadModelSelection)
    const displayedModel = selectedModel ?? this.actualModelProfile
    const runtime: TuiRuntime = {
      ...(displayedModel
        ? {
            ...this.baseRuntime,
            modelName: displayedModel.model,
            modelProfileId: displayedModel.id,
            modelConfigured: true,
          }
        : { ...this.baseRuntime }),
      approvalMode: this.approvalModeOverride ?? this.baseRuntime.approvalMode,
    }
    return {
      state: this.state,
      runtime,
      displayedModelName: this.actualModelProfile?.model ?? this.threadModelSelection,
      draft: this.draft,
      draftCursor: this.draftCursor,
      commandMenu: { ...this.commandMenu },
      commandOptions: findCommandMenuItems(
        this.draft,
        this.skills,
        tuiCommandContext(runtime, this.state),
        this.commandRegistry,
      ),
      selectedSkill: this.selectedSkill,
      skills: this.pickerSnapshot(this.skillPicker, filterSkills(this.skills, this.skillPicker.query)),
      threads: this.pickerSnapshot(this.threadPicker, filterThreads(this.threads, this.threadPicker.query)),
      models: this.pickerSnapshot(this.modelPicker, filterModels(this.models, this.modelPicker.query)),
      commandDialog: this.commandDialog,
      modelBindingDialog: this.modelBindingDialog,
      showToolDetails: this.showToolDetails,
      expandedTools: new Set(this.expandedTools),
      scrollRequest: this.scrollRequest,
    }
  }

  /** 统一创建选择器 snapshot，防止三个 Picker 再维护三套展示结构。 */
  private pickerSnapshot<T>(picker: InternalPicker<T>, items: readonly T[]): PickerSnapshot<T> {
    return {
      visible: picker.visible,
      loading: picker.loading,
      query: picker.query,
      selectedIndex: picker.selectedIndex,
      error: picker.error,
      syncingDefault: picker.syncingDefault,
      items: [...items],
    }
  }

  /** 发布状态时复制 listener 集合，避免回调中取消订阅影响当前轮次。 */
  private publish(): void {
    this.snapshot = this.buildSnapshot()
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }

  /** 更新 draft 并按同一规则控制 Slash 菜单。 */
  private updateDraft(value: string): void {
    if (this.historyApplyValue === value) this.historyApplyValue = undefined
    else this.promptHistoryCursor = undefined
    this.draftCursor = undefined
    this.draft = value
    const query = value.trimStart()
    const resolution = resolveSlashCommand(query, this.commandRegistry)
    const shouldShowMenu = query.startsWith("/")
      && !query.startsWith("//")
      && !query.slice(1).match(/\s/)
      && resolution.kind !== "command"
    if (shouldShowMenu && this.commandMenuDismissedValue !== value) {
      this.commandMenu = { visible: true, selectedIndex: 0 }
    } else {
      if (!shouldShowMenu) this.commandMenuDismissedValue = undefined
      this.commandMenu = this.commandMenu.visible ? { ...this.commandMenu, visible: false } : this.commandMenu
    }
    this.publish()
  }

  /** 清空输入和命令菜单；不撤销已经选中的一次性 Skill。 */
  private clearDraft(): void {
    this.commandMenuDismissedValue = undefined
    this.promptHistoryCursor = undefined
    this.historyApplyValue = undefined
    this.draftCursor = undefined
    this.draft = ""
    this.commandMenu = { visible: false, selectedIndex: 0 }
    this.publish()
  }

  /** 将历史项写入 snapshot，实际 textarea 文本由 React adapter 同步到 ref。 */
  private navigatePromptHistory(direction: "previous" | "next"): void {
    const move = movePromptHistory(this.promptHistory, this.draft, this.promptHistoryCursor, direction)
    if (!move) return
    this.promptHistoryCursor = move.cursor
    this.historyApplyValue = move.value
    this.draftCursor = direction === "previous" ? "start" : "end"
    this.commandMenuDismissedValue = undefined
    this.draft = move.value
    this.commandMenu = { visible: false, selectedIndex: 0 }
    this.publish()
  }

  /** 提交用户输入，统一处理问题回答、Slash Command、转义文本和普通 Run。 */
  private async submit(rawValue: string): Promise<void> {
    const input = rawValue.trim()
    if (!input) return
    if (this.state.pendingOperation) {
      this.commit(current => appendNotice(current, "上下文正在压缩；完成前不能提交新消息。"))
      return
    }
    this.clearDraft()
    if (this.state.pendingQuestion) {
      this.respondQuestion(input)
      return
    }
    const resolution = resolveSlashCommand(rawValue, this.commandRegistry)
    if (resolution.kind === "command") {
      await this.executeSlashCommand(resolution.command)
      return
    }
    if (resolution.kind === "unknown") {
      this.commit(current => appendNotice(current, unknownCommandNotice(resolution)))
      return
    }
    const message = resolution.kind === "escaped" ? resolution.message : input
    const previousHistory = this.promptHistory
    const nextHistory = rememberPrompt(previousHistory, message)
    this.promptHistory = nextHistory
    void persistPromptHistory(previousHistory, nextHistory, this.promptHistoryFile)
    this.scrollRequest += 1
    this.publish()
    await this.sendAgentMessage(message)
  }

  /** 解析稳定命令 ID 后交给 Dispatcher，再由 Controller 执行完整工作流。 */
  private async executeSlashCommand(command: SlashCommand): Promise<void> {
    const current = this.state
    await this.applyCommandResult(
      dispatchSlashCommandForController(
        command,
        this.snapshot.runtime,
        current,
        this.commandRegistry,
      ),
    )
  }

  /** 执行 Dispatcher 结果；React 不再解释任何领域命令分支。 */
  private async applyCommandResult(result: CommandResult): Promise<void> {
    switch (result.type) {
      case "notice":
        this.commit(current => appendNotice(current, result.message))
        return
      case "exit":
        this.onRequestExit()
        return
      case "local-action":
        if (result.action === "clear-thread") {
          this.resetThread()
          return
        }
        if (await this.cancelActiveRun({ exitOnRepeatedCancellation: false })) {
          this.resetThread()
        } else {
          this.commit(current => appendNotice(current, "未能取消当前任务，已保留当前 thread。请等待任务结束后重试。"))
        }
        return
      case "open-picker":
        if (result.picker === "skills") this.openSkillPicker()
        else if (result.picker === "threads") this.openThreadPicker()
        else this.openModelPicker(result.initialQuery)
        return
      case "open-dialog":
        this.commandDialog = result.dialog
          ? { kind: result.dialog.kind, title: result.dialog.title, message: result.dialog.message }
          : undefined
        this.publish()
        return
      case "rpc":
        this.commit(startContextCompaction)
        try {
          if (!isClientMethod(result.method)) throw new Error(`Unsupported operation: ${result.method}`)
          const threadId = result.params.thread_id
          if (typeof threadId !== "string" || !threadId) {
            throw new Error("context.compact requires a thread_id")
          }
          const value = await this.client.compactContext(threadId)
          await this.applyCommandResult(result.onSuccess(value))
        } catch (error) {
          await this.applyCommandResult(result.onError(error))
        } finally {
          this.commit(finishContextCompaction)
        }
        return
      case "mcp":
        await this.handleMcp(result.argument)
        return
      case "web":
        if (!this.openWeb) {
          this.commit(current => appendNotice(current, "当前启动方式未提供 Web launcher。"))
          return
        }
        try {
          const url = await this.openWeb(result.threadId)
          this.commit(current => appendNotice(current, `Web 已附着当前 thread：${url}`))
        } catch (error) {
          this.commit(current => appendNotice(current, `Web 启动失败：${errorMessage(error)}`))
        }
        return
      case "submit-prompt":
        await this.sendAgentMessage(result.prompt, result.requestedSkill)
    }
  }

  /** 将快捷键动作转成 Controller 内部状态转换；滚动动作由 adapter 消费。 */
  private async handleShortcut(action: ShortcutAction): Promise<void> {
    switch (action) {
      case "none":
      case "scroll-line-up":
      case "scroll-line-down":
      case "scroll-page-up":
      case "scroll-page-down":
      case "scroll-top":
      case "scroll-bottom":
      case "thread-block":
      case "model-block":
      case "skill-block":
        return
      case "confirm-command-dialog":
        await this.resolveDialog(this.modelBindingDialog ? "model-binding" : "command", true)
        return
      case "cancel-command-dialog":
        await this.resolveDialog(this.modelBindingDialog ? "model-binding" : "command", false)
        return
      case "close-command-menu":
        this.commandMenuDismissedValue = this.draft
        this.commandMenu = { ...this.commandMenu, visible: false }
        this.publish()
        return
      case "command-previous":
        this.moveCommandMenu(-1)
        return
      case "command-next":
        this.moveCommandMenu(1)
        return
      case "command-select":
        await this.selectCommandMenu()
        return
      case "command-block": {
        const resolution = resolveSlashCommand(this.draft, this.commandRegistry)
        if (resolution.kind === "unknown") {
          this.clearDraft()
          this.commit(current => appendNotice(current, unknownCommandNotice(resolution)))
        }
        return
      }
      case "command-open":
        this.openCommandMenu()
        return
      case "clear-draft":
        this.clearDraft()
        return
      case "cancel-run":
        await this.cancelActiveRun()
        return
      case "toggle-tool-details":
        this.showToolDetails = !this.showToolDetails
        this.publish()
        return
      case "cycle-approval-mode":
        this.cycleApprovalMode()
        return
      case "clear-selected-skill":
        this.selectedSkill = undefined
        this.publish()
        return
      case "exit":
        this.onRequestExit()
        return
      case "close-skill-picker":
        this.closePicker("skills")
        return
      case "close-thread-picker":
        this.closePicker("threads")
        return
      case "close-model-picker":
        this.closePicker("models")
        return
      case "skill-previous":
        this.movePicker("skills", -1)
        return
      case "skill-next":
        this.movePicker("skills", 1)
        return
      case "skill-select":
        this.selectVisibleSkill()
        return
      case "thread-previous":
        this.movePicker("threads", -1)
        return
      case "thread-next":
        this.movePicker("threads", 1)
        return
      case "thread-select":
        await this.selectVisibleThread()
        return
      case "model-previous":
        if (!this.modelPicker.loading) this.movePicker("models", -1)
        return
      case "model-next":
        if (!this.modelPicker.loading) this.movePicker("models", 1)
        return
      case "model-select":
        if (!this.modelPicker.loading) await this.selectVisibleModel()
    }
  }

  /** 打开命令菜单并保留当前输入语义。 */
  private openCommandMenu(): void {
    const value = this.draft.trimStart()
    if (!value.startsWith("/") || value.slice(1).match(/\s/)) this.updateDraft("/")
    this.commandMenuDismissedValue = undefined
    this.commandMenu = { visible: true, selectedIndex: 0 }
    this.publish()
  }

  /** 移动命令菜单选中项。 */
  private moveCommandMenu(direction: number): void {
    const options = this.snapshot.commandOptions
    this.commandMenu = {
      ...this.commandMenu,
      selectedIndex: options.length ? (this.commandMenu.selectedIndex + direction + options.length) % options.length : 0,
    }
    this.publish()
  }

  /** 处理当前命令菜单选中项；领域命令仍按 canonical ID 执行。 */
  private async selectCommandMenu(): Promise<void> {
    const directCommand = parseSlashCommand(this.draft, this.commandRegistry)
    if (directCommand && !directCommand.argument) {
      this.clearDraft()
      await this.executeSlashCommand(directCommand)
      return
    }
    const item = this.snapshot.commandOptions[this.commandMenu.selectedIndex]
    if (item) await this.selectCommandMenuItem(item)
  }

  /** 处理鼠标或键盘选中的命令/Skill。 */
  private async selectCommandMenuItem(item: CommandMenuItem): Promise<void> {
    if (this.state.pendingOperation) {
      this.commandMenu = { visible: false, selectedIndex: 0 }
      this.commit(current => appendNotice(current, "上下文正在压缩；完成前不能选择新命令或 Skill。"))
      return
    }
    if (item.kind === "skill") {
      this.selectSkill(item.skill)
      return
    }
    if (item.availability.state === "disabled") {
        const reason = item.availability.reason
        this.commit(current => appendNotice(current, `/${item.command.name} 暂不可用：${reason}。`))
      return
    }
    if (this.state.activeRun) {
      const command = parseSlashCommand(`/${item.command.name}`, this.commandRegistry)
      this.clearDraft()
      if (command) await this.executeSlashCommand(command)
      return
    }
    const value = `/${item.command.name}`
    this.commandMenuDismissedValue = value
    this.draft = value
    this.draftCursor = "end"
    this.commandMenu = { visible: false, selectedIndex: 0 }
    this.publish()
  }

  /** 读取当前 catalog 并打开 Skill Picker。 */
  private openSkillPicker(): void {
    const epoch = ++this.pickerEpoch.skills
    this.skillPicker = { ...this.skillPicker, visible: true, loading: true, query: "", selectedIndex: 0, error: undefined }
    this.publish()
    void this.refreshSkillCatalog(epoch)
  }

  /** 读取当前 project 的 Thread 摘要并打开恢复 Picker。 */
  private openThreadPicker(): void {
    if (this.state.activeRun || this.state.pendingOperation || this.state.pendingApproval || this.state.pendingQuestion) {
      this.commit(current => appendNotice(current, "当前 thread 尚未回到空闲状态，不能恢复其他 thread。"))
      return
    }
    const epoch = ++this.pickerEpoch.threads
    this.threadPicker = { ...this.threadPicker, visible: true, loading: true, query: "", selectedIndex: 0, error: undefined }
    this.publish()
    void this.refreshThreadCatalog(epoch)
  }

  /** 打开 Model Picker；legacy immutable binding 只展示说明 Dialog。 */
  private openModelPicker(initialQuery = ""): void {
    const current = this.state
    if (current.pendingOperation) {
      this.commit(state => appendNotice(state, "上下文正在压缩；完成前不能切换模型。"))
      return
    }
    if (current.threadId && !this.supportsThreadModelSelection) {
      void this.client.listModels(current.threadId).then(result => {
        const binding = result.thread_binding
        const executor = binding?.roles.executor ?? binding?.roles.primary
        this.modelBindingDialog = {
          title: "当前 Thread 的模型不可变",
          message: executor
            ? `当前 Thread 已绑定 ${executor.provider_label} · ${executor.model}（${executor.id}）。请新建 Thread 后使用 /model 选择模型。`
            : "当前 Thread 使用 legacy immutable binding，不能热切换模型。请新建 Thread 后使用 /model 选择模型。",
        }
        this.publish()
      }).catch(error => {
        this.modelBindingDialog = {
          title: "模型绑定不可读取",
          message: `无法读取当前 Thread 的模型绑定：${errorMessage(error)}。请新建 Thread 后再选择模型。`,
        }
        this.publish()
      })
      return
    }
    const epoch = ++this.pickerEpoch.models
    this.modelPicker = { ...this.modelPicker, visible: true, loading: true, query: initialQuery, selectedIndex: 0, error: undefined, syncingDefault: undefined }
    this.publish()
    void this.client.listModels(current.threadId).then(result => {
      if (!this.modelPicker.visible || this.pickerEpoch.models !== epoch) return
      this.models = result.profiles
      if (result.thread_selection?.primary_profile) this.threadModelSelection = result.thread_selection.primary_profile
      const actual = result.last_run_binding?.profile
      if (actual) this.actualModelProfile = actual
      this.modelPicker = { ...this.modelPicker, loading: false }
      this.publish()
    }).catch(error => {
      if (this.modelPicker.visible && this.pickerEpoch.models === epoch) {
        this.modelPicker = { ...this.modelPicker, loading: false, error: `模型目录读取失败：${errorMessage(error)}` }
        this.publish()
      }
    })
  }

  /** 关闭 Picker；保存默认模型期间不允许通过 Esc 打断事务。 */
  private closePicker(picker: PickerKind): void {
    if (picker === "models" && this.modelPicker.syncingDefault) return
    this.pickerEpoch[picker] += 1
    if (picker === "skills") this.skillPicker = { ...this.skillPicker, visible: false, loading: false, error: undefined }
    if (picker === "threads") this.threadPicker = { ...this.threadPicker, visible: false, loading: false, error: undefined }
    if (picker === "models") this.modelPicker = { ...this.modelPicker, visible: false, loading: false, error: undefined, syncingDefault: undefined }
    this.publish()
  }

  /** 更新 Picker 搜索词；过滤留在 Controller，避免 React 重新请求 sidecar。 */
  private updatePickerQuery(picker: PickerKind, query: string): void {
    const value = { query, selectedIndex: 0 }
    if (picker === "skills") this.skillPicker = { ...this.skillPicker, ...value }
    if (picker === "threads") this.threadPicker = { ...this.threadPicker, ...value }
    if (picker === "models") this.modelPicker = { ...this.modelPicker, ...value }
    this.publish()
  }

  /** 更新 Picker hover/键盘索引。 */
  private updatePickerIndex(picker: PickerKind, selectedIndex: number): void {
    if (picker === "skills") this.skillPicker = { ...this.skillPicker, selectedIndex }
    if (picker === "threads") this.threadPicker = { ...this.threadPicker, selectedIndex }
    if (picker === "models") this.modelPicker = { ...this.modelPicker, selectedIndex }
    this.publish()
  }

  /** 在可见选项中循环移动索引。 */
  private movePicker(picker: PickerKind, direction: number): void {
    const items = picker === "skills"
      ? filterSkills(this.skills, this.skillPicker.query)
      : picker === "threads"
        ? filterThreads(this.threads, this.threadPicker.query)
        : filterModels(this.models, this.modelPicker.query)
    const current = picker === "skills" ? this.skillPicker : picker === "threads" ? this.threadPicker : this.modelPicker
    this.updatePickerIndex(picker, items.length ? (current.selectedIndex + direction + items.length) % items.length : 0)
  }

  /** 选择当前 Skill，并把它附着到下一次真实消息。 */
  private selectVisibleSkill(): void {
    const selected = filterSkills(this.skills, this.skillPicker.query)[this.skillPicker.selectedIndex]
    if (selected) this.selectSkill(selected)
  }

  /** 选择当前 Thread，并恢复 sidecar 返回的历史。 */
  private async selectVisibleThread(): Promise<void> {
    const selected = filterThreads(this.threads, this.threadPicker.query)[this.threadPicker.selectedIndex]
    if (selected) await this.selectThread(selected)
  }

  /** 选择当前模型 Profile。 */
  private async selectVisibleModel(): Promise<void> {
    const selected = filterModels(this.models, this.modelPicker.query)[this.modelPicker.selectedIndex]
    if (selected) await this.selectModel(selected)
  }

  /** Skill 选择会清掉搜索草稿，但不影响当前 Thread。 */
  private selectSkill(skill: SkillMenuItem): void {
    this.clearDraft()
    this.selectedSkill = skill
    this.skillPicker = { ...this.skillPicker, visible: false, loading: false, query: "", selectedIndex: 0 }
    this.publish()
  }

  /** 读取 Skill catalog；异步结果只允许写回对应打开轮次。 */
  private async refreshSkillCatalog(epoch?: number): Promise<void> {
    try {
      const result = await this.client.request("skills.list", { include_disabled: false }) as { skills?: unknown[] }
      const next = Array.isArray(result.skills)
        ? result.skills.map(skillMenuItem).filter((item): item is SkillMenuItem => item !== undefined)
        : []
      this.skills = next
      if (epoch !== undefined && (!this.skillPicker.visible || this.pickerEpoch.skills !== epoch)) return
      if (epoch !== undefined) this.skillPicker = { ...this.skillPicker, loading: false }
      this.publish()
    } catch (error) {
      if (epoch !== undefined && this.skillPicker.visible && this.pickerEpoch.skills === epoch) {
        this.skillPicker = { ...this.skillPicker, loading: false, error: `Skill catalog 读取失败：${errorMessage(error)}` }
        this.publish()
      }
    }
  }

  /** 读取 Thread catalog；异步结果只允许写回对应打开轮次。 */
  private async refreshThreadCatalog(epoch: number): Promise<void> {
    try {
      const result = await this.client.listThreads()
      const next = result.threads.map(threadPickerItem).filter((item): item is ThreadPickerItem => item !== undefined)
      if (!this.threadPicker.visible || this.pickerEpoch.threads !== epoch) return
      this.threads = next
      this.threadPicker = { ...this.threadPicker, loading: false }
      this.publish()
    } catch (error) {
      if (this.threadPicker.visible && this.pickerEpoch.threads === epoch) {
        this.threadPicker = { ...this.threadPicker, loading: false, error: `Thread 列表读取失败：${errorMessage(error)}` }
        this.publish()
      }
    }
  }

  /** 恢复 Thread 时只在最后一步替换 state，避免半条历史覆盖当前内容。 */
  private async selectThread(thread: ThreadPickerItem): Promise<void> {
    if (this.openingThread) return
    if (this.state.activeRun || this.state.pendingOperation || this.state.pendingApproval || this.state.pendingQuestion) {
      this.closePicker("threads")
      this.commit(current => appendNotice(current, "当前 thread 状态已变化且不再空闲，未恢复其他 thread。"))
      return
    }
    this.openingThread = true
    const epoch = this.pickerEpoch.threads
    this.threadPicker = { ...this.threadPicker, loading: true, error: undefined }
    this.publish()
    try {
      const opened = threadOpenResult(await this.client.openThread(thread.threadId))
      if (!this.threadPicker.visible || this.pickerEpoch.threads !== epoch) return
      let recoveredSelection: string | undefined
      let actualModel: ModelProfile | undefined
      let recoveredModels: readonly ModelProfile[] = []
      try {
        const result = await this.client.listModels(opened.threadId)
        recoveredModels = result.profiles
        recoveredSelection = result.thread_selection?.primary_profile
        actualModel = result.last_run_binding?.profile
          ?? result.thread_binding?.roles.executor
          ?? result.thread_binding?.roles.primary
      } catch {
        // 模型绑定读取失败不阻断历史恢复；本次 Thread 不展示旧 Thread 的模型。
      }
      if (!this.threadPicker.visible || this.pickerEpoch.threads !== epoch) return
      if (this.state.activeRun || this.state.pendingOperation || this.state.pendingApproval || this.state.pendingQuestion) {
        this.closePicker("threads")
        this.commit(current => appendNotice(current, "当前 thread 状态已变化且不再空闲，未恢复其他 thread。"))
        return
      }
      this.resetThread(restoreThread(opened.threadId, opened.messages), {
        models: recoveredModels,
        selection: recoveredSelection,
        actual: actualModel,
      })
    } catch (error) {
      if (this.threadPicker.visible) {
        this.threadPicker = { ...this.threadPicker, loading: false, error: `Thread 恢复失败：${errorMessage(error)}` }
        this.publish()
      }
    } finally {
      this.openingThread = false
    }
  }

  /** 先改变当前 Thread 的下一次模型，再独立同步未来新 Thread 默认值。 */
  private async selectModel(model: ModelProfile): Promise<void> {
    if (this.state.pendingOperation) {
      this.closePicker("models")
      this.commit(state => appendNotice(state, "上下文正在压缩；完成前不能切换模型。"))
      return
    }
    if (!model.available) {
      this.modelPicker = { ...this.modelPicker, error: `${model.provider_label} · ${model.model} 不可用：${model.unavailable_reason ?? "配置不可用"}` }
      this.publish()
      return
    }
    if (this.modelPicker.loading) return
    this.threadModelSelection = model.id
    this.modelPicker = { ...this.modelPicker, loading: true, syncingDefault: true, error: undefined }
    this.publish()
    const label = `${model.provider_label} · ${model.model}`
    try {
      if (!this.baseRuntime.capabilities?.includes(Capability.CONFIG_WRITE)) throw new ModelDefaultSyncError("CONFIG_WRITE_CAPABILITY_REQUIRED")
      const details = await this.client.configDetails()
      const field = details.fields.find(value => value.path === "models.default_profile")
      if (!field) throw new ModelDefaultSyncError("CONFIG_FIELD_NOT_ALLOWED")
      if (!field.editable) throw new ModelDefaultSyncError(field.unavailable_reason ?? "CONFIG_FIELD_NOT_WRITABLE")
      if (field.value !== model.id) {
        const changes = [{ path: "models.default_profile", value: model.id }]
        const preview = await this.client.previewConfig(changes)
        await this.client.commitConfig(preview.revision, changes)
      }
      try {
        const refreshed = await this.client.listModels(this.state.threadId)
        this.models = refreshed.profiles
      } catch {
        this.models = this.models.map(value => ({ ...value, is_default: value.id === model.id }))
      }
      this.modelPicker = { ...this.modelPicker, visible: false, loading: false, syncingDefault: false, error: undefined }
      this.commit(current => appendNotice(current, `当前 Thread 已切换到 ${label}；后续新 Thread 默认模型已同步。`))
    } catch (error) {
      this.modelPicker = { ...this.modelPicker, visible: false, loading: false, syncingDefault: false, error: undefined }
      this.commit(current => appendNotice(current, `当前 Thread 已切换到 ${label}；未来新 Thread 默认未更新：${safeModelDefaultSyncError(error)}`))
    }
  }

  /** 执行模型绑定 Dialog 的确认动作。 */
  private async resolveDialog(kind: "command" | "model-binding", confirmed: boolean): Promise<void> {
    if (kind === "model-binding") {
      this.modelBindingDialog = undefined
      if (confirmed) this.resetThread()
      else this.publish()
      return
    }
    const dialog = this.commandDialog
    this.commandDialog = undefined
    this.publish()
    if (confirmed && dialog) await this.applyCommandResult({
      type: "local-action",
      action: "cancel-active-run-and-clear-thread",
    })
  }

  /** 统一清空 Thread、模型意图、Skill、浮层和视图展开状态。 */
  private resetThread(
    nextState: TuiState = clearThread(this.state),
    modelState: { models: readonly ModelProfile[]; selection?: string; actual?: ModelProfile } = { models: [] },
  ): void {
    for (const requestId of this.pendingInteractions.keys()) this.settleAbandonedInteraction(requestId)
    this.pickerEpoch.skills += 1
    this.pickerEpoch.threads += 1
    this.pickerEpoch.models += 1
    this.state = nextState
    this.models = modelState.models
    this.threadModelSelection = modelState.selection
    this.actualModelProfile = modelState.actual
    this.selectedSkill = undefined
    this.commandDialog = undefined
    this.modelBindingDialog = undefined
    this.draft = ""
    this.draftCursor = undefined
    this.commandMenu = { visible: false, selectedIndex: 0 }
    this.commandMenuDismissedValue = undefined
    this.promptHistoryCursor = undefined
    this.historyApplyValue = undefined
    this.skillPicker = { ...this.skillPicker, visible: false, loading: false, error: undefined }
    this.threadPicker = { ...this.threadPicker, visible: false, loading: false, error: undefined }
    this.modelPicker = { ...this.modelPicker, visible: false, loading: false, syncingDefault: undefined, error: undefined }
    this.showToolDetails = false
    this.expandedTools = new Set()
    this.scrollRequest += 1
    this.publish()
  }

  /** 取消当前 Run；成功只返回结果，调用方决定是否清理 Thread。 */
  private async cancelActiveRun({ exitOnRepeatedCancellation = true }: { exitOnRepeatedCancellation?: boolean } = {}): Promise<boolean> {
    const active = this.state.activeRun
    if (!active) return false
    if (this.state.status === "正在取消") {
      if (exitOnRepeatedCancellation) this.onRequestExit()
      return false
    }
    this.commit(markCancelling)
    try {
      const result = await this.client.cancel(active.threadId, active.runId)
      if (!result.cancelled || result.run_id !== active.runId) throw new Error("Agent 未确认取消当前运行")
      return true
    } catch (error) {
      this.commit(current => markRunFailed(current, active.runId, errorMessage(error)))
      return false
    }
  }

  /** Shift+Tab：循环切换审批模式，从下一次 Run 起生效并立即更新右下角展示。 */
  private cycleApprovalMode(): void {
    const current = this.approvalModeOverride ?? this.baseRuntime.approvalMode
    this.approvalModeOverride = nextApprovalMode(current)
    this.publish()
  }

  /** 登记消息、启动 Run，并把 AgentRun 队列交给 drain 释放。 */
  private async sendAgentMessage(message: string, requestedSkill?: RequestedSkill): Promise<void> {
    const current = this.state
    if (current.pendingOperation) {
      this.commit(state => appendNotice(state, "上下文正在压缩；完成前不能启动新任务。"))
      return
    }
    if (current.activeRun) {
      this.commit(state => appendNotice(state, "当前 thread 仍在执行；请等待、审批或按 Ctrl+C 取消。"))
      return
    }
    const armedSkill = requestedSkill ?? (this.selectedSkill ? { id: this.selectedSkill.id, args: message } : undefined)
    const modelSelection = this.threadModelSelection ? { primary_profile: this.threadModelSelection } : undefined
    if (armedSkill && !requestedSkill) {
      this.selectedSkill = undefined
      this.publish()
    }
    const agentRun = this.client.startRun({
      message,
      threadId: current.threadId,
      requestedSkill: armedSkill,
      modelSelection,
      approvalMode: this.approvalModeOverride ?? this.baseRuntime.approvalMode,
    })
    const run = agentRun.ref
    this.commit(state => startRun(state, run, message))
    void this.drainEvents(agentRun.events)
    void agentRun.completion.catch(() => undefined)
    try {
      await agentRun.accepted
    } catch (error) {
      this.commit(state => markRunFailed(state, run.runId, errorMessage(error)))
    }
  }

  /** 处理 Agent 发起的 Interaction，并隐藏 request resolver 细节。 */
  private handleInteractionRequest(request: InteractionRequestEnvelope): Promise<InteractionResponse> {
    const active = this.state.activeRun
    if (!active || active.threadId !== request.thread_id || active.runId !== request.run_id) return Promise.resolve(interactionCancellation(request))
    return new Promise(resolve => {
      this.pendingInteractions.set(request.request_id, { request, resolve })
      this.commit(current => applyInteractionRequest(current, request))
    })
  }

  /** 回写审批结果并更新时间线中的 Interaction 状态。 */
  private respondApproval(decision: ApprovalDecision): void {
    const pendingApproval = this.state.pendingApproval
    if (!pendingApproval) return
    const pending = this.pendingInteractions.get(pendingApproval.requestId)
    if (!pending) return
    this.pendingInteractions.delete(pendingApproval.requestId)
    const outcome = decision === "reject" || decision === "reject_with_feedback" ? "rejected" : "approved"
    this.commit(state => clearPendingInteraction(state, outcome))
    pending.resolve({
      type: "approval",
      request_id: pendingApproval.requestId,
      decision,
      ...(decision === "reject_with_feedback" ? { feedback: "" } : {}),
    })
  }

  /** 回写当前问题答案；多题表单继续沿用现有首题行为。 */
  private respondQuestion(answer: string): void {
    const pendingQuestion = this.state.pendingQuestion
    if (!pendingQuestion) return
    const pending = this.pendingInteractions.get(pendingQuestion.requestId)
    if (!pending) return
    this.pendingInteractions.delete(pendingQuestion.requestId)
    this.commit(state => clearPendingInteraction(state, "answered"))
    pending.resolve({
      type: "question",
      request_id: pendingQuestion.requestId,
      answers: { [pendingQuestion.questionId]: [answer] },
    })
  }

  /** 终态或关闭时废弃尚未回写的 Interaction。 */
  private settleAbandonedInteraction(requestId: string): void {
    const pending = this.pendingInteractions.get(requestId)
    if (!pending) return
    this.client.abandonInteraction(requestId)
    this.pendingInteractions.delete(requestId)
    pending.resolve(interactionCancellation(pending.request))
  }

  /** 消费 AgentRun 队列，公开事件仍由 Controller 的 AgentClient listener 处理。 */
  private async drainEvents(events: AsyncIterable<unknown>): Promise<void> {
    try {
      for await (const _event of events) {
        // 事件已由 AgentClient listener 路由到 applyAgentEvent。
      }
    } catch {
      // accepted/completion 的错误路径负责更新 TUI 状态。
    }
  }

  /** MCP 状态、添加、删除和参数校验的唯一工作流入口。 */
  private async handleMcp(argument?: string): Promise<void> {
    const subArgs = argument?.trim()
    if (!subArgs) {
      try {
        const result = await this.client.mcpStatus()
        this.commit(current => appendNotice(current, formatMcpStatus(result)))
      } catch (error) {
        this.commit(current => appendNotice(current, `MCP 状态查询失败：${errorMessage(error)}`))
      }
      return
    }
    const [sub, ...rest] = subArgs.split(/\s+/)
    if (sub === "add") {
      const usage = "用法：/mcp add <name> <command> [args...]  或  /mcp add <name> --url <url> [--sse]"
      if (rest.length < 2) {
        this.commit(current => appendNotice(current, usage))
        return
      }
      const name = rest[0]!
      const remaining = rest.slice(1)
      const urlIdx = remaining.indexOf("--url")
      const hasSse = remaining.includes("--sse")
      if (urlIdx !== -1) {
        const url = remaining[urlIdx + 1]
        if (!url || url.startsWith("--")) {
          this.commit(current => appendNotice(current, `错误：--url 后需要提供 URL\n${usage}`))
          return
        }
        try {
          const result = await this.client.mcpAdd({ name, transport: hasSse ? "sse" : "http", url })
          this.commit(current => appendNotice(current, formatMcpAddResult(name, result)))
        } catch (error) {
          this.commit(current => appendNotice(current, `添加 MCP 服务器失败：${errorMessage(error)}`))
        }
        return
      }
      const command = remaining[0]
      const args = remaining.slice(1)
      try {
        const result = await this.client.mcpAdd({ name, transport: "stdio", command, args: args.length ? args : undefined })
        this.commit(current => appendNotice(current, formatMcpAddResult(name, result)))
      } catch (error) {
        this.commit(current => appendNotice(current, `添加 MCP 服务器失败：${errorMessage(error)}`))
      }
      return
    }
    if (sub === "remove") {
      if (!rest.length) {
        this.commit(current => appendNotice(current, "用法：/mcp remove <name>"))
        return
      }
      const name = rest[0]!
      try {
        await this.client.mcpRemove(name)
        this.commit(current => appendNotice(current, `已删除 MCP 服务器 "${name}"`))
      } catch (error) {
        this.commit(current => appendNotice(current, `删除 MCP 服务器失败：${errorMessage(error)}`))
      }
      return
    }
    this.commit(current => appendNotice(current, `未知子命令 "${sub}"\n用法：/mcp [add|remove] ...`))
  }

  /** 切换单个工具卡片展开状态。 */
  private toggleTool(toolId: string): void {
    const next = new Set(this.expandedTools)
    if (next.has(toolId)) next.delete(toolId)
    else next.add(toolId)
    this.expandedTools = next
    this.publish()
  }

  /** 当前运行是否允许切换模型 Profile。 */
  private get supportsThreadModelSelection(): boolean {
    return this.baseRuntime.capabilities?.includes(Capability.MODELS_SELECT) === true
  }
}

/** 创建一个 TUI Controller；它是一次 TUI 挂载对应的内存工作流，不是持久化 Session。 */
export function createTuiController(options: TuiControllerOptions): TuiController {
  return new TuiControllerImpl(options)
}

const TERMINAL_EVENT_TYPES = new Set<EventEnvelope["type"]>([
  EventType.INTERACTION_RESOLVED,
  EventType.RUN_COMPLETED,
  EventType.RUN_CANCELLED,
  EventType.RUN_FAILED,
])

function emptyPicker<T>(): InternalPicker<T> {
  return { visible: false, loading: false, query: "", selectedIndex: 0 }
}

function dispatchSlashCommandForController(
  command: SlashCommand,
  runtime: TuiRuntime,
  state: TuiState,
  registry: CommandRegistry,
): CommandResult {
  return dispatchSlashCommand(command, {
    commandContext: tuiCommandContext(runtime, state),
    threadId: state.threadId,
    runtimeStatus: runtimeStatusSummary(runtime),
    versionSummary: `za38-cli ${runtime.cliVersion} · JSON-RPC v3`,
  }, registry)
}

function tuiCommandContext(runtime: TuiRuntime, state: TuiState) {
  return defaultCommandContext({
    capabilities: runtime.capabilities,
    hasThread: Boolean(state.threadId),
    activeRun: Boolean(state.activeRun),
    pendingOperation: Boolean(state.pendingOperation),
    hasPendingInteraction: Boolean(state.pendingApproval || state.pendingQuestion),
  })
}

function interactionCancellation(request: InteractionRequestEnvelope): InteractionResponse {
  return request.type === "approval"
    ? { type: "approval", request_id: request.request_id, decision: "reject" }
    : { type: "question", request_id: request.request_id, answers: {} }
}

function skillMenuItem(value: unknown): SkillMenuItem | undefined {
  if (!value || typeof value !== "object") return undefined
  const record = value as Record<string, unknown>
  if (typeof record.id !== "string" || !record.id || typeof record.name !== "string" || !record.name || typeof record.description !== "string") return undefined
  return {
    id: record.id,
    name: record.name,
    description: record.description,
    source: typeof record.source === "string" ? record.source : "unknown",
    enabled: record.enabled !== false,
    userInvocable: record.user_invocable !== false,
    argumentHint: typeof record.argument_hint === "string" ? record.argument_hint : undefined,
  }
}

function modelProfileFromRunStarted(payload: Record<string, unknown>): ModelProfile | undefined {
  const primary = payload.primary_model
  if (!primary || typeof primary !== "object") return undefined
  const profile = (primary as Record<string, unknown>).profile
  if (!profile || typeof profile !== "object") return undefined
  const value = profile as Record<string, unknown>
  if (
    typeof value.id !== "string" || !value.id
    || typeof value.model !== "string" || !value.model
    || typeof value.provider_label !== "string" || !value.provider_label
    || typeof value.context_window_tokens !== "number" || !Number.isInteger(value.context_window_tokens)
    || !Array.isArray(value.capabilities) || !value.capabilities.every(item => typeof item === "string")
    || typeof value.is_default !== "boolean" || typeof value.available !== "boolean"
    || typeof value.source !== "string" || !value.source
  ) return undefined
  return {
    id: value.id,
    model: value.model,
    provider_label: value.provider_label,
    context_window_tokens: value.context_window_tokens,
    capabilities: value.capabilities,
    is_default: value.is_default,
    available: value.available,
    unavailable_reason: typeof value.unavailable_reason === "string" ? value.unavailable_reason : null,
    source: value.source,
  }
}

function threadPickerItem(value: unknown): ThreadPickerItem | undefined {
  if (!value || typeof value !== "object") return undefined
  const record = value as Record<string, unknown>
  if (
    typeof record.thread_id !== "string" || !record.thread_id
    || typeof record.created_at_ms !== "number" || !Number.isInteger(record.created_at_ms) || record.created_at_ms < 0
    || typeof record.updated_at_ms !== "number" || !Number.isInteger(record.updated_at_ms) || record.updated_at_ms < 0
    || typeof record.first_message !== "string" || typeof record.latest_message !== "string"
    || typeof record.message_count !== "number" || !Number.isInteger(record.message_count) || record.message_count < 0
  ) return undefined
  return {
    threadId: record.thread_id,
    createdAtMs: record.created_at_ms,
    updatedAtMs: record.updated_at_ms,
    firstMessage: record.first_message,
    latestMessage: record.latest_message,
    messageCount: record.message_count,
  }
}

function threadOpenResult(value: unknown): { threadId: string; messages: Array<{ kind: "user" | "assistant" | "tool"; content: string; toolName?: string }> } {
  if (!value || typeof value !== "object") throw new Error("Agent 返回的 thread 恢复结果无效")
  const record = value as Record<string, unknown>
  const thread = threadPickerItem(record.thread)
  if (!thread || !Array.isArray(record.messages)) throw new Error("Agent 返回的 thread 恢复结果无效")
  const messages = record.messages.map(threadMessage).filter((message): message is ThreadMessage => message !== undefined)
  if (messages.length !== record.messages.length) throw new Error("Agent 返回了无效的 thread message")
  return {
    threadId: thread.threadId,
    messages: messages.map(message => ({ kind: message.kind, content: message.content, toolName: message.tool_name })),
  }
}

function threadMessage(value: unknown): ThreadMessage | undefined {
  if (!value || typeof value !== "object") return undefined
  const record = value as Record<string, unknown>
  if (
    (record.kind !== "user" && record.kind !== "assistant" && record.kind !== "tool")
    || typeof record.content !== "string"
    || (record.tool_name !== undefined && typeof record.tool_name !== "string")
  ) return undefined
  return {
    kind: record.kind,
    content: record.content,
    tool_name: typeof record.tool_name === "string" ? record.tool_name : undefined,
  }
}

function filterSkills(skills: readonly SkillMenuItem[], query: string): readonly SkillMenuItem[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return skills
  return skills.filter(skill => [skill.id, skill.name, skill.source, skill.description].some(value => value.toLowerCase().includes(needle)))
}

function filterThreads(threads: readonly ThreadPickerItem[], query: string): readonly ThreadPickerItem[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return threads
  return threads.filter(thread => [thread.firstMessage, thread.latestMessage].some(value => value.toLowerCase().includes(needle)))
}

function filterModels(models: readonly ModelProfile[], query: string): readonly ModelProfile[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return models
  return models.filter(model => [model.id, model.model, model.provider_label].some(value => value.toLowerCase().includes(needle)))
}

function formatMcpAddResult(name: string, result: McpAddResult): string {
  const lines = [`已添加 MCP 服务器 "${name}"`]
  if (result.connected) {
    lines.push(`连接成功，加载 ${result.tool_names.length} 个工具`)
    if (result.tool_names.length > 0) lines.push(`工具：${result.tool_names.join(", ")}`)
  } else {
    lines.push("连接失败，配置已保存，重启后自动重试")
    if (result.error) lines.push(`错误：${result.error}`)
  }
  return lines.join("\n")
}

function formatMcpStatus(result: McpStatusResult): string {
  if (!result.servers.length) return "未配置 MCP 服务器\n在 ~/.harness/config.toml 的 [[mcp.servers]] 中添加配置。"
  const lines: string[] = ["MCP 服务器状态", ""]
  for (const server of result.servers) {
    const icon = server.status === "connected" ? "●" : server.status === "failed" ? "✗" : "○"
    lines.push(`${icon} ${server.name}  [${server.transport}]  ${server.status}`)
    if (server.error) lines.push(`  错误：${server.error}`)
    if (server.tool_names.length) lines.push(`  工具：${server.tool_names.join(", ")}`)
  }
  lines.push("", `共 ${result.servers.length} 个服务器，${result.total_tools} 个工具`)
  return lines.join("\n")
}

class ModelDefaultSyncError extends Error {
  constructor(readonly reason: string) {
    super(reason)
    this.name = "ModelDefaultSyncError"
  }
}

function safeModelDefaultSyncError(error: unknown): string {
  const reason = error instanceof ModelDefaultSyncError
    ? error.reason
    : error instanceof JsonRpcRemoteError
      ? remoteConfigReason(error)
      : "配置服务暂时不可用"
  const messages: Record<string, string> = {
    CONFIG_WRITE_CAPABILITY_REQUIRED: "当前客户端未协商 config.write",
    CONFIG_FIELD_NOT_ALLOWED: "默认模型字段不可写",
    CONFIG_FIELD_NOT_WRITABLE: "默认模型字段当前不可写",
    CONFIG_USER_FILE_MISSING: "用户配置文件不存在",
    MANAGED_POLICY_LOCKED: "默认模型字段受受管策略锁定",
    SOURCE_OVERRIDE_ACTIVE: "默认模型由更高优先级来源覆盖",
    UNTRUSTED_PROJECT_CONFIGURATION: "项目配置不允许写入用户默认值",
    EXPLICIT_CONFIGURATION_ACTIVE: "当前显式配置来源不可写",
    CONFIG_REVISION_CONFLICT: "配置已被其他操作修改，请重试",
    CONFIG_WRITE_FAILED: "用户配置写入失败",
    MODEL_PROFILE_NOT_FOUND: "所选模型 Profile 不存在",
    MODEL_PROFILE_UNAVAILABLE: "所选模型不可用",
    MODEL_PROFILE_CAPABILITY_MISSING: "所选模型缺少 Single Agent 所需能力",
  }
  return messages[reason] ?? "配置服务拒绝本次更新"
}

function remoteConfigReason(error: JsonRpcRemoteError): string {
  if (error.data && typeof error.data === "object") {
    const code = (error.data as Record<string, unknown>).code
    if (typeof code === "string") return code
  }
  return error.message
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
