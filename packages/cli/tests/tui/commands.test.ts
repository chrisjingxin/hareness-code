import { expect, test } from "bun:test"

import { parseSlashCommand } from "../../src/tui/commands"

test("解析核心 Slash Command 与别名", () => {
  expect(parseSlashCommand("/q")).toEqual({ name: "quit", argument: undefined })
  expect(parseSlashCommand("/force-clear")).toEqual({ name: "force-clear", argument: undefined })
  expect(parseSlashCommand("/skill:scaffold")).toEqual({ name: "skill", argument: "scaffold" })
})

test("普通 Agent 文本和不完整技能命令不被拦截", () => {
  expect(parseSlashCommand("帮我重构这个模块")).toBeNull()
  expect(parseSlashCommand("/skill:")).toBeNull()
})
