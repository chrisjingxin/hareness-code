/** OpenTUI 应用根：协调 IPC 事件、输入状态、快捷键、历史和界面生命周期。 */

import { createCliRenderer, type KeyEvent, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { createRoot, useKeyboard, useTerminalDimensions } from "@opentui/react"
import { randomUUID } from "node:crypto"
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import type { InteractionRequestEnvelope, InteractionResponse, ModelProfile, RequestedSkill, ThreadMessage } from "@za38/protocol"

import { IpcClient } from "../ipc/client"
import {
  defaultCommandContext,
  findCommandMenuItems,
  parseSlashCommand,
  resolveSlashCommand,
  unknownCommandNotice,
  type CommandMenuItem,
  type SkillMenuItem,
  type SlashCommand,
} from "./commands"
import { dispatchSlashCommand, type CommandDialog, type CommandResult } from "./command-dispatcher"
import { HomeView, SkillPicker, ThreadPicker, ThreadView, type CommandMenuState, type SelectedSkill, type ThreadPickerItem } from "./components"
import { TuiErrorBoundary } from "./error-boundary"
import { runtimeStatusSummary, type TuiRuntime } from "./model"
import { DialogShell, SearchPicker, type SearchPickerRenderContext } from "./overlays"
import {
  loadPromptHistory,
  movePromptHistory,
  persistPromptHistory,
  rememberPrompt,
  type PromptHistoryCursor,
} from "./prompt-history"
import { resolveShortcut, type ScrollIntent } from "./shortcuts"
import { registerCommonSyntaxParsers } from "./syntax-parsers"
import { tuiTheme } from "./theme"
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
  restoreThread,
  startRun,
  type TuiState,
} from "./state"

