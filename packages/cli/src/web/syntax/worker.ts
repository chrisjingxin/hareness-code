/** Web Syntax Worker 实现：基于 web-tree-sitter 离线解析并返回范围 Token。 */

import { Parser, Language, type Query } from "web-tree-sitter"

import bashHighlights from "../../tui/platform/assets/syntax/bash/highlights.scm" with { type: "text" }
import cHighlights from "../../tui/platform/assets/syntax/c/highlights.scm" with { type: "text" }
import cppHighlights from "../../tui/platform/assets/syntax/cpp/highlights.scm" with { type: "text" }
import cssHighlights from "../../tui/platform/assets/syntax/css/highlights.scm" with { type: "text" }
import goHighlights from "../../tui/platform/assets/syntax/go/highlights.scm" with { type: "text" }
import htmlHighlights from "../../tui/platform/assets/syntax/html/highlights.scm" with { type: "text" }
import javaHighlights from "../../tui/platform/assets/syntax/java/highlights.scm" with { type: "text" }
import jsonHighlights from "../../tui/platform/assets/syntax/json/highlights.scm" with { type: "text" }
import pythonHighlights from "../../tui/platform/assets/syntax/python/highlights.scm" with { type: "text" }
import yamlHighlights from "../../tui/platform/assets/syntax/yaml/highlights.scm" with { type: "text" }

import { resolveSyntaxLanguage } from "./catalog.generated"
import type { SyntaxScope, SyntaxSpan, SyntaxWorkerRequest, SyntaxWorkerResponse } from "./protocol"

const queriesMap: Record<string, string> = {
  bash: bashHighlights,
  c: cHighlights,
  cpp: cppHighlights,
  css: cssHighlights,
  go: goHighlights,
  html: htmlHighlights,
  java: javaHighlights,
  json: jsonHighlights,
  python: pythonHighlights,
  yaml: yamlHighlights,
}

let parserInitPromise: Promise<void> | null = null
const languageCache = new Map<string, { lang: Language; query: Query }>()
const parserInstanceMap = new Map<string, Parser>()

async function initParser(): Promise<void> {
  if (!parserInitPromise) {
    parserInitPromise = Parser.init({
      locateFile: (scriptName: string) => `/web/syntax/${scriptName}`,
    })
  }
  return parserInitPromise
}

export function captureToScope(captureName: string): SyntaxScope {
  const root = captureName.split(".")[0]
  switch (root) {
    case "comment":
      return "comment"
    case "keyword":
    case "repeat":
    case "conditional":
    case "include":
    case "exception":
      return "keyword"
    case "function":
    case "method":
    case "constructor":
      return "function"
    case "variable":
    case "field":
    case "property":
    case "parameter":
    case "member":
      return "variable"
    case "string":
    case "character":
    case "escape":
      return "string"
    case "number":
    case "float":
    case "boolean":
      return "number"
    case "type":
    case "class":
    case "structure":
    case "enum":
      return "type"
    case "operator":
      return "operator"
    case "punctuation":
    case "delimiter":
    case "bracket":
      return "punctuation"
    case "tag":
      return "tag"
    case "attribute":
      return "attribute"
    case "constant":
      return "constant"
    default:
      return "plain"
  }
}

export async function processHighlightRequest(request: {
  requestId: number
  language: string
  code: string
}): Promise<SyntaxWorkerResponse> {
  const { requestId, language, code } = request

  // 代码超过 64 KiB 边界直接 plain 降级
  if (new TextEncoder().encode(code).length > 64 * 1024) {
    return { type: "plain", requestId, reason: "too-large" }
  }

  const catalogEntry = resolveSyntaxLanguage(language)
  if (!catalogEntry) {
    return { type: "plain", requestId, reason: "unknown-language" }
  }

  const queryText = queriesMap[catalogEntry.filetype]
  if (!queryText) {
    return { type: "plain", requestId, reason: "unknown-language" }
  }

  try {
    await initParser()

    let cacheItem = languageCache.get(catalogEntry.filetype)
    if (!cacheItem) {
      const wasmUrl = `/web/syntax/lang/${catalogEntry.assetId}.wasm`
      const lang = await Language.load(wasmUrl)
      const query = lang.query(queryText)
      cacheItem = { lang, query }
      languageCache.set(catalogEntry.filetype, cacheItem)
    }

    let parser = parserInstanceMap.get(catalogEntry.filetype)
    if (!parser) {
      parser = new Parser()
      parser.setLanguage(cacheItem.lang)
      parserInstanceMap.set(catalogEntry.filetype, parser)
    }

    const tree = parser.parse(code)
    if (!tree) {
      return { type: "plain", requestId, reason: "parse-failed" }
    }

    const captures = cacheItem.query.captures(tree.rootNode)

    const rawSpans: SyntaxSpan[] = []
    for (const capture of captures) {
      const scope = captureToScope(capture.name)
      if (scope === "plain") continue
      const startByte = capture.node.startIndex
      const endByte = capture.node.endIndex
      if (startByte < endByte) {
        rawSpans.push({ startByte, endByte, scope })
      }
    }

    // 按起始位置与范围排序去重
    rawSpans.sort((a, b) => a.startByte - b.startByte || (b.endByte - a.endByte))

    tree.delete()
    return {
      type: "highlighted",
      requestId,
      language: catalogEntry.filetype,
      spans: rawSpans,
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
      for (const parser of parserInstanceMap.values()) {
        parser.delete()
      }
      parserInstanceMap.clear()
      languageCache.clear()
      return
    }
    if (data.type === "highlight") {
      const response = await processHighlightRequest(data)
      postMessage(response)
    }
  }
}
