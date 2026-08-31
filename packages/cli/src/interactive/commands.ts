/** Interactive Core 的 Slash Command Registry、解析与可用性策略。 */

import { Capability, type AgentCommand } from "@za38/protocol"

/** 命令打开的交互入口类型；由 Result Adapter 映射为具体副作用。 */
export type CommandPresentation = "action" | "picker" | "viewer" | "dialog"

/** Command 来源预留给后续 Skill、MCP 与 Plugin Loader；当前只注册内置命令。 */
export type CommandSource =
  | { type: "builtin" }
  | { type: "skill"; id: string }
  | { type: "mcp"; server: string }
  | { type: "plugin"; id: string }

/** 命令可见性依赖的 Work Mode；与 state.ts 的 WorkMode 结构兼容。 */
export type WorkMode = "build" | "compose"

/** 命令的运行状态约束；缺 capability 时隐藏，暂时不可执行时给出禁用原因。 */
export type CommandRequirements = {
  capabilities?: readonly string[]
  requiresThread?: boolean
  requiresIdle?: boolean
  /** 命令仅在这些 Work Mode 下可见；不包含当前 mode 时隐藏。 */
  workModes?: readonly WorkMode[]
  /** 命令要求当前是否存在进行中的 Work Item；与状态不符时禁用。 */
  requiresActiveWorkItem?: boolean
}

/** 后续 Dispatcher 复用的最小安全元数据，不能由表现层自行扩大权限。 */
export type CommandSafety = {
  allowedDuringRun?: boolean
  confirmation?: "never" | "when-running" | "always"
}

/** 兼容命令只保留解析入口，菜单默认不推荐，并由 Dispatcher 输出迁移说明。 */
export type DeprecatedCommand = {
  replacement: string
}

/** Registry 对外暴露的稳定命令描述。 */
export type CommandDefinition = {
  id: string
  name: string
  aliases?: readonly string[]
  description: string
  source: CommandSource
  presentation: CommandPresentation
  argumentHint?: string
  suggested?: boolean
  requirements?: CommandRequirements
  safety?: CommandSafety
  deprecated?: DeprecatedCommand
  requestedSkillId?: string
}

/** 用于计算当前命令菜单可用性的最小共享状态快照。 */
export type CommandContext = {
  capabilities: ReadonlySet<string>
  hasThread: boolean
  activeRun: boolean
  pendingOperation: boolean
  hasPendingInteraction: boolean
  workMode: WorkMode
  hasActiveWorkItem: boolean
}

export type CommandAvailability =
  | { state: "available" }
  | { state: "disabled"; reason: string }
  | { state: "hidden"; reason: string }

/** 解析成功后携带 canonical 名称、规范化参数和受保护的 Plugin 原文。 */
export type SlashCommand = {
  id: string
  name: string
  argument?: string
  /** Plugin Command 必须把用户实际输入一路交给 Host，不能由 canonical 名称重建。 */
  rawInvocation?: string
}

export type SlashCommandResolution =
  | { kind: "not-command" }
  | { kind: "escaped"; message: string }
  | { kind: "command"; command: SlashCommand }
  | { kind: "unknown"; name: string; suggestions: readonly CommandDefinition[] }

/** Skill catalog 经 JSON-RPC 归一化后供 Slash 菜单和选择器共同使用的最小视图模型。 */
export type SkillMenuItem = {
  id: string
  name: string
  description: string
  source: string
  enabled: boolean
  userInvocable: boolean
  argumentHint?: string
}

/** Slash 菜单同时展示 Registry 中可见的命令和可显式调用的 Skill。 */
export type CommandMenuItem =
  | { kind: "command"; command: CommandDefinition; availability: CommandAvailability }
  | { kind: "skill"; skill: SkillMenuItem }

/** 内置命令所需 capability 的全集，供旧的测试运行时提供兼容默认值。 */
export const builtinCommandCapabilities = [
  Capability.THREADS_READ,
  Capability.CONTEXT_MANAGE,
  Capability.MODELS_READ,
  Capability.MODELS_SELECT,
  Capability.SKILLS_READ,
  Capability.AGENTS_READ,
  Capability.TEAMS_READ,
  Capability.TEAMS_MANAGE,
  Capability.HOST_ATTACH,
] as const

