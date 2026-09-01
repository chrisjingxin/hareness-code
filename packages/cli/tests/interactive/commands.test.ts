import { expect, test } from "bun:test"

import {
  CommandRegistry,
  builtinCommandDefinitions,
  commandMenuItemDescription,
  commandMenuItemLabel,
  commandRegistry,
  createCommandRegistry,
  defaultCommandContext,
  findCommandMenuItems,
  findSlashCommands,
  parseSlashCommand,
  resolveSlashCommand,
  unknownCommandNotice,
} from "../../src/interactive/commands"
import { dispatchSlashCommand } from "../../src/interactive/command-dispatcher"

test("Registry 以 canonical ID 解析核心 Slash Command 与别名", () => {
  expect(parseSlashCommand("/q")).toEqual({ id: "system.quit", name: "quit", argument: undefined })
  expect(parseSlashCommand("/new")).toEqual({ id: "thread.new", name: "new", argument: undefined })
  expect(parseSlashCommand("/clear")).toEqual({ id: "thread.new", name: "new", argument: undefined })
  expect(parseSlashCommand("/force-clear")).toBeNull()
  expect(parseSlashCommand("/compact")).toEqual({ id: "context.compact", name: "compact", argument: undefined })
  expect(parseSlashCommand("/status")).toEqual({ id: "system.status", name: "status", argument: undefined })
  expect(parseSlashCommand("/version")).toBeNull()
  expect(parseSlashCommand("/resume")).toEqual({ id: "thread.resume", name: "resume", argument: undefined })
  expect(parseSlashCommand("/continue")).toEqual({ id: "thread.resume", name: "resume", argument: undefined })
  expect(parseSlashCommand("/threads")).toEqual({ id: "thread.resume", name: "resume", argument: undefined })
  expect(parseSlashCommand("/skills")).toEqual({ id: "skills.open", name: "skills", argument: undefined })
  expect(parseSlashCommand("/agents")).toEqual({ id: "agents.list", name: "agents", argument: undefined })
  expect(parseSlashCommand("/teams list")).toEqual({ id: "teams.manage", name: "teams", argument: "list" })
  expect(parseSlashCommand("/models")).toEqual({ id: "model.select", name: "model", argument: undefined })
  expect(parseSlashCommand("/mcp")).toEqual({ id: "mcp.manage", name: "mcp", argument: undefined })
  expect(parseSlashCommand("/web")).toEqual({ id: "host.web", name: "web", argument: undefined })
  expect(parseSlashCommand("/plan")).toEqual({ id: "approval.plan", name: "plan", argument: undefined })
  expect(parseSlashCommand("/plan 给登录做个方案")).toEqual({ id: "approval.plan", name: "plan", argument: "给登录做个方案" })
  expect(parseSlashCommand("/plan exit")).toEqual({ id: "approval.plan", name: "plan", argument: "exit" })
  expect(parseSlashCommand("/plan-view")).toEqual({ id: "approval.plan-view", name: "plan-view", argument: undefined })
  expect(parseSlashCommand("/view-plan")).toBeNull()
  expect(parseSlashCommand("/show-plan")).toBeNull()
})

test("Dispatcher 仅按稳定 ID 返回 semantic operation，并统一处理兼容命令", () => {
  const base = {
    commandContext: defaultCommandContext({ capabilities: ["threads.read", "context.manage", "skills.read"], hasThread: true }),
    threadId: "thread-1",
    runtimeStatus: "运行摘要",
  }
  const clear = parseSlashCommand("/clear")
  const help = parseSlashCommand("/help")
  const resume = parseSlashCommand("/continue")
  const compact = parseSlashCommand("/compact")
  const model = parseSlashCommand("/model pro")
  const status = parseSlashCommand("/status")
  const statusWithArg = parseSlashCommand("/status extra")
  if (!clear || !help || !resume || !compact || !model || !status || !statusWithArg) throw new Error("expected built-in commands")

  expect(dispatchSlashCommand(clear, base)).toEqual({ type: "clear-thread" })
  expect(dispatchSlashCommand(help, base)).toMatchObject({ type: "notice", message: expect.stringContaining("/new, /clear") })
  expect(dispatchSlashCommand(resume, base)).toEqual({ type: "present", target: "threads" })
  expect(dispatchSlashCommand(status, base)).toEqual({ type: "present", target: "status" })
  expect(dispatchSlashCommand(statusWithArg, base)).toEqual({ type: "notice", message: "/status 不接受参数。" })
  expect(dispatchSlashCommand(model, {
    ...base,
    commandContext: defaultCommandContext({ capabilities: ["models.read"] }),
  })).toEqual({ type: "present", target: "models", initialQuery: "pro" })
  expect(dispatchSlashCommand(compact, base)).toEqual({ type: "compact", threadId: "thread-1" })
})

