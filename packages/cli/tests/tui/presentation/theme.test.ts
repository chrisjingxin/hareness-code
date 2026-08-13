import { expect, test } from "bun:test"

import { RGBA } from "@opentui/core"

import { markdownSyntax, modeAccent, tuiTheme, userMessageAccent } from "../../../src/tui/presentation/theme"

test("Mode 与 Semantic token 使用 HC-145 色表字面量，蓝主强调不再代表 Mode", () => {
  expect(tuiTheme.modeBuild).toBe("#EAB308")
  expect(tuiTheme.modeCompose).toBe("#A9A5D4")
  expect(tuiTheme.background).toBe("#0B0C0E")
  expect(tuiTheme.surface).toBe("#15171A")
  expect(tuiTheme.surfaceElevated).toBe("#1B1D21")
  expect(tuiTheme.border).toBe("#2A2D33")
  expect(tuiTheme.text).toBe("#E8E9EC")
  expect(tuiTheme.muted).toBe("#A0A4AE")
  expect(tuiTheme.subtle).toBe("#676C76")
  expect(tuiTheme.success).toBe("#7FA37A")
  expect(tuiTheme.danger).toBe("#C56F6F")
  expect(tuiTheme.warning).toBe("#C88758")
  expect(tuiTheme.diffAdd).toBe("#6F9A72")
  expect(tuiTheme.diffRemove).toBe("#B96A6A")
  expect(modeAccent("build")).toBe("#EAB308")
  expect(modeAccent("compose")).toBe("#A9A5D4")
  expect(modeAccent("build")).not.toBe(tuiTheme.primary)
  expect(modeAccent("compose")).not.toBe(tuiTheme.primary)
  expect(tuiTheme.thinking).toBe("#7EB6C9")
  expect(tuiTheme.thinking).not.toBe(tuiTheme.modeBuild)
})

test("用户消息强调色只看该条 workMode，缺字段按 build，不读当前会话 Mode", () => {
  expect(userMessageAccent("build")).toBe("#EAB308")
  expect(userMessageAccent("compose")).toBe("#A9A5D4")
  expect(userMessageAccent(undefined)).toBe("#EAB308")
})

test("Markdown 和代码高亮注册 OpenTUI 的真实 scope", () => {
  const names = markdownSyntax.getRegisteredNames()
  expect(names).toContain("markup.heading")
  expect(names).toContain("markup.raw.block")
  expect(names).toContain("keyword")
  expect(names).toContain("function")
  expect(names).toContain("string")
  expect(names).toContain("tag")
  expect(names).toContain("attribute")
})

test("Diff 增删背景与普通代码面保持可辨识色差", () => {
  const surface = RGBA.fromHex(tuiTheme.toolSurface).toInts()
  const added = RGBA.fromHex(tuiTheme.diffAddedBackground).toInts()
  const removed = RGBA.fromHex(tuiTheme.diffRemovedBackground).toInts()
  const colorDistance = (left: number[], right: number[]) =>
    Math.abs(left[0]! - right[0]!) + Math.abs(left[1]! - right[1]!) + Math.abs(left[2]! - right[2]!)

  expect(colorDistance(added, surface)).toBeGreaterThanOrEqual(64)
  expect(colorDistance(removed, surface)).toBeGreaterThanOrEqual(64)
  expect(added[1]).toBeGreaterThan(added[0]!)
  expect(removed[0]).toBeGreaterThan(removed[1]!)
})
