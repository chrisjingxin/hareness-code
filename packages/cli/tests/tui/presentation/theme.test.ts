import { expect, test } from "bun:test"

import { RGBA } from "@opentui/core"

import { markdownSyntax, tuiTheme } from "../../../src/tui/presentation/theme"

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