test("Compose /new-work 与 /abandon 按目标有无返回 submit 或确认", () => {
  const base = {
    commandContext: defaultCommandContext({ workMode: "compose", hasThread: true, hasActiveWorkItem: true }),
    threadId: "thread-1",
    runtimeStatus: "运行摘要",
  }
  const newWork = parseSlashCommand("/new-work 写 HTTP 服务")
  const emptyNewWork = parseSlashCommand("/new-work")
  const abandon = parseSlashCommand("/abandon")
  const abandonWithGoal = parseSlashCommand("/abandon 写服务")
  if (!newWork || !emptyNewWork || !abandon || !abandonWithGoal) throw new Error("expected compose commands")
  expect(dispatchSlashCommand(newWork, base)).toEqual({
    type: "submit-prompt",
    prompt: "/new-work 写 HTTP 服务",
  })
  expect(dispatchSlashCommand(emptyNewWork, base)).toMatchObject({
    type: "notice",
    message: expect.stringContaining("开新需求请带目标"),
  })
  expect(dispatchSlashCommand(abandon, base)).toMatchObject({
    type: "request-confirmation",
    confirmationId: "compose-abandon",
  })
  expect(dispatchSlashCommand(abandonWithGoal, base)).toMatchObject({
    type: "notice",
    message: expect.stringContaining("/new-work"),
  })
})

test("Agent 与 Team 命令只生成受控目录和固定参数 RPC", () => {
  const base = {
    commandContext: defaultCommandContext({
      capabilities: ["agents.read", "teams.read", "teams.manage"],
      hasThread: true,
    }),
    threadId: "thread-1",
    runtimeStatus: "运行摘要",
    idGenerator: { uuid: () => "00000000-0000-4000-8000-000000000000" },
  }
  const agents = parseSlashCommand("/agents")
  const generate = parseSlashCommand("/teams generate review lead worker-a,worker-b 2")
  const run = parseSlashCommand("/teams run review 检查当前变更")
  const status = parseSlashCommand("/teams status team-run-1")
  if (!agents || !generate || !run || !status) throw new Error("expected Agent/Team commands")

  expect(dispatchSlashCommand(agents, base)).toEqual({ type: "present", target: "agents" })
  expect(dispatchSlashCommand(generate, base)).toMatchObject({
    type: "rpc",
    method: "teams.generate",
    params: {
      id: "review",
      lead_agent_id: "lead",
      worker_agent_ids: ["worker-a", "worker-b"],
      max_parallelism: 2,
    },
  })
  const runResult = dispatchSlashCommand(run, base)
  expect(runResult).toMatchObject({
    type: "rpc",
    method: "teams.run",
    params: {
      team_id: "review",
      request: "检查当前变更",
      thread_id: "thread-1",
      run_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    },
  })
  expect(dispatchSlashCommand(status, base)).toMatchObject({
    type: "rpc",
    method: "teams.inspect",
    params: { kind: "run", id: "team-run-1" },
  })
})

test("活动任务下 /new 返回确认 semantic operation，而不是旧分支", () => {
  const command = parseSlashCommand("/new")
  if (!command) throw new Error("expected new command")
  const result = dispatchSlashCommand(command, {
    commandContext: defaultCommandContext({ activeRun: true }),
    threadId: null,
    runtimeStatus: "运行摘要",
  })
  expect(result).toEqual({
    type: "request-confirmation",
    confirmationId: "clear-thread",
    title: "开始新的 Thread？",
    message: "当前任务仍在执行。确认后将先取消任务，再清空当前 Thread。",
    confirmLabel: "取消任务并新建",
    cancelLabel: "保留当前 Thread",
  })
})

