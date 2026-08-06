/** Git 工作区状态文案：把四种探测结果收敛为 TUI 与 Web 共用的短标签。 */

import type { GitWorkspaceState } from "../interactive/runtime"

/** 生成底栏/顶栏可显示的 Git 状态标签；探测未完成时返回 undefined 表示不渲染。 */
export function gitWorkspaceLabel(state: GitWorkspaceState | undefined): string | undefined {
  if (!state) return undefined
  switch (state.kind) {
    case "branch": return state.branch
    case "detached": return `detached@${state.shortSha}`
    case "not-repository": return "非 Git 工作区"
    case "unavailable": return "Git 状态不可用"
  }
}
