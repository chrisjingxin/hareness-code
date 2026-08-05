/** Slash Command 的 Dispatcher：Handler 只返回结构化结果，TUI 只负责适配副作用。 */

import {
  Capability,
  type AgentsListResult,
  type RequestedSkill,
  type TeamDefinition,
  type TeamsListResult,
} from "@za38/protocol"

import {
  commandRegistry,
  commandHelp,
  type CommandContext,
  type CommandDefinition,
  type CommandRegistry,
  type SlashCommand,
} from "./commands"

/** 由 TUI Adapter 执行的本地状态变更；不能在 Handler 中直接操作 React 状态。 */
export type CommandLocalAction = "clear-thread" | "cancel-active-run-and-clear-thread"

/** 现有命令能够打开的选择器；后续 Manager/Viewer 会按相同模式扩展。 */
export type CommandPicker = "skills" | "threads" | "models"

/** 当前只实现新建 thread 的确认框，并复用现有 Dialog Shell。 */
export type CommandDialog = {
  kind: "confirm-new-thread"
  title: string
  message: string
  confirm: { type: "local-action"; action: "cancel-active-run-and-clear-thread" }
}

/** JSON-RPC 结果可将成功与失败重新映射到下一条结构化命令结果。 */
export type CommandRpcResult = {
  type: "rpc"
  method:
    | "context.compact"
    | "agents.list"
    | "teams.list"
    | "teams.inspect"
    | "teams.generate"
    | "teams.run"
    | "teams.cancel"
  params: Record<string, unknown>
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
  | { type: "mcp"; argument?: string }
  | { type: "web"; threadId: string }

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
  registry: CommandRegistry
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

  if (definition.source.type === "plugin" && definition.requestedSkillId) {
    const args = command.argument?.trim() ?? ""
    return {
      type: "submit-prompt",
      prompt: args || `执行 Plugin Command /${definition.name}`,
      requestedSkill: { id: definition.requestedSkillId, args },
    }
  }
  const handler = builtinHandlers[definition.id]
  if (!handler) return notice(`/${definition.name} 尚未接入当前 TUI。`)
  return handler({ ...context, command, definition, registry })
}

/** 所有现有 Builtin Handler 的稳定 ID 映射；禁止回退为按 name 的 switch。 */
const builtinHandlers: Readonly<Record<string, CommandHandler>> = {
  "system.help": context => notice(commandHelp(context.registry).map(item => `${item.command}  ${item.description}`).join("\n")),
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
  "agents.list": context => context.command.argument
    ? notice("/agents 不接受参数。")
    : {
        type: "rpc",
        method: "agents.list",
        params: {},
        onSuccess: value => notice(formatAgents(value)),
        onError: error => notice(`Agent 查询失败：${errorMessage(error)}`),
      },
  "teams.manage": handleTeamsCommand,
  "mcp.manage": context => ({ type: "mcp", argument: context.command.argument }),
  "host.web": context => context.command.argument
    ? notice("/web 不接受参数。")
    : { type: "web", threadId: context.threadId! },
}

/** 把 `/teams` 子命令映射成类型化 RPC；客户端不能提交任意 TeamDefinition。 */
function handleTeamsCommand(context: CommandHandlerContext): CommandResult {
  const argument = context.command.argument?.trim() ?? ""
  if (!argument || argument === "list") {
    return teamRpc(
      "teams.list",
      {},
      formatTeams,
      "Team 查询失败",
    )
  }
  const [action, remainder = ""] = splitFirst(argument)
  if (action === "show" || action === "status") {
    const id = remainder.trim()
    if (!id) return notice(`/teams ${action} 需要 ID。`)
    return teamRpc(
      "teams.inspect",
      { kind: action === "show" ? "definition" : "run", id },
      formatTeamInspect,
      "Team 详情查询失败",
    )
  }
  if (!context.commandContext.capabilities.has(Capability.TEAMS_MANAGE)) {
    return notice("当前客户端未协商 teams.manage。")
  }
  if (action === "cancel") {
    const runId = remainder.trim()
    if (!runId) return notice("/teams cancel 需要 run ID。")
    return teamRpc(
      "teams.cancel",
      { run_id: runId },
      value => {
        const result = value as { run_id?: unknown; cancelled?: unknown }
        return result.cancelled === true
          ? `已请求取消 Team Run ${String(result.run_id)}。`
          : `Team Run ${String(result.run_id)} 已结束或不在当前 Host 中运行。`
      },
      "Team 取消失败",
    )
  }
  if (action === "generate") {
    const parts = remainder.trim().split(/\s+/)
    if (parts.length < 3) {
      return notice("/teams generate 用法：/teams generate <team-id> <lead-agent> <worker1,worker2> [max-parallelism]")
    }
    const [id, leadAgentId, workersRaw, parallelRaw] = parts
    const workerAgentIds = workersRaw.split(",").map(value => value.trim()).filter(Boolean)
    const maxParallelism = parallelRaw === undefined ? 4 : Number(parallelRaw)
    if (!workerAgentIds.length || !Number.isInteger(maxParallelism) || maxParallelism < 1 || maxParallelism > 32) {
      return notice("worker 列表或 max-parallelism 无效。")
    }
    return teamRpc(
      "teams.generate",
      {
        id,
        lead_agent_id: leadAgentId,
        worker_agent_ids: workerAgentIds,
        max_parallelism: maxParallelism,
      },
      value => formatTeamDefinition(value as TeamDefinition),
      "Team 生成失败",
    )
  }
  if (action === "run") {
    const [teamId, request] = splitFirst(remainder.trim())
    if (!teamId || !request.trim()) {
      return notice("/teams run 用法：/teams run <team-id> <任务描述>")
    }
    if (!context.threadId) return notice("请先发送消息创建 Thread，再启动 Team。")
    const runId = crypto.randomUUID()
    return teamRpc(
      "teams.run",
      {
        team_id: teamId,
        request,
        thread_id: context.threadId,
        run_id: runId,
      },
      () => `Team ${teamId} 已启动，run ID：${runId}。使用 /teams status ${runId} 查看进度。`,
      "Team 启动失败",
    )
  }
  return notice("未知 Team 子命令。可用：list、show、status、generate、run、cancel。")
}

