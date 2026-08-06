/**
 * 语法高亮 Hook：plain-first —— 未知/plaintext 语言、空代码与高亮失败
 * 一律直接返回 plain；成功结果走 Browser 侧 8 条 LRU 缓存（key =
 * canonical 语言 + 主题 + 代码哈希），避免重复文件反复触发 Worker。
 */

import { useEffect, useState } from "react"

import { resolveLanguage } from "../../../presentation-shared/language-catalog"
import { acquireHighlightService, releaseHighlightService } from "../../syntax/highlight-service"
import type { SyntaxSpan } from "../../syntax/protocol"

/** Browser 侧高亮结果缓存上限；按 LRU 淘汰最久未使用。 */
const HIGHLIGHT_CACHE_LIMIT = 8

export type HighlightResult = {
  readonly status: "plain" | "loading" | "highlighted"
  readonly spans: readonly SyntaxSpan[] | null
}

/** 模块级 LRU：Map 插入序即最近使用序。 */
const highlightCache = new Map<string, readonly SyntaxSpan[]>()

/** 订阅 Shiki 高亮：input 变化时重新请求，卸载/输入再变时丢弃旧结果。 */
export function useSyntaxHighlight(input: { code: string; language: string | null; theme: string }): HighlightResult {
  const { code, language, theme } = input
  const [result, setResult] = useState<HighlightResult>({ status: "plain", spans: null })

  useEffect(() => {
    let active = true
    if (language === null) {
      setResult({ status: "plain", spans: null })
      return
    }
    const canonical = resolveLanguage(language).canonical
    if (canonical === "plaintext" || !code.trim()) {
      setResult({ status: "plain", spans: null })
      return
    }
    const cacheKey = `${canonical}:${theme}:${hashCode(code)}`
    const cached = getCached(cacheKey)
    if (cached !== undefined) {
      setResult({ status: "highlighted", spans: cached })
      return
    }
    setResult({ status: "loading", spans: null })
    const service = acquireHighlightService()
    service
      .highlight(canonical, code, theme)
      .then(response => {
        if (!active) return
        if (response.type === "highlighted") {
          putCached(cacheKey, response.spans)
          setResult({ status: "highlighted", spans: response.spans })
        } else {
          // unknown-language / too-large / load-failed / parse-failed / timeout → plain。
          setResult({ status: "plain", spans: null })
        }
      })
      .catch(() => {
        if (!active) return
        setResult({ status: "plain", spans: null })
      })
      .finally(() => {
        releaseHighlightService()
      })
    return () => {
      active = false
    }
  }, [code, language, theme])

  return result
}

function getCached(key: string): readonly SyntaxSpan[] | undefined {
  const entry = highlightCache.get(key)
  if (!entry) return undefined
  highlightCache.delete(key)
  highlightCache.set(key, entry)
  return entry
}

function putCached(key: string, spans: readonly SyntaxSpan[]): void {
  highlightCache.delete(key)
  highlightCache.set(key, spans)
  while (highlightCache.size > HIGHLIGHT_CACHE_LIMIT) {
    const oldest = highlightCache.keys().next().value
    if (oldest === undefined) break
    highlightCache.delete(oldest)
  }
}

/** 字符串哈希（FNV-1a 32 位）：只作缓存 key 使用，碰撞仅导致错误缓存复用，无害。 */
function hashCode(value: string): string {
  let hash = 0x811c9dc5
  for (let i = 0; i < value.length; i++) {
    hash ^= value.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(36)
}
