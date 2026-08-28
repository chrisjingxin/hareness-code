/** 命令行参数解析模块：把用户输入转换成稳定的内部命令描述。 */

export type Command =
  | { kind: "run"; message?: string; nonInteractive: boolean; json: boolean; cwd: string; configPath?: string; resume: boolean; sandbox?: "remote" | false }
  | { kind: "config.show" | "config.path"; cwd: string; configPath?: string; params?: Record<string, unknown> }
  | { kind: SkillCommandKind | PluginCommandKind; cwd: string; configPath?: string; params: Record<string, unknown> }
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
  | "plugins.set_enabled"
  | "plugins.remove"

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

/** 解析 Plugin 本地安装和 trust 管理命令。 */
function parsePluginsCommand(args: string[], cwd: string): Command {
  const action = args[0] ?? "list"
  const workspace = optionValue(args, "--workspace") ?? optionValue(args, "--cwd") ?? cwd
  const configPath = optionValue(args, "--config")
  if (action === "list") {
    return {
      kind: "plugins.list",
      cwd: workspace,
      configPath,
      params: { include_disabled: !hasOption(args, "--enabled-only") },
    }
  }
  if (action === "inspect") {
    return {
      kind: "plugins.inspect",
      cwd: workspace,
      configPath,
      params: { id: positionalValue(args, "harness plugins inspect requires a Plugin id") },
    }
  }
  if (action === "validate" || action === "install") {
    return {
      kind: action === "validate" ? "plugins.validate" : "plugins.install",
      cwd: workspace,
      configPath,
      params: {
        source: positionalValue(args, `harness plugins ${action} requires a directory or zip path`),
        format: pluginFormat(args),
      },
    }
  }
  if (action === "enable" || action === "disable") {
    const fingerprint = optionValue(args, "--capability-fingerprint")
    if (action === "enable" && !fingerprint) {
      throw new Error("harness plugins enable requires --capability-fingerprint SHA256")
    }
    return {
      kind: "plugins.set_enabled",
      cwd: workspace,
      configPath,
      params: {
        id: positionalValue(args, `harness plugins ${action} requires a Plugin id`),
        enabled: action === "enable",
        capability_fingerprint: fingerprint,
      },
    }
  }
  if (action === "remove") {
    return {
      kind: "plugins.remove",
      cwd: workspace,
      configPath,
      params: {
        id: positionalValue(args, "harness plugins remove requires a Plugin id"),
        purge_data: hasOption(args, "--purge-data"),
      },
    }
  }
  throw new Error("Usage: harness plugins <list|inspect|validate|install|enable|disable|remove>")
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
    "--capability-fingerprint",
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
function pluginFormat(args: string[]): "auto" | "agent-plugins-1.0" | "claude-code" {
  const value = optionValue(args, "--format") ?? "auto"
  if (value === "auto" || value === "agent-plugins-1.0" || value === "claude-code") return value
  throw new Error("--format only supports auto, agent-plugins-1.0 or claude-code")
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
