/** TUI 中可直接处理的 Slash Command 解析器。 */

export type SlashCommandName =
  | "help"
  | "quit"
  | "clear"
  | "force-clear"
  | "status"
  | "version"
  | "resume"
  | "skills"

export type SlashCommand =
  | { name: SlashCommandName; argument?: string }
  | { name: "continue"; argument?: string }

export type SlashCommandDefinition = {
  name: SlashCommandName
  aliases?: readonly string[]
  description: string
}

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

/** Slash 菜单同时展示内置命令和用户可直接调用的 Skill。 */
export type CommandMenuItem =
  | { kind: "command"; command: SlashCommandDefinition }
  | { kind: "skill"; skill: SkillMenuItem }

/** 命令帮助、解析器和自动完成共用同一注册表，避免界面显示不存在的能力。 */
export const slashCommandDefinitions: readonly SlashCommandDefinition[] = [
  { name: "help", description: "显示可用命令" },
  { name: "quit", aliases: ["q"], description: "退出 za38" },
  { name: "clear", description: "开启新的 thread" },
  { name: "force-clear", description: "取消当前执行并开启新的 thread" },
  { name: "status", description: "显示运行状态" },
  { name: "version", description: "显示版本" },
  { name: "resume", description: "打开 thread 恢复选择器" },
  { name: "skills", description: "打开 Skill 选择器" },
]

export const slashCommandHelp: ReadonlyArray<{ command: string; description: string }> = slashCommandDefinitions.map(definition => ({
  command: `/${definition.name}${definition.aliases?.length ? `, /${definition.aliases.join(", /")}` : ""}`,
  description: definition.description,
}))

/** 只在输入以 / 开头且尚未进入参数区时，为 Prompt 提供可选命令。 */
export function findSlashCommands(value: string): readonly SlashCommandDefinition[] {
  const query = value.trimStart()
  if (!query.startsWith("/")) return []
  const name = query.slice(1).split(/\s/, 1)[0]?.toLowerCase() ?? ""
  if (query.slice(1).match(/\s/)) return []
  return slashCommandDefinitions.filter(definition => {
    const candidates = [definition.name, ...(definition.aliases ?? [])]
    return candidates.some(candidate => candidate.startsWith(name))
  })
}

/** 根据当前 `/` 前缀动态组合本地命令和可显式调用的 Skill。 */
export function findCommandMenuItems(value: string, skills: readonly SkillMenuItem[]): readonly CommandMenuItem[] {
  const query = value.trimStart()
  if (!query.startsWith("/") || query.slice(1).match(/\s/)) return []
  const needle = query.slice(1).toLowerCase()
  const commands: CommandMenuItem[] = findSlashCommands(value).map(command => ({ kind: "command", command }))
  const skillItems: CommandMenuItem[] = skills
    .filter(skill => skill.enabled && skill.userInvocable)
    .filter(skill => {
      const label = `skill:${skill.id}`.toLowerCase()
      const shortNeedle = needle.startsWith("skill:") ? needle.slice("skill:".length) : needle
      return [label, skill.id.toLowerCase(), skill.name.toLowerCase(), skill.description.toLowerCase()]
        .some(value => value.includes(needle) || value.includes(shortNeedle))
    })
    .map(skill => ({ kind: "skill", skill }))
  return [...commands, ...skillItems]
}

/** 将动态 Skill 项渲染成和 OpenCode 一致的 Slash 形式。 */
export function commandMenuItemLabel(item: CommandMenuItem): string {
  return item.kind === "command" ? `/${item.command.name}` : `/skill:${item.skill.id}`
}

/** 返回菜单右侧的紧凑说明，保持终端选择器一行一个选项。 */
export function commandMenuItemDescription(item: CommandMenuItem): string {
  return item.kind === "command"
    ? item.command.description
    : `${item.skill.source} · ${item.skill.description}`
}

/** 将完整输入解析为本地控制命令；未知命令返回 null 并交给 Agent 处理。 */
export function parseSlashCommand(input: string): SlashCommand | null {
  const value = input.trim()
  if (!value.startsWith("/")) return null

  const [rawName, ...rest] = value.slice(1).split(/\s+/)
  const argument = rest.join(" ").trim() || undefined
  if (rawName === "continue") return { name: "continue", argument }
  const definition = slashCommandDefinitions.find(item => item.name === rawName || item.aliases?.includes(rawName))
  return definition ? { name: definition.name, argument } : null
}
