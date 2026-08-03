/** TUI Interactive Adapter：只拥有终端表现状态，把用户动作映射为 InteractiveIntent。 */

import type { ModelProfile } from "@za38/protocol"

import type { InteractiveController, InteractiveIntent, InteractiveResult, InteractiveSnapshot } from "../../interactive/types"
import { filterCommandMenuItems, parseSlashCommand, resolveSlashCommand, type CommandMenuItem, type SkillMenuItem } from "../../interactive/commands"
import type { ThreadSummary } from "@za38/protocol"
import {
  loadPromptHistory,
  movePromptHistory,
  persistPromptHistory,
  rememberPrompt,
  type PromptHistoryCursor,
} from "./prompt-history"
import type { ShortcutAction } from "./shortcuts"

export type ApprovalDecision = "approve_once" | "approve_thread" | "approve_always" | "reject" | "reject_with_feedback"

export type CommandMenuState = {
  visible: boolean
  selectedIndex: number
}

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

/** TUI Adapter 发布的完整表现快照；领域事实来自 interactive。 */
export type TuiAdapterSnapshot = {
  readonly interactive: InteractiveSnapshot
  readonly draft: string
  readonly draftCursor?: "start" | "end"
  readonly commandMenu: CommandMenuState
  readonly commandOptions: readonly CommandMenuItem[]
  readonly selectedSkill?: SkillMenuItem
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
  readonly transientNotice?: {
    readonly id: string
    readonly message: string
  }
  readonly showToolDetails: boolean
  readonly expandedTools: ReadonlySet<string>
  /** 递增后由 React adapter 滚动到最新内容。 */
  readonly scrollRequest: number
}

/** React、快捷键和鼠标只能通过这些语义意图驱动 Adapter。 */
export type TuiIntent =
  | { type: "draft-input"; value: string }
  | { type: "submit"; value: string }
  | { type: "history"; direction: "previous" | "next" }
  | { type: "execute-command"; commandId: string; argument?: string }
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

/** TUI Adapter 的最小 external interface；终端动作与共享 Controller 解耦。 */
export interface TuiAdapter {
  getSnapshot(): TuiAdapterSnapshot
  subscribe(listener: (snapshot: TuiAdapterSnapshot) => void): () => void
  dispatch(intent: TuiIntent): Promise<void>
  close(): Promise<void>
}

/** 创建 TUI Adapter；一次 TUI 挂载对应一个 Adapter 与共享 Controller。 */
export type TuiAdapterOptions = {
  controller: InteractiveController
  promptHistoryFile?: string
  resume?: boolean
  onRequestExit: () => void
  openWeb?: (threadId: string | null) => Promise<void>
}

type InternalPicker<T> = {
  visible: boolean
  loading: boolean
  query: string
  selectedIndex: number
  error?: string
  syncingDefault?: boolean
}

/** TUI Adapter 的具体实现；所有可变表现状态都集中在这个 module 内。 */
class TuiAdapterImpl implements TuiAdapter {
  private readonly controller: InteractiveController
  private readonly promptHistoryFile: string | undefined
  private readonly onRequestExit: () => void
  private readonly openWeb?: (threadId: string | null) => Promise<void>
  private readonly listeners = new Set<(snapshot: TuiAdapterSnapshot) => void>()
  private readonly unsubscribeInteractive: () => void

  private snapshot: TuiAdapterSnapshot
  private draft = ""
  private draftCursor: "start" | "end" | undefined
  private commandMenu: CommandMenuState = { visible: false, selectedIndex: 0 }
  private commandMenuDismissedValue: string | undefined
  private skillPicker: InternalPicker<SkillMenuItem> = emptyPicker()
  private threadPicker: InternalPicker<ThreadPickerItem> = emptyPicker()
  private modelPicker: InternalPicker<ModelProfile> = emptyPicker()
  private promptHistory: string[] = []
  private promptHistoryCursor: PromptHistoryCursor | undefined
  private historyApplyValue: string | undefined
  private showToolDetails = false
  private expandedTools: ReadonlySet<string> = new Set()
  private scrollRequest = 0
  private transientNotice: TuiAdapterSnapshot["transientNotice"]
  private closed = false

