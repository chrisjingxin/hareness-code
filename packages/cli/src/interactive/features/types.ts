/** Feature 上下文与共享契约；Feature 之间禁止互相依赖，只依赖 Context 与 Ports/State。 */

import type {
  AgentGateway,
  Clock,
  IdGenerator,
  Scheduler,
} from "../ports"
import type { InteractiveRuntime } from "../runtime"
import type { InteractiveState } from "../state"

/** 传递给各个 Feature 的受控上下文与操作句柄。 */
export type FeatureContext = {
  readonly gateway: AgentGateway
  readonly clock: Clock
  readonly scheduler: Scheduler
  readonly idGenerator: IdGenerator
  readonly baseRuntime: InteractiveRuntime
  getState(): InteractiveState
  commit(updater: (current: InteractiveState) => InteractiveState): void
  publish(): void
}