test("/web 无需 Host 能力（ZC-114 共享 Core），空闲即可用；运行中禁用", () => {
  const command = parseSlashCommand("/web")
  if (!command) throw new Error("expected web command")
  const base = {
    threadId: "thread-1" as string | null,
    runtimeStatus: "运行摘要",
  }

  // 不声明任何 Host 能力也可用：内置 Web 只消费 CLI 进程内共享 Core。
  expect(dispatchSlashCommand(command, {
    ...base,
    commandContext: defaultCommandContext({
      capabilities: [],
      hasThread: true,
    }),
  })).toEqual({ type: "request-handoff", threadId: "thread-1" })

  expect(dispatchSlashCommand(command, {
    ...base,
    threadId: null,
    commandContext: defaultCommandContext({
      capabilities: [],
      hasThread: false,
    }),
  })).toEqual({ type: "request-handoff", threadId: null })

  expect(dispatchSlashCommand(command, {
    ...base,
    commandContext: defaultCommandContext({
      capabilities: [],
      hasThread: true,
      activeRun: true,
    }),
  })).toEqual({
    type: "notice",
    message: "/web 暂不可用：当前任务结束或交互完成后可用。",
  })
})

test("命令名称大小写无关，参数保留原始引号和内部空白", () => {
  expect(resolveSlashCommand('/HELP  "保留认证  决策"  ')).toEqual({
    kind: "command",
    command: { id: "system.help", name: "help", argument: '"保留认证  决策"  ' },
  })
  expect(resolveSlashCommand("普通 Agent 文本")).toEqual({ kind: "not-command" })
})

test("未知命令不被解析为普通文本，且提供 canonical 建议", () => {
  const resolution = resolveSlashCommand("/contnue")
  expect(resolution).toMatchObject({ kind: "unknown", name: "contnue" })
  if (resolution.kind !== "unknown") throw new Error("expected unknown command")
  expect(resolution.suggestions.map(command => command.name)).toContain("resume")
  expect(unknownCommandNotice(resolution)).toContain("/resume")
  expect(parseSlashCommand("/skill project/review 检查变更")).toBeNull()
})

test("斜杠输入只展示已接入命令并按前缀过滤", () => {
  expect(findSlashCommands("/mcp").map(item => item.name)).toEqual(["mcp"])
  expect(findSlashCommands("/cl").map(item => item.name)).toEqual(["new"])
  expect(findSlashCommands("/clear now")).toEqual([])
})

test("双斜杠转义会将以 / 开头的文本交给 Agent", () => {
  expect(resolveSlashCommand("//api/users 的路由在哪里")).toEqual({
    kind: "escaped",
    message: "/api/users 的路由在哪里",
  })
})

test("Registry 拒绝重复的命令名称与别名", () => {
  expect(() => new CommandRegistry([
    { id: "one", name: "first", description: "first", source: { type: "builtin" }, presentation: "action" },
    { id: "two", name: "second", aliases: ["FIRST"], description: "second", source: { type: "builtin" }, presentation: "action" },
  ])).toThrow("Command 名称或别名冲突")
})

test("菜单按 capability 隐藏命令，并以稳定原因展示运行态禁用项", () => {
  const withoutCapabilities = defaultCommandContext({ capabilities: [] })
  expect(findSlashCommands("/", withoutCapabilities).map(item => item.name)).not.toContain("compact")
  expect(findSlashCommands("/", withoutCapabilities).map(item => item.name)).not.toContain("resume")
  expect(findSlashCommands("/", withoutCapabilities).map(item => item.name)).not.toContain("skills")
  expect(findSlashCommands("/", withoutCapabilities).map(item => item.name)).not.toContain("model")

  const compactMenu = findCommandMenuItems("/compact", [], defaultCommandContext({
    capabilities: ["context.manage"],
    hasThread: false,
  }))
  expect(compactMenu).toHaveLength(1)
  const compact = compactMenu[0]
  if (!compact || compact.kind !== "command") throw new Error("expected compact command")
  expect(compact.availability).toEqual({ state: "disabled", reason: "当前没有可用 thread" })
  expect(commandMenuItemDescription(compact)).toContain("当前没有可用 thread")

  const modelMenu = findCommandMenuItems("/model", [], defaultCommandContext({
    capabilities: ["models.read"],
    activeRun: true,
  }))
  expect(modelMenu).toHaveLength(1)
  const model = modelMenu[0]
  if (!model || model.kind !== "command") throw new Error("expected model command")
  expect(model.availability).toEqual({ state: "disabled", reason: "当前任务结束或交互完成后可用" })
})

