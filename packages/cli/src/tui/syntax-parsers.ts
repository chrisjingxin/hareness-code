import { addDefaultParsers, type FiletypeParserOptions } from "@opentui/core"

import { bundledSyntaxParsers } from "./generated-syntax-parsers"

let registered = false

/**
 * 运行时只把随 CLI 分发的本地路径交给 OpenTUI，避免企业网络环境首次展示代码时访问 GitHub。
 * Markdown、JavaScript、TypeScript、Zig 由 OpenTUI 内置 parser 处理，不能重复注册。
 */
export function registerCommonSyntaxParsers(): void {
  if (registered) return
  registered = true
  addDefaultParsers([...bundledSyntaxParsers])
}

export const SUPPORTED_SYNTAX_LANGUAGES = [
  "markdown",
  "javascript",
  "typescript",
  "zig",
  ...bundledSyntaxParsers.map(parser => parser.filetype),
] as const

/** 仅供测试和诊断使用，调用方不得修改 parser 配置。 */
export function getBundledSyntaxParsers(): readonly FiletypeParserOptions[] {
  return bundledSyntaxParsers
}
