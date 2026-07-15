/** TUI 中可直接处理的 Slash Command 解析器。 */

export type SlashCommandName =
  | "help"
  | "quit"
  | "clear"
  | "force-clear"
  | "version"

export type SlashCommand = {
  name: SlashCommandName
  argument?: string
}

export type SlashCommandDefinition = {
  name: SlashCommandName
  aliases?: readonly string[]
  description: string
}

/** 命令帮助、解析器和自动完成共用同一注册表，避免界面显示不存在的能力。 */
export const slashCommandDefinitions: readonly SlashCommandDefinition[] = [
  { name: "help", description: "显示可用命令" },
  { name: "quit", aliases: ["q"], description: "退出 za38" },
  { name: "clear", description: "开启新会话" },
  { name: "force-clear", description: "取消当前执行并开启新会话" },
  { name: "version", description: "显示版本" },
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

/** 只把完整的 Slash Command 视为本地控制命令，普通文本仍交给 Agent。 */
export function parseSlashCommand(input: string): SlashCommand | null {
  const value = input.trim()
  if (!value.startsWith("/")) return null

  const [rawName, ...rest] = value.slice(1).split(/\s+/)
  const argument = rest.join(" ").trim() || undefined
  const definition = slashCommandDefinitions.find(item => item.name === rawName || item.aliases?.includes(rawName))
  return definition ? { name: definition.name, argument } : null
}
