/** Web Syntax Worker 实现：基于 Shiki (fine-grained imports) 离线高亮并返回范围 Token。 */

import { resolveLanguage } from "../../presentation-shared/language-catalog"
import { getShikiHighlighter } from "./language-loader"
import type { SyntaxScope, SyntaxSpan, SyntaxWorkerRequest, SyntaxWorkerResponse } from "./protocol"

export function colorToScope(color?: string): SyntaxScope {
  if (!color) return "plain"
  const hex = color.toLowerCase()
  // 注释
  if (hex === "#6a9955" || hex === "#6e7781" || hex === "#8b949e") return "comment"
  // 关键字与控制流
  if (hex === "#569cd6" || hex === "#c586c0" || hex === "#cf222e" || hex === "#ff7b72" || hex === "#d73a49") return "keyword"
  // 函数与方法
  if (hex === "#dcdcaa" || hex === "#8250df" || hex === "#d2a8ff" || hex === "#6f42c1") return "function"
  // 变量与属性
  if (hex === "#9cdcfe" || hex === "#4fc1ff" || hex === "#953800" || hex === "#ffa657" || hex === "#0550ae") return "variable"
  // 字符串
  if (hex === "#ce9178" || hex === "#d7ba7d" || hex === "#0a3069" || hex === "#a5d6ff" || hex === "#032f62") return "string"
  // 数字与常量
  if (hex === "#b5cea8" || hex === "#79c0ff" || hex === "#005cc5") return "number"
  // 类型与类
  if (hex === "#4ec9b0") return "type"
  return "plain"
}

export async function processHighlightRequest(request: {
  requestId: number
  language: string
  code: string
  theme?: string
}): Promise<SyntaxWorkerResponse> {
  const { requestId, language, code, theme = "dark-plus" } = request

  const codeBytes = new TextEncoder().encode(code).length
  if (codeBytes > 64 * 1024 || (code ? code.split("\n").length : 0) > 2_000) {
    return { type: "plain", requestId, reason: "too-large" }
  }

  const catalogEntry = resolveLanguage(language)
  if (catalogEntry.canonical === "plaintext" && language && language.trim() !== "" && language !== "plaintext" && language !== "text" && language !== "txt") {
    return { type: "plain", requestId, reason: "unknown-language" }
  }

  try {
    const highlighter = await getShikiHighlighter()
    const tokensResult = highlighter.codeToTokens(code, {
      lang: catalogEntry.webLanguage,
      theme,
    })

    const encoder = new TextEncoder()
    const spans: SyntaxSpan[] = []

    let currentByte = 0
    for (let lineIndex = 0; lineIndex < tokensResult.tokens.length; lineIndex++) {
      const lineTokens = tokensResult.tokens[lineIndex]!
      for (const token of lineTokens) {
        const tokenBytes = encoder.encode(token.content).length
        const startByte = currentByte
        const endByte = currentByte + tokenBytes
        currentByte = endByte

        const scope = colorToScope(token.color)
        if (scope !== "plain" && startByte < endByte) {
          spans.push({ startByte, endByte, scope })
        }
      }
      // 如果不是最后一行，说明这行后面有换行符 \n (1 字节)
      if (lineIndex < tokensResult.tokens.length - 1) {
        currentByte += 1
      }
    }

    return {
      type: "highlighted",
      requestId,
      language: catalogEntry.canonical,
      spans,
    }
  } catch (error) {
    return { type: "plain", requestId, reason: "parse-failed" }
  }
}

// 在 Web Worker 环境自动挂载 onmessage
if (typeof self !== "undefined" && typeof postMessage === "function" && "onmessage" in self) {
  (self as unknown as { onmessage: (event: MessageEvent<SyntaxWorkerRequest>) => void }).onmessage = async (event: MessageEvent<SyntaxWorkerRequest>) => {
    const data = event.data
    if (data.type === "dispose") {
      return
    }
    if (data.type === "highlight") {
      const response = await processHighlightRequest(data)
      postMessage(response)
    }
  }
}
