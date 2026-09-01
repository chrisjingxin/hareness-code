/** Slash Command 的 Dispatcher：只返回表现层无关的 semantic operation。 */

import {
  Capability,
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
import type { IdGenerator } from "./ports/id-generator"
import type { InteractiveApprovalMode } from "./runtime"

/** 命令选择器目标；与 InteractiveResult.present 的 target 保持一致。 */
export type CommandPickerTarget = "skills" | "threads" | "models" | "agents"

/** Handler 的唯一输出协议。它不返回 RPC method 字符串、success/error closure、
 * React callback 或 TUI local action；Controller 解释这些语义并完成所有 Agent effect。 */
export type CommandRpcMethod =
  | "threads.open"
  | "agents.list"
  | "teams.list"
  | "teams.inspect"
  | "teams.generate"
  | "teams.run"
  | "teams.cancel"

/** RPC 结果可将成功与失败重新映射到下一条结构化命令结果。 */
export type CommandRpcResult = {
  type: "rpc"
  method: CommandRpcMethod
  params: Record<string, unknown>
  onSuccess: (value: unknown) => CommandResult
  onError: (error: unknown) => CommandResult
}

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
  | { type: "side-question"; question: string; threadId: string | null }
  | { type: "set-approval-mode"; mode: "plan"; prompt?: string; notice?: string }
  | { type: "restore-approval-mode" }
  | { type: "focus-plan" }
  | { type: "view-plan"; threadId: string; markdown: string; virtualPath: string; displayPath: string }
  | CommandRpcResult

/** Dispatcher 所需的最小状态快照；展示文案由调用方在进入 Handler 前生成。 */
export type CommandDispatchContext = {
  commandContext: CommandContext
  threadId: string | null
  runtimeStatus: string
  /** 唯一 ID 生成器（Port 注入），供需要本地生成 run_id 的命令使用。 */
  idGenerator: IdGenerator
  /** 当前会话审批档位，供 /plan 判断是否已在 plan。 */
  approvalMode?: InteractiveApprovalMode
  /** 当前是否正挂起计划审批；/plan-view 复用该视图，不重复读取或创建审批。 */
  pendingPlanInteraction?: boolean
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
  if (availability.state === "hidden") return notice(availability.reason)
  if (availability.state === "disabled") return notice(`/${definition.name} 暂不可用：${availability.reason}。`)

  if (definition.source.type === "plugin" && definition.requestedSkillId) {
    const args = command.argument?.trim() ?? ""
    const rawInvocation = command.rawInvocation ?? `/${definition.name}${args ? ` ${args}` : ""}`
    return {
      type: "submit-prompt",
      prompt: rawInvocation,
      requestedSkill: {
        id: definition.requestedSkillId,
        args,
        raw_invocation: rawInvocation,
        command_name: definition.name,
      },
    }
  }
  const handler = builtinHandlers[definition.id]
  if (!handler) return notice(`/${definition.name} 尚未接入当前客户端。`)
  return handler({ ...context, command, definition, registry })
}

/** 所有现有 Builtin Handler 的稳定 ID 映射；禁止回退为按 name 的 switch。 */
const builtinHandlers: Readonly<Record<string, CommandHandler>> = {
  "system.help": context => notice(commandHelp(context.registry).map(item => `${item.command}  ${item.description}`).join("\n")),
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
  "context.compact": context => {
    if (context.command.argument) return notice("/compact 不接受参数。")
    if (!context.threadId) return notice("当前没有可压缩的 thread。")
    return { type: "compact", threadId: context.threadId }
  },
  "system.status": context => notice(context.runtimeStatus),
  "thread.resume": context => context.command.argument
    ? notice("/resume 不接受 thread_id；请在选择器中选择要恢复的 thread。")
    : { type: "present", target: "threads" },
  "model.select": context => ({ type: "present", target: "models", initialQuery: context.command.argument }),
  "skills.open": () => ({ type: "present", target: "skills" }),
  "agents.list": context => context.command.argument
    ? notice("/agents 不接受参数。")
    : { type: "present", target: "agents" },
  "teams.manage": handleTeamsCommand,
  "mcp.manage": context => ({ type: "mcp", argument: context.command.argument }),
  "host.web": context => context.command.argument
    ? notice("/web 不接受参数。")
    : { type: "request-handoff", threadId: context.threadId },
  "assist.btw": context => {
    const question = context.command.argument?.trim()
    if (!question) return notice("用法：/btw <你的问题>")
    return { type: "side-question", question, threadId: context.threadId }
  },
  "compose.new-work": context => {
    const goal = context.command.argument?.trim()
    if (!goal) return notice("开新需求请带目标，例如 /new-work 写 HTTP 服务。只要停用请用 /abandon。")
    return { type: "submit-prompt", prompt: `/new-work ${goal}` }
  },
  "compose.abandon": context => {
    if (context.command.argument?.trim()) {
      return notice("换题请用 /new-work <目标>。")
    }
    if (!context.threadId) return notice("当前没有可用 thread。")
    return {
      type: "request-confirmation",
      confirmationId: "compose-abandon",
      title: "废弃当前 Compose 需求？",
      message: "文档和已改代码会留在磁盘，进度回到空闲。",
      confirmLabel: "废弃",
      cancelLabel: "继续当前需求",
    }
  },
  "approval.plan": handlePlanCommand,
  "approval.plan-view": handlePlanViewCommand,
}

const ALREADY_IN_PLAN_NOTICE = "已在计划模式。改计划请直接发消息；离开请用 `/plan exit`。"

/** `/plan`：空参切档、整段 exit 恢复、其它参数当规划目标提交。 */
function handlePlanCommand(context: CommandHandlerContext): CommandResult {
  const argument = context.command.argument?.trim() ?? ""
  const current = context.approvalMode ?? "default"
  const inPlan = current === "plan"
  const running = context.commandContext.activeRun

  if (argument.toLowerCase() === "exit") {
    return inPlan ? { type: "restore-approval-mode" } : notice("当前不在计划模式。")
  }

  if (!argument) {
    if (inPlan) return notice(ALREADY_IN_PLAN_NOTICE)
    return {
      type: "set-approval-mode",
      mode: "plan",
      notice: "已进入计划模式。发送消息开始规划；离开请用 `/plan exit`。",
    }
  }

  if (inPlan) {
    if (running) return notice(ALREADY_IN_PLAN_NOTICE)
    return { type: "submit-prompt", prompt: argument }
  }
  if (running) {
    return {
      type: "set-approval-mode",
      mode: "plan",
      notice: "已切换到计划模式；当前任务继续，空闲后再发送规划目标。",
    }
  }
  return { type: "set-approval-mode", mode: "plan", prompt: argument }
}

/** `/plan-view`：挂起审批直接聚焦；空闲时通过 canonical threads.open 读取计划。 */
function handlePlanViewCommand(context: CommandHandlerContext): CommandResult {
  if (context.command.argument?.trim()) return notice("/plan-view 不接受参数。")
  if (context.pendingPlanInteraction) return { type: "focus-plan" }
  if (!context.threadId) return notice("当前没有可查看计划的 thread。")
  return {
    type: "rpc",
    method: "threads.open",
    params: { thread_id: context.threadId },
    onSuccess: value => {
      const opened = value as { plan?: Record<string, unknown> }
      const plan = opened.plan
      if (!plan || typeof plan.has_plan !== "boolean" || typeof plan.plan_markdown !== "string"
        || plan.plan_virtual_path !== "/.harness/plan.md" || typeof plan.plan_display_path !== "string") {
        return notice("读取计划失败：Agent 返回的计划结构无效。")
      }
      if (!plan.has_plan) return notice("还没有计划。")
      return {
        type: "view-plan",
        threadId: context.threadId!,
        markdown: plan.plan_markdown,
        virtualPath: plan.plan_virtual_path,
        displayPath: plan.plan_display_path,
      }
    },
    onError: error => notice(`读取计划失败：${error instanceof Error ? error.message : String(error)}`),
  }
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
    const runId = context.idGenerator.uuid()
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
