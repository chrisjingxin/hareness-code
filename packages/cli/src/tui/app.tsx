import { createCliRenderer, type InputRenderable } from "@opentui/core"
import { createRoot, useKeyboard } from "@opentui/react"
import { randomUUID } from "node:crypto"
import { useCallback, useEffect, useRef, useState } from "react"

import { IpcClient } from "../ipc/client"
import { parseSlashCommand, slashCommandHelp, type SlashCommand } from "./commands"
import {
  appendNotice,
  applyAgentEvent,
  clearPendingInteraction,
  clearThread,
  createInitialState,
  markCancelling,
  markRunFailed,
  startRun,
  type TuiState,
} from "./state"

type TuiOptions = {
  client: IpcClient
  threadId?: string
  onRequestExit: () => void
}

const colors = {
  background: "#10131a",
  panel: "#181d27",
  border: "#39465f",
  muted: "#96a0b5",
  text: "#e7edf8",
  accent: "#70a5ff",
  success: "#89c995",
  warning: "#e5b567",
  danger: "#ef7f7f",
} as const

/** 正式 OpenTUI 根组件：所有 Agent 输出必须经状态归约后才进入终端。 */
export function Za38Tui({ client, threadId, onRequestExit }: TuiOptions) {
  const [state, setState] = useState(() => createInitialState(threadId))
  const stateRef = useRef(state)
  const [draft, setDraft] = useState("")
  const inputRef = useRef<InputRenderable | null>(null)

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
        }
        return
      case "force-clear":
        await cancelActiveRun()
        commit(clearThread)
        return
      case "version":
        commit(current => appendNotice(current, "za38-cli 0.1.0 · JSON-RPC v1"))
        return
      case "skill":
        commit(current => appendNotice(current, `技能 /skill:${command.argument} 将在 za38 原生技能发现接入后可用。`))
        return
      default:
        commit(current => appendNotice(current, `/${command.name} 已保留在命令面，但对应内核能力尚未接入。`))
    }
  }, [cancelActiveRun, commit, onRequestExit])

  const handleSubmit = useCallback((value: string) => {
    const input = value.trim()
    if (!input) return
    // OpenTUI Input 会保留内部编辑缓冲区，提交后需主动清空，不能只依赖 React state。
    if (inputRef.current) inputRef.current.value = ""
    setDraft("")
    if (stateRef.current.pendingQuestion) {
      void respondQuestion(input)
      return
    }
    const command = parseSlashCommand(input)
    if (command) {
      void executeSlashCommand(command)
      return
    }
    void sendAgentMessage(input)
  }, [executeSlashCommand, respondQuestion, sendAgentMessage])

  useKeyboard(key => {
    if (key.ctrl && key.name === "c") {
      void cancelActiveRun()
      return
    }
    if (key.ctrl && key.name === "d" && !stateRef.current.activeRun) onRequestExit()
  })

  const interaction = state.pendingApproval
    ? (
      <box title="需要审批" border borderColor={colors.warning} style={{ marginBottom: 1, padding: 1 }}>
        <text content={state.pendingApproval.description} fg={colors.warning} />
        <select
          focused
          options={[
            { name: "允许", description: "继续执行该工具操作", value: "approve" },
            { name: "拒绝", description: "拒绝该工具操作", value: "reject" },
          ]}
          onSelect={(_, option) => { if (option?.value === "approve" || option?.value === "reject") void respondApproval(option.value) }}
        />
      </box>
    )
    : state.pendingQuestion?.options.length
      ? (
        <box title="Agent 需要你的回答" border borderColor={colors.warning} style={{ marginBottom: 1, padding: 1 }}>
          <text content={state.pendingQuestion.question} fg={colors.warning} />
          <select
            focused
            options={state.pendingQuestion.options.map(option => ({ ...option, description: option.name }))}
            onSelect={(_, option) => { if (typeof option?.value === "string") void respondQuestion(option.value) }}
          />
        </box>
      )
      : null

  const inputPlaceholder = state.pendingQuestion
    ? "输入你的回答后按 Enter"
    : state.activeRun
      ? "正在执行；Ctrl+C 取消"
      : "描述你想完成的编码任务…"

  return (
    <box style={{ flexDirection: "column", flexGrow: 1, backgroundColor: colors.background, padding: 1 }}>
      <box style={{ justifyContent: "space-between", marginBottom: 1 }}>
        <text content="za38" fg={colors.accent} />
        <text content={state.threadId ? `会话 ${state.threadId.slice(0, 8)} · ${state.status}` : state.status} fg={colors.muted} />
      </box>

      <scrollbox
        stickyScroll
        stickyStart="bottom"
        style={{ flexGrow: 1, border: true, borderColor: colors.border, padding: 1, marginBottom: 1 }}
      >
        {state.messages.map(message => (
          <box key={message.id} style={{ flexDirection: "column", marginBottom: 1 }}>
            <text
              content={message.role === "user" ? "你" : message.role === "assistant" ? "za38" : "系统"}
              fg={message.role === "user" ? colors.accent : message.role === "assistant" ? colors.success : colors.muted}
            />
            <text content={message.content || (message.streaming ? "…" : "")} fg={colors.text} />
          </box>
        ))}
        {state.tools.map(tool => (
          <box key={tool.id} title={`工具 · ${tool.name}`} border borderColor={tool.status === "failed" ? colors.danger : colors.border} style={{ flexDirection: "column", marginBottom: 1, padding: 1 }}>
            <text content={tool.status === "running" ? "执行中" : tool.status === "failed" ? "失败" : "完成"} fg={tool.status === "failed" ? colors.danger : colors.muted} />
            {tool.detail ? <text content={tool.detail} fg={colors.text} /> : null}
          </box>
        ))}
      </scrollbox>

      {interaction}
      {!state.pendingApproval && !(state.pendingQuestion?.options.length) ? (
        <box title="输入" border borderColor={colors.border} style={{ paddingLeft: 1, paddingRight: 1 }}>
          {/* OpenTUI 0.4.3 的声明同时继承了 textarea/input 的 onSubmit；运行时实际传入 string。 */}
          <input ref={inputRef} value={draft} placeholder={inputPlaceholder} focused={!state.activeRun || Boolean(state.pendingQuestion)} onInput={setDraft} onSubmit={handleSubmit as never} />
        </box>
      ) : null}
      <text content="Enter 发送 · Ctrl+C 取消/退出 · /help 命令" fg={colors.muted} />
    </box>
  )
}

/** 负责 OpenTUI 生命周期；退出后将控制权交回 CLI 以关闭 Python sidecar。 */
export async function runTui(options: TuiOptions): Promise<void> {
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
