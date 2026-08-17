/** Files 分区：标题 + 刷新按钮 + 文件树（本版设计无文件过滤）。 */
/** @jsxImportSource react */

import { MoreHorizontal, RefreshCw } from "lucide-react"

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
  return (
    <section className="file-explorer" aria-label="文件列表">
      <div className="file-explorer-header">
        <span className="file-explorer-title">文件</span>
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
