/** Web 文件审批 Diff card：响应式 split/unified、行号、语义色与 Shiki 降级。 */
/** @jsxImportSource react */

import type { FileDiffPresentation } from "@za38/protocol"
import { useEffect, useMemo, useState, type ReactNode } from "react"

import {
  alignFileDiffHunk,
  parseFileDiff,
  type FileDiffHunk,
  type FileDiffLine,
} from "../../presentation-shared/file-diff"
import { resolveLanguageForPath } from "../../presentation-shared/language-catalog"
import type { SyntaxSpan } from "../syntax/protocol"
import { renderSpans } from "./code-block"
import { useSyntaxHighlight } from "./code/use-syntax-highlight"

const SPLIT_MEDIA_QUERY = "(min-width: 760px)"
type DiffViewMode = "split" | "unified"

/** 纯断点规则供响应式默认值与测试共同使用。 */
export function defaultDiffModeForWidth(width: number): DiffViewMode {
  return width >= 760 ? "split" : "unified"
}

/** 渲染与授权无关的文件 Diff 展示；按钮仍由外层 ApprovalForm 管理。 */
export function FileDiffApproval(props: {
  presentation: FileDiffPresentation
  requests: unknown
}): ReactNode {
  const { presentation } = props
  const parsed = useMemo(() => parseFileDiff(presentation.unified_diff), [presentation.unified_diff])
  const responsiveMode = useResponsiveDiffMode()
  const [selectedMode, setSelectedMode] = useState<DiffViewMode | null>(null)
  const mode = selectedMode ?? responsiveMode
  const language = resolveLanguageForPath(presentation.path).webLanguage
  const operation = operationLabel(presentation.operation)

  return (
    <section className="file-diff-approval" aria-label={`${operation} ${presentation.path}`}>
      <header className="file-diff-header">
        <div className="file-diff-identity">
          <span className="file-diff-operation">{operation}</span>
          <code className="file-diff-path">{presentation.path}</code>
          <span className="file-diff-stats" aria-label={`新增 ${presentation.added_lines} 行，删除 ${presentation.removed_lines} 行`}>
            <span className="file-diff-added">+{presentation.added_lines}</span>
            <span className="file-diff-removed">-{presentation.removed_lines}</span>
          </span>
        </div>
        <div className="file-diff-mode" role="group" aria-label="Diff 展示模式">
          <button type="button" aria-pressed={mode === "split"} onClick={() => setSelectedMode("split")}>左右</button>
          <button type="button" aria-pressed={mode === "unified"} onClick={() => setSelectedMode("unified")}>行内</button>
        </div>
      </header>
      {presentation.truncated ? (
        <p className="file-diff-truncated" role="status">
          预览已按 200 行或 16 KiB 上限截断；批准仍会应用完整变更。
        </p>
      ) : null}
      {presentation.unified_diff === "" ? (
        <p className="file-diff-empty">创建空文件（没有可显示的内容行）</p>
      ) : parsed.status === "invalid" ? (
        <div className="file-diff-fallback">
          <p>无法解析结构化 Diff，以下按纯文本展示。</p>
          <pre>{presentation.unified_diff}</pre>
        </div>
      ) : (
        <div className={`file-diff-body file-diff-${mode}`} data-view={mode}>
          {parsed.hunks.map((hunk, index) => mode === "split"
            ? <SplitHunk key={`${hunk.header}-${index}`} hunk={hunk} language={language} />
            : <UnifiedHunk key={`${hunk.header}-${index}`} hunk={hunk} language={language} />)}
        </div>
      )}
      <RequestDetails requests={props.requests} />
    </section>
  )
}

function SplitHunk(props: { hunk: FileDiffHunk; language: string }): ReactNode {
  const rows = alignFileDiffHunk(props.hunk)
  const left = useHighlightedRows(rows.map(row => row.left), props.language)
  const right = useHighlightedRows(rows.map(row => row.right), props.language)
  return (
    <section className="file-diff-hunk">
      <div className="file-diff-hunk-header">{props.hunk.header}</div>
      <div className="file-diff-split-head" aria-hidden="true"><span>修改前</span><span>修改后</span></div>
      {rows.map((row, index) => (
        <div className="file-diff-split-row" key={index}>
          <DiffCell line={row.left} content={left[index]} side="old" />
          <DiffCell line={row.right} content={right[index]} side="new" />
        </div>
      ))}
    </section>
  )
}

