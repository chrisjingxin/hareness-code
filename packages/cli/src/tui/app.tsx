/** OpenTUI 应用根：协调 IPC 事件、输入状态、快捷键、历史和界面生命周期。 */

import { createCliRenderer, type KeyEvent, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { createRoot, useKeyboard, useTerminalDimensions } from "@opentui/react"
import { randomUUID } from "node:crypto"
import { useCallback, useEffect, useRef, useState } from "react"
import type { InteractionRequestEnvelope, InteractionResponse, RequestedSkill } from "@za38/protocol"

import { IpcClient } from "../ipc/client"
import {
  findCommandMenuItems,
  parseSlashCommand,
  slashCommandHelp,
  type CommandMenuItem,
  type SkillMenuItem,
  type SlashCommand,
} from "./commands"
import { HomeView, SessionView, SkillPicker, type CommandMenuState, type SelectedSkill } from "./components"
import { TuiErrorBoundary } from "./error-boundary"
import { runtimeStatusSummary, type TuiRuntime } from "./model"
import {
  loadPromptHistory,
  movePromptHistory,
  persistPromptHistory,
  rememberPrompt,
  type PromptHistoryCursor,
} from "./prompt-history"
import { resolveShortcut } from "./shortcuts"
import { registerCommonSyntaxParsers } from "./syntax-parsers"
import {
  appendNotice,
  applyAgentEvent,
  applyInteractionRequest,
  clearPendingInteraction,
  clearThread,
  createInitialState,
  isHomeState,
  markCancelling,
  markRunFailed,
  startRun,
  type TuiState,
} from "./state"

type TuiOptions = {
  client: IpcClient
  runtime: TuiRuntime
  threadId?: string
  /** 仅供测试隔离本地历史；正式入口始终使用 ~/.harness/prompt-history.jsonl。 */
  promptHistoryFile?: string
  onRequestExit: () => void
}

type SkillPickerState = {
  visible: boolean
  loading: boolean
  query: string
  selectedIndex: number
  error?: string
}

type SkillsListResult = {
  skills?: unknown[]
}

/** 正式 OpenTUI 根组件：所有 Agent 输出必须经状态归约后才进入终端。 */
export function Za38Tui({ client, runtime, threadId, promptHistoryFile, onRequestExit }: TuiOptions) {
  const [state, setState] = useState(() => createInitialState(threadId))
  const stateRef = useRef(state)
  const [draft, setDraft] = useState("")
  const inputRef = useRef<TextareaRenderable | null>(null)
  const conversationScrollRef = useRef<ScrollBoxRenderable | null>(null)
  const [commandMenu, setCommandMenu] = useState<CommandMenuState>({ visible: false, selectedIndex: 0 })
  const [skills, setSkills] = useState<readonly SkillMenuItem[]>([])
  const [skillPicker, setSkillPicker] = useState<SkillPickerState>({ visible: false, loading: false, query: "", selectedIndex: 0 })
  const [selectedSkill, setSelectedSkill] = useState<SelectedSkill | undefined>(undefined)
  const commandMenuDismissedValue = useRef<string | undefined>(undefined)
  const [showToolDetails, setShowToolDetails] = useState(false)
  const [expandedTools, setExpandedTools] = useState<ReadonlySet<string>>(() => new Set())
  const promptHistoryRef = useRef<string[]>([])
  const promptHistoryCursorRef = useRef<PromptHistoryCursor | undefined>(undefined)
  const interactionResolversRef = useRef(new Map<string, {
    request: InteractionRequestEnvelope
    resolve: (response: InteractionResponse) => void
  }>())
  const historyApplyValueRef = useRef<string | undefined>(undefined)
  const skillSearchRef = useRef<TextareaRenderable | null>(null)
  const terminal = useTerminalDimensions()

  /** 提交不可变状态转换；长生命周期 IPC 回调通过 ref 读取最新状态。 */
  const commit = useCallback((transition: (current: TuiState) => TuiState) => {
    const next = transition(stateRef.current)
    stateRef.current = next
    setState(next)
  }, [])

  /** 读取启动快照中的 Skill 摘要；正文仍只在选中后由 sidecar 按需加载。 */
  const refreshSkills = useCallback(async (): Promise<readonly SkillMenuItem[]> => {
    const result = await client.call("skills.list", { include_disabled: false }) as SkillsListResult
    const next = Array.isArray(result.skills)
      ? result.skills.map(skillMenuItem).filter((item): item is SkillMenuItem => item !== undefined)
      : []
    setSkills(next)
    return next
  }, [client])

  useEffect(() => {
    const settleAbandoned = (requestId: string | undefined) => {
      if (!requestId) return
      const pending = interactionResolversRef.current.get(requestId)
      if (!pending) return
      client.abandonInteraction(requestId)
      interactionResolversRef.current.delete(requestId)
      pending.resolve(pending.request.type === "approval"
        ? { type: "approval", request_id: requestId, decision: "reject" }
        : { type: "question", request_id: requestId, answers: {} })
    }
    const eventListener = (event: import("@za38/protocol").EventEnvelope) => {
      if (["interaction.resolved", "run.completed", "run.cancelled", "run.failed"].includes(event.type)) {
        settleAbandoned(stateRef.current.pendingApproval?.requestId ?? stateRef.current.pendingQuestion?.requestId)
      }
      commit(current => applyAgentEvent(current, event))
    }
    client.on("event", eventListener)
    const clearRequestHandler = client.setRequestHandler(request => new Promise(resolve => {
      interactionResolversRef.current.set(request.request_id, { request, resolve })
      commit(current => applyInteractionRequest(current, request))
    }))
    const protocolError = (error: Error) => commit(current => appendNotice(current, `协议错误：${error.message}`))
    const closed = (error: Error) => commit(current => appendNotice(current, `Agent 连接已关闭：${error.message}`))
    client.on("protocolError", protocolError)
    client.on("close", closed)

    return () => {
      client.off("event", eventListener)
      clearRequestHandler()
      for (const [requestId, pending] of interactionResolversRef.current) {
        client.abandonInteraction(requestId)
        pending.resolve(pending.request.type === "approval"
          ? { type: "approval", request_id: requestId, decision: "reject" }
          : { type: "question", request_id: requestId, answers: {} })
      }
      interactionResolversRef.current.clear()
      client.off("protocolError", protocolError)
      client.off("close", closed)
    }
  }, [client, commit])

  useEffect(() => {
    void refreshSkills().catch(() => {
      // 目录读取失败不阻断正常对话；用户打开 /skills 时会得到明确错误。
    })
  }, [refreshSkills])

  useEffect(() => {
    let disposed = false
    void loadPromptHistory(promptHistoryFile).then(history => {
      if (!disposed) promptHistoryRef.current = history
    })
    return () => { disposed = true }
  }, [promptHistoryFile])

  /** 取消当前运行；重复按取消键时把退出意图交给根生命周期。 */
  const cancelActiveRun = useCallback(async () => {
    const active = stateRef.current.activeRun
    if (!active) return false
    if (stateRef.current.status === "正在取消") {
      onRequestExit()
      return true
    }
    commit(markCancelling)
    try {
      await client.cancel(active.threadId, active.runId)
    } catch (error) {
      commit(current => markRunFailed(current, active.runId, errorMessage(error)))
    }
    return true
  }, [client, commit, onRequestExit])

  /** 登记用户消息、发起 run.start，并校验 sidecar 返回的 run 标识。 */
  const sendAgentMessage = useCallback(async (message: string, requestedSkill?: RequestedSkill) => {
    const current = stateRef.current
    if (current.activeRun) {
      commit(state => appendNotice(state, "当前会话仍在执行；请等待、审批或按 Ctrl+C 取消。"))
      return
    }
    const run = {
      threadId: current.threadId ?? randomUUID(),
      runId: randomUUID(),
    }
    const armedSkill = requestedSkill ?? (selectedSkill
      ? { id: selectedSkill.id, args: message }
      : undefined)
    if (armedSkill && !requestedSkill) setSelectedSkill(undefined)
    commit(state => startRun(state, run, message))
    try {
      const accepted = await client.query(message, run.threadId, run.runId, armedSkill)
      if (!accepted.accepted || accepted.thread_id !== run.threadId || accepted.run_id !== run.runId) {
        throw new Error("Agent 返回的 run 标识与请求不一致")
      }
    } catch (error) {
      commit(state => markRunFailed(state, run.runId, errorMessage(error)))
    }
  }, [client, commit, selectedSkill])

  /** 解析 Agent 发起的审批 request，由 JsonRpcPeer 自动回写标准 response。 */
  const respondApproval = useCallback(async (decision: "approve" | "reject") => {
    const { pendingApproval } = stateRef.current
    if (!pendingApproval?.requestId) return
    const pending = interactionResolversRef.current.get(pendingApproval.requestId)
    if (!pending) return
    interactionResolversRef.current.delete(pendingApproval.requestId)
    commit(state => clearPendingInteraction(state, decision === "approve" ? "approved" : "rejected"))
    pending.resolve({
      type: "approval",
      request_id: pendingApproval.requestId,
      decision: decision === "approve" ? "approve_once" : "reject",
    })
  }, [commit])

  /** 将当前首题回答映射到稳定 question ID；多题表单将在后续 TUI 任务扩展。 */
  const respondQuestion = useCallback(async (answer: string) => {
    const { pendingQuestion } = stateRef.current
    if (!pendingQuestion?.requestId) return
    const pending = interactionResolversRef.current.get(pendingQuestion.requestId)
    if (!pending) return
    interactionResolversRef.current.delete(pendingQuestion.requestId)
    commit(state => clearPendingInteraction(state, "answered"))
    pending.resolve({
      type: "question",
      request_id: pendingQuestion.requestId,
      answers: { [pendingQuestion.questionId]: [answer] },
    })
  }, [commit])

  /** 打开搜索选择器并刷新当前 sidecar 固定快照中的 Skill 摘要。 */
  const openSkillPicker = useCallback(() => {
    setSkillPicker({ visible: true, loading: true, query: "", selectedIndex: 0 })
    void refreshSkills().then(() => {
      setSkillPicker(current => current.visible ? { ...current, loading: false } : current)
    }).catch(error => {
      setSkillPicker(current => current.visible
        ? { ...current, loading: false, error: `Skill catalog 读取失败：${errorMessage(error)}` }
        : current)
    })
  }, [refreshSkills])

  /** 筛选保持在纯视图层，避免每次搜索重新请求 sidecar 或改变 snapshot。 */
  const visibleSkills = filterSkills(skills, skillPicker.query)

  /** 执行本地控制命令和 Skill 入口；目录读取失败只显示可恢复通知。 */
  const executeSlashCommand = useCallback(async (command: SlashCommand) => {
    switch (command.name) {
      case "help":
        commit(current => appendNotice(current, slashCommandHelp.map(item => `${item.command}  ${item.description}`).join("\n")))
        return
      case "quit":
        onRequestExit()
        return
      case "clear":
        if (stateRef.current.activeRun) {
          commit(current => appendNotice(current, "请先等待当前执行结束，或使用 /force-clear。"))
        } else {
          commit(clearThread)
        }
        return
      case "force-clear":
        void cancelActiveRun().then(() => commit(clearThread))
        return
      case "status":
        commit(current => appendNotice(current, runtimeStatusSummary(runtime)))
        return
      case "version":
        commit(current => appendNotice(current, `za38-cli ${runtime.cliVersion} · JSON-RPC v2`))
        return
      case "skills":
        openSkillPicker()
        return
      case "skill": {
        const [skillId, ...argParts] = (command.argument ?? "").split(/\s+/).filter(Boolean)
        if (!skillId) {
          commit(current => appendNotice(current, "用法：/skill <id> [args] 会直接运行；输入 /skills 打开选择器"))
          return
        }
        const args = argParts.join(" ")
        await sendAgentMessage(args || "请按照该 Skill 的说明处理当前任务。", { id: skillId, args })
        return
      }
    }
  }, [cancelActiveRun, commit, onRequestExit, openSkillPicker, runtime, sendAgentMessage])

  /** 同步 textarea 草稿、命令菜单过滤状态和历史游标。 */
  const updateDraft = useCallback((value: string) => {
    // 回填历史会触发 textarea 的内容事件；仅它保留历史游标，用户编辑则立即退出历史浏览。
    if (historyApplyValueRef.current === value) historyApplyValueRef.current = undefined
    else promptHistoryCursorRef.current = undefined
    setDraft(value)
    const slashQuery = value.trimStart()
    // 输入完整的本地命令后收起菜单，让 Enter 直接执行；未完成前缀继续保留
    // 筛选菜单，以支持 `/st` + Enter 的补全工作流。
    const exactLocalCommand = parseSlashCommand(slashQuery)
    const shouldShowMenu = slashQuery.startsWith("/")
      && !slashQuery.slice(1).match(/\s/)
      && !exactLocalCommand
    if (shouldShowMenu && commandMenuDismissedValue.current !== value) {
      setCommandMenu({ visible: true, selectedIndex: 0 })
      return
    }
    if (!shouldShowMenu) commandMenuDismissedValue.current = undefined
    setCommandMenu(current => current.visible ? { ...current, visible: false } : current)
  }, [])

  /** 清空 textarea 内部缓冲区和 React 草稿状态。 */
  const clearDraft = useCallback(() => {
    inputRef.current?.clear()
    commandMenuDismissedValue.current = undefined
    promptHistoryCursorRef.current = undefined
    historyApplyValueRef.current = undefined
    setDraft("")
    setCommandMenu({ visible: false, selectedIndex: 0 })
  }, [])

  /** 选择 Skill 会清掉用于筛选的 Slash 草稿，随后回到 composer 等待真实任务。 */
  const selectSkill = useCallback((skill: SkillMenuItem) => {
    clearDraft()
    setSelectedSkill(skill)
    setSkillPicker({ visible: false, loading: false, query: "", selectedIndex: 0 })
  }, [clearDraft])

  /** 用历史项或命令项替换草稿，并把光标放到指定端点。 */
  const replaceDraft = useCallback((value: string, cursor: "start" | "end" = "end", historyCursor?: PromptHistoryCursor) => {
    // setText 会同步 textarea 内部缓冲区，不能只更新 React state，否则 Enter 会发送旧内容。
    promptHistoryCursorRef.current = historyCursor
    historyApplyValueRef.current = value
    inputRef.current?.setText(value)
    if (cursor === "start") inputRef.current?.gotoBufferHome()
    else inputRef.current?.gotoBufferEnd()
    commandMenuDismissedValue.current = undefined
    setDraft(value)
    setCommandMenu({ visible: false, selectedIndex: 0 })
  }, [])

  /** 在真实编辑缓冲区上移动提示词历史游标。 */
  const navigatePromptHistory = useCallback((direction: "previous" | "next"): boolean => {
    const input = inputRef.current
    const move = movePromptHistory(promptHistoryRef.current, input?.plainText ?? draft, promptHistoryCursorRef.current, direction)
    if (!move) return false
    replaceDraft(move.value, direction === "previous" ? "start" : "end", move.cursor)
    return true
  }, [draft, replaceDraft])

  /** 选择静态命令时补全输入；选择 Skill 时进入下一条消息的一次性上下文。 */
  const selectCommandMenuItem = useCallback((item: CommandMenuItem) => {
    if (item.kind === "skill") {
      selectSkill(item.skill)
      return
    }
    const value = `/${item.command.name}`
    commandMenuDismissedValue.current = value
    inputRef.current?.setText(value)
    inputRef.current?.gotoBufferEnd()
    setDraft(value)
    setCommandMenu({ visible: false, selectedIndex: 0 })
  }, [selectSkill])

  /** 通过 `/` 或 Ctrl+P 打开命令菜单并补齐命令前缀。 */
  const openCommandMenu = useCallback(() => {
    const value = draft.trimStart()
    if (!value.startsWith("/") || value.slice(1).match(/\s/)) {
      inputRef.current?.setText("/")
      inputRef.current?.gotoBufferEnd()
      setDraft("/")
    }
    commandMenuDismissedValue.current = undefined
    setCommandMenu({ visible: true, selectedIndex: 0 })
  }, [draft])

  /** 处理 Enter 提交：问答、Slash Command 和普通 Agent 消息走不同路径。 */
  const handleSubmit = useCallback(() => {
    const input = (inputRef.current?.plainText ?? draft).trim()
    if (!input) return
    // OpenTUI Input 会保留内部编辑缓冲区，提交后需主动清空，不能只依赖 React state。
    clearDraft()
    if (stateRef.current.pendingQuestion) {
      void respondQuestion(input)
      return
    }
    const command = parseSlashCommand(input)
    if (command) {
      void executeSlashCommand(command)
      return
    }
    const previousHistory = promptHistoryRef.current
    const nextHistory = rememberPrompt(previousHistory, input)
    promptHistoryRef.current = nextHistory
    promptHistoryCursorRef.current = undefined
    void persistPromptHistory(previousHistory, nextHistory, promptHistoryFile)
    void sendAgentMessage(input)
  }, [clearDraft, draft, executeSlashCommand, respondQuestion, sendAgentMessage])

  /** 按行或半页滚动当前会话，供空 composer 的方向键使用。 */
  const scrollConversationBy = useCallback((amount: "line-up" | "line-down" | "page-up" | "page-down") => {
    const scroll = conversationScrollRef.current
    if (!scroll || scroll.isDestroyed) return false
    const delta = amount === "line-up"
      ? -1
      : amount === "line-down"
        ? 1
        : amount === "page-up"
          ? -Math.max(1, Math.floor(scroll.height / 2))
          : Math.max(1, Math.floor(scroll.height / 2))
    scroll.scrollBy(delta)
    return true
  }, [])

  /** 在 textarea 层处理历史与会话滚动，避免全局 key handler 抢走方向键。 */
  const handleComposerKeyDown = useCallback((key: KeyEvent) => {
    // Slash 菜单由全局快捷键优先处理，不能在 textarea 内重复消费方向键。
    if (commandMenu.visible || skillPicker.visible) return
    const input = inputRef.current
    if (!input) return

    const atStart = input.cursorOffset === 0
    const atEnd = input.cursorOffset === input.plainText.length
    if (key.name === "up" && atStart && navigatePromptHistory("previous")) {
      key.preventDefault()
      return
    }
    if (key.name === "down" && atEnd && navigatePromptHistory("next")) {
      key.preventDefault()
      return
    }

    // 只有空 composer 才借出方向键给会话；编辑任何文本时完全保持 textarea 原生语义。
    if (!input.plainText && !isHomeState(stateRef.current)) {
      const scrollAction = key.name === "up" ? "line-up"
        : key.name === "down" ? "line-down"
          : key.name === "pageup" ? "page-up"
            : key.name === "pagedown" ? "page-down"
              : undefined
      if (scrollAction && scrollConversationBy(scrollAction)) key.preventDefault()
    }
  }, [commandMenu.visible, navigatePromptHistory, scrollConversationBy, skillPicker.visible])

  useKeyboard(key => {
    const commandOptions = findCommandMenuItems(draft, skills)
    const action = resolveShortcut(key, {
      skillPickerVisible: skillPicker.visible,
      skillOptionCount: visibleSkills.length,
      commandMenuVisible: commandMenu.visible,
      commandOptionCount: commandOptions.length,
      activeRun: Boolean(stateRef.current.activeRun),
      hasDraft: Boolean(draft),
    })
    if (action === "none") return
    key.preventDefault()

    if (action === "close-skill-picker") {
      setSkillPicker(current => ({ ...current, visible: false, loading: false, error: undefined }))
      return
    }
    if (action === "skill-previous" || action === "skill-next") {
      const direction = action === "skill-previous" ? -1 : 1
      setSkillPicker(current => ({
        ...current,
        selectedIndex: visibleSkills.length ? (current.selectedIndex + direction + visibleSkills.length) % visibleSkills.length : 0,
      }))
      return
    }
    if (action === "skill-select") {
      const selected = visibleSkills[skillPicker.selectedIndex]
      if (selected) selectSkill(selected)
      return
    }
    if (action === "skill-block") return
    if (action === "close-command-menu") {
      commandMenuDismissedValue.current = draft
      setCommandMenu(current => ({ ...current, visible: false }))
      return
    }
    if (action === "command-previous" || action === "command-next") {
      const direction = action === "command-previous" ? -1 : 1
      setCommandMenu(current => ({
        ...current,
        selectedIndex: commandOptions.length ? (current.selectedIndex + direction + commandOptions.length) % commandOptions.length : 0,
      }))
      return
    }
    if (action === "command-select") {
      // 手输完整本地命令时，Enter 应与普通提交一致地直接执行；只有 `/st`
      // 这类未完成前缀才由菜单补全，避免每个命令都需要按两次 Enter。
      const directCommand = parseSlashCommand(inputRef.current?.plainText ?? draft)
      if (directCommand && !directCommand.argument) {
        clearDraft()
        void executeSlashCommand(directCommand)
        return
      }
      const selected = commandOptions[commandMenu.selectedIndex]
      if (selected) selectCommandMenuItem(selected)
      return
    }
    if (action === "command-block") return
    if (action === "command-open") {
      openCommandMenu()
      return
    }
    if (action === "clear-draft") {
      clearDraft()
      return
    }
    if (action === "cancel-run") {
      void cancelActiveRun()
      return
    }
    if (action === "toggle-tool-details") {
      setShowToolDetails(current => !current)
      return
    }
    if (action === "clear-selected-skill") {
      setSelectedSkill(undefined)
      return
    }
    if (action === "exit") onRequestExit()
  })

  /** 切换单个工具卡片的展开状态。 */
  const toggleTool = useCallback((toolId: string) => {
    setExpandedTools(current => {
      const next = new Set(current)
      if (next.has(toolId)) next.delete(toolId)
      else next.add(toolId)
      return next
    })
  }, [])

  const viewProps = {
    runtime,
    state,
    terminalWidth: terminal.width,
    terminalHeight: terminal.height,
    inputRef,
    conversationScrollRef,
    value: draft,
    onInput: updateDraft,
    onComposerKeyDown: handleComposerKeyDown,
    onSubmit: handleSubmit,
    commandMenu,
    commandOptions: findCommandMenuItems(draft, skills),
    onSelectCommand: selectCommandMenuItem,
    onHoverCommand: (selectedIndex: number) => setCommandMenu(current => ({ ...current, selectedIndex })),
    selectedSkill,
    skillPickerVisible: skillPicker.visible,
    onClearSelectedSkill: () => setSelectedSkill(undefined),
    showToolDetails,
    expandedTools,
    onToggleTool: toggleTool,
    onApproval: (decision: "approve" | "reject") => { void respondApproval(decision) },
    onQuestion: (answer: string) => { void respondQuestion(answer) },
  }

  return (
    <box position="relative" flexGrow={1}>
      {isHomeState(state) ? <HomeView {...viewProps} /> : <SessionView {...viewProps} />}
      <SkillPicker
        visible={skillPicker.visible}
        loading={skillPicker.loading}
        error={skillPicker.error}
        skills={visibleSkills}
        query={skillPicker.query}
        selectedIndex={skillPicker.selectedIndex}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        searchRef={skillSearchRef}
        onSearch={query => setSkillPicker(current => ({ ...current, query, selectedIndex: 0 }))}
        onSelect={selectSkill}
        onHover={selectedIndex => setSkillPicker(current => ({ ...current, selectedIndex }))}
      />
    </box>
  )
}

/** 创建 OpenTUI renderer、挂载错误边界；退出时将控制权交回 CLI 关闭 Python sidecar。 */
export async function runTui(options: TuiOptions): Promise<void> {
  registerCommonSyntaxParsers()
  // 与 OpenCode 保持一致：renderer 直接占用终端，避免外层控制台捕获 Warp 的能力响应。
  const renderer = await createCliRenderer({
    externalOutputMode: "passthrough",
    targetFps: 60,
    maxFps: 60,
    gatherStats: false,
    exitOnCtrlC: false,
    clearOnShutdown: true,
    useKittyKeyboard: {},
    autoFocus: false,
    openConsoleOnError: false,
    useMouse: true,
  })
  const root = createRoot(renderer)
  await new Promise<void>(resolve => {
    let closed = false
    const close = () => {
      if (closed) return
      closed = true
      root.unmount()
      renderer.destroy()
      resolve()
    }
    root.render(
      <TuiErrorBoundary onRequestExit={close}>
        <Za38Tui {...options} onRequestExit={close} />
      </TuiErrorBoundary>,
    )
  })
}

/** 将未知异常转换为可安全展示的字符串。 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 将不可信 RPC 摘要收敛为 TUI 需要的字段，避免字段缺失破坏 Slash 菜单。 */
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

/** 用名称、canonical ID、来源和描述过滤可用目录，匹配逻辑保持大小写无关。 */
function filterSkills(skills: readonly SkillMenuItem[], query: string): readonly SkillMenuItem[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return skills
  return skills.filter(skill => [skill.id, skill.name, skill.source, skill.description]
    .some(value => value.toLowerCase().includes(needle)))
}
