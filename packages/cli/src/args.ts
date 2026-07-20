/** 命令行参数解析模块：把用户输入转换成稳定的内部命令描述。 */

export type Command =
  | { kind: "run"; message?: string; nonInteractive: boolean; json: boolean; cwd: string; configPath?: string; resume: boolean; sandbox?: "remote" | false }
  | { kind: "config.show" | "config.path"; cwd: string; configPath?: string; params?: Record<string, unknown> }
  | { kind: SkillCommandKind; cwd: string; configPath?: string; params: Record<string, unknown> }

export type SkillCommandKind =
  | "skills.list"
  | "skills.inspect"
  | "skills.set_enabled"
  | "skills.install"
  | "skills.update"
  | "skills.remove"
  | "skills.market.list"

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
  if (command === "skills") return parseSkillsCommand(args.slice(1), cwd)

  const configPath = optionValue(args, "--config")
  const cwdValue = optionValue(args, "--cwd")
  const nonInteractive = hasOption(args, "-n") || hasOption(args, "--non-interactive")
  const message = optionValue(args, "-n") ?? optionValue(args, "--non-interactive") ?? optionValue(args, "-m") ?? optionValue(args, "--message")
  const json = hasOption(args, "--json")
  const resume = hasOption(args, "--resume")
  rejectRetiredContinueOption(args)
  rejectResumeArgument(args)
  const sandbox = sandboxOption(args)
  if (nonInteractive && !message) throw new Error("--non-interactive requires a message")
  if (resume && nonInteractive) throw new Error("--resume requires the interactive TUI")
  return { kind: "run", message, nonInteractive, json, cwd: cwdValue ?? cwd, configPath, resume, sandbox }
}

/** 解析 Skill 管理命令；管理操作只通过 JSON-RPC 交给已启动的 sidecar。 */
function parseSkillsCommand(args: string[], cwd: string): Command {
  const action = args[0] ?? "list"
  const workspace = optionValue(args, "--workspace") ?? optionValue(args, "--cwd") ?? cwd
  const configPath = optionValue(args, "--config")
  if (action === "list") {
    return {
      kind: "skills.list",
      cwd: workspace,
      configPath,
      params: { include_disabled: !hasOption(args, "--enabled-only") },
    }
  }
  if (action === "inspect") {
    return skillIdCommand(args, workspace, configPath, "skills.inspect")
  }
  if (action === "enable" || action === "disable" || action === "trust") {
    return {
      kind: "skills.set_enabled",
      cwd: workspace,
      configPath,
      params: { id: positionalValue(args, `harness skills ${action} requires a Skill id`), enabled: action !== "disable" },
    }
  }
  if (action === "remove") return skillIdCommand(args, workspace, configPath, "skills.remove")
  if (action === "install" || action === "update") {
    const market = optionValue(args, "--market")
    const name = positionalValue(args, `harness skills ${action} requires a Skill name`)
    if (!market) throw new Error(`harness skills ${action} requires --market MARKET`)
    return {
      kind: action === "install" ? "skills.install" : "skills.update",
      cwd: workspace,
      configPath,
      params: { market, name, version: optionValue(args, "--version") },
    }
  }
  if (action === "market" || action === "market-list") {
    return {
      kind: "skills.market.list",
      cwd: workspace,
      configPath,
      params: { market: optionValue(args, "--market") },
    }
  }
  throw new Error("Usage: harness skills <list|inspect|enable|disable|trust|install|update|remove|market>")
}

/** 解析需要一个 canonical Skill id 的管理命令。 */
function skillIdCommand(args: string[], cwd: string, configPath: string | undefined, kind: "skills.inspect" | "skills.remove" | "skills.set_enabled"): Command {
  return { kind, cwd, configPath, params: { id: positionalValue(args, `${kind} requires a Skill id`) } }
}

/** 读取指定位置的非开关参数，避免把选项值误当成 Skill 名称。 */
function positionalValue(args: string[], message: string): string {
  const valueOptions = new Set(["--workspace", "--cwd", "--config", "--market", "--version"])
  for (let index = 1; index < args.length; index += 1) {
    const value = args[index]
    if (valueOptions.has(value)) {
      index += 1
      continue
    }
    if (value && !value.startsWith("-")) return value
  }
  throw new Error(message)
}

/** 判断参数列表是否包含指定的无值开关。 */
function hasOption(args: string[], name: string): boolean {
  return args.includes(name)
}

/** `--resume` 只打开交互式 thread 选择器，禁止用户输入或暴露内部 thread_id。 */
function rejectResumeArgument(args: string[]): void {
  if (args.some(argument => argument.startsWith("--resume="))) {
    throw new Error("--resume does not accept a thread id; choose a thread in the TUI")
  }
  const index = args.indexOf("--resume")
  if (index < 0) return
  const next = args[index + 1]
  if (next && !next.startsWith("-")) throw new Error("--resume does not accept a thread id; choose a thread in the TUI")
}

/** 恢复入口只保留 `--resume`，避免旧别名被静默当作普通参数忽略。 */
function rejectRetiredContinueOption(args: string[]): void {
  if (args.includes("--continue") || args.includes("-c")) {
    throw new Error("--continue is not supported; use --resume to choose a thread in the TUI")
  }
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