  constructor(options: TuiAdapterOptions) {
    this.controller = options.controller
    this.promptHistoryFile = options.promptHistoryFile
    this.onRequestExit = options.onRequestExit
    this.openWeb = options.openWeb
    this.snapshot = this.buildSnapshot()
    this.unsubscribeInteractive = this.controller.subscribe(() => this.publish())

    void loadPromptHistory(this.promptHistoryFile).then(history => {
      if (!this.closed) this.promptHistory = history
    })
    if (options.resume) {
      void this.openThreadPicker()
    }
  }

  getSnapshot(): TuiAdapterSnapshot {
    return this.snapshot
  }

  subscribe(listener: (snapshot: TuiAdapterSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** 执行用户意图；表现层动作留在 Adapter，领域动作转发给共享 Controller。 */
  async dispatch(intent: TuiIntent): Promise<void> {
    if (this.closed) return
    switch (intent.type) {
      case "draft-input":
        this.updateDraft(intent.value)
        return
      case "submit":
        await this.submit(intent.value)
        return
      case "history":
        this.navigatePromptHistory(intent.direction)
        return
      case "execute-command":
        await this.executeCommand(intent.commandId, intent.argument)
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
        await this.controller.dispatch({ type: "skill.clear" })
        return
      case "approval":
        await this.respondApproval(intent.decision)
        return
      case "question":
        await this.respondQuestion(intent.answer)
        return
      case "tool-toggle":
        this.toggleTool(intent.toolId)
    }
  }

  /** 关闭 Adapter 自己的订阅；共享 Controller 由宿主负责关闭。 */
  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.unsubscribeInteractive()
  }

  /** 把共享 snapshot 与表现状态一起发布为新快照。 */
  private publish(): void {
    this.snapshot = this.buildSnapshot()
    for (const listener of [...this.listeners]) listener(this.snapshot)
  }

  /** 生成稳定的只读快照；数组和 Set 均不复用外部可变容器。 */
  private buildSnapshot(): TuiAdapterSnapshot {
    const interactive = this.controller.getSnapshot()
    const skills = interactive.catalogs.skills
    const threads = interactive.catalogs.threads
    const models = interactive.catalogs.models
    return {
      interactive,
      draft: this.draft,
      draftCursor: this.draftCursor,
      commandMenu: { ...this.commandMenu },
      commandOptions: this.filterCommandOptions(interactive.commands),
      selectedSkill: interactive.selection.armedSkill ?? undefined,
      skills: this.pickerSnapshot(this.skillPicker, filterSkills(skills.items, this.skillPicker.query), skills.status === "loading", skills.status === "error" ? skills.message : undefined),
      threads: this.pickerSnapshot(this.threadPicker, filterThreads(threadItems(threads.items), this.threadPicker.query), threads.status === "loading", threads.status === "error" ? threads.message : undefined),
      models: this.pickerSnapshot(this.modelPicker, filterModels(models.items, this.modelPicker.query), models.status === "loading", models.status === "error" ? models.message : undefined),
      commandDialog: this.commandDialog(interactive),
      modelBindingDialog: this.modelBindingDialog(interactive),
      transientNotice: this.transientNotice,
      showToolDetails: this.showToolDetails,
      expandedTools: new Set(this.expandedTools),
      scrollRequest: this.scrollRequest,
    }
  }

  /** 统一创建选择器 snapshot，防止三个 Picker 再维护三套展示结构。 */
  private pickerSnapshot<T>(picker: InternalPicker<T>, items: readonly T[], loading: boolean, error: string | undefined): PickerSnapshot<T> {
    return {
      visible: picker.visible,
      loading: picker.loading || loading,
      query: picker.query,
      selectedIndex: picker.selectedIndex,
      error: picker.error ?? error,
      syncingDefault: picker.syncingDefault,
      items: [...items],
    }
  }

  /** 命令菜单数据来自共享 snapshot，只按 draft 做显示过滤，不重新计算可用性。 */
  private filterCommandOptions(items: readonly CommandMenuItem[]): readonly CommandMenuItem[] {
    return filterCommandMenuItems(items, this.draft)
  }

  /** 更新 draft 并按同一规则控制 Slash 菜单。 */
  private updateDraft(value: string): void {
    if (this.historyApplyValue === value) this.historyApplyValue = undefined
    else this.promptHistoryCursor = undefined
    this.draftCursor = undefined
    this.draft = value
    const query = value.trimStart()
    const shouldShowMenu = query.startsWith("/")
      && !query.startsWith("//")
      && !query.slice(1).match(/\s/)
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

  /** 提交用户输入；共享 Controller 统一处理 Slash/转义/普通消息。 */
  private async submit(rawValue: string): Promise<void> {
    const input = rawValue.trim()
    if (!input) return
    this.clearDraft()
    const interactive = this.controller.getSnapshot()
    if (interactive.interaction?.type === "question") {
      const firstQuestion = interactive.interaction.questions[0]
      if (firstQuestion) {
        await this.controller.dispatch({
          type: "interaction.respond",
          requestId: interactive.interaction.requestId,
          response: { kind: "question", answers: { [firstQuestion.id]: [input] } },
        })
        return
      }
    }
    const previousHistory = this.promptHistory
    const nextHistory = rememberPrompt(previousHistory, input)
    this.promptHistory = nextHistory
    void persistPromptHistory(previousHistory, nextHistory, this.promptHistoryFile)
    this.scrollRequest += 1
    this.publish()
    await this.controller.dispatch({ type: "input.submit", value: rawValue })
  }

  /** 解析稳定命令 ID 后交给共享 Dispatcher。 */
  private async executeCommand(commandId: string, argument?: string): Promise<void> {
    await this.dispatchInteractive({ type: "command.execute", commandId, argument })
  }

  /** dispatch 共享 intent，并解释宿主级 InteractiveResult。 */
  private async dispatchInteractive(intent: InteractiveIntent): Promise<void> {
    const result = await this.controller.dispatch(intent)
    if (!result) return
    switch (result.type) {
      case "present":
        if (result.target === "threads") this.openThreadPicker()
        else if (result.target === "models") this.openModelPicker(result.initialQuery)
        else this.openSkillPicker()
        return
      case "request-handoff":
        if (!this.openWeb) {
          this.showTransientNotice("当前启动方式未提供 Web launcher。")
          return
        }
        try {
          await this.openWeb(result.threadId)
          this.showTransientNotice("Web 会话已启动，浏览器就绪并取得控制权后 TUI 将锁定。")
        } catch (error) {
          this.showTransientNotice(`Web 启动失败：${errorMessage(error)}`)
        }
        return
      case "request-exit":
        this.onRequestExit()
    }
  }

  /** 展示宿主级结果通知；这是 adapter 的表现状态，不进入共享 Timeline。 */
  private showTransientNotice(message: string): void {
    this.transientNotice = { id: crypto.randomUUID(), message }
    this.publish()
  }

  /** 将快捷键动作转成表现状态或共享 intent。 */
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
        await this.resolveDialog(this.snapshot.modelBindingDialog ? "model-binding" : "command", true)
        return
      case "cancel-command-dialog":
        await this.resolveDialog(this.snapshot.modelBindingDialog ? "model-binding" : "command", false)
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
        const resolution = resolveSlashCommand(this.draft)
        const value = this.draft
        this.clearDraft()
        if (resolution.kind === "unknown") {
          await this.controller.dispatch({ type: "input.submit", value })
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
        await this.controller.dispatch({ type: "run.cancel" })
        return
      case "toggle-tool-details":
        this.showToolDetails = !this.showToolDetails
        this.publish()
        return
      case "cycle-approval-mode":
        await this.controller.dispatch({ type: "approval-mode.cycle" })
        return
      case "clear-selected-skill":
        await this.controller.dispatch({ type: "skill.clear" })
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
    const directCommand = parseSlashCommand(this.draft)
    if (directCommand && !directCommand.argument) {
      this.clearDraft()
      await this.executeCommand(directCommand.id, directCommand.argument)
      return
    }
    const item = this.snapshot.commandOptions[this.commandMenu.selectedIndex]
    if (item) await this.selectCommandMenuItem(item)
  }

  /** 处理鼠标或键盘选中的命令/Skill。 */
  private async selectCommandMenuItem(item: CommandMenuItem): Promise<void> {
    if (item.kind === "skill") {
      this.selectSkill(item.skill)
      return
    }
    if (item.availability.state === "disabled") {
      this.showTransientNotice(`/${item.command.name} 暂不可用：${item.availability.reason}。`)
      return
    }
    const interactive = this.controller.getSnapshot()
    if (interactive.activeRun) {
      this.clearDraft()
      await this.dispatchInteractive({ type: "command.execute", commandId: item.command.id })
      return
    }
    const value = `/${item.command.name}`
    this.commandMenuDismissedValue = value
    this.draft = value
    this.draftCursor = "end"
    this.commandMenu = { visible: false, selectedIndex: 0 }
    this.publish()
  }

  /** 打开 Skill Picker；catalog 数据与状态来自共享 snapshot。 */
  private openSkillPicker(): void {
    this.skillPicker = { ...this.skillPicker, visible: true, loading: false, query: "", selectedIndex: 0, error: undefined }
    this.publish()
    void this.controller.dispatch({ type: "catalog.refresh", catalog: "skills" })
  }

  /** 打开 Thread Picker；运行态校验由共享 Controller 的 present 语义保证。 */
  private openThreadPicker(): void {
    this.threadPicker = { ...this.threadPicker, visible: true, loading: false, query: "", selectedIndex: 0, error: undefined }
    this.publish()
    void this.controller.dispatch({ type: "catalog.refresh", catalog: "threads" })
  }

  /** 打开 Model Picker；legacy immutable binding 由共享 Controller 发布 confirmation。 */
  private openModelPicker(initialQuery = ""): void {
    this.modelPicker = { ...this.modelPicker, visible: true, loading: false, query: initialQuery, selectedIndex: 0, error: undefined, syncingDefault: undefined }
    this.publish()
    void this.controller.dispatch({ type: "catalog.refresh", catalog: "models" })
  }

  /** 关闭 Picker；保存默认模型期间不允许通过 Esc 打断事务。 */
  private closePicker(picker: PickerKind): void {
    if (picker === "models" && this.modelPicker.syncingDefault) return
    if (picker === "skills") this.skillPicker = { ...this.skillPicker, visible: false, loading: false, error: undefined }
    if (picker === "threads") this.threadPicker = { ...this.threadPicker, visible: false, loading: false, error: undefined }
    if (picker === "models") this.modelPicker = { ...this.modelPicker, visible: false, loading: false, error: undefined, syncingDefault: undefined }
    this.publish()
  }

  /** 更新 Picker 搜索词；过滤留在 Adapter，共享 catalog 不被污染。 */
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
    const interactive = this.controller.getSnapshot()
    const items = picker === "skills"
      ? filterSkills(interactive.catalogs.skills.items, this.skillPicker.query)
      : picker === "threads"
        ? filterThreads(threadItems(interactive.catalogs.threads.items), this.threadPicker.query)
        : filterModels(interactive.catalogs.models.items, this.modelPicker.query)
    const current = picker === "skills" ? this.skillPicker : picker === "threads" ? this.threadPicker : this.modelPicker
    this.updatePickerIndex(picker, items.length ? (current.selectedIndex + direction + items.length) % items.length : 0)
  }

  /** 选择当前 Skill，并把它附着到下一次真实消息。 */
  private selectVisibleSkill(): void {
    const interactive = this.controller.getSnapshot()
    const selected = filterSkills(interactive.catalogs.skills.items, this.skillPicker.query)[this.skillPicker.selectedIndex]
    if (selected) this.selectSkill(selected)
  }

  /** 选择当前 Thread，并恢复 sidecar 返回的历史。 */
  private async selectVisibleThread(): Promise<void> {
    const interactive = this.controller.getSnapshot()
    const selected = filterThreads(threadItems(interactive.catalogs.threads.items), this.threadPicker.query)[this.threadPicker.selectedIndex]
    if (selected) await this.selectThread(selected)
  }

  /** 选择当前模型 Profile。 */
  private async selectVisibleModel(): Promise<void> {
    const interactive = this.controller.getSnapshot()
    const selected = filterModels(interactive.catalogs.models.items, this.modelPicker.query)[this.modelPicker.selectedIndex]
    if (selected) await this.selectModel(selected)
  }

  /** Skill 选择会清掉搜索草稿，但不影响当前 Thread。 */
  private selectSkill(skill: SkillMenuItem): void {
    this.clearDraft()
    this.skillPicker = { ...this.skillPicker, visible: false, loading: false, query: "", selectedIndex: 0 }
    void this.controller.dispatch({ type: "skill.arm", skillId: skill.id })
  }

  /** 恢复 Thread；共享 Controller 完成原子替换与 generation 校验。 */
  private async selectThread(thread: ThreadPickerItem): Promise<void> {
    this.threadPicker = { ...this.threadPicker, visible: false, loading: false, error: undefined }
    this.publish()
    await this.controller.dispatch({ type: "thread.open", threadId: thread.threadId })
  }

  /** 选择模型；共享 Controller 更新当前选择并独立同步默认值。 */
  private async selectModel(model: ModelProfile): Promise<void> {
    if (this.modelPicker.loading) return
    if (!model.available) {
      this.modelPicker = { ...this.modelPicker, error: `${model.provider_label} · ${model.model} 不可用：${model.unavailable_reason ?? "配置不可用"}` }
      this.publish()
      return
    }
    this.modelPicker = { ...this.modelPicker, syncingDefault: true, error: undefined }
    this.publish()
    try {
      await this.controller.dispatch({ type: "model.select", profileId: model.id })
    } finally {
      this.modelPicker = { ...this.modelPicker, visible: false, loading: false, syncingDefault: false, error: undefined }
      this.publish()
    }
  }

  /** 执行 confirmation 的确认动作；共享 Controller 解释 confirmationId。 */
  private async resolveDialog(kind: "command" | "model-binding", confirmed: boolean): Promise<void> {
    const interactive = this.controller.getSnapshot()
    const confirmation = interactive.confirmation
    if (!confirmation) return
    await this.controller.dispatch({
      type: "confirmation.resolve",
      confirmationId: confirmation.confirmationId,
      confirmed,
    })
  }

  /** 回写审批结果；共享 Controller 校验 allowlist 并组装 wire response。 */
  private async respondApproval(decision: ApprovalDecision): Promise<void> {
    const interactive = this.controller.getSnapshot()
    const approval = interactive.interaction
    if (!approval || approval.type !== "approval") return
    await this.controller.dispatch({
      type: "interaction.respond",
      requestId: approval.requestId,
      response: { kind: "approval", decision },
    })
  }

  /** 回写当前问题答案；TUI 仍按首题回答，完整多题由共享 Controller 校验。 */
  private async respondQuestion(answer: string): Promise<void> {
    const interactive = this.controller.getSnapshot()
    const question = interactive.interaction
    if (!question || question.type !== "question") return
    const firstQuestion = question.questions[0]
    if (!firstQuestion) return
    await this.controller.dispatch({
      type: "interaction.respond",
      requestId: question.requestId,
      response: { kind: "question", answers: { [firstQuestion.id]: [answer] } },
    })
  }

  /** 切换单个工具卡片展开状态。 */
  private toggleTool(toolId: string): void {
    const next = new Set(this.expandedTools)
    if (next.has(toolId)) next.delete(toolId)
    else next.add(toolId)
    this.expandedTools = next
    this.publish()
  }

  /** 从共享 confirmation 派生命令确认 Dialog。 */
  private commandDialog(snapshot: InteractiveSnapshot): TuiAdapterSnapshot["commandDialog"] {
    const confirmation = snapshot.confirmation
    if (confirmation?.confirmationId !== "clear-thread") return undefined
    return {
      kind: "confirm-new-thread",
      title: confirmation.title,
      message: confirmation.message,
    }
  }

  /** 从共享 confirmation 派生模型绑定 Dialog。 */
  private modelBindingDialog(snapshot: InteractiveSnapshot): TuiAdapterSnapshot["modelBindingDialog"] {
    const confirmation = snapshot.confirmation
    if (confirmation?.confirmationId !== "model-binding") return undefined
    return {
      title: confirmation.title,
      message: confirmation.message,
    }
  }
}

/** 创建 TUI Adapter；它组合共享 InteractiveController 与终端表现状态。 */
export function createTuiAdapter(options: TuiAdapterOptions): TuiAdapter {
  return new TuiAdapterImpl(options)
}

function emptyPicker<T>(): InternalPicker<T> {
  return { visible: false, loading: false, query: "", selectedIndex: 0 }
}

function threadItems(items: readonly ThreadSummary[]): readonly ThreadPickerItem[] {
  return items.map(thread => ({
    threadId: thread.thread_id,
    createdAtMs: thread.created_at_ms,
    updatedAtMs: thread.updated_at_ms,
    firstMessage: thread.first_message,
    latestMessage: thread.latest_message,
    messageCount: thread.message_count,
  }))
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