test("已废弃命令不出现在空 Slash 菜单，但仍可按名称搜索以显示迁移说明", () => {
  const customRegistry = new CommandRegistry([
    ...builtinCommandDefinitions,
    {
      id: "test.legacy",
      name: "legacy-op",
      description: "已废弃操作",
      source: { type: "builtin" },
      presentation: "action",
      deprecated: { replacement: "/new" },
    },
  ])
  expect(findSlashCommands("/", defaultCommandContext(), customRegistry).map(item => item.name)).not.toContain("legacy-op")
  expect(findSlashCommands("/legacy", defaultCommandContext(), customRegistry).map(item => item.name)).toEqual(["legacy-op"])
})

test("Slash 菜单将可调用 Skill 渲染为 skill:<canonical-id>", () => {
  const skills = [{
    id: "user/repo-review-demo",
    name: "repo-review-demo",
    description: "只读审查",
    source: "user",
    enabled: true,
    userInvocable: true,
  }]
  expect(findCommandMenuItems("/", skills).map(commandMenuItemLabel)).toContain("/skill:user/repo-review-demo")
  expect(findCommandMenuItems("/skill:repo", skills).map(commandMenuItemLabel)).toEqual(["/skill:user/repo-review-demo"])
  expect(findCommandMenuItems("/skill:", [{ ...skills[0]!, enabled: false }])).toEqual([])
})

test("Host Plugin Command 合并进同一 Registry，并提交 requested Skill 与原始参数", () => {
  const registry = createCommandRegistry([{
    id: "plugin/local/review-tools/command/audit",
    name: "plugin:local:review-tools:audit",
    description: "审计指定文件",
    argument_hint: "<paths>",
    requested_skill_id: "plugin/local/review-tools/command/audit",
    plugin_id: "local/review-tools",
  }])
  const command = parseSlashCommand(
    "/plugin:local:review-tools:audit src/auth.ts  --strict",
    registry,
  )
  if (!command) throw new Error("expected Plugin command")
  expect(findCommandMenuItems(
    "/plugin:local",
    [],
    defaultCommandContext({ capabilities: ["skills.read"] }),
    registry,
  ).map(commandMenuItemLabel)).toEqual(["/plugin:local:review-tools:audit"])
  expect(dispatchSlashCommand(command, {
    commandContext: defaultCommandContext({ capabilities: ["skills.read"] }),
    runtimeStatus: "status",
  }, registry)).toEqual({
    type: "submit-prompt",
    prompt: "/plugin:local:review-tools:audit src/auth.ts  --strict",
    requestedSkill: {
      id: "plugin/local/review-tools/command/audit",
      args: "src/auth.ts  --strict",
      raw_invocation: "/plugin:local:review-tools:audit src/auth.ts  --strict",
      command_name: "plugin:local:review-tools:audit",
    },
  })
  const help = parseSlashCommand("/help", registry)
  if (!help) throw new Error("expected help")
  expect(dispatchSlashCommand(help, {
    commandContext: defaultCommandContext({ capabilities: ["skills.read"] }),
    runtimeStatus: "status",
  }, registry)).toMatchObject({
    type: "notice",
    message: expect.stringContaining("/plugin:local:review-tools:audit"),
  })
})

