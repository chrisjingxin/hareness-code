import { describe, expect, it } from "bun:test"
import { FixedSpeedScroll, DEFAULT_SCROLL_SPEED, createScrollAcceleration } from "../../src/tui/scroll.js"

describe("FixedSpeedScroll", () => {
  it("tick 返回构造时传入的固定速度", () => {
    const scroll = new FixedSpeedScroll(5)
    expect(scroll.tick()).toBe(5)
    expect(scroll.tick(Date.now())).toBe(5)
  })

  it("reset 为空操作，不影响后续 tick", () => {
    const scroll = new FixedSpeedScroll(3)
    scroll.reset()
    expect(scroll.tick()).toBe(3)
  })
})

describe("createScrollAcceleration", () => {
  it("返回 FixedSpeedScroll 实例且默认速度为 3", () => {
    const accel = createScrollAcceleration()
    expect(accel).toBeInstanceOf(FixedSpeedScroll)
    expect(accel.tick()).toBe(DEFAULT_SCROLL_SPEED)
    expect(DEFAULT_SCROLL_SPEED).toBe(3)
  })
})
