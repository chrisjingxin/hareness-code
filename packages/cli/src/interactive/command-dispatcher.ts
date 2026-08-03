/** Slash Command 的 Dispatcher：只返回表现层无关的 semantic operation。 */

import type { RequestedSkill } from "@za38/protocol"

import {
  commandRegistry,
  slashCommandHelp,
  type CommandContext,
  type CommandDefinition,
  type CommandRegistry,
  type SlashCommand,
} from "./commands"

/** 命令选择器目标；与 InteractiveResult.present 的 target 保持一致。 */
export type CommandPickerTarget = "skills" | "threads" | "models"

/**
 * Handler 的唯一输出协议。它不返回 RPC method 字符串、success/error closure、
 * React callback 或 TUI local action；Controller 解释这些语义并完成所有 Agent effect。
 */
export type CommandResult =
  | { type: "notice"; message: string }
  | { type: "request-exit" }
  | { type: "clear-thread" }
  | { type: "request-confirmation"; confirmationId: string; title: string; message: string; confirmLabel?: string; cancelLabel?: string }
  | { type: "present"; target: CommandPickerTarget; initialQuery?: string }
  | { type: "compact"; threadId: string }
  | { type: "mcp"; argument?: string }
  | { type: "request-handoff"; threadId: string | null }
  | { type: "submit-prompt"; prompt: string; requestedSkill?: RequestedSkill }

/** Dispatcher 所需的最小状态快照；展示文案由调用方在进入 Handler 前生成。 */
export type CommandDispatchContext = {
  commandContext: CommandContext
  threadId: string | null
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
  if (!handler) return notice(`/${definition.name} 尚未接入当前客户端。`)
  return handler({ ...context, command, definition })
}

/** 所有现有 Builtin Handler 的稳定 ID 映射；禁止回退为按 name 的 switch。 */
const builtinHandlers: Readonly<Record<string, CommandHandler>> = {
  "system.help": () => notice(slashCommandHelp.map(item => `${item.command}  ${item.description}`).join("\n")),
  "system.quit": () => ({ type: "request-exit" }),
  "thread.new": context => context.commandContext.activeRun
    ? {
        type: "request-confirmation",
        confirmationId: "clear-thread",
        title: "开始新的 Thread？",
        message: "当前任务仍在执行。确认后将先取消任务，再清空当前 Thread。",
        confirmLabel: "取消任务并新建",
        cancelLabel: "保留当前 Thread",
      }
    : { type: "clear-thread" },
  "thread.force-clear": () => notice("/force-clear 已废弃，请使用 /new；当前任务执行时会先请求确认。"),
  "context.compact": context => {
    if (context.command.argument) return notice("/compact 不接受参数。")
    if (!context.threadId) return notice("当前没有可压缩的 thread。")
    return { type: "compact", threadId: context.threadId }
  },
  "system.status": context => notice(context.runtimeStatus),
  "system.version": context => notice(context.versionSummary),
  "thread.resume": context => context.command.argument
    ? notice("/resume 不接受 thread_id；请在选择器中选择要恢复的 thread。")
    : { type: "present", target: "threads" },
  "model.select": context => ({ type: "present", target: "models", initialQuery: context.command.argument }),
  "skills.open": () => ({ type: "present", target: "skills" }),
  "mcp.manage": context => ({ type: "mcp", argument: context.command.argument }),
  "host.web": context => context.command.argument
    ? notice("/web 不接受参数。")
    : { type: "request-handoff", threadId: context.threadId },
}

/** 生成统一 notice，减少 Handler 中重复的结构字面量。 */
function notice(message: string): CommandResult {
  return { type: "notice", message }
}

/** 将 context.compact 结果压缩为不暴露归档正文的本地通知。 */
export function contextCompactNotice(value: unknown): string {
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