test("Plugin Slash Command 保留精确 raw invocation，并单独规范化 args", () => {
  const registry = createCommandRegistry([{
    id: "plugin/local/ZA38/command/za38-sdd",
    name: "za38-sdd",
    description: "SDD",
    argument_hint: "<goal>",
    requested_skill_id: "plugin/local/ZA38/command/za38-sdd",
    plugin_id: "local/ZA38",
  }])
  const rawInvocation = "/ZA38-SDD   创建登录功能  "
  const command = resolveSlashCommand(rawInvocation, registry)
  expect(command).toMatchObject({
    kind: "command",
    command: {
      id: "plugin/local/ZA38/command/za38-sdd",
      name: "za38-sdd",
      rawInvocation,
      argument: "创建登录功能  ",
    },
  })
  if (command.kind !== "command") throw new Error("expected Plugin command")
  expect(dispatchSlashCommand(command.command, {
    commandContext: defaultCommandContext({ capabilities: ["skills.read"] }),
    runtimeStatus: "status",
  }, registry)).toEqual({
    type: "submit-prompt",
    prompt: rawInvocation,
    requestedSkill: {
      id: "plugin/local/ZA38/command/za38-sdd",
      args: "创建登录功能",
      raw_invocation: rawInvocation,
      command_name: "za38-sdd",
    },
  })
})

test("Qwen 自然命令与内置冲突时稳定回退为 extension.command", () => {
  const registry = createCommandRegistry([{
    id: "plugin/local/bad/command/help",
    name: "help",
    description: "bad",
    argument_hint: null,
    requested_skill_id: "plugin/local/bad/command/help",
    plugin_id: "local/bad",
  }])
  expect(registry.get("plugin/local/bad/command/help")?.name).toBe("bad.help")
  expect(parseSlashCommand("/bad.help", registry)).toMatchObject({
    id: "plugin/local/bad/command/help",
    name: "bad.help",
  })
  expect(parseSlashCommand("/help", registry)).toMatchObject({
    id: "system.help",
    name: "help",
  })
})

test("未来 builtin/alias 变化由同一 CLI Registry 解析，Host 不复制当前 builtin 表", () => {
  const plugin = {
    id: "plugin/local/future/command/preview",
    name: "preview",
    description: "future collision",
    argument_hint: null,
    requested_skill_id: "plugin/local/future/command/preview",
    plugin_id: "local/future",
  }
  const futureBuiltin = {
    id: "future.preview",
    name: "preview",
    aliases: ["pre"],
    description: "future builtin",
    source: { type: "builtin" as const },
    presentation: "viewer" as const,
  }
  const registry = createCommandRegistry(
    [plugin],
    [...builtinCommandDefinitions, futureBuiltin],
  )
  const command = parseSlashCommand("/future.preview   args  ", registry)
  if (!command) throw new Error("expected future collision command")
  expect(command).toMatchObject({
    id: plugin.id,
    name: "future.preview",
  })
  expect(dispatchSlashCommand(command, {
    commandContext: defaultCommandContext({ capabilities: ["skills.read"] }),
    runtimeStatus: "status",
    versionSummary: "version",
  }, registry)).toMatchObject({
    type: "submit-prompt",
    requestedSkill: {
      id: plugin.id,
      command_name: "future.preview",
      args: "args",
    },
  })
})

test("Qwen 三个自然命令、嵌套命令和冲突回退不依赖加载顺序", () => {
  const commands = [
    {
      id: "plugin/local/ZA38.03_CLI_EXTENSION/command/za38-sdd",
      name: "za38-sdd",
      description: "SDD",
      argument_hint: "<goal>",
      requested_skill_id: "plugin/local/ZA38.03_CLI_EXTENSION/command/za38-sdd",
      plugin_id: "local/ZA38.03_CLI_EXTENSION",
    },
    {
      id: "plugin/local/alpha/command/help",
      name: "help",
      description: "alpha help",
      argument_hint: null,
      requested_skill_id: "plugin/local/alpha/command/help",
      plugin_id: "local/alpha",
    },
    {
      id: "plugin/local/tools/command/check",
      name: "tools:check",
      description: "nested",
      argument_hint: null,
      requested_skill_id: "plugin/local/tools/command/check",
      plugin_id: "local/tools",
    },
  ] as const
  const first = createCommandRegistry(commands)
  const second = createCommandRegistry([...commands].reverse())
  expect(first.definitions.map(item => [item.id, item.name])).toEqual(
    second.definitions.map(item => [item.id, item.name]),
  )
  expect(findSlashCommands("/za38", defaultCommandContext({ capabilities: ["skills.read"] }), first)
    .map(item => item.name)).toEqual(["za38-sdd"])
  expect(parseSlashCommand("/tools:check", first)?.id).toBe("plugin/local/tools/command/check")
  expect(first.get("plugin/local/alpha/command/help")?.name).toBe("alpha.help")
})

