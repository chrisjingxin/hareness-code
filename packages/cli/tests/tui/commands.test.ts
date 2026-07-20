import { expect, test } from "bun:test"

import { commandMenuItemLabel, findCommandMenuItems, findSlashCommands, parseSlashCommand } from "../../src/tui/commands"

test("解析核心 Slash Command 与别名", () => {
  expect(parseSlashCommand("/q")).toEqual({ name: "quit", argument: undefined })
  expect(parseSlashCommand("/force-clear")).toEqual({ name: "force-clear", argument: undefined })
  expect(parseSlashCommand("/status")).toEqual({ name: "status", argument: undefined })
  expect(parseSlashCommand("/version")).toEqual({ name: "version", argument: undefined })
  expect(parseSlashCommand("/resume")).toEqual({ name: "resume", argument: undefined })
  expect(parseSlashCommand("/continue")).toEqual({ name: "continue", argument: undefined })
  expect(parseSlashCommand("/skills")).toEqual({ name: "skills", argument: undefined })
})

test("普通 Agent 文本和未接入命令不被本地界面拦截", () => {
  expect(parseSlashCommand("帮我重构这个模块")).toBeNull()
  expect(parseSlashCommand("/skill project/review 检查变更")).toBeNull()
  expect(parseSlashCommand("/skill:scaffold")).toBeNull()
  expect(parseSlashCommand("/skill:")).toBeNull()
})

test("斜杠输入只展示已接入命令并按前缀过滤", () => {
  expect(findSlashCommands("/").map(item => item.name)).toEqual(["help", "quit", "clear", "force-clear", "status", "version", "resume", "skills"])
  expect(findSlashCommands("/cl").map(item => item.name)).toEqual(["clear"])
  expect(findSlashCommands("/mcp")).toEqual([])
  expect(findSlashCommands("/clear now")).toEqual([])
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
