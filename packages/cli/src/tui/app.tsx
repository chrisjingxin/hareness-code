import { createCliRenderer, type ScrollBoxRenderable, type TextareaRenderable } from "@opentui/core"
import { createRoot, useKeyboard, useTerminalDimensions } from "@opentui/react"
import { randomUUID } from "node:crypto"
import { useCallback, useEffect, useRef, useState } from "react"

import { IpcClient } from "../ipc/client"
import { findSlashCommands, parseSlashCommand, slashCommandHelp, type SlashCommand, type SlashCommandDefinition } from "./commands"
import { HomeView, SessionView, type CommandMenuState } from "./components"
import type { TuiRuntime } from "./model"
import { canNavigatePromptHistory, rememberPrompt, selectPromptHistory } from "./prompt-history"
import { resolveShortcut } from "./shortcuts"
import { registerCommonSyntaxParsers } from "./syntax-parsers"
import {
  appendNotice,
  applyAgentEvent,
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
  onRequestExit: () => void
}

/** 正式 OpenTUI 根组件：所有 Agent 输出必须经状态归约后才进入终端。 */
export function Za38Tui({ client, runtime, threadId, onRequestExit }: TuiOptions) {
  const [state, setState] = useState(() => createInitialState(threadId))
  const stateRef = useRef(state)
  const [draft, setDraft] = useState("")
  const inputRef = useRef<TextareaRenderable | null>(null)
  const conversationScrollRef = useRef<ScrollBoxRenderable | null>(null)
  const [commandMenu, setCommandMenu] = useState<CommandMenuState>({ visible: false, selectedIndex: 0 })
  const commandMenuDismissedValue = useRef<string | undefined>(undefined)
  const [showToolDetails, setShowToolDetails] = useState(false)
  const [expandedTools, setExpandedTools] = useState<ReadonlySet<string>>(() => new Set())
  const promptHistoryRef = useRef<string[]>([])
  const terminal = useTerminalDimensions()

  // 回调可能来自长生命周期的 IPC 监听器，使用 ref 使其始终读取到最新会话状态。
  const commit = useCallback((transition: (current: TuiState) => TuiState) => {
    const next = transition(stateRef.current)
    stateRef.current = next
    setState(next)
  }, [])

  useEffect(() => {
    const listeners = [
      "run/started",
      "message/delta",
      "tool/started",
      "tool/updated",
      "tool/completed",
      "approval/requested",
      "question/requested",
      "run/completed",
      "run/cancelled",
      "run/failed",
    ].map(method => {
      const listener = (payload: Record<string, unknown>) => commit(current => applyAgentEvent(current, method, payload))
      client.on(method, listener)
      return { method, listener }
    })
    const protocolError = (error: Error) => commit(current => appendNotice(current, `协议错误：${error.message}`))
    const closed = (error: Error) => commit(current => appendNotice(current, `Agent 连接已关闭：${error.message}`))
    client.on("protocolError", protocolError)
    client.on("close", closed)

    return () => {
      for (const { method, listener } of listeners) client.off(method, listener)
      client.off("protocolError", protocolError)
      client.off("close", closed)
    }
  }, [client, commit])

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

  const clearPromptHistory = useCallback(() => {
    promptHistoryRef.current = []
  }, [])

  const sendAgentMessage = useCallback(async (message: string) => {
    const current = stateRef.current
    if (current.activeRun) {
      commit(state => appendNotice(state, "当前会话仍在执行；请等待、审批或按 Ctrl+C 取消。"))
      return
    }
    const run = {
      threadId: current.threadId ?? randomUUID(),
      runId: randomUUID(),
    }
    commit(state => startRun(state, run, message))
    try {
      const accepted = await client.query(message, run.threadId, run.runId)
      if (!accepted.accepted || accepted.thread_id !== run.threadId || accepted.run_id !== run.runId) {
        throw new Error("Agent 返回的 run 标识与请求不一致")
      }
    } catch (error) {
      commit(state => markRunFailed(state, run.runId, errorMessage(error)))
    }
  }, [client, commit])

  const respondApproval = useCallback(async (decision: "approve" | "reject") => {
    const { activeRun, pendingApproval } = stateRef.current
    if (!activeRun || !pendingApproval?.interruptId) return
    commit(clearPendingInteraction)
    try {
      // HumanInTheLoopMiddleware 的 resume 契约需要 decisions 包装对象。
      await client.respond(activeRun.threadId, activeRun.runId, pendingApproval.interruptId, {
        decisions: [{ type: decision }],
      })
    } catch (error) {
      commit(state => markRunFailed(state, activeRun.runId, errorMessage(error)))
    }
  }, [client, commit])

  const respondQuestion = useCallback(async (answer: string) => {
    const { activeRun, pendingQuestion } = stateRef.current
    if (!activeRun || !pendingQuestion?.interruptId) return
    commit(clearPendingInteraction)
    try {
      // AskUserMiddleware 只接受明确的 answered/answers 结构，避免把自由文本误判为取消。
      await client.respond(activeRun.threadId, activeRun.runId, pendingQuestion.interruptId, {
        status: "answered",
        answers: [answer],
      })
    } catch (error) {
      commit(state => markRunFailed(state, activeRun.runId, errorMessage(error)))
    }
  }, [client, commit])

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
          clearPromptHistory()
        }
        return
      case "force-clear":
        await cancelActiveRun()
        commit(clearThread)
        clearPromptHistory()
        return
      case "version":
        commit(current => appendNotice(current, `za38-cli ${runtime.cliVersion} · JSON-RPC v1`))
        return
    }
  }, [cancelActiveRun, clearPromptHistory, commit, onRequestExit, runtime.cliVersion])

  const updateDraft = useCallback((value: string) => {
    setDraft(value)
    const slashQuery = value.trimStart()
    const shouldShowMenu = slashQuery.startsWith("/") && !slashQuery.slice(1).match(/\s/)
    if (shouldShowMenu && commandMenuDismissedValue.current !== value) {
      setCommandMenu({ visible: true, selectedIndex: 0 })
      return
    }
    if (!shouldShowMenu) commandMenuDismissedValue.current = undefined
    setCommandMenu(current => current.visible ? { ...current, visible: false } : current)
  }, [])

  const clearDraft = useCallback(() => {
    inputRef.current?.clear()
    commandMenuDismissedValue.current = undefined
    setDraft("")
    setCommandMenu({ visible: false, selectedIndex: 0 })
  }, [])

  const replaceDraft = useCallback((value: string) => {
    // setText 会同步 textarea 内部缓冲区，不能只更新 React state，否则 Enter 会发送旧内容。
    inputRef.current?.setText(value)
    inputRef.current?.gotoBufferEnd()
    commandMenuDismissedValue.current = undefined
    setDraft(value)
    setCommandMenu({ visible: false, selectedIndex: 0 })
  }, [])

  const navigatePromptHistory = useCallback((direction: "previous" | "next") => {
    const value = selectPromptHistory(promptHistoryRef.current, draft, direction)
    if (value !== undefined) replaceDraft(value)
  }, [draft, replaceDraft])

  const selectSlashCommand = useCallback((command: SlashCommandDefinition) => {
    const value = `/${command.name}`
    commandMenuDismissedValue.current = value
    inputRef.current?.setText(value)
    inputRef.current?.gotoBufferEnd()
    setDraft(value)
    setCommandMenu({ visible: false, selectedIndex: 0 })
  }, [])

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
    promptHistoryRef.current = rememberPrompt(promptHistoryRef.current, input)
    void sendAgentMessage(input)
  }, [clearDraft, draft, executeSlashCommand, respondQuestion, sendAgentMessage])

  useKeyboard(key => {
    const commandOptions = findSlashCommands(draft)
    const action = resolveShortcut(key, {
      commandMenuVisible: commandMenu.visible,
      commandOptionCount: commandOptions.length,
      activeRun: Boolean(stateRef.current.activeRun),
      hasDraft: Boolean(draft),
      canScrollConversation: !isHomeState(stateRef.current),
      canNavigatePromptHistory: canNavigatePromptHistory(promptHistoryRef.current, draft),
    })
    if (action === "none") return
    key.preventDefault()

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
      const selected = commandOptions[commandMenu.selectedIndex]
      if (selected) selectSlashCommand(selected)
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
    if (action === "history-previous" || action === "history-next") {
      navigatePromptHistory(action === "history-previous" ? "previous" : "next")
      return
    }
    if (action === "scroll-conversation-up" || action === "scroll-conversation-down" || action === "scroll-conversation-page-up" || action === "scroll-conversation-page-down") {
      const scroll = conversationScrollRef.current
      if (!scroll || scroll.isDestroyed) return
      const delta = action === "scroll-conversation-up"
        ? -1
        : action === "scroll-conversation-down"
          ? 1
          : action === "scroll-conversation-page-up"
            ? -Math.max(1, Math.floor(scroll.height / 2))
            : Math.max(1, Math.floor(scroll.height / 2))
      scroll.scrollBy(delta)
      return
    }
    if (action === "exit") onRequestExit()
  })

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
    onSubmit: handleSubmit,
    commandMenu,
    onSelectCommand: selectSlashCommand,
    onHoverCommand: (selectedIndex: number) => setCommandMenu(current => ({ ...current, selectedIndex })),
    showToolDetails,
    expandedTools,
    onToggleTool: toggleTool,
    onApproval: (decision: "approve" | "reject") => { void respondApproval(decision) },
    onQuestion: (answer: string) => { void respondQuestion(answer) },
  }

  return isHomeState(state) ? <HomeView {...viewProps} /> : <SessionView {...viewProps} />
}

/** 负责 OpenTUI 生命周期；退出后将控制权交回 CLI 以关闭 Python sidecar。 */
export async function runTui(options: TuiOptions): Promise<void> {
  registerCommonSyntaxParsers()
  const renderer = await createCliRenderer({ exitOnCtrlC: false, clearOnShutdown: true })
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
    root.render(<Za38Tui {...options} onRequestExit={close} />)
  })
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