test("Compose 命令按 Work Mode 与 Work Item 状态决定可见性与禁用原因", () => {
  const newWork = commandRegistry.get("compose.new-work")!
  const abandon = commandRegistry.get("compose.abandon")!
  const btw = commandRegistry.get("assist.btw")!

  expect(commandRegistry.availability(newWork, defaultCommandContext({ workMode: "build" }))).toEqual({
    state: "hidden",
    reason: "当前模式不可用（COMMAND_MODE_UNAVAILABLE）",
  })
  expect(commandRegistry.availability(newWork, defaultCommandContext({ workMode: "compose", hasThread: true }))).toEqual({ state: "available" })

  expect(commandRegistry.availability(abandon, defaultCommandContext({ workMode: "build", hasActiveWorkItem: true, hasThread: true }))).toEqual({
    state: "hidden",
    reason: "当前模式不可用（COMMAND_MODE_UNAVAILABLE）",
  })
  expect(commandRegistry.availability(abandon, defaultCommandContext({ workMode: "compose", hasActiveWorkItem: false, hasThread: true }))).toEqual({
    state: "disabled",
    reason: "当前没有进行中的 Compose 需求",
  })
  expect(commandRegistry.availability(abandon, defaultCommandContext({ workMode: "compose", hasActiveWorkItem: true, hasThread: true }))).toEqual({ state: "available" })

  // btw：Build/Compose 双模式可用。
  expect(commandRegistry.availability(btw, defaultCommandContext({ workMode: "build" }))).toEqual({ state: "available" })
  expect(commandRegistry.availability(btw, defaultCommandContext({ workMode: "compose" }))).toEqual({ state: "available" })
})

test("Compose-only 命令不出现在 Build 模式 Slash 菜单，Compose 模式下可见", () => {
  const buildContext = defaultCommandContext({ workMode: "build" })
  const composeContext = defaultCommandContext({ workMode: "compose" })
  expect(findSlashCommands("/new-work", buildContext).map(item => item.name)).toEqual([])
  expect(findSlashCommands("/abandon", buildContext).map(item => item.name)).toEqual([])
  expect(findSlashCommands("/new-work", composeContext).map(item => item.name)).toEqual(["new-work"])
  expect(findSlashCommands("/abandon", composeContext).map(item => item.name)).toEqual(["abandon"])
  expect(findSlashCommands("/btw", buildContext).map(item => item.name)).toEqual(["btw"])
  expect(findSlashCommands("/btw", composeContext).map(item => item.name)).toEqual(["btw"])
})

test("/plan 仅 Build 可见，Compose 手输给出仅 Build 提示", () => {
  const plan = commandRegistry.get("approval.plan")!
  expect(plan).toMatchObject({
    id: "approval.plan",
    name: "plan",
    presentation: "action",
    argumentHint: "[exit | <目标>]",
  })
  expect(plan.requirements?.workModes).toEqual(["build"])
  expect(plan.requirements?.requiresIdle).toBeUndefined()

  expect(commandRegistry.availability(plan, defaultCommandContext({ workMode: "build" }))).toEqual({ state: "available" })
  expect(commandRegistry.availability(plan, defaultCommandContext({ workMode: "build", activeRun: true }))).toEqual({ state: "available" })
  expect(commandRegistry.availability(plan, defaultCommandContext({ workMode: "compose" }))).toMatchObject({
    state: "hidden",
    reason: "`/plan` 仅在 Build 工作模式可用。",
  })

  expect(findSlashCommands("/plan", defaultCommandContext({ workMode: "build" })).map(item => item.name)).toEqual(["plan", "plan-view"])
  expect(findSlashCommands("/plan", defaultCommandContext({ workMode: "compose" })).map(item => item.name)).toEqual([])

  const composeDispatch = {
    commandContext: defaultCommandContext({ workMode: "compose" }),
    threadId: "thread-1",
    runtimeStatus: "idle",
    approvalMode: "default" as const,
  }
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan" }, composeDispatch)).toEqual({
    type: "notice",
    message: "`/plan` 仅在 Build 工作模式可用。",
  })
})

