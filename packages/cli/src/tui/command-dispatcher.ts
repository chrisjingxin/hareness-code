/** Slash Command 的 Dispatcher：Handler 只返回结构化结果，TUI 只负责适配副作用。 */

import type { RequestedSkill } from "@za38/protocol"

import {
  commandRegistry,
  slashCommandHelp,
  type CommandContext,
  type CommandDefinition,
  type CommandRegistry,
  type SlashCommand,
} from "./commands"

/** 由 TUI Adapter 执行的本地状态变更；不能在 Handler 中直接操作 React 状态。 */
export type CommandLocalAction = "clear-thread" | "cancel-active-run-and-clear-thread"

/** 现有命令能够打开的选择器；后续 Manager/Viewer 会按相同模式扩展。 */
export type CommandPicker = "skills" | "threads" | "models"

/** 当前只实现新建 thread 的确认框，Dialog Shell 将由 ZC-065 继续抽象。 */
export type CommandDialog = {
  kind: "confirm-new-thread"
  title: string
  message: string
  confirm: { type: "local-action"; action: "cancel-active-run-and-clear-thread" }
}

/** JSON-RPC 结果可将成功与失败重新映射到下一条结构化命令结果。 */
export type CommandRpcResult = {
  type: "rpc"
  method: "context.compact"
  params: { thread_id: string }
  onSuccess: (value: unknown) => CommandResult
  onError: (error: unknown) => CommandResult
}

/** Handler 的唯一输出协议，避免根组件再按命令名称解释业务语义。 */
export type CommandResult =
  | { type: "notice"; message: string }
  | { type: "exit" }
  | { type: "local-action"; action: CommandLocalAction }
  | { type: "open-picker"; picker: CommandPicker; initialQuery?: string }
  | { type: "open-dialog"; dialog: CommandDialog }
  | CommandRpcResult
  | { type: "submit-prompt"; prompt: string; requestedSkill?: RequestedSkill }

/** Dispatcher 所需的最小状态快照；展示文案由调用方在进入 Handler 前生成。 */
export type CommandDispatchContext = {
  commandContext: CommandContext
  threadId?: string
  runtimeStatus: string
  versionSummary: string
}

type CommandHandlerContext = CommandDispatchContext & {
  command: SlashCommand
  definition: CommandDefinition
}

type CommandHandler = (context: CommandHandlerContext) => CommandResult

/**
 * 统一复核 Registry 可用性后调用对应 Handler。
 * 名称、别名和 capability 判断已经在 Registry 中完成，因此此处只能按稳定 ID 分派。
 */
export function dispatchSlashCommand(
  command: SlashCommand,
  context: CommandDispatchContext,
  registry: CommandRegistry = commandRegistry,
): CommandResult {
  const definition = registry.get(command.id)
  if (!definition) return notice(`未知命令：/${command.name}。输入 /help 查看可用命令。`)

  const availability = registry.availability(definition, context.commandContext)
  if (availability.state === "hidden") return notice(`/${definition.name} 当前不可用。`)
  if (availability.state === "disabled") return notice(`/${definition.name} 暂不可用：${availability.reason}。`)

  const handler = builtinHandlers[definition.id]
  if (!handler) return notice(`/${definition.name} 尚未接入当前 TUI。`)
  return handler({ ...context, command, definition })
}

/** 所有现有 Builtin Handler 的稳定 ID 映射；禁止回退为按 name 的 switch。 */
const builtinHandlers: Readonly<Record<string, CommandHandler>> = {
  "system.help": () => notice(slashCommandHelp.map(item => `${item.command}  ${item.description}`).join("\n")),
  "system.quit": () => ({ type: "exit" }),
  "thread.new": context => context.commandContext.activeRun
    ? {
        type: "open-dialog",
        dialog: {
          kind: "confirm-new-thread",
          title: "开始新的 Thread？",
          message: "当前任务仍在执行。确认后将先取消任务，再清空当前 Thread。",
          confirm: { type: "local-action", action: "cancel-active-run-and-clear-thread" },
        },
      }
    : { type: "local-action", action: "clear-thread" },
  "thread.force-clear": () => notice("/force-clear 已废弃，请使用 /new；当前任务执行时会先请求确认。"),
  "context.compact": context => {
    if (context.command.argument) return notice("/compact 不接受参数。")
    if (!context.threadId) return notice("当前没有可压缩的 thread。")
    return {
      type: "rpc",
      method: "context.compact",
      params: { thread_id: context.threadId },
      onSuccess: value => notice(contextCompactNotice(value)),
      onError: error => notice(`上下文压缩失败：${errorMessage(error)}`),
    }
  },
  "system.status": context => notice(context.runtimeStatus),
  "system.version": context => notice(context.versionSummary),
  "thread.resume": context => context.command.argument
    ? notice("/resume 不接受 thread_id；请在选择器中选择要恢复的 thread。")
    : { type: "open-picker", picker: "threads" },
  "model.select": context => ({ type: "open-picker", picker: "models", initialQuery: context.command.argument }),
  "skills.open": () => ({ type: "open-picker", picker: "skills" }),
}

/** 生成统一 notice，减少 Handler 中重复的结构字面量。 */
function notice(message: string): CommandResult {
  return { type: "notice", message }
}

/** 将 context.compact 结果压缩为不暴露归档正文的本地通知。 */
function contextCompactNotice(value: unknown): string {
  const result = value && typeof value === "object" ? value as Record<string, unknown> : {}
  const context = result.context && typeof result.context === "object"
    ? result.context as Record<string, unknown>
    : {}
  const action = typeof context.action === "string" ? context.action : "unknown"
  const estimated = typeof context.estimated_tokens === "number" ? context.estimated_tokens : undefined
  const cap = typeof context.input_cap_tokens === "number" ? context.input_cap_tokens : undefined
  const artifacts = Array.isArray(context.artifact_ids) ? context.artifact_ids.length : 0
  if (result.compacted === true) {
    const budget = estimated !== undefined && cap !== undefined ? ` ${estimated}/${cap}` : ""
    return `上下文已压缩${budget}${artifacts ? `，归档 ${artifacts} 项` : ""}。`
  }
  const reason = typeof context.miss_reason === "string" ? `：${context.miss_reason}` : ""
  return action === "manual_compaction_skipped"
    ? `上下文无需压缩${reason}。`
    : `上下文压缩未完成${reason}。`
}

/** 将未知错误转成可展示但不会泄漏 Error 对象结构的文字。 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
