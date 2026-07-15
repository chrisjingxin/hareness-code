import { expect, test } from "bun:test"
import { RGBA } from "@opentui/core"

import { blendRgba } from "../../src/tui/colors"

test("归一化 RGBA 插值保留可见亮度，避免动画颜色退化为近黑色", () => {
  const start = RGBA.fromHex("#456da8")
  const end = RGBA.fromHex("#e5f1ff")

  expect(blendRgba(start, end, 0).toInts()).toEqual([69, 109, 168, 255])
  expect(blendRgba(start, end, 1).toInts()).toEqual([229, 241, 255, 255])
  expect(blendRgba(start, end, 0.5).toInts()).toEqual([149, 175, 211, 255])
})
