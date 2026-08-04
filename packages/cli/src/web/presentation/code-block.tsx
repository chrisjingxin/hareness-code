/** Web CodeBlock 组件：无缝 plain-first 离线高亮、复制按钮与安全 DOM 渲染。 */
/** @jsxImportSource react */

import { useEffect, useState } from "react"
import { Check, Copy } from "lucide-react"

import { resolveSyntaxLanguage } from "../syntax/catalog.generated"
import { getSyntaxClient } from "../syntax/client"
import type { SyntaxSpan } from "../syntax/protocol"

export function CodeBlock(props: {
  code: string
  language?: string
}): React.ReactElement {
  const { code, language = "" } = props
  const [spans, setSpans] = useState<readonly SyntaxSpan[] | null>(null)
  const [copied, setCopied] = useState(false)
  const [loading, setLoading] = useState(false)

  const catalogEntry = resolveSyntaxLanguage(language)
  const displayLanguage = catalogEntry ? catalogEntry.filetype : language || "文本"

  useEffect(() => {
    let active = true
    if (!catalogEntry || !code.trim()) {
      setSpans(null)
      return
    }

    setLoading(true)
    getSyntaxClient()
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
    }
  }, [code, language, catalogEntry])

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // 复制失败不做破坏性操作
    }
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
          onClick={handleCopy}
        >
          {copied ? <Check aria-hidden="true" className="icon-sm" /> : <Copy aria-hidden="true" className="icon-sm" />}
          <span>{copied ? "已复制" : "复制"}</span>
        </button>
      </div>
      <div className="code-block-body">
        <pre className="markdown-code">
          <code data-language={language || undefined}>{spans ? renderSpans(code, spans) : code}</code>
        </pre>
      </div>
    </div>
  )
}

/** 将 UTF-8 字节范围精确转换为 UTF-16 字符范围并输出带 Class 的 React 节点组。 */
export function renderSpans(code: string, spans: readonly SyntaxSpan[]): React.ReactNode {
  if (!spans || spans.length === 0) return code

  // 建立 UTF-8 byte offset 到 UTF-16 char index 的映射表
  const encoder = new TextEncoder()
  const bytes = encoder.encode(code)
  const byteToCharIndex = new Int32Array(bytes.length + 1)

  let byteIdx = 0
  let charIdx = 0
  for (const char of code) {
    const charLen = char.length
    const charBytes = encoder.encode(char).length
    for (let b = 0; b < charBytes; b++) {
      byteToCharIndex[byteIdx + b] = charIdx
    }
    byteIdx += charBytes
    charIdx += charLen
  }
  byteToCharIndex[bytes.length] = code.length

  const charSpans: { startChar: number; endChar: number; scope: string }[] = []
  for (const span of spans) {
    const startChar = byteToCharIndex[Math.min(span.startByte, bytes.length)] ?? 0
    const endChar = byteToCharIndex[Math.min(span.endByte, bytes.length)] ?? code.length
    if (startChar < endChar) {
      charSpans.push({ startChar, endChar, scope: span.scope })
    }
  }

  // 排序并进行无交集线性区间切片
  charSpans.sort((a, b) => a.startChar - b.startChar)

  const elements: React.ReactNode[] = []
  let lastCharIndex = 0

  for (let i = 0; i < charSpans.length; i++) {
    const span = charSpans[i]
    if (span.startChar < lastCharIndex) continue // 忽略重叠或越界片段

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
