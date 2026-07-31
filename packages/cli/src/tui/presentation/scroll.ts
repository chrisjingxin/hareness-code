/** 滚动加速策略，改善鼠标滚轮翻阅历史会话的体验。 */

import { MacOSScrollAccel, type ScrollAcceleration } from "@opentui/core"

/** 固定速度滚动策略，每次滚轮事件滚动固定行数。 */
export class FixedSpeedScroll implements ScrollAcceleration {
  constructor(private speed: number) {}
  tick(_now?: number): number {
    return this.speed
  }
  reset(): void {}
}

/** 默认滚轮速度：每次滚动 3 行，与 opencode 一致。 */
export const DEFAULT_SCROLL_SPEED = 3

/** 创建默认滚动加速策略实例。 */
export function createScrollAcceleration(): ScrollAcceleration {
  return new FixedSpeedScroll(DEFAULT_SCROLL_SPEED)
}

export { MacOSScrollAccel }