/** 所有动态来源必须通过同一不可变 Registry 构造，避免覆盖内置命令或产生不确定别名。 */
export class CommandRegistry {
  private readonly definitionsById = new Map<string, CommandDefinition>()
  private readonly definitionsByName = new Map<string, CommandDefinition>()
  readonly definitions: readonly CommandDefinition[]

  constructor(definitions: readonly CommandDefinition[]) {
    for (const definition of definitions) {
      if (!definition.id || !definition.name) throw new Error("Command 必须包含非空 id 和 name")
      if (this.definitionsById.has(definition.id)) throw new Error(`重复的 Command id：${definition.id}`)
      this.definitionsById.set(definition.id, definition)
      for (const name of [definition.name, ...(definition.aliases ?? [])]) {
        const key = normalizeCommandName(name)
        if (!key) throw new Error(`Command ${definition.id} 包含空名称或别名`)
        const existing = this.definitionsByName.get(key)
        if (existing) throw new Error(`Command 名称或别名冲突：${name}（${existing.id}、${definition.id}）`)
        this.definitionsByName.set(key, definition)
      }
    }
    this.definitions = [...definitions]
  }

  /** 通过 canonical ID 获取定义，供执行层避免按名称另建特殊分支。 */
  get(id: string): CommandDefinition | undefined {
    return this.definitionsById.get(id)
  }

  /** 名称和别名始终按 ASCII 小写比较；参数不参与归一化。 */
  resolveName(name: string): CommandDefinition | undefined {
    return this.definitionsByName.get(normalizeCommandName(name))
  }

  /** 将 capability 缺失与运行态约束收敛到一处，菜单和执行入口共同使用。 */
  availability(definition: CommandDefinition, context: CommandContext): CommandAvailability {
    const missing = definition.requirements?.capabilities?.find(capability => !context.capabilities.has(capability))
    if (missing) return { state: "hidden", reason: `当前客户端未协商 ${missing}` }
    if (definition.requirements?.workModes && !definition.requirements.workModes.includes(context.workMode)) {
      return { state: "hidden", reason: "当前模式不可用（COMMAND_MODE_UNAVAILABLE）" }
    }
    if (definition.requirements?.requiresThread && !context.hasThread) {
      return { state: "disabled", reason: "当前没有可用 thread" }
    }
    if (definition.requirements?.requiresActiveWorkItem !== undefined && definition.requirements.requiresActiveWorkItem !== context.hasActiveWorkItem) {
      return {
        state: "disabled",
        reason: definition.requirements.requiresActiveWorkItem
          ? "当前没有进行中的 Compose 需求"
          : "当前已有进行中的 Compose 需求",
      }
    }
    if (definition.requirements?.requiresIdle && (context.activeRun || context.pendingOperation || context.hasPendingInteraction)) {
      return { state: "disabled", reason: "当前任务结束或交互完成后可用" }
    }
    return { state: "available" }
  }

  /** 只返回当前环境可见的命令；disabled 项保留在列表中以解释不可用原因。 */
  list(context: CommandContext): readonly { definition: CommandDefinition; availability: CommandAvailability }[] {
    return this.definitions
      .map(definition => ({ definition, availability: this.availability(definition, context) }))
      .filter((entry): entry is { definition: CommandDefinition; availability: Exclude<CommandAvailability, { state: "hidden" }> } => entry.availability.state !== "hidden")
  }

  /** 用候选名称的最小编辑距离给未知命令提供少量可执行的 canonical 建议。 */
  suggest(name: string, limit = 3): readonly CommandDefinition[] {
    const query = normalizeCommandName(name)
    if (!query) return []
    const candidates = new Map<string, { definition: CommandDefinition; distance: number }>()
    for (const definition of this.definitions) {
      const distance = Math.min(...[definition.name, ...(definition.aliases ?? [])]
        .map(candidate => levenshtein(query, normalizeCommandName(candidate))))
      const threshold = Math.max(2, Math.floor(Math.max(query.length, definition.name.length) * 0.4))
      if (distance <= threshold || definition.name.includes(query) || query.includes(definition.name)) {
        candidates.set(definition.id, { definition, distance })
      }
    }
    return [...candidates.values()]
      .sort((left, right) => left.distance - right.distance || left.definition.name.localeCompare(right.definition.name))
      .slice(0, limit)
      .map(candidate => candidate.definition)
  }
}

