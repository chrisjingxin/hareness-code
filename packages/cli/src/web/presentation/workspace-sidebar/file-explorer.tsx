/** Files 分区：工作区名标题 + 刷新按钮 + 文件树（本版设计无文件过滤）。 */
/** @jsxImportSource react */

import { MoreHorizontal, RefreshCw } from "lucide-react"

import { workspaceLabel } from "../../../interactive/runtime"
import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { FileTree } from "./file-tree"

/** 渲染 Files 分区；刷新按钮 dispatch workspace-refresh 重建文件树。 */
export function FileExplorer({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const workspace = snapshot.interactive.runtime.workspace
  return (
    <section className="file-explorer" aria-label="文件列表">
      <div className="file-explorer-header">
        {/* 分区标题即工作区名（自顶栏迁入，2026-08-18 与用户确认）：与线程分区标题同为纯文字规格，悬停可见完整路径。 */}
        <span className="file-explorer-title" title={workspace}>
          <span className="file-explorer-title-text">{workspaceLabel(workspace)}</span>
        </span>
        <span className="file-explorer-actions">
          <button
            type="button"
            className="icon-button"
            aria-label="刷新文件树"
            title="刷新"
            disabled={disabled}
            onClick={() => dispatch({ type: "workspace-refresh" })}
          >
            <RefreshCw aria-hidden="true" size={14} />
          </button>
          <span className="file-explorer-more" aria-hidden="true"><MoreHorizontal size={16} /></span>
        </span>
      </div>
      <FileTree snapshot={snapshot} dispatch={dispatch} disabled={disabled} />
    </section>
  )
}