test("/plan 按空参、exit、目标返回切档或提交", () => {
  const base = {
    commandContext: defaultCommandContext({ workMode: "build" }),
    threadId: "thread-1",
    runtimeStatus: "idle",
    approvalMode: "yolo" as const,
  }

  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan" }, base)).toMatchObject({
    type: "set-approval-mode",
    mode: "plan",
  })
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan", argument: "exit" }, base)).toMatchObject({
    type: "notice",
    message: expect.stringContaining("当前不在计划模式"),
  })
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan", argument: "给登录做个方案" }, base)).toEqual({
    type: "set-approval-mode",
    mode: "plan",
    prompt: "给登录做个方案",
  })
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan", argument: "exit the login" }, base)).toEqual({
    type: "set-approval-mode",
    mode: "plan",
    prompt: "exit the login",
  })

  const alreadyInPlan = { ...base, approvalMode: "plan" as const }
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan" }, alreadyInPlan)).toMatchObject({
    type: "notice",
    message: expect.stringContaining("已在计划模式"),
  })
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan", argument: "exit" }, alreadyInPlan)).toEqual({
    type: "restore-approval-mode",
  })
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan", argument: "EXIT" }, alreadyInPlan)).toEqual({
    type: "restore-approval-mode",
  })
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan", argument: "继续改方案" }, alreadyInPlan)).toEqual({
    type: "submit-prompt",
    prompt: "继续改方案",
  })

  const runningInPlan = {
    ...alreadyInPlan,
    commandContext: defaultCommandContext({ workMode: "build", activeRun: true }),
  }
  expect(dispatchSlashCommand({ id: "approval.plan", name: "plan", argument: "悄悄提交" }, runningInPlan)).toMatchObject({
    type: "notice",
    message: expect.stringContaining("已在计划模式"),
  })
})

test("/plan-view 仅 Build 当前 thread 可用；挂起审批复用原交互，否则读取计划", () => {
  const definition = commandRegistry.get("approval.plan-view")!
  expect(definition).toMatchObject({
    id: "approval.plan-view",
    name: "plan-view",
    requirements: { workModes: ["build"], requiresThread: true },
  })
  expect(definition.aliases).toBeUndefined()
  expect(commandRegistry.availability(definition, defaultCommandContext({ workMode: "compose", hasThread: true }))).toMatchObject({ state: "hidden" })
  expect(commandRegistry.availability(definition, defaultCommandContext({ workMode: "build", hasThread: false }))).toMatchObject({ state: "disabled" })

  const base = {
    commandContext: defaultCommandContext({ workMode: "build", hasThread: true }),
    threadId: "thread-1",
    runtimeStatus: "idle",
    approvalMode: "default" as const,
  }
  expect(dispatchSlashCommand({ id: "approval.plan-view", name: "plan-view" }, {
    ...base,
    pendingPlanInteraction: true,
  })).toEqual({ type: "focus-plan" })

  const result = dispatchSlashCommand({ id: "approval.plan-view", name: "plan-view" }, base)
  expect(result).toMatchObject({ type: "rpc", method: "threads.open", params: { thread_id: "thread-1" } })
  if (result.type !== "rpc") throw new Error("expected threads.open rpc")
  expect(result.onSuccess({
    thread: { thread_id: "thread-1" },
    messages: [],
    plan: {
      has_plan: true,
      plan_markdown: "# 方案",
      plan_virtual_path: "/.harness/plan.md",
      plan_display_path: "~/.harness/plans/thread-1.md",
    },
  })).toEqual({
    type: "view-plan",
    threadId: "thread-1",
    markdown: "# 方案",
    virtualPath: "/.harness/plan.md",
    displayPath: "~/.harness/plans/thread-1.md",
  })
  expect(result.onSuccess({ thread: { thread_id: "thread-1" }, messages: [], plan: { has_plan: false, plan_markdown: "", plan_virtual_path: "/.harness/plan.md", plan_display_path: "~/.harness/plans/thread-1.md" } })).toEqual({
    type: "notice",
    message: "还没有计划。",
  })
})

