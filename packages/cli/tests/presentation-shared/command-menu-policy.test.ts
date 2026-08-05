/** 共享命令菜单策略测试：Slash 查询、可见项过滤与排序的确定性。 */

import { expect, test } from "bun:test"
import { filterCommandMenuItems } from "../../src/presentation-shared/command-menu-policy"
import type { CommandMenuItem } from "../../src/interactive/commands"

const COMMANDS: readonly CommandMenuItem[] = [
  { kind: "command", command: { id: "thread.resume", name: "resume", aliases: ["continue"], description: "恢复线程", deprecated: false } },
  { kind: "command", command: { id: "system.status", name: "status", aliases: [], description: "状态", deprecated: false } },
  { kind: "command", command: { id: "legacy.old", name: "old", aliases: [], description: "旧命令", deprecated: true } },
  { kind: "skill", skill: { id: "user/repo-review-demo", name: "repo-review-demo", description: "描述 user/repo-review-demo", source: "user", enabled: true, userInvocable: true } },
]

test("非 Slash 输入、转义与带空格输入都不展示菜单", () => {
  expect(filterCommandMenuItems(COMMANDS, "普通文本")).toEqual([])
  expect(filterCommandMenuItems(COMMANDS, "//resume")).toEqual([])
  expect(filterCommandMenuItems(COMMANDS, "/resume args")).toEqual([])
})

test("按名称前缀过滤命令", () => {
  const items = filterCommandMenuItems(COMMANDS, "/res")
  expect(items.map(item => item.kind === "command" ? item.command.id : null)).toEqual(["thread.resume"])
})

test("别名参与匹配", () => {
  const items = filterCommandMenuItems(COMMANDS, "/con")
  expect(items.map(item => item.kind === "command" ? item.command.id : null)).toEqual(["thread.resume"])
})

test("空查询不展示已废弃命令", () => {
  const items = filterCommandMenuItems(COMMANDS, "/")
  const ids = items.map(item => item.kind === "command" ? item.command.id : null)
  expect(ids).not.toContain("legacy.old")
  expect(ids).toContain("thread.resume")
})

test("Skill 项按 id/name/描述匹配", () => {
  const items = filterCommandMenuItems(COMMANDS, "/repo-review")
  expect(items).toHaveLength(1)
  expect(items[0]).toMatchObject({ kind: "skill" })
})

test("命令排在 Skill 之前", () => {
  const items = filterCommandMenuItems(COMMANDS, "/")
  expect(items[0]?.kind).toBe("command")
})
