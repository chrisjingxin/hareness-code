/** 文件预览头部：相对路径 + 刷新按钮 + 语言/大小/行数元信息 + 截断/错误提示。 */
/** @jsxImportSource react */

import { RefreshCw } from "lucide-react"

import type { WorkspacePreviewView } from "../../../../presentation-coordinator"
import type { WebIntent } from "../../../application/adapter"
import { fileLanguageDisplayLabel, formatFileSize } from "./file-meta"

/** 渲染当前文件预览头部；error 时保留旧内容、仅头部显示错误与刷新入口（设计 14.4）。 */
export function FilePreviewHeader({
  path,
  preview,
  error,
  dispatch,
  disabled = false,
}: {
  path: string
  preview: WorkspacePreviewView | undefined
  /** 独立于内容预览的错误提示（保留旧内容时头部仍然可见）。 */
  error: string | undefined
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const meta = preview?.status === "ready" || preview?.status === "unsupported"
    ? buildMeta(preview)
    : null
  return (
    <div className="file-preview-header">
      <div className="file-preview-path">
        <span className="file-preview-path-text" title={path}>{path}</span>
        {error ? <span className="file-preview-error-hint" role="alert">{error}</span> : null}
      </div>
      <div className="file-preview-actions">
        {meta ? <span className="file-preview-meta">{meta}</span> : null}
        {preview?.status === "ready" && preview.file.truncated ? (
          <span className="file-preview-truncated">文件较大，仅展示前 256 KiB</span>
        ) : null}
        <button
          type="button"
          className="icon-button"
          aria-label="刷新预览"
          title="刷新"
          disabled={disabled}
          onClick={() => dispatch({ type: "workspace-preview-refresh", path })}
        >
          <RefreshCw aria-hidden="true" size={14} />
        </button>
      </div>
    </div>
  )
}

function buildMeta(preview: Extract<WorkspacePreviewView, { status: "ready" | "unsupported" }>): string {
  if (preview.status === "unsupported") {
    return `${preview.reason} · ${formatFileSize(preview.sizeBytes)}`
  }
  return `${fileLanguageDisplayLabel(preview.file.language)} · ${formatFileSize(preview.file.sizeBytes)} · ${preview.file.lineCount} 行`
}