/** 当前已交付命令的唯一注册来源；未来 Loader 只能构造新的 Registry 快照。 */
export const builtinCommandDefinitions: readonly CommandDefinition[] = [
  { id: "system.help", name: "help", description: "显示可用命令", source: { type: "builtin" }, presentation: "viewer", suggested: true },
  { id: "system.quit", name: "quit", aliases: ["q"], description: "退出 za38", source: { type: "builtin" }, presentation: "action", suggested: true },
  { id: "thread.new", name: "new", aliases: ["clear"], description: "开启新的 thread", source: { type: "builtin" }, presentation: "dialog", suggested: true, safety: { confirmation: "when-running" } },
  { id: "thread.force-clear", name: "force-clear", description: "已废弃；请使用 /new", source: { type: "builtin" }, presentation: "action", deprecated: { replacement: "/new" } },
  { id: "context.compact", name: "compact", description: "压缩当前 thread 上下文", source: { type: "builtin" }, presentation: "dialog", suggested: true, requirements: { capabilities: [Capability.CONTEXT_MANAGE], requiresThread: true, requiresIdle: true } },
  { id: "system.status", name: "status", description: "显示运行状态", source: { type: "builtin" }, presentation: "viewer", suggested: true },
  { id: "system.version", name: "version", description: "显示版本", source: { type: "builtin" }, presentation: "viewer" },
  { id: "thread.resume", name: "resume", aliases: ["continue", "threads"], description: "打开 thread 恢复选择器", source: { type: "builtin" }, presentation: "picker", suggested: true, requirements: { capabilities: [Capability.THREADS_READ], requiresIdle: true } },
  { id: "model.select", name: "model", aliases: ["models"], description: "选择当前 thread 下一次运行的模型 Profile", source: { type: "builtin" }, presentation: "picker", suggested: true, argumentHint: "[query]", requirements: { capabilities: [Capability.MODELS_READ], requiresIdle: true } },
  { id: "skills.open", name: "skills", description: "打开 Skill 选择器", source: { type: "builtin" }, presentation: "picker", suggested: true, requirements: { capabilities: [Capability.SKILLS_READ] } },
  { id: "agents.list", name: "agents", description: "查看可派发的 Agent（内置 + Plugin）", source: { type: "builtin" }, presentation: "picker", suggested: true, requirements: { capabilities: [Capability.AGENTS_READ] } },
  { id: "teams.manage", name: "teams", description: "查看或控制 Agent Team", source: { type: "builtin" }, presentation: "viewer", argumentHint: "[show|status|generate|run|cancel] ...", suggested: true, requirements: { capabilities: [Capability.TEAMS_READ] } },
  { id: "mcp.manage", name: "mcp", description: "查看 MCP 服务器状态", source: { type: "builtin" }, presentation: "viewer", suggested: true },
  { id: "host.web", name: "web", description: "在浏览器中接管当前会话，可从空首页或当前 thread 启动", source: { type: "builtin" }, presentation: "action", suggested: true, requirements: { requiresIdle: true } },
  { id: "compose.new-work", name: "new-work", description: "放下当前 Compose 需求并立刻访谈新目标", source: { type: "builtin" }, presentation: "action", argumentHint: "<目标>", requirements: { workModes: ["compose"], requiresThread: true } },
  { id: "compose.abandon", name: "abandon", description: "废弃当前 Compose 需求并回到空闲", source: { type: "builtin" }, presentation: "action", requirements: { workModes: ["compose"], requiresThread: true, requiresActiveWorkItem: true }, safety: { confirmation: "always" } },
  { id: "assist.btw", name: "btw", description: "向 Agent 提出一个与当前任务无关的问题", source: { type: "builtin" }, presentation: "action", argumentHint: "[question]", requirements: { workModes: ["build", "compose"] } },
]

export const commandRegistry = new CommandRegistry(builtinCommandDefinitions)

