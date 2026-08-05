/**
 * Presentation 所有权状态：TUI 与内置 Web UI 之间表现层输入权的显式状态机。
 *
 * 状态只表达"表现层输入权"（谁可以提交可变 intent）；Agent Host 控制权始终属于
 * CLI owner，本状态机不迁移 Host control lease（与 ZC-101 ControlLease 解耦）。
 */

/** 当前拥有表现层输入权的表现层。 */
export type PresentationOwner = "tui" | "web"

/** 进入 returning-tui 的可观察原因；只用于状态展示，不参与业务分支。 */
export type ReturnReason =
  | "browser-close"
  | "ready-timeout"
  | "invalid-message"
  | "returned"
  | "exit-requested"
  | "opener-failed"
  | "cli-exit"

/** Presentation 状态机：tui-active → opening-web → web-active → returning-tui → tui-active。 */
export type PresentationState =
  | { phase: "tui-active" }
  | { phase: "opening-web"; handoffId: string }
  | { phase: "web-active"; handoffId: string }
  | { phase: "returning-tui"; handoffId: string; reason: ReturnReason }

/** 当前拥有输入权的表现层；tui-active 之外都视为 Web（含过渡状态）。 */
export function ownerOf(state: PresentationState): PresentationOwner {
  return state.phase === "tui-active" ? "tui" : "web"
}

/** TUI 是否处于输入锁定：web-active 起锁定，returning-tui 收敛期间保持锁定。 */
export function tuiLocked(state: PresentationState): boolean {
  return state.phase === "web-active" || state.phase === "returning-tui"
}

/** Web 是否处于可提交 intent 的活跃状态。 */
export function isWebActive(state: PresentationState): boolean {
  return state.phase === "web-active"
}

/** 是否属于某个 handoff 会话（含过渡状态），供静态 server 判定路径活性。 */
export function isHandoffPhase(state: PresentationState): state is Extract<PresentationState, { handoffId: string }> {
  return state.phase !== "tui-active"
}
