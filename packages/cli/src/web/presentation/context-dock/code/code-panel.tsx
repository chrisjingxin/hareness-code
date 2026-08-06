/** Code 面板：文件 Tab 条 + 当前文件预览（高亮 + 行号）；无文件时显示空状态（设计 6.3）。 */
/** @jsxImportSource react */

import type { WebAdapterSnapshot, WebIntent } from "../../../application/adapter"
import { FileTabs } from "./file-tabs"
import { FilePreviewHeader } from "./file-preview-header"
import { FileCodeView } from "./file-code-view"

/** 渲染 Code 面板；关闭最后一个 Tab 后 Dock 保持打开并显示空状态。 */
export function CodePanel({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const code = snapshot.contextDock.code
  if (code.activePath === null) {
    return (
      <div className="panel code-panel code-panel-empty">
        <p className="panel-empty">从左侧文件树打开文件查看预览</p>
      </div>
    )
  }
  const preview = code.previews[code.activePath]
  const error = code.previewErrors[code.activePath]
  return (
    <div className="panel code-panel">
      <FileTabs tabs={code.tabs} activePath={code.activePath} dispatch={dispatch} disabled={disabled} />
      <FilePreviewHeader
        path={code.activePath}
        preview={preview}
        error={error}
        dispatch={dispatch}
        disabled={disabled}
      />
      <FileCodeView preview={preview} />
    </div>
  )
}