/** 把 Host 已校验的 Plugin Command 摘要合并进单一 Registry；正文从不进入 CLI。 */
export function createCommandRegistry(
  agentCommands: readonly AgentCommand[] = [],
  baseDefinitions: readonly CommandDefinition[] = builtinCommandDefinitions,
): CommandRegistry {
  const reservedNames = new Set(
    baseDefinitions.flatMap(definition => [definition.name, ...(definition.aliases ?? [])])
      .map(normalizeCommandName),
  )
  const usedIds = new Set<string>()
  const pluginDefinitions = [...agentCommands]
    .sort((left, right) => left.id.localeCompare(right.id))
    .map((command): CommandDefinition => {
      if (usedIds.has(command.id)) throw new Error(`重复的 Command id：${command.id}`)
      usedIds.add(command.id)
      let name = command.name
      if (!name.startsWith("plugin:") && reservedNames.has(normalizeCommandName(name))) {
        const extension = safeExtensionName(command.plugin_id)
        name = `${extension}.${name}`
        let suffix = 1
        while (reservedNames.has(normalizeCommandName(name))) {
          name = `${extension}.${command.name}.${suffix}`
          suffix += 1
        }
      }
      reservedNames.add(normalizeCommandName(name))
      return {
        id: command.id,
        name,
        description: command.description,
        source: { type: "plugin", id: command.plugin_id },
        presentation: "action",
        argumentHint: command.argument_hint ?? undefined,
        suggested: true,
        requirements: { capabilities: [Capability.SKILLS_READ], requiresIdle: true },
        safety: { confirmation: "never" },
        requestedSkillId: command.requested_skill_id,
      }
    })
  return new CommandRegistry([...baseDefinitions, ...pluginDefinitions])
}

/** 兼容既有帮助渲染器；内容仍完全由 Registry 生成。 */
export const slashCommandHelp: ReadonlyArray<{ command: string; description: string }> = commandRegistry.definitions.map(definition => ({
  command: `/${definition.name}${definition.aliases?.length ? `, /${definition.aliases.join(", /")}` : ""}`,
  description: definition.description,
}))

/** 从指定不可变 Registry 生成帮助，动态 Plugin Command 与菜单保持同一事实来源。 */
export function commandHelp(
  registry: CommandRegistry,
): ReadonlyArray<{ command: string; description: string }> {
  return registry.definitions.map(definition => ({
    command: `/${definition.name}${definition.aliases?.length ? `, /${definition.aliases.join(", /")}` : ""}`,
    description: definition.description,
  }))
}

/** 返回默认交互式 capability 集合，旧测试未提供握手结果时保持已有菜单行为。 */
export function defaultCommandContext(overrides: Partial<Omit<CommandContext, "capabilities">> & { capabilities?: Iterable<string> } = {}): CommandContext {
  return {
    capabilities: new Set(overrides.capabilities ?? builtinCommandCapabilities),
    hasThread: overrides.hasThread ?? false,
    activeRun: overrides.activeRun ?? false,
    pendingOperation: overrides.pendingOperation ?? false,
    hasPendingInteraction: overrides.hasPendingInteraction ?? false,
    workMode: overrides.workMode ?? "build",
    hasActiveWorkItem: overrides.hasActiveWorkItem ?? false,
  }
}

/** 只在输入以 / 开头且尚未进入参数区时，为 Prompt 提供可选内置命令。 */
export function findSlashCommands(
  value: string,
  context = defaultCommandContext(),
  registry = commandRegistry,
): readonly CommandDefinition[] {
  const query = value.trimStart()
  if (!query.startsWith("/") || query.startsWith("//") || query.slice(1).match(/\s/)) return []
  const name = query.slice(1).toLowerCase()
  return registry.list(context)
    .filter(({ definition }) => shouldShowInMenu(definition, name))
    .filter(({ definition }) => [definition.name, ...(definition.aliases ?? [])]
      .some(candidate => candidate.startsWith(name)))
    .map(({ definition }) => definition)
}

/** 根据当前 / 前缀动态组合可见内置命令和可显式调用的 Skill。 */
export function findCommandMenuItems(
  value: string,
  skills: readonly SkillMenuItem[],
  context = defaultCommandContext(),
  registry = commandRegistry,
): readonly CommandMenuItem[] {
  const query = value.trimStart()
  if (!query.startsWith("/") || query.startsWith("//") || query.slice(1).match(/\s/)) return []
  const needle = query.slice(1).toLowerCase()
  const commands: CommandMenuItem[] = registry.list(context)
    .filter(({ definition }) => shouldShowInMenu(definition, needle))
    .filter(({ definition }) => [definition.name, ...(definition.aliases ?? [])]
      .some(candidate => candidate.startsWith(needle)))
    .map(({ definition, availability }) => ({ kind: "command", command: definition, availability }))
  const skillItems: CommandMenuItem[] = skills
    .filter(skill => skill.enabled && skill.userInvocable)
    .filter(skill => {
      const label = `skill:${skill.id}`.toLowerCase()
      const shortNeedle = needle.startsWith("skill:") ? needle.slice("skill:".length) : needle
      return [label, skill.id.toLowerCase(), skill.name.toLowerCase(), skill.description.toLowerCase()]
        .some(candidate => candidate.includes(needle) || candidate.includes(shortNeedle))
    })
    .map(skill => ({ kind: "skill", skill }))
  return [...commands, ...skillItems]
}

