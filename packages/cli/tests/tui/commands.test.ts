import { expect, test } from "bun:test"

import { findSlashCommands, parseSlashCommand } from "../../src/tui/commands"

test("解析核心 Slash Command 与别名", () => {
  expect(parseSlashCommand("/q")).toEqual({ name: "quit", argument: undefined })
  expect(parseSlashCommand("/force-clear")).toEqual({ name: "force-clear", argument: undefined })
  expect(parseSlashCommand("/version")).toEqual({ name: "version", argument: undefined })
})

test("普通 Agent 文本和未接入命令不被本地界面拦截", () => {
  expect(parseSlashCommand("帮我重构这个模块")).toBeNull()
  expect(parseSlashCommand("/skill:scaffold")).toBeNull()
  expect(parseSlashCommand("/skill:")).toBeNull()
})

test("斜杠输入只展示已接入命令并按前缀过滤", () => {
  expect(findSlashCommands("/").map(item => item.name)).toEqual(["help", "quit", "clear", "force-clear", "version"])
  expect(findSlashCommands("/cl").map(item => item.name)).toEqual(["clear"])
  expect(findSlashCommands("/mcp")).toEqual([])
  expect(findSlashCommands("/clear now")).toEqual([])
})
