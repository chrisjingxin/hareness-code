/**
 * 验收矩阵共享的架构断言工具。
 *
 * 统一目录递归、import 提取与正则门禁的读取逻辑；A-01~A-12 矩阵与
 * tests/tui、tests/web、tests/interactive 的架构测试共用，避免各目录
 * 各自维护一套重复实现（§13.2 禁止仅靠目录白名单/正则字符串代表完整架构约束）。
 */

import { readdirSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

/** 递归收集目录内全部 TS/TSX 源文件绝对路径。 */
export function sourceFiles(dir: string): string[] {
  let results: string[] = []
  const entries = readdirSync(dir, { withFileTypes: true })
  for (const entry of entries) {
    const fullPath = resolve(dir, entry.name)
    if (entry.isDirectory()) {
      results.push(...sourceFiles(fullPath))
    } else if (/\.[cm]?[jt]sx?$/.test(entry.name)) {
      results.push(fullPath)
    }
  }
  return results
}

/** 拼接目录内全部源文件内容，用于跨文件正则断言。 */
export function readAllSourceFiles(dir: string): string {
  return sourceFiles(dir).map(file => readFileSync(file, "utf8")).join("\n")
}

/** 拼接单层目录内全部 TS/TSX 文件内容（不递归），用于表现层局部断言。 */
export function readDirectory(dir: string): string {
  return readdirSync(dir, { withFileTypes: true })
    .filter(entry => entry.isFile() && /\.[cm]?[jt]sx?$/.test(entry.name))
    .map(entry => readFileSync(resolve(dir, entry.name), "utf8"))
    .join("\n")
}

/** 提取目录内全部文件的顶层 import 语句；跨行 import 只取首行，门禁按模式匹配。 */
export function layerImports(dir: string): string {
  return sourceFiles(dir)
    .map(file => readFileSync(file, "utf8"))
    .flatMap(source => source.match(/^import .*$/gm) ?? [])
    .join("\n")
}

/** 断言 source 不含匹配；失败信息带可读标签，便于定位违反的验收项。 */
export function expectNoMatch(source: string, pattern: RegExp, label: string): void {
  const match = source.match(pattern)
  if (match) {
    throw new Error(`${label} 违反约束：匹配到 ${JSON.stringify(match[0])}`)
  }
}
