/** 命令行参数解析模块：把用户输入转换成稳定的内部命令描述。 */

export type Command =
  | { kind: "run"; message?: string; nonInteractive: boolean; json: boolean; cwd: string; configPath?: string; threadId?: string; sandbox?: "remote" | false }
  | { kind: "config.show" | "config.path"; cwd: string; configPath?: string }

/** 解析交互、无头执行和配置管理命令，并保留工作区与配置路径。 */
export function parseArgs(argv: string[], cwd = process.cwd()): Command {
  const args = [...argv]
  const command = args[0]
  if (command === "config") {
    const action = args[1]
    if (action !== "show" && action !== "path") throw new Error("Usage: za38 config <show|path> [--config PATH]")
    const configPath = optionValue(args.slice(2), "--config")
    return { kind: `config.${action}`, cwd, configPath }
  }
  if (["threads", "agents", "skills", "mcp"].includes(command ?? "")) {
    throw new Error(`'za38 ${command}' is planned but not implemented in this vertical slice`)
  }

  const configPath = optionValue(args, "--config")
  const cwdValue = optionValue(args, "--cwd")
  const nonInteractive = hasOption(args, "-n") || hasOption(args, "--non-interactive")
  const message = optionValue(args, "-n") ?? optionValue(args, "--non-interactive") ?? optionValue(args, "-m") ?? optionValue(args, "--message")
  const json = hasOption(args, "--json")
  const threadId = optionValue(args, "--resume")
  const sandbox = sandboxOption(args)
  if (nonInteractive && !message) throw new Error("--non-interactive requires a message")
  return { kind: "run", message, nonInteractive, json, cwd: cwdValue ?? cwd, configPath, threadId, sandbox }
}

/** 判断参数列表是否包含指定的无值开关。 */
function hasOption(args: string[], name: string): boolean {
  return args.includes(name)
}

/** 读取带值选项，并统一处理缺少值或误把下一个开关当值的情况。 */
function optionValue(args: string[], name: string): string | undefined {
  const index = args.indexOf(name)
  if (index < 0) return undefined
  const value = args[index + 1]
  if (!value || value.startsWith("-")) throw new Error(`${name} requires a value`)
  return value
}

/** 解析 Qwen 风格 sandbox 开关；当前只支持企业远端 provider。 */
function sandboxOption(args: string[]): "remote" | false | undefined {
  if (args.includes("-s") || args.includes("--sandbox")) return "remote"
  const prefixed = args.find(arg => arg.startsWith("--sandbox="))
  if (!prefixed) return undefined
  const value = prefixed.slice("--sandbox=".length).trim().toLowerCase()
  if (["true", "remote"].includes(value)) return "remote"
  if (["false", "off"].includes(value)) return false
  throw new Error("--sandbox only supports remote or false")
}
