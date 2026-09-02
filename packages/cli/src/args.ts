/** 命令行参数解析模块：把用户输入转换成稳定的内部命令描述。 */

export type Command =
  | { kind: "run"; message?: string; nonInteractive: boolean; json: boolean; cwd: string; configPath?: string; resume: boolean; sandbox?: "remote" | false }
  | { kind: "config.show" | "config.path"; cwd: string; configPath?: string; params?: Record<string, unknown> }
  | { kind: SkillCommandKind | PluginCommandKind; cwd: string; configPath?: string; params: Record<string, unknown>; secretStdin?: boolean }
  | {
      kind: "logs"
      cwd: string
      json: boolean
      flat: boolean
      limit: number
      thread?: string
      run?: string
      level?: "debug" | "info" | "warn" | "error"
      event?: string
      component?: "cli" | "agent"
      cursor?: string
    }

export type SkillCommandKind =
  | "skills.list"
  | "skills.inspect"
  | "skills.set_enabled"
  | "skills.install"
  | "skills.update"
  | "skills.remove"
  | "skills.market.list"

export type PluginCommandKind =
  | "plugins.list"
  | "plugins.inspect"
  | "plugins.validate"
  | "plugins.install"
  | "plugins.update"
  | "plugins.set_enabled"
  | "plugins.remove"
  | "plugins.settings.list"
  | "plugins.settings.set"
  | "plugins.settings.remove"

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
  if (command === "plugins") return parsePluginsCommand(args.slice(1), cwd)
  if (command === "logs") return parseLogsCommand(args.slice(1), cwd)

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

/** 解析 Plugin 本地安装、更新和 scope activation 管理命令。 */
function parsePluginsCommand(args: string[], cwd: string): Command {
  const action = args[0] ?? "list"
  const workspace = optionValue(args, "--workspace") ?? optionValue(args, "--cwd") ?? cwd
  const configPath = optionValue(args, "--config")
  if (action === "settings") return parsePluginSettingsCommand(args.slice(1), workspace, configPath)
  if (action === "list") {
    validatePluginOptions(args, action, ["--scope", "--workspace", "--cwd", "--config"], ["--enabled-only"])
    return {
      kind: "plugins.list",
      cwd: workspace,
      configPath,
      params: {
        scope: pluginScope(args) ?? "user",
        include_disabled: !hasOption(args, "--enabled-only"),
      },
    }
  }
  if (action === "inspect") {
    validatePluginOptions(args, action, ["--scope", "--workspace", "--cwd", "--config"], [])
    return {
      kind: "plugins.inspect",
      cwd: workspace,
      configPath,
      params: {
        name: pluginPositional(args, "harness plugins inspect requires a Plugin name"),
        scope: pluginScope(args) ?? "user",
      },
    }
  }
  if (action === "validate") {
    validatePluginOptions(args, action, ["--format", "--workspace", "--cwd", "--config"], [])
    return {
      kind: "plugins.validate",
      cwd: workspace,
      configPath,
      params: {
        source: pluginPositional(args, "harness plugins validate requires a directory or zip path"),
        format: pluginFormat(args),
      },
    }
  }
  if (action === "install") {
    validatePluginOptions(args, action, ["--scope", "--workspace", "--cwd", "--config"], [])
    return {
      kind: "plugins.install",
      cwd: workspace,
      configPath,
      params: {
        source: pluginPositional(args, "harness plugins install requires a directory or zip path"),
        scope: pluginScope(args) ?? "user",
      },
    }
  }
  if (action === "update") {
    validatePluginOptions(args, action, ["--source", "--workspace", "--cwd", "--config"], [])
    const source = optionValue(args, "--source")
    return {
      kind: "plugins.update",
      cwd: workspace,
      configPath,
      params: {
        name: pluginPositional(args, "harness plugins update requires a Plugin name"),
        ...(source === undefined ? {} : { source }),
      },
    }
  }
  if (action === "enable" || action === "disable") {
    validatePluginOptions(args, action, ["--scope", "--workspace", "--cwd", "--config"], [])
    return {
      kind: "plugins.set_enabled",
      cwd: workspace,
      configPath,
      params: {
        name: pluginPositional(args, `harness plugins ${action} requires a Plugin name`),
        enabled: action === "enable",
        scope: pluginScope(args) ?? "user",
      },
    }
  }
  if (action === "remove") {
    validatePluginOptions(args, action, ["--workspace", "--cwd", "--config"], ["--purge-data"])
    return {
      kind: "plugins.remove",
      cwd: workspace,
      configPath,
      params: {
        name: pluginPositional(args, "harness plugins remove requires a Plugin name"),
        purge_data: hasOption(args, "--purge-data"),
      },
    }
  }
  throw new Error("Usage: harness plugins <list|inspect|validate|install|update|enable|disable|remove|settings>")
}