type TuiOptions = {
  client: IpcClient
  runtime: TuiRuntime
  /** 启动后立即打开恢复选择器；选择器而非参数负责持有内部 thread_id。 */
  resume?: boolean
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

type ThreadPickerState = {
  visible: boolean
  loading: boolean
  query: string
  selectedIndex: number
  error?: string
}

type ModelPickerState = {
  visible: boolean
  loading: boolean
  query: string
  selectedIndex: number
  error?: string
}

type ModelBindingDialog = {
  title: string
  message: string
}

/** 正式 OpenTUI 根组件：所有 Agent 输出必须经状态归约后才进入终端。 */
export function Za38Tui({ client, runtime, resume, promptHistoryFile, onRequestExit }: TuiOptions) {
  const [state, setState] = useState(() => createInitialState())
  const stateRef = useRef(state)
  const [draft, setDraft] = useState("")
  const inputRef = useRef<TextareaRenderable | null>(null)
  const conversationScrollRef = useRef<ScrollBoxRenderable | null>(null)
  const [commandMenu, setCommandMenu] = useState<CommandMenuState>({ visible: false, selectedIndex: 0 })
  const [skills, setSkills] = useState<readonly SkillMenuItem[]>([])
  const [skillPicker, setSkillPicker] = useState<SkillPickerState>({ visible: false, loading: false, query: "", selectedIndex: 0 })
  const [threadPicker, setThreadPicker] = useState<ThreadPickerState>({ visible: false, loading: false, query: "", selectedIndex: 0 })
  const [modelPicker, setModelPicker] = useState<ModelPickerState>({ visible: false, loading: false, query: "", selectedIndex: 0 })
  const [models, setModels] = useState<readonly ModelProfile[]>([])
  const [pendingModelProfile, setPendingModelProfile] = useState<string | undefined>(undefined)
  const [modelBindingDialog, setModelBindingDialog] = useState<ModelBindingDialog | undefined>(undefined)
  const [commandDialog, setCommandDialog] = useState<CommandDialog | undefined>(undefined)
  const [threads, setThreads] = useState<readonly ThreadPickerItem[]>([])
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
  const threadSearchRef = useRef<TextareaRenderable | null>(null)
  const modelSearchRef = useRef<TextareaRenderable | null>(null)
  const initialResumeRef = useRef(resume === true)
  const openingThreadRef = useRef(false)
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

  /** 读取当前 project 的 thread 摘要；renderer 后续只使用摘要，不显示内部 ID。 */
  const refreshThreads = useCallback(async (): Promise<readonly ThreadPickerItem[]> => {
    const result = await client.listThreads()
    const next = result.threads.map(threadPickerItem).filter((item): item is ThreadPickerItem => item !== undefined)
    setThreads(next)
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

  /** 取消当前运行；交互式确认流程不会把重复取消误解为退出应用。 */
  const cancelActiveRun = useCallback(async ({ exitOnRepeatedCancellation = true }: { exitOnRepeatedCancellation?: boolean } = {}) => {
    const active = stateRef.current.activeRun
    if (!active) return false
    if (stateRef.current.status === "正在取消") {
      if (exitOnRepeatedCancellation) onRequestExit()
      return false
    }
    commit(markCancelling)
    try {
      const result = await client.cancel(active.threadId, active.runId)
      if (!result.cancelled || result.run_id !== active.runId) throw new Error("Agent 未确认取消当前运行")
      return true
    } catch (error) {
      commit(current => markRunFailed(current, active.runId, errorMessage(error)))
      return false
    }
  }, [client, commit, onRequestExit])

  /** 登记用户消息、发起 run.start，并校验 sidecar 返回的 run 标识。 */
  const sendAgentMessage = useCallback(async (message: string, requestedSkill?: RequestedSkill) => {
    const current = stateRef.current
    if (current.activeRun) {
      commit(state => appendNotice(state, "当前 thread 仍在执行；请等待、审批或按 Ctrl+C 取消。"))
      return
    }
    const run = {
      threadId: current.threadId ?? randomUUID(),
      runId: randomUUID(),
    }
    const armedSkill = requestedSkill ?? (selectedSkill
      ? { id: selectedSkill.id, args: message }
      : undefined)
    // 仅无 Thread 的首次 run 携带待选 Profile；服务端在创建 Thread 时原子绑定它。
    const requestedModelProfile = current.threadId ? undefined : pendingModelProfile
    if (armedSkill && !requestedSkill) setSelectedSkill(undefined)
    commit(state => startRun(state, run, message))
    try {
      const accepted = await client.query(message, run.threadId, run.runId, armedSkill, requestedModelProfile)
      if (!accepted.accepted || accepted.thread_id !== run.threadId || accepted.run_id !== run.runId) {
        throw new Error("Agent 返回的 run 标识与请求不一致")
      }
      if (requestedModelProfile) setPendingModelProfile(undefined)
    } catch (error) {
      commit(state => markRunFailed(state, run.runId, errorMessage(error)))
    }
  }, [client, commit, pendingModelProfile, selectedSkill])

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

  /** 打开与 `harness --resume` 共用的选择器；活动 run 或交互期间禁止替换当前 thread。 */
  const openThreadPicker = useCallback(() => {
    const current = stateRef.current
    if (current.activeRun || current.pendingApproval || current.pendingQuestion) {
      commit(state => appendNotice(state, "当前 thread 仍在执行或等待交互，不能恢复其他 thread。"))
      return
    }
    setThreadPicker({ visible: true, loading: true, query: "", selectedIndex: 0 })
    void refreshThreads().then(() => {
      setThreadPicker(current => current.visible ? { ...current, loading: false } : current)
    }).catch(error => {
      setThreadPicker(current => current.visible
        ? { ...current, loading: false, error: `Thread 列表读取失败：${errorMessage(error)}` }
        : current)
    })
  }, [commit, refreshThreads])

  /** 关闭搜索浮层时保留草稿和原 thread；Composer 会因 pickerVisible 复位而重新获取焦点。 */
  const closeSkillPicker = useCallback(() => {
    setSkillPicker(current => ({ ...current, visible: false, loading: false, error: undefined }))
  }, [])

  /** Thread Picker 与 Skill Picker 共享相同关闭语义，不能在关闭时恢复或修改 thread。 */
  const closeThreadPicker = useCallback(() => {
    setThreadPicker(current => ({ ...current, visible: false, loading: false, error: undefined }))
  }, [])

  /** `/model` 只为尚未绑定的下一条新 Thread 选择 Profile；既有 Thread 保持不可变。 */
  const openModelPicker = useCallback((initialQuery = "") => {
    const current = stateRef.current
    if (current.threadId) {
      void client.listModels(current.threadId).then(result => {
        const binding = result.thread_binding
        const executor = binding?.roles.executor ?? binding?.roles.primary
        setModelBindingDialog({
          title: "当前 Thread 的模型不可变",
          message: executor
            ? `当前 Thread 已绑定 ${executor.provider_label} · ${executor.model}（${executor.id}）。请新建 Thread 后使用 /model 选择模型。`
            : "当前 Thread 使用 legacy immutable binding，不能热切换模型。请新建 Thread 后使用 /model 选择模型。",
        })
      }).catch(error => {
        setModelBindingDialog({
          title: "模型绑定不可读取",
          message: `无法读取当前 Thread 的模型绑定：${errorMessage(error)}。请新建 Thread 后再选择模型。`,
        })
      })
      return
    }
    setModelPicker({ visible: true, loading: true, query: initialQuery, selectedIndex: 0 })
    void client.listModels().then(result => {
      setModels(result.profiles)
      setModelPicker(value => value.visible ? { ...value, loading: false } : value)
    }).catch(error => {
      setModelPicker(value => value.visible
        ? { ...value, loading: false, error: `模型目录读取失败：${errorMessage(error)}` }
        : value)
    })
  }, [client])

  /** 取消模型选择会回到配置默认值，避免旧的待绑定选择在后续输入中意外生效。 */
  const closeModelPicker = useCallback(() => {
    setPendingModelProfile(undefined)
    setModelPicker(current => ({ ...current, visible: false, loading: false, error: undefined }))
  }, [])

  useEffect(() => {
    if (!initialResumeRef.current) return
    initialResumeRef.current = false
    openThreadPicker()
  }, [openThreadPicker])

  /** 筛选保持在纯视图层，避免每次搜索重新请求 sidecar 或改变 snapshot。 */
  const visibleSkills = filterSkills(skills, skillPicker.query)
  const visibleThreads = filterThreads(threads, threadPicker.query)
  const visibleModels = filterModels(models, modelPicker.query)

  /** 把 Dispatcher 的结构化结果映射为 TUI 状态、JSON-RPC 或退出动作。 */
  const applyCommandResult = useCallback(async (result: CommandResult): Promise<void> => {
    switch (result.type) {
      case "notice":
        commit(current => appendNotice(current, result.message))
        return
      case "exit":
        onRequestExit()
        return
      case "local-action":
        if (result.action === "clear-thread") {
          setPendingModelProfile(undefined)
          commit(clearThread)
          return
        }
        if (await cancelActiveRun({ exitOnRepeatedCancellation: false })) {
          setPendingModelProfile(undefined)
          commit(clearThread)
        } else {
          commit(current => appendNotice(current, "未能取消当前任务，已保留当前 thread。请等待任务结束后重试。"))
        }
        return
      case "open-picker":
        if (result.picker === "skills") openSkillPicker()
        else if (result.picker === "threads") openThreadPicker()
        else openModelPicker(result.initialQuery)
        return
      case "open-dialog":
        setCommandDialog(result.dialog)
        return
      case "rpc":
        try {
          const value = await client.call(result.method, result.params)
          await applyCommandResult(result.onSuccess(value))
        } catch (error) {
          await applyCommandResult(result.onError(error))
        }
        return
      case "submit-prompt":
        await sendAgentMessage(result.prompt, result.requestedSkill)
    }
  }, [cancelActiveRun, client, commit, onRequestExit, openModelPicker, openSkillPicker, openThreadPicker, sendAgentMessage])

  /** 由 Dispatcher 解析稳定 ID 和运行态，根组件不再解释每个命令的业务分支。 */
  const executeSlashCommand = useCallback((command: SlashCommand) => {
    const current = stateRef.current
    void applyCommandResult(dispatchSlashCommand(command, {
      commandContext: tuiCommandContext(runtime, current),
      threadId: current.threadId,
      runtimeStatus: runtimeStatusSummary(runtime),
      versionSummary: `za38-cli ${runtime.cliVersion} · JSON-RPC v2`,
    }))
  }, [applyCommandResult, runtime])

  /** 确认框仅保存 Dispatcher 返回的后续动作，取消时不改变当前 thread。 */
  const resolveCommandDialog = useCallback((confirmed: boolean) => {
    const dialog = commandDialog
    setCommandDialog(undefined)
    if (confirmed && dialog) void applyCommandResult(dialog.confirm)
  }, [applyCommandResult, commandDialog])

  /** 同步 textarea 草稿、命令菜单过滤状态和历史游标。 */
  const updateDraft = useCallback((value: string) => {
    // 回填历史会触发 textarea 的内容事件；仅它保留历史游标，用户编辑则立即退出历史浏览。
    if (historyApplyValueRef.current === value) historyApplyValueRef.current = undefined
    else promptHistoryCursorRef.current = undefined
    setDraft(value)
    const slashQuery = value.trimStart()
    // 输入完整的本地命令后收起菜单，让 Enter 直接执行；未完成前缀继续保留
    // 筛选菜单，以支持 `/st` + Enter 的补全工作流。
    const resolution = resolveSlashCommand(slashQuery)
    const shouldShowMenu = slashQuery.startsWith("/")
      && !slashQuery.startsWith("//")
      && !slashQuery.slice(1).match(/\s/)
      && resolution.kind !== "command"
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

  /** 读取选中的 thread 后一次性替换状态，旧 thread 的 sequence 与草稿都不能残留。 */
  const selectThread = useCallback(async (thread: ThreadPickerItem) => {
    if (openingThreadRef.current) return
    if (stateRef.current.activeRun || stateRef.current.pendingApproval || stateRef.current.pendingQuestion) {
      setThreadPicker(current => ({ ...current, visible: false, loading: false }))
      commit(current => appendNotice(current, "当前 thread 状态已变化，未恢复其他 thread。"))
      return
    }
    openingThreadRef.current = true
    setThreadPicker(current => ({ ...current, loading: true, error: undefined }))
    try {
      const opened = threadOpenResult(await client.openThread(thread.threadId))
      clearDraft()
      setSelectedSkill(undefined)
      setExpandedTools(new Set())
      setShowToolDetails(false)
      commit(() => restoreThread(opened.threadId, opened.messages))
      setThreadPicker({ visible: false, loading: false, query: "", selectedIndex: 0 })
    } catch (error) {
      setThreadPicker(current => ({ ...current, loading: false, error: `Thread 恢复失败：${errorMessage(error)}` }))
    } finally {
      openingThreadRef.current = false
    }
  }, [clearDraft, client, commit])

  /** 将可用 Profile 写入 Composer 的一次性待绑定状态；Enter 显式确认后才会生效。 */
  const selectModel = useCallback((model: ModelProfile) => {
    if (!model.available) {
      setModelPicker(current => ({
        ...current,
        error: `${model.provider_label} · ${model.model} 不可用：${model.unavailable_reason ?? "配置不可用"}`,
      }))
      return
    }
    setPendingModelProfile(model.id)
    // 选择与取消不同：确认后保留 pending 状态，直到下一新 Thread 成功启动或 /new 清理它。
    setModelPicker(current => ({ ...current, visible: false, loading: false, error: undefined }))
    commit(current => appendNotice(current, `已选择 ${model.provider_label} · ${model.model}；将在下一条新 Thread 中生效。`))
  }, [commit])

  /** 已绑定 Thread 的说明框只允许回到空白 Composer；不修改既有绑定。 */
  const resolveModelBindingDialog = useCallback((createNewThread: boolean) => {
    setModelBindingDialog(undefined)
    if (!createNewThread) return
    setPendingModelProfile(undefined)
    commit(clearThread)
  }, [commit])

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
    if (item.availability.state === "disabled") {
      const reason = item.availability.reason
      commit(current => appendNotice(current, `/${item.command.name} 暂不可用：${reason}。`))
      return
    }
    // 活动 run 期间 composer 不接收普通 Prompt；从命令菜单选择可执行项时直接交给
    // Dispatcher，确保 /new 的确认流程和 /quit 等控制命令仍然可达。
    if (stateRef.current.activeRun) {
      const command = parseSlashCommand(`/${item.command.name}`)
      clearDraft()
      if (command) executeSlashCommand(command)
      return
    }
    const value = `/${item.command.name}`
    commandMenuDismissedValue.current = value
    inputRef.current?.setText(value)
    inputRef.current?.gotoBufferEnd()
    setDraft(value)
    setCommandMenu({ visible: false, selectedIndex: 0 })
  }, [clearDraft, commit, executeSlashCommand, selectSkill])

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
    const rawInput = inputRef.current?.plainText ?? draft
    const input = rawInput.trim()
    if (!input) return
    // OpenTUI Input 会保留内部编辑缓冲区，提交后需主动清空，不能只依赖 React state。
    clearDraft()
    if (stateRef.current.pendingQuestion) {
      void respondQuestion(input)
      return
    }
    const resolution = resolveSlashCommand(rawInput)
    if (resolution.kind === "command") {
      void executeSlashCommand(resolution.command)
      return
    }
    if (resolution.kind === "unknown") {
      commit(current => appendNotice(current, unknownCommandNotice(resolution)))
      return
    }
    const message = resolution.kind === "escaped" ? resolution.message : input
    const previousHistory = promptHistoryRef.current
    const nextHistory = rememberPrompt(previousHistory, message)
    promptHistoryRef.current = nextHistory
    promptHistoryCursorRef.current = undefined
    void persistPromptHistory(previousHistory, nextHistory, promptHistoryFile)
    void sendAgentMessage(message)
  }, [clearDraft, commit, draft, executeSlashCommand, respondQuestion, sendAgentMessage])

  /** 按行、半页或跳转首尾滚动当前 thread；全局快捷键与空 composer 方向键共用。 */
  const scrollConversation = useCallback((intent: ScrollIntent) => {
    const scroll = conversationScrollRef.current
    if (!scroll || scroll.isDestroyed) return false
    if (intent === "top") {
      scroll.scrollTo(0)
      return true
    }
    if (intent === "bottom") {
      scroll.scrollTo(scroll.scrollHeight)
      return true
    }
    const half = Math.max(1, Math.floor(scroll.height / 2))
    const delta = intent === "line-up" ? -1
      : intent === "line-down" ? 1
        : intent === "page-up" ? -half
          : half
    scroll.scrollBy(delta)
    return true
  }, [])

  /** 在 textarea 层处理历史与 thread 滚动，避免全局 key handler 抢走方向键。 */
  const handleComposerKeyDown = useCallback((key: KeyEvent) => {
    // Slash 菜单由全局快捷键优先处理，不能在 textarea 内重复消费方向键。
    if (commandMenu.visible || commandDialog || modelBindingDialog || skillPicker.visible || threadPicker.visible || modelPicker.visible) return
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

    // 只有空 composer 才借出方向键给 thread 行滚动；编辑任何文本时完全保持 textarea 原生语义。
    // PageUp/PageDown 已由全局快捷键处理，这里不再重复消费。
    if (!input.plainText && !isHomeState(stateRef.current)) {
      const scrollAction = key.name === "up" ? "line-up"
        : key.name === "down" ? "line-down"
          : undefined
      if (scrollAction && scrollConversation(scrollAction)) key.preventDefault()
    }
  }, [commandDialog, commandMenu.visible, modelBindingDialog, modelPicker.visible, navigatePromptHistory, scrollConversation, skillPicker.visible, threadPicker.visible])

  useKeyboard(key => {
    const commandOptions = findCommandMenuItems(draft, skills, tuiCommandContext(runtime, stateRef.current))
    const action = resolveShortcut(key, {
      commandDialogVisible: Boolean(commandDialog || modelBindingDialog),
      skillPickerVisible: skillPicker.visible,
      skillOptionCount: visibleSkills.length,
      threadPickerVisible: threadPicker.visible,
      threadOptionCount: visibleThreads.length,
      modelPickerVisible: modelPicker.visible,
      modelOptionCount: visibleModels.length,
      commandMenuVisible: commandMenu.visible,
      commandOptionCount: commandOptions.length,
      activeRun: Boolean(stateRef.current.activeRun),
      hasDraft: Boolean(draft),
    })
    if (action === "none") return
    key.preventDefault()

    if (action === "confirm-command-dialog") {
      if (modelBindingDialog) resolveModelBindingDialog(true)
      else resolveCommandDialog(true)
      return
    }
    if (action === "cancel-command-dialog") {
      if (modelBindingDialog) resolveModelBindingDialog(false)
      else resolveCommandDialog(false)
      return
    }

    // 滚动键全局生效，可在输入或运行中随时回看历史；与 opencode 的 session.global 对齐。
    const scrollIntent: ScrollIntent | undefined =
      action === "scroll-line-up" ? "line-up"
      : action === "scroll-line-down" ? "line-down"
      : action === "scroll-page-up" ? "page-up"
      : action === "scroll-page-down" ? "page-down"
      : action === "scroll-top" ? "top"
      : action === "scroll-bottom" ? "bottom"
      : undefined
    if (scrollIntent) {
      scrollConversation(scrollIntent)
      return
    }
    if (action === "close-skill-picker") {
      closeSkillPicker()
      return
    }
    if (action === "close-thread-picker") {
      closeThreadPicker()
      return
    }
    if (action === "close-model-picker") {
      closeModelPicker()
      return
    }
    if (action === "thread-previous" || action === "thread-next") {
      const direction = action === "thread-previous" ? -1 : 1
      setThreadPicker(current => ({
        ...current,
        selectedIndex: visibleThreads.length ? (current.selectedIndex + direction + visibleThreads.length) % visibleThreads.length : 0,
      }))
      return
    }
    if (action === "thread-select") {
      const selected = visibleThreads[threadPicker.selectedIndex]
      if (selected) void selectThread(selected)
      return
    }
    if (action === "thread-block") return
    if (action === "model-previous" || action === "model-next") {
      const direction = action === "model-previous" ? -1 : 1
      setModelPicker(current => ({
        ...current,
        selectedIndex: visibleModels.length ? (current.selectedIndex + direction + visibleModels.length) % visibleModels.length : 0,
      }))
      return
    }
    if (action === "model-select") {
      const selected = visibleModels[modelPicker.selectedIndex]
      if (selected) selectModel(selected)
      return
    }
    if (action === "model-block") return
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
    if (action === "command-block") {
      const resolution = resolveSlashCommand(inputRef.current?.plainText ?? draft)
      if (resolution.kind === "unknown") {
        clearDraft()
        commit(current => appendNotice(current, unknownCommandNotice(resolution)))
      }
      return
    }
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
    commandOptions: findCommandMenuItems(draft, skills, tuiCommandContext(runtime, state)),
    onSelectCommand: selectCommandMenuItem,
    onHoverCommand: (selectedIndex: number) => setCommandMenu(current => ({ ...current, selectedIndex })),
    selectedSkill,
    pickerVisible: Boolean(commandDialog || modelBindingDialog) || skillPicker.visible || threadPicker.visible || modelPicker.visible,
    onClearSelectedSkill: () => setSelectedSkill(undefined),
    showToolDetails,
    expandedTools,
    onToggleTool: toggleTool,
    onApproval: (decision: "approve" | "reject") => { void respondApproval(decision) },
    onQuestion: (answer: string) => { void respondQuestion(answer) },
  }

  return (
    <box position="relative" flexGrow={1}>
      {isHomeState(state) ? <HomeView {...viewProps} /> : <ThreadView {...viewProps} />}
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
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!state.activeRun}
        onSearch={query => setSkillPicker(current => ({ ...current, query, selectedIndex: 0 }))}
        onSelect={selectSkill}
        onHover={selectedIndex => setSkillPicker(current => ({ ...current, selectedIndex }))}
        onClose={closeSkillPicker}
      />
      <ThreadPicker
        visible={threadPicker.visible}
        loading={threadPicker.loading}
        error={threadPicker.error}
        threads={visibleThreads}
        query={threadPicker.query}
        selectedIndex={threadPicker.selectedIndex}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        searchRef={threadSearchRef}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!state.activeRun}
        onSearch={query => setThreadPicker(current => ({ ...current, query, selectedIndex: 0 }))}
        onSelect={thread => { void selectThread(thread) }}
        onHover={selectedIndex => setThreadPicker(current => ({ ...current, selectedIndex }))}
        onClose={closeThreadPicker}
      />
      <SearchPicker<ModelProfile>
        visible={modelPicker.visible}
        loading={modelPicker.loading}
        error={modelPicker.error}
        items={visibleModels}
        query={modelPicker.query}
        selectedIndex={modelPicker.selectedIndex}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        searchRef={modelSearchRef}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!state.activeRun}
        searchId="model-search"
        title="选择下一新 Thread 的模型"
        searchPlaceholder="按 Profile、模型或 Provider 搜索"
        emptyMessage="没有匹配的模型 Profile"
        itemKey={model => model.id}
        renderItem={(model, context) => modelPickerRow(model, context)}
        onSearch={query => setModelPicker(current => ({ ...current, query, selectedIndex: 0 }))}
        onSelect={selectModel}
        onHover={selectedIndex => setModelPicker(current => ({ ...current, selectedIndex }))}
        onClose={closeModelPicker}
      />
      <DialogShell
        visible={commandDialog?.kind === "confirm-new-thread"}
        title={commandDialog?.title ?? ""}
        message={commandDialog?.message ?? ""}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!state.activeRun}
        onConfirm={() => resolveCommandDialog(true)}
        onCancel={() => resolveCommandDialog(false)}
      />
      <DialogShell
        visible={Boolean(modelBindingDialog)}
        title={modelBindingDialog?.title ?? ""}
        message={modelBindingDialog?.message ?? ""}
        terminalWidth={terminal.width}
        terminalHeight={terminal.height}
        restoreFocusRef={inputRef}
        shouldRestoreFocus={!state.activeRun}
        confirmLabel="新建 Thread"
        cancelLabel="保留当前 Thread"
        onConfirm={() => resolveModelBindingDialog(true)}
        onCancel={() => resolveModelBindingDialog(false)}
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

