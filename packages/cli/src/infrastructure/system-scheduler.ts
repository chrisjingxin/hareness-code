/** SystemScheduler 基础设施：基于 JavaScript 原生 setTimeout 实现 Scheduler 接口。 */

import type { Scheduler } from "../interactive/ports/scheduler"

export class SystemScheduler implements Scheduler {
  setTimeout(callback: () => void, ms: number): () => void {
    const timer = setTimeout(callback, ms)
    return () => clearTimeout(timer)
  }
}

export const systemScheduler = new SystemScheduler()