/** 解析 Settings 管理命令；值只从受控 stdin/TTY 读取，不进入 argv 或解析结果。 */
function parsePluginSettingsCommand(
  args: string[],
  cwd: string,
  configPath: string | undefined,
): Command {
  const action = args[0] ?? "list"
  if (action !== "list" && action !== "set" && action !== "remove") {
    throw new Error("Usage: harness plugins settings <list|set|remove>")
  }
  validateSettingOptions(args, action)
  const scope = settingsScope(args)
  const positionals = settingPositionals(args)
  if (action === "list") {
    if (positionals.length > 1) throw new Error("harness plugins settings list accepts at most one Plugin name")
    return {
      kind: "plugins.settings.list",
      cwd,
      configPath,
      params: { scope, ...(positionals[0] === undefined ? {} : { name: positionals[0] }) },
    }
  }
  if (positionals.length < 2) {
    throw new Error(`harness plugins settings ${action} requires a Plugin name and setting name`)
  }
  if (positionals.length > 2) {
    throw new Error("Plugin Settings does not accept a value in argv; use --secret-stdin")
  }
  return {
    kind: `plugins.settings.${action}`,
    cwd,
    configPath,
    params: {
      name: positionals[0]!,
      setting: positionals[1]!,
      scope,
    },
    secretStdin: action === "set" && hasOption(args, "--secret-stdin"),
  }
}

/** 读取并校验 Settings 作用域；workspace 通过当前 --workspace/cwd 绑定。 */
function settingsScope(args: string[]): "user" | "workspace" {
  const value = optionValue(args, "--scope") ?? "user"
  if (value !== "user" && value !== "workspace") throw new Error("--scope only supports user or workspace")
  return value
}

type PluginSettingsAction = "list" | "set" | "remove"

const SETTING_COMMON_VALUE_OPTIONS = ["--scope", "--workspace", "--cwd", "--config"] as const

/** 按 Settings action 建立唯一 option 白名单，未知/重复/缺值一律在解析期失败。 */
function validateSettingOptions(args: string[], action: PluginSettingsAction): void {
  const valueOptions = new Set<string>(SETTING_COMMON_VALUE_OPTIONS)
  const flagOptions = action === "set" ? new Set(["--secret-stdin"]) : new Set<string>()
  const allowed = new Set<string>([...valueOptions, ...flagOptions])
  const seen = new Set<string>()
  for (let index = 1; index < args.length; index += 1) {
    const token = args[index]!
    if (!token.startsWith("-")) continue
    if (!allowed.has(token)) {
      throw new Error(`harness plugins settings ${action} received an unsupported option`)
    }
    if (seen.has(token)) {
      throw new Error(`${token} may only be specified once`)
    }
    seen.add(token)
    if (!valueOptions.has(token)) continue
    const value = args[index + 1]
    if (!value || value.startsWith("-")) throw new Error(`${token} requires a value`)
    index += 1
  }
  if (seen.has("--workspace") && seen.has("--cwd")) {
    throw new Error("--workspace and --cwd are mutually exclusive")
  }
}

/** 获取 Settings 命令的非选项名称字段。 */
function settingPositionals(args: string[]): string[] {
  const valueOptions = new Set<string>(SETTING_COMMON_VALUE_OPTIONS)
  const values: string[] = []
  for (let index = 1; index < args.length; index += 1) {
    const value = args[index]
    if (valueOptions.has(value!)) {
      index += 1
      continue
    }
    if (value === "--secret-stdin") continue
    if (value && !value.startsWith("-")) values.push(value)
  }
  return values
}