/** 构造统一 Team RPC 结果并将远端错误收敛为 notice。 */
function teamRpc(
  method: CommandRpcResult["method"],
  params: Record<string, unknown>,
  format: (value: unknown) => string,
  errorPrefix: string,
): CommandRpcResult {
  return {
    type: "rpc",
    method,
    params,
    onSuccess: value => notice(format(value)),
    onError: error => notice(`${errorPrefix}：${errorMessage(error)}`),
  }
}

function formatAgents(value: unknown): string {
  const result = value as AgentsListResult
  if (!result.agents.length) return "当前没有可派发的 Plugin Agent。"
  return [
    `Plugin Agents（snapshot ${result.snapshot_id.slice(0, 12)}）：`,
    ...result.agents.map(agent =>
      `- ${agent.id} · ${agent.description ?? agent.purpose} · model=${agent.model_profile_id}`,
    ),
    ...(result.diagnostics.length ? [`诊断：${result.diagnostics.join("；")}`] : []),
  ].join("\n")
}

function formatTeams(value: unknown): string {
  const result = value as TeamsListResult
  if (!result.teams.length) return "当前没有固定或已生成的 Agent Team。"
  return [
    "Agent Teams：",
    ...result.teams.map(team =>
      `- ${team.id} · ${team.description ?? "无说明"} · ${team.tasks.length} tasks · max=${team.max_parallelism}`,
    ),
    ...(result.diagnostics.length ? [`诊断：${result.diagnostics.join("；")}`] : []),
  ].join("\n")
}

function formatTeamInspect(value: unknown): string {
  const record = value && typeof value === "object" ? value as Record<string, unknown> : {}
  if (Array.isArray(record.tasks) && typeof record.run_id === "string") {
    const tasks = record.tasks as Array<Record<string, unknown>>
    return [
      `Team Run ${record.run_id} · ${String(record.status)}`,
      ...tasks.map(task =>
        `- ${String(task.id)} · ${String(task.status)}${task.error_code ? ` · ${String(task.error_code)}` : ""}`,
      ),
    ].join("\n")
  }
  return formatTeamDefinition(value as TeamDefinition)
}

function formatTeamDefinition(team: TeamDefinition): string {
  return [
    `Team ${team.id} · ${team.failure_policy} · max=${team.max_parallelism}`,
    ...team.tasks.map(task =>
      `- ${task.id} → ${task.agent_id} · ${task.access}${task.depends_on.length ? ` · after=${task.depends_on.join(",")}` : ""}`,
    ),
  ].join("\n")
}

function splitFirst(value: string): [string, string] {
  const match = /^(\S+)(?:\s+([\s\S]*))?$/.exec(value)
  return match ? [match[1], match[2] ?? ""] : ["", ""]
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
  const reason = compactMissReason(context.miss_reason)
  return action === "manual_compaction_skipped" || action === "manual_skipped"
    ? `上下文无需压缩：${reason}。`
    : `上下文压缩未完成：${reason}。`
}

/** 将服务端稳定诊断码翻译为用户可执行的说明，避免直接暴露内部枚举。 */
function compactMissReason(value: unknown): string {
  if (typeof value !== "string") return "未满足安全压缩条件"
  return {
    short_history: "可压缩历史不足两轮",
    manual_history_too_small: "当前可压缩历史过短，压缩后不会减少上下文",
    manual_no_savings: "摘要后上下文没有减少",
    summary_input_cap_exhausted: "摘要模型的输入预算不足",
    summary_input_no_complete_group: "没有可安全压缩的完整对话",
    savings_below_20_percent: "预计节省不足 20%",
  }[value] ?? "未满足安全压缩条件"
}

/** 将未知错误转成可展示但不会泄漏 Error 对象结构的文字。 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
