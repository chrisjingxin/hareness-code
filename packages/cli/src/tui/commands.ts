/** TUI 中可直接处理的 Slash Command 解析器。 */

export type SlashCommandName =
  | "help"
  | "quit"
  | "clear"
  | "force-clear"
  | "threads"
  | "compact"
  | "mcp"
  | "tools"
  | "reload"
  | "remember"
  | "skill"
  | "agents"
  | "version"

export type SlashCommand = {
  name: SlashCommandName
  argument?: string
}

export const slashCommandHelp: ReadonlyArray<{ command: string; description: string }> = [
  { command: "/help", description: "显示可用命令" },
  { command: "/quit, /q", description: "退出 za38" },
  { command: "/clear", description: "开启新会话" },
  { command: "/force-clear", description: "取消当前执行并开启新会话" },
  { command: "/threads", description: "浏览和恢复会话（待接入持久化）" },
  { command: "/compact", description: "压缩当前会话（待接入）" },
  { command: "/mcp, /tools, /reload", description: "查看或重载 Agent 能力（待接入）" },
  { command: "/remember, /skill:<name>, /agents", description: "使用 za38 原生记忆、技能和子 Agent（待接入）" },
  { command: "/version", description: "显示版本" },
]

/** 只把完整的 Slash Command 视为本地控制命令，普通文本仍交给 Agent。 */
export function parseSlashCommand(input: string): SlashCommand | null {
  const value = input.trim()
  if (!value.startsWith("/")) return null

  const [rawName, ...rest] = value.slice(1).split(/\s+/)
  const argument = rest.join(" ").trim() || undefined
  switch (rawName) {
    case "help":
    case "clear":
    case "force-clear":
    case "threads":
    case "compact":
    case "mcp":
    case "tools":
    case "reload":
    case "remember":
    case "agents":
    case "version":
      return { name: rawName, argument }
    case "quit":
    case "q":
      return { name: "quit", argument }
    default:
      if (rawName.startsWith("skill:")) {
        const skillName = rawName.slice("skill:".length).trim()
        return skillName ? { name: "skill", argument: skillName } : null
      }
      return null
  }
}