function UnifiedHunk(props: { hunk: FileDiffHunk; language: string }): ReactNode {
  const highlighted = useHighlightedRows(props.hunk.lines, props.language)
  return (
    <section className="file-diff-hunk">
      <div className="file-diff-hunk-header">{props.hunk.header}</div>
      {props.hunk.lines.map((line, index) => (
        <div className={`file-diff-unified-row diff-${line.kind}`} key={index}>
          <span className="file-diff-line-number">{line.oldLine ?? ""}</span>
          <span className="file-diff-line-number">{line.newLine ?? ""}</span>
          <span className="file-diff-sign" aria-hidden="true">{lineSign(line)}</span>
          <code>{highlighted[index]}</code>
        </div>
      ))}
    </section>
  )
}

function DiffCell(props: {
  line: FileDiffLine | null
  content: ReactNode
  side: "old" | "new"
}): ReactNode {
  const { line } = props
  const kind = line?.kind ?? "placeholder"
  const lineNumber = props.side === "old" ? line?.oldLine : line?.newLine
  return (
    <div className={`file-diff-cell diff-${kind}`} aria-hidden={line === null ? "true" : undefined}>
      <span className="file-diff-line-number">{lineNumber ?? ""}</span>
      <span className="file-diff-sign" aria-hidden="true">{line ? lineSign(line) : ""}</span>
      <code>{props.content}</code>
    </div>
  )
}

function useHighlightedRows(lines: readonly (FileDiffLine | null)[], language: string): readonly ReactNode[] {
  const codeLines = lines.map(line => line && line.kind !== "no-newline" ? line.text : "")
  const code = codeLines.join("\n")
  const highlightLanguage = language === "plaintext" ? null : language
  const highlight = useSyntaxHighlight({ code, language: highlightLanguage, theme: "dark-plus" })
  return useMemo(() => {
    if (highlight.status !== "highlighted" || !highlight.spans) return codeLines
    return renderHighlightedLines(codeLines, highlight.spans)
  }, [code, highlight.spans, highlight.status])
}

/** 把一次连续源码高亮的 UTF-8 span 切回视觉行，保留多行语法上下文。 */
function renderHighlightedLines(lines: readonly string[], spans: readonly SyntaxSpan[]): readonly ReactNode[] {
  const encoder = new TextEncoder()
  let startByte = 0
  return lines.map((line, index) => {
    const length = encoder.encode(line).length
    const endByte = startByte + length
    const lineSpans = spans.flatMap(span => {
      const start = Math.max(span.startByte, startByte)
      const end = Math.min(span.endByte, endByte)
      return start < end ? [{ ...span, startByte: start - startByte, endByte: end - startByte }] : []
    })
    const rendered = renderSpans(line, lineSpans)
    startByte = endByte + (index < lines.length - 1 ? 1 : 0)
    return rendered
  })
}

function useResponsiveDiffMode(): DiffViewMode {
  const read = (): DiffViewMode => {
    if (typeof window === "undefined") return "unified"
    if (typeof window.matchMedia === "function") return window.matchMedia(SPLIT_MEDIA_QUERY).matches ? "split" : "unified"
    return defaultDiffModeForWidth(window.innerWidth)
  }
  const [mode, setMode] = useState<DiffViewMode>(read)
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return
    const query = window.matchMedia(SPLIT_MEDIA_QUERY)
    const update = () => setMode(query.matches ? "split" : "unified")
    query.addEventListener?.("change", update)
    return () => query.removeEventListener?.("change", update)
  }, [])
  return mode
}

function RequestDetails(props: { requests: unknown }): ReactNode {
  let preview: string | null = null
  try {
    preview = JSON.stringify(props.requests, null, 2)
  } catch {}
  if (!preview) return null
  return (
    <details className="file-diff-request-details">
      <summary>请求参数</summary>
      <pre>{preview}</pre>
    </details>
  )
}

function operationLabel(operation: FileDiffPresentation["operation"]): string {
  return operation === "write" ? "创建文件" : operation === "delete" ? "删除文件" : "编辑文件"
}

function lineSign(line: FileDiffLine): string {
  if (line.kind === "add") return "+"
  if (line.kind === "remove") return "-"
  if (line.kind === "no-newline") return "\\"
  return " "
}
