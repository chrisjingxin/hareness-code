export type Command =
  | { kind: "run"; message?: string; nonInteractive: boolean; json: boolean; cwd: string; configPath?: string; threadId?: string }
  | { kind: "config.show" | "config.path"; cwd: string; configPath?: string }

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
  if (nonInteractive && !message) throw new Error("--non-interactive requires a message")
  return { kind: "run", message, nonInteractive, json, cwd: cwdValue ?? cwd, configPath, threadId }
}

function hasOption(args: string[], name: string): boolean {
  return args.includes(name)
}

function optionValue(args: string[], name: string): string | undefined {
  const index = args.indexOf(name)
  if (index < 0) return undefined
  const value = args[index + 1]
  if (!value || value.startsWith("-")) throw new Error(`${name} requires a value`)
  return value
}