/** 将握手 capability 和即时 thread 状态收敛为 Registry 可重复使用的可用性上下文。 */
function tuiCommandContext(runtime: TuiRuntime, state: ReturnType<typeof createInitialState>) {
  return defaultCommandContext({
    capabilities: runtime.capabilities,
    hasThread: Boolean(state.threadId),
    activeRun: Boolean(state.activeRun),
    hasPendingInteraction: Boolean(state.pendingApproval || state.pendingQuestion),
  })
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

/** 校验 thread 摘要的最小字段，并将 wire snake_case 限制在 IPC 边界。 */
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

/** 检查恢复结果，确保无效 sidecar 数据不会以半条历史覆盖当前 thread。 */
function threadOpenResult(value: unknown): { threadId: string; messages: Array<{ kind: "user" | "assistant" | "tool"; content: string; toolName?: string }> } {
  if (!value || typeof value !== "object") throw new Error("Agent 返回的 thread 恢复结果无效")
  const record = value as Record<string, unknown>
  const thread = threadPickerItem(record.thread)
  if (!thread || !Array.isArray(record.messages)) throw new Error("Agent 返回的 thread 恢复结果无效")
  const messages = record.messages.map(threadMessage).filter((message): message is ThreadMessage => message !== undefined)
  if (messages.length !== record.messages.length) throw new Error("Agent 返回了无效的 thread message")
  return {
    threadId: thread.threadId,
    messages: messages.map(message => ({
      kind: message.kind,
      content: message.content,
      toolName: message.tool_name,
    })),
  }
}

/** 把协议 message 校验为三种恢复历史项；工具名称缺失时由视图使用安全默认值。 */
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

/** 用首条和最新消息过滤 thread，不把不透明 thread_id 作为用户可搜索数据。 */
function filterThreads(threads: readonly ThreadPickerItem[], query: string): readonly ThreadPickerItem[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return threads
  return threads.filter(thread => [thread.firstMessage, thread.latestMessage]
    .some(value => value.toLowerCase().includes(needle)))
}

/** Profile 搜索同时覆盖稳定 ID、模型名和可展示的 Provider 标签。 */
function filterModels(models: readonly ModelProfile[], query: string): readonly ModelProfile[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return models
  return models.filter(model => [model.id, model.model, model.provider_label]
    .some(value => value.toLowerCase().includes(needle)))
}

/** Model Picker 行避免展示 endpoint 或凭据，只展示已脱敏的 Profile DTO。 */
function modelPickerRow(model: ModelProfile, context: SearchPickerRenderContext): ReactNode {
  const idWidth = context.compact
    ? Math.max(16, context.width - 6)
    : Math.max(16, Math.min(26, Math.floor(context.width * 0.28)))
  const contextWindow = model.context_window_tokens >= 1_000
    ? `${Math.round(model.context_window_tokens / 1_000)}k`
    : String(model.context_window_tokens)
  const detail = model.available
    ? `${model.is_default ? "默认 · " : ""}${model.provider_label} · ${model.model} · ${contextWindow} · ${model.capabilities.join(",")}`
    : `${model.is_default ? "默认 · " : ""}${model.provider_label} · 不可用：${model.unavailable_reason ?? "配置不可用"}`
  const foreground = context.selected ? tuiTheme.background : model.available ? tuiTheme.primary : tuiTheme.muted
  const detailForeground = context.selected ? tuiTheme.background : model.available ? tuiTheme.muted : tuiTheme.danger
  return (
    <>
      <text width={idWidth} fg={foreground} wrapMode="none" overflow="hidden">{model.id}</text>
      {!context.compact ? <text flexGrow={1} fg={detailForeground} wrapMode="none" overflow="hidden">{detail}</text> : null}
    </>
  )
}