/** 将动态 Skill 项渲染成稳定 Slash 形式；内置命令一律显示 canonical 名称。 */
export function commandMenuItemLabel(item: CommandMenuItem): string {
  return item.kind === "command" ? `/${item.command.name}` : `/skill:${item.skill.id}`
}

/** disabled 命令在菜单中保留原说明和原因，避免用户误以为能力不存在。 */
export function commandMenuItemDescription(item: CommandMenuItem): string {
  if (item.kind === "skill") return `${item.skill.source} · ${item.skill.description}`
  return item.availability.state === "disabled"
    ? `${item.command.description} · ${item.availability.reason}`
    : item.command.description
}

/** 解析输入为命令、普通文本、转义 Slash 或未知命令；参数不会被小写化或解引号。 */
export function resolveSlashCommand(input: string, registry = commandRegistry): SlashCommandResolution {
  const value = input.trimStart()
  if (!value.startsWith("/")) return { kind: "not-command" }
  if (value.startsWith("//")) return { kind: "escaped", message: value.slice(1) }
  const match = /^\/([^\s/]+)([\s\S]*)$/.exec(value)
  if (!match) return { kind: "unknown", name: "", suggestions: [] }
  const [, rawName, rawArgument] = match
  const definition = registry.resolveName(rawName)
  if (!definition) return { kind: "unknown", name: rawName, suggestions: registry.suggest(rawName) }
  const argument = rawArgument.trimStart() || undefined
  return {
    kind: "command",
    command: {
      id: definition.id,
      name: definition.name,
      argument,
      ...(definition.source.type === "plugin" ? { rawInvocation: input } : {}),
    },
  }
}

/** 保留旧调用点的成功解析 API；未知命令必须使用 resolveSlashCommand 区分。 */
export function parseSlashCommand(
  input: string,
  registry = commandRegistry,
): SlashCommand | null {
  const resolution = resolveSlashCommand(input, registry)
  return resolution.kind === "command" ? resolution.command : null
}

/** 将未知命令转换为不经模型的本地提示，并只提供当前 Registry 可解析的建议。 */
export function unknownCommandNotice(resolution: Extract<SlashCommandResolution, { kind: "unknown" }>): string {
  const received = resolution.name ? `/${resolution.name}` : "/"
  if (!resolution.suggestions.length) return `未知命令：${received}。输入 /help 查看可用命令。`
  return `未知命令：${received}。你是否想使用：${resolution.suggestions.map(command => `/${command.name}`).join("、")}？`
}

/** 空 Slash 菜单不展示已废弃命令；用户继续输入其名称时仍可看到迁移说明。 */
function shouldShowInMenu(definition: CommandDefinition, needle: string): boolean {
  return Boolean(needle) || !definition.deprecated
}

/** 名称只允许按不区分大小写匹配；不对参数或用户消息执行这种归一化。 */
function normalizeCommandName(value: string): string {
  return value.trim().toLowerCase()
}

/** 冲突回退只使用 Host 提供的 Plugin 身份，不把正文或 store 路径带入 CLI。 */
function safeExtensionName(pluginId: string): string {
  const segment = pluginId.split("/").filter(Boolean).at(-1) ?? "plugin"
  return segment.replace(/[^A-Za-z0-9._-]+/g, "-") || "plugin"
}

/** 小型无依赖编辑距离实现，命令数量很小，优先保持建议结果可预测。 */
function levenshtein(left: string, right: string): number {
  if (left === right) return 0
  if (!left) return right.length
  if (!right) return left.length
  let previous = Array.from({ length: right.length + 1 }, (_, index) => index)
  for (let leftIndex = 1; leftIndex <= left.length; leftIndex += 1) {
    const next = [leftIndex]
    for (let rightIndex = 1; rightIndex <= right.length; rightIndex += 1) {
      next[rightIndex] = Math.min(
        previous[rightIndex]! + 1,
        next[rightIndex - 1]! + 1,
        previous[rightIndex - 1]! + (left[leftIndex - 1] === right[rightIndex - 1] ? 0 : 1),
      )
    }
    previous = next
  }
  return previous[right.length]!
}
