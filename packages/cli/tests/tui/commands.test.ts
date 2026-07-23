import { expect, test } from "bun:test"

import {
  CommandRegistry,
  commandMenuItemDescription,
  commandMenuItemLabel,
  defaultCommandContext,
  findCommandMenuItems,
  findSlashCommands,
  parseSlashCommand,
  resolveSlashCommand,
  unknownCommandNotice,
} from "../../src/tui/commands"
import { dispatchSlashCommand } from "../../src/tui/command-dispatcher"

test("Registry 以 canonical ID 解析核心 Slash Command 与别名", () => {
  expect(parseSlashCommand("/q")).toEqual({ id: "system.quit", name: "quit", argument: undefined })
  expect(parseSlashCommand("/new")).toEqual({ id: "thread.new", name: "new", argument: undefined })
  expect(parseSlashCommand("/clear")).toEqual({ id: "thread.new", name: "new", argument: undefined })
  expect(parseSlashCommand("/force-clear")).toEqual({ id: "thread.force-clear", name: "force-clear", argument: undefined })
  expect(parseSlashCommand("/compact")).toEqual({ id: "context.compact", name: "compact", argument: undefined })
  expect(parseSlashCommand("/status")).toEqual({ id: "system.status", name: "status", argument: undefined })
  expect(parseSlashCommand("/version")).toEqual({ id: "system.version", name: "version", argument: undefined })
  expect(parseSlashCommand("/resume")).toEqual({ id: "thread.resume", name: "resume", argument: undefined })
  expect(parseSlashCommand("/continue")).toEqual({ id: "thread.resume", name: "resume", argument: undefined })
  expect(parseSlashCommand("/threads")).toEqual({ id: "thread.resume", name: "resume", argument: undefined })
  expect(parseSlashCommand("/skills")).toEqual({ id: "skills.open", name: "skills", argument: undefined })
})

test("Dispatcher 仅按稳定 ID 返回结构化结果，并统一处理兼容命令", () => {
  const base = {
    commandContext: defaultCommandContext({ capabilities: ["threads.read", "context.manage", "skills.read"], hasThread: true }),
    threadId: "thread-1",
    runtimeStatus: "运行摘要",
    versionSummary: "za38-cli 0.1.0 · JSON-RPC v2",
  }
  const clear = parseSlashCommand("/clear")
  const help = parseSlashCommand("/help")
  const resume = parseSlashCommand("/continue")
  const forceClear = parseSlashCommand("/force-clear")
  const compact = parseSlashCommand("/compact")
  if (!clear || !help || !resume || !forceClear || !compact) throw new Error("expected built-in commands")

  expect(dispatchSlashCommand(clear, base)).toEqual({ type: "local-action", action: "clear-thread" })
  expect(dispatchSlashCommand(help, base)).toMatchObject({ type: "notice", message: expect.stringContaining("/new, /clear") })
  expect(dispatchSlashCommand(resume, base)).toEqual({ type: "open-picker", picker: "threads" })
  expect(dispatchSlashCommand(forceClear, base)).toEqual({
    type: "notice",
    message: "/force-clear 已废弃，请使用 /new；当前任务执行时会先请求确认。",
  })

  const compactResult = dispatchSlashCommand(compact, base)
  if (compactResult.type !== "rpc") throw new Error("expected context.compact RPC result")
  expect(compactResult.method).toBe("context.compact")
  expect(compactResult.params).toEqual({ thread_id: "thread-1" })
  expect(compactResult.onSuccess({ compacted: true, context: { artifact_ids: ["archive-1"] } })).toEqual({
    type: "notice",
    message: "上下文已压缩，归档 1 项。",
  })
  expect(compactResult.onError(new Error("sidecar offline"))).toEqual({
    type: "notice",
    message: "上下文压缩失败：sidecar offline",
  })
})

test("活动任务下 /new 返回确认 Dialog，而不是旧的强制清理分支", () => {
  const command = parseSlashCommand("/new")
  if (!command) throw new Error("expected new command")
  const result = dispatchSlashCommand(command, {
    commandContext: defaultCommandContext({ activeRun: true }),
    runtimeStatus: "运行摘要",
    versionSummary: "version",
  })
  expect(result).toEqual({
    type: "open-dialog",
    dialog: {
      kind: "confirm-new-thread",
      title: "开始新的 Thread？",
      message: "当前任务仍在执行。确认后将先取消任务，再清空当前 Thread。",
      confirm: { type: "local-action", action: "cancel-active-run-and-clear-thread" },
    },
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

  const compactMenu = findCommandMenuItems("/compact", [], defaultCommandContext({
    capabilities: ["context.manage"],
    hasThread: false,
  }))
  expect(compactMenu).toHaveLength(1)
  const compact = compactMenu[0]
  if (!compact || compact.kind !== "command") throw new Error("expected compact command")
  expect(compact.availability).toEqual({ state: "disabled", reason: "当前没有可用 thread" })
  expect(commandMenuItemDescription(compact)).toContain("当前没有可用 thread")
})

test("已废弃命令不出现在空 Slash 菜单，但仍可按名称搜索以显示迁移说明", () => {
  expect(findSlashCommands("/").map(item => item.name)).not.toContain("force-clear")
  expect(findSlashCommands("/force").map(item => item.name)).toEqual(["force-clear"])
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
