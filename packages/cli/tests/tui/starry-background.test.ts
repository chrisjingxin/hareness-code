import { expect, test } from "bun:test"

import {
  brailleBit,
  createMeteorCells,
  createStarField,
  METEOR_DURATION_MS,
  twinkleStarField,
} from "../../src/tui/starry-background"

test("星场按给定随机源生成固定拓扑，闪烁只影响已有星点", () => {
  const field = createStarField(20, 10, () => 0)
  expect(field.grid.flat().filter(char => char !== " ")).toHaveLength(200)
  expect(field.brightness[0]?.[0]).toBe(0.15)

  const twinkled = twinkleStarField(field, () => 0)
  expect(twinkled.grid).toBe(field.grid)
  expect(twinkled.brightness).not.toBe(field.brightness)
  expect(twinkled.brightness[0]?.[0]).toBe(0.92)
})

test("Braille 位映射符合 2×4 子像素布局", () => {
  expect([0, 1, 2, 6, 3, 4, 5, 7]).toEqual([
    brailleBit(0, 0), brailleBit(0, 1), brailleBit(0, 2), brailleBit(0, 3),
    brailleBit(1, 0), brailleBit(1, 1), brailleBit(1, 2), brailleBit(1, 3),
  ])
})

test("流星生成 Braille 头尾，超出生命周期或边界时安全裁剪", () => {
  const field = createStarField(40, 16, () => 1)
  const meteor = { at: 100, startX: 38, startY: 0, speed: 0.02 }
  const cells = createMeteorCells(field, meteor, 900)
  expect(cells.size).toBeGreaterThan(1)
  expect([...cells.values()].some(cell => cell.minT === 0)).toBeTrue()
  expect([...cells.values()].some(cell => cell.dots > 0)).toBeTrue()
  expect(createMeteorCells(field, meteor, 100 + METEOR_DURATION_MS + 1).size).toBe(0)
})