/** 读取指定位置的非开关参数，避免把选项值误当成资源名称。 */
function positionalValue(args: string[], message: string): string {
  const valueOptions = new Set([
    "--workspace",
    "--cwd",
    "--config",
    "--market",
    "--version",
    "--format",
    "--source",
    "--scope",
  ])
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

/** 校验 Plugin action 的公开 option 白名单，并拒绝旧的内部 identity 参数。 */
function validatePluginOptions(
  args: string[],
  action: string,
  valueNames: readonly string[],
  flagNames: readonly string[],
): void {
  const valueOptions = new Set(valueNames)
  const flags = new Set(flagNames)
  const seen = new Set<string>()
  for (let index = 1; index < args.length; index += 1) {
    const token = args[index]!
    if (!token.startsWith("-")) continue
    if (!valueOptions.has(token) && !flags.has(token)) {
      throw new Error(`harness plugins ${action} received an unsupported option`)
    }
    if (seen.has(token)) throw new Error(`${token} may only be specified once`)
    seen.add(token)
    if (!valueOptions.has(token)) continue
    const value = args[index + 1]
    if (!value || value.startsWith("-")) throw new Error(`${token} requires a value`)
    index += 1
  }
  if (seen.has("--workspace") && seen.has("--cwd")) {
    throw new Error("--workspace and --cwd are mutually exclusive")
  }
}

/** 读取并校验 Plugin scope；正常管理命令省略时统一由 action 设定为 user。 */
function pluginScope(args: string[]): "user" | "workspace" | undefined {
  const value = optionValue(args, "--scope")
  if (value === undefined) return undefined
  if (value !== "user" && value !== "workspace") throw new Error("--scope only supports user or workspace")
  return value
}

/** 读取唯一的 Plugin name/source positional，避免 option value 被误认成身份。 */
function pluginPositional(args: string[], message: string): string {
  const values = args.slice(1).filter((value, index, all) => {
    if (value.startsWith("-")) return false
    const previous = all[index - 1]
    return previous !== "--scope"
      && previous !== "--workspace"
      && previous !== "--cwd"
      && previous !== "--config"
      && previous !== "--format"
      && previous !== "--source"
  })
  if (values.length !== 1 || !values[0]) throw new Error(message)
  return values[0]
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

/** 限制 Plugin Adapter 选择，未知值在启动 sidecar 前失败。 */
function pluginFormat(args: string[]): "auto" | "agent-plugins-1.0" | "claude-code" | "qwen-code" {
  const value = optionValue(args, "--format") ?? "auto"
  if (value === "auto" || value === "agent-plugins-1.0" || value === "claude-code" || value === "qwen-code") return value
  throw new Error("--format only supports auto, agent-plugins-1.0, claude-code or qwen-code")
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

/** 解析 logs 离线查询命令；必须在 startAgent 之前短路，且不接受 --config。 */
function parseLogsCommand(args: string[], cwd: string): Command {
  // 显式拒绝 --config，logs 是纯离线不读配置
  if (hasOption(args, "--config") || hasOption(args, "-c")) {
    // 注意：-c 是 continue 的旧别名，这里对 logs 也拒绝任何 config
    throw new Error("harness logs does not accept --config")
  }
  const workspace = optionValue(args, "--cwd") ?? cwd
  const json = hasOption(args, "--json")
  const flat = hasOption(args, "--flat")
  const limitStr = optionValue(args, "--limit")
  let limit = limitStr ? Number(limitStr) : undefined
  const thread = optionValue(args, "--thread")
  const run = optionValue(args, "--run")
  const levelRaw = optionValue(args, "--level")
  const event = optionValue(args, "--event")
  const componentRaw = optionValue(args, "--component")
  const cursor = optionValue(args, "--cursor")

  if (thread && run) throw new Error("THREAD_RUN_CONFLICT: --thread and --run are mutually exclusive")
  if (flat && !thread && !run) throw new Error("--flat requires --thread or --run")
  if (flat && json) throw new Error("--flat and --json are mutually exclusive")
  if (cursor && !thread && !run) throw new Error("--cursor requires --thread or --run")
  if (cursor && !flat && !json) throw new Error("--cursor requires --flat or --json")

  if (limitStr !== undefined) {
    if (!Number.isInteger(limit) || (limit as number) < 1 || (limit as number) > 1000) {
      throw new Error("--limit must be integer 1..1000")
    }
  }
  const allowedLevels = ["debug", "info", "warn", "error"] as const
  let level: "debug" | "info" | "warn" | "error" | undefined
  if (levelRaw) {
    if (!(allowedLevels as readonly string[]).includes(levelRaw)) {
      throw new Error("--level must be one of debug|info|warn|error")
    }
    level = levelRaw as any
  }
  if (componentRaw && componentRaw !== "cli" && componentRaw !== "agent") {
    throw new Error("--component must be cli or agent")
  }
  const component = componentRaw as "cli" | "agent" | undefined

  // 默认列表 20 个 Thread；选择 Thread/Run 时每页 200 条事件。
  if (limit === undefined) {
    limit = thread || run ? 200 : 20
  }

  return {
    kind: "logs",
    cwd: workspace,
    json,
    flat,
    limit,
    thread,
    run,
    level,
    event,
    component,
    cursor,
  }
}
