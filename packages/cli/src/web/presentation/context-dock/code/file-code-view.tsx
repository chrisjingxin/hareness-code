/** 文件代码视图：行号列 + 高亮内容（white-space: pre 不折行，行号与代码稳定对应）。 */
/** @jsxImportSource react */

import type { WorkspacePreviewView } from "../../../../presentation-coordinator"
import { HighlightedCode } from "../../code/highlighted-code"
import { formatFileSize } from "./file-meta"

/** 渲染预览内容：loading 骨架 / unsupported 元信息 / error 提示 / ready 高亮代码。 */
export function FileCodeView({
  preview,
}: {
  preview: WorkspacePreviewView | undefined
}): React.ReactElement {
  if (!preview || preview.status === "idle" || preview.status === "loading") {
    return (
      <div className="file-code-view">
        <p className="file-code-status">加载中…</p>
      </div>
    )
  }
  if (preview.status === "unsupported") {
    const name = preview.path.split("/").at(-1) ?? preview.path
    return (
      <div className="file-code-view file-code-view-meta">
        <p className="file-code-status">{name}</p>
        <p className="file-code-status">{`${preview.reason} · ${formatFileSize(preview.sizeBytes)}`}</p>
      </div>
    )
  }
  if (preview.status === "error") {
    // 头部保留刷新入口；此处展示错误信息（错误页只在无旧内容时出现）。
    return (
      <div className="file-code-view file-code-view-error" role="alert">
        <p className="file-code-status">{preview.message}</p>
      </div>
    )
  }
  const { file } = preview
  return (
    <div className="file-code-view">
      <div className="line-numbers" aria-hidden="true">
        {lineNumbers(file.lineCount)}
      </div>
      <pre className="file-code-pre">
        <code data-language={file.language ?? undefined}>
          <HighlightedCode code={file.content} language={file.language} theme="dark-plus" />
        </code>
      </pre>
    </div>
  )
}

/** 1..count 行号文本（与代码行高一一对应）。 */
function lineNumbers(count: number): string {
  const lines: string[] = []
  for (let i = 1; i <= count; i++) lines.push(String(i))
  return lines.join("\n")
}
