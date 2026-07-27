/** 离线 Tree-sitter parser 注册与企业常用语言别名映射。 */

import { addDefaultParsers, getDataPaths, TreeSitterClient, type FiletypeParserOptions } from "@opentui/core"

import { bundledSyntaxParsers } from "./generated-syntax-parsers"

let registered = false
let sharedClient: TreeSitterClient | undefined

/**
 * 运行时只把随 CLI 分发的本地路径交给 OpenTUI，避免企业网络环境首次展示代码时访问 GitHub。
 * Markdown、JavaScript、TypeScript、Zig 由 OpenTUI 内置 parser 处理，不能重复注册。
 */
export function registerCommonSyntaxParsers(): void {
  if (registered) return
  registered = true
  addDefaultParsers([...bundledSyntaxParsers])
}

/** 返回由 CLI 自己持有的高亮 client，避免被 OpenTUI renderer 的全局生命周期提前销毁。 */
export function getCommonSyntaxClient(): TreeSitterClient {
  registerCommonSyntaxParsers()
  sharedClient ??= new TreeSitterClient({ dataPath: getDataPaths().globalDataPath })
  return sharedClient
}

/** TUI 退出时释放独立高亮 worker；重复调用保持安全。 */
export async function shutdownCommonSyntaxClient(): Promise<void> {
  const client = sharedClient
  sharedClient = undefined
  if (client) await client.destroy()
}

/** 对外暴露已内置的语法语言清单，供诊断与测试复用。 */
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