test("/btw 命令分发：无参返回用法提示，有参返回 side-question 语义结构", () => {
  const base = {
    commandContext: defaultCommandContext({ workMode: "build" }),
    threadId: "thread-btw-1",
    runtimeStatus: "idle",
    idGenerator: { generate: () => "id-1" },
  }

  const btwEmpty = { id: "assist.btw", name: "btw" }
  expect(dispatchSlashCommand(btwEmpty, base)).toEqual({
    type: "notice",
    message: "用法：/btw <你的问题>",
  })

  const btwWhitespace = { id: "assist.btw", name: "btw", argument: "   " }
  expect(dispatchSlashCommand(btwWhitespace, base)).toEqual({
    type: "notice",
    message: "用法：/btw <你的问题>",
  })

  const btwWithQuestion = { id: "assist.btw", name: "btw", argument: "什么是 AST 抽象语法树？" }
  expect(dispatchSlashCommand(btwWithQuestion, base)).toEqual({
    type: "side-question",
    question: "什么是 AST 抽象语法树？",
    threadId: "thread-btw-1",
  })

  const composeBase = {
    commandContext: defaultCommandContext({ workMode: "compose" }),
    threadId: "thread-compose-1",
    runtimeStatus: "idle",
    idGenerator: { generate: () => "id-1" },
  }
  expect(dispatchSlashCommand(btwWithQuestion, composeBase)).toEqual({
    type: "side-question",
    question: "什么是 AST 抽象语法树？",
    threadId: "thread-compose-1",
  })
})

test("/undo 与 /redo 的命令解析与可用性状态机", () => {
  expect(parseSlashCommand("/undo")).toEqual({ id: "thread.undo", name: "undo", argument: undefined })
  expect(parseSlashCommand("/rewind")).toEqual({ id: "thread.undo", name: "undo", argument: undefined })
  expect(parseSlashCommand("/rollback")).toEqual({ id: "thread.undo", name: "undo", argument: undefined })
  expect(parseSlashCommand("/redo")).toEqual({ id: "thread.redo", name: "redo", argument: undefined })

  const normalContext = defaultCommandContext({
    capabilities: ["threads.read", "context.manage"],
    hasThread: true,
    isReverted: false,
  })
  const revertedContext = defaultCommandContext({
    capabilities: ["threads.read", "context.manage"],
    hasThread: true,
    isReverted: true,
  })
  const noThreadContext = defaultCommandContext({
    capabilities: ["threads.read", "context.manage"],
    hasThread: false,
  })

  const undoDef = commandRegistry.get("thread.undo")
  const redoDef = commandRegistry.get("thread.redo")
  if (!undoDef || !redoDef) throw new Error("expected thread.undo and thread.redo definitions")

  // /undo 在有 thread 时可用，无 thread 时 disabled
  expect(commandRegistry.availability(undoDef, normalContext)).toEqual({ state: "available" })
  expect(commandRegistry.availability(undoDef, noThreadContext)).toEqual({ state: "disabled", reason: "当前没有可用 thread" })

  // /redo 在非暂存态时 disabled，暂存态时 available
  expect(commandRegistry.availability(redoDef, normalContext)).toEqual({ state: "disabled", reason: "当前没有可重做的撤销操作" })
  expect(commandRegistry.availability(redoDef, revertedContext)).toEqual({ state: "available" })
})

test("/undo 与 /redo 的 dispatch 映射", () => {
  const base: CommandDispatchContext = {
    commandContext: defaultCommandContext({ hasThread: true, isReverted: true }),
    threadId: "thread-1",
    runtimeStatus: "normal",
    idGenerator: { uuid: () => "id-1" },
  }
  expect(dispatchSlashCommand({ id: "thread.undo", name: "undo" }, base)).toEqual({
    type: "present",
    target: "undo",
  })
  expect(dispatchSlashCommand({ id: "thread.redo", name: "redo" }, base)).toEqual({
    type: "request-redo",
    threadId: "thread-1",
  })
})

