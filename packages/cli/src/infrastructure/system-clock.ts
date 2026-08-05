/** SystemClock 基础设施：基于系统原生的 Date.now() 实现 Clock 接口。 */

import type { Clock } from "../interactive/ports/clock"

export class SystemClock implements Clock {
  now(): number {
    return Date.now()
  }

  duration(startMs: number, endMs: number): number {
    return Math.max(0, endMs - startMs)
  }
}

export const systemClock = new SystemClock()
