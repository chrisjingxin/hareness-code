/** 工作区侧栏头部：紧凑的工作区名 + Git 状态 + 新建 Thread / 刷新（顶栏已展示完整路径与 Git 状态）。 */
/** @jsxImportSource react */

import { Plus, RefreshCw } from "lucide-react"

import { workspaceLabel } from "../../../interactive/runtime"
import { gitWorkspaceLabel } from "../../../presentation-shared"
import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"

/** 渲染侧栏头部；新建 Thread 按钮在 active Run 期间禁用，与旧 ThreadSidebar 语义一致。 */
export function WorkspaceHeader({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const runtime = snapshot.interactive.runtime
  const busy = Boolean(snapshot.interactive.activeRun) || Boolean(snapshot.interactive.interaction)
  const git = gitWorkspaceLabel(runtime.gitWorkspace)
  return (
    <header className="workspace-header">
      <div className="workspace-header-copy" title={runtime.workspace}>
        <span className="workspace-header-name">{workspaceLabel(runtime.workspace)}</span>
        {git ? <span className="workspace-header-meta">{git}</span> : null}
      </div>
      <div className="workspace-header-actions">
        <button
          type="button"
          className="new-thread-button"
          onClick={() => dispatch({ type: "thread-new" })}
          disabled={disabled || busy}
          aria-disabled={disabled || busy}
          title={disabled ? "接管尚未完成或连接不可用" : busy ? "当前任务结束后可用" : "新建 Thread"}
        >
          <Plus aria-hidden="true" size={14} />
          <span>新建 Thread</span>
        </button>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新 Thread 列表"
          title="刷新"
          disabled={disabled}
          onClick={() => dispatch({ type: "thread-refresh" })}
        >
          <RefreshCw aria-hidden="true" size={14} />
        </button>
      </div>
    </header>
  )
}
