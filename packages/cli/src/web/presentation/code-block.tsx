/** Web CodeBlock 组件：无缝 plain-first 离线高亮、复制按钮与安全 DOM 渲染。 */
/** @jsxImportSource react */

import { useEffect, useRef, useState } from "react"
import { Check, Copy } from "lucide-react"

import { resolveSyntaxLanguage } from "../syntax/catalog.generated"
import { acquireSyntaxClient, releaseSyntaxClient } from "../syntax/client"
import type { SyntaxSpan } from "../syntax/protocol"

/** 渲染 fenced code 的 plain-first 高亮、复制状态与安全 span。 */
export function CodeBlock(props: {
  code: string
  language?: string
}): React.ReactElement {
  const { code, language = "" } = props
  const [spans, setSpans] = useState<readonly SyntaxSpan[] | null>(null)
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle")
  const [loading, setLoading] = useState(false)
  const resetCopyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const catalogEntry = resolveSyntaxLanguage(language)
  const displayLanguage = catalogEntry ? catalogEntry.filetype : language || "文本"

  useEffect(() => {
    let active = true
    if (!catalogEntry || !code.trim()) {
      setSpans(null)
      setLoading(false)
      return
    }

    setSpans(null)
    setLoading(true)
    acquireSyntaxClient()
      .highlight(catalogEntry.filetype, code)
      .then(res => {
        if (!active) return
        setLoading(false)
        if (res.type === "highlighted") {
          setSpans(res.spans)
        } else {
          setSpans(null)
        }
      })
      .catch(() => {
        if (!active) return
        setLoading(false)
        setSpans(null)
      })

    return () => {
      active = false
      releaseSyntaxClient()
    }
  }, [code, language, catalogEntry])

  useEffect(() => {
    return () => {
      if (resetCopyTimerRef.current) clearTimeout(resetCopyTimerRef.current)
    }
  }, [])

  const handleCopy = async () => {
    if (resetCopyTimerRef.current) clearTimeout(resetCopyTimerRef.current)
    try {
      await navigator.clipboard.writeText(code)
      setCopyStatus("copied")
    } catch {
      setCopyStatus("failed")
    }
    resetCopyTimerRef.current = setTimeout(() => setCopyStatus("idle"), 1_500)
  }

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-block-lang">
          {displayLanguage}
          {loading ? <span className="code-block-loading-hint"> (高亮中)</span> : null}
        </span>
        <button
          type="button"
          className="code-block-copy"
          aria-label="复制代码"
          title="复制代码"
          onClick={handleCopy}
        >
          {copyStatus === "copied" ? <Check aria-hidden="true" className="icon-sm" /> : <Copy aria-hidden="true" className="icon-sm" />}
        </button>
      </div>
      <div className="code-block-body">
        <pre className="code-block-pre">
          <code data-language={language || undefined}>{spans ? renderSpans(code, spans) : code}</code>
        </pre>
      </div>
      <span className="sr-only" role="status" aria-live="polite">
        {copyStatus === "copied" ? "代码已复制" : copyStatus === "failed" ? "复制失败，请手动选择代码" : ""}
      </span>
    </div>
  )
}

/** 将 UTF-8 字节范围精确转换为 UTF-16 字符范围并输出带 Class 的 React 节点组。 */
export function renderSpans(code: string, spans: readonly SyntaxSpan[]): React.ReactNode {
  if (!spans || spans.length === 0) return code

  // 建立 UTF-8 边界到 UTF-16 索引的映射；落在多字节字符中间的范围一律 fail closed。
  const encoder = new TextEncoder()
  const bytes = encoder.encode(code)
  const byteToCharIndex = new Int32Array(bytes.length + 1).fill(-1)

  let byteIdx = 0
  let charIdx = 0
  for (const char of code) {
    const charLen = char.length
    const charBytes = encoder.encode(char).length
    byteToCharIndex[byteIdx] = charIdx
    byteIdx += charBytes
    charIdx += charLen
  }
  byteToCharIndex[bytes.length] = code.length

  const charSpans: { startByte: number; endByte: number; startChar: number; endChar: number; scope: string }[] = []
  for (const span of spans) {
    if (!Number.isInteger(span.startByte) || !Number.isInteger(span.endByte)
      || span.startByte < 0 || span.endByte > bytes.length || span.startByte >= span.endByte) return code
    const startChar = byteToCharIndex[span.startByte]
    const endChar = byteToCharIndex[span.endByte]
    if (startChar < 0 || endChar < 0 || startChar >= endChar) return code
    charSpans.push({ startByte: span.startByte, endByte: span.endByte, startChar, endChar, scope: span.scope })
  }

  // 范围重叠无法安全决定 token 优先级时整块降级，不猜测切割。
  charSpans.sort((a, b) => a.startByte - b.startByte || a.endByte - b.endByte)
  for (let i = 1; i < charSpans.length; i++) {
    if (charSpans[i]!.startByte < charSpans[i - 1]!.endByte) return code
  }

  const elements: React.ReactNode[] = []
  let lastCharIndex = 0

  for (let i = 0; i < charSpans.length; i++) {
    const span = charSpans[i]
    if (span.startChar > lastCharIndex) {
      elements.push(code.slice(lastCharIndex, span.startChar))
    }

    elements.push(
      <span key={`span-${i}-${span.startChar}`} className={`syntax-${span.scope}`}>
        {code.slice(span.startChar, span.endChar)}
      </span>,
    )
    lastCharIndex = span.endChar
  }

  if (lastCharIndex < code.length) {
    elements.push(code.slice(lastCharIndex))
  }

  return elements
}
