/** workspace 分层规则测试：独立领域模块，零 UI/平台/Agent 内部依赖。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"

import { sourceFiles } from "../acceptance/arch-imports"

const workspaceSrcDir = resolve(import.meta.dir, "../../src/workspace")

test("workspace/ 生产代码只允许 node 内置与白名单 import", async () => {
  const files = sourceFiles(workspaceSrcDir)
  expect(files.length).toBeGreaterThan(0)

  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    expect(content).not.toMatch(/from\s+["']react["']/)
    expect(content).not.toMatch(/from\s+["']react-dom["']/)
    expect(content).not.toMatch(/@opentui/)
    expect(content).not.toMatch(/from\s+["']\.\.\/interactive\/controller/)
    expect(content).not.toMatch(/from\s+["']\.\.\/interactive\/agent-port/)
    expect(content).not.toMatch(/from\s+["']\.\.\/web(?:\/.*)?["']/)
    expect(content).not.toMatch(/from\s+["']\.\.\/ipc(?:\/.*)?["']/)
    expect(content).not.toContain("WebSocket")
  }
})

test("workspace/ 生产代码只 import 白名单模块路径", async () => {
  const files = sourceFiles(workspaceSrcDir)
  const imports: string[] = []
  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    const matches = content.match(/^import .*$/gm) ?? []
    imports.push(...matches)
  }
  const joined = imports.join("\n")
  // 允许：node 内置、presentation-shared（纯函数）、workspace 内部相对路径。
  expect(joined).toMatch(/from\s+["']node:/)
  expect(joined).toMatch(/from\s+["']\.\.\/presentation-shared/)
  expect(joined).not.toMatch(/from\s+["']\.\.\/interactive\/(?!types)/)
  expect(joined).not.toMatch(/from\s+["']\.\.\/(?!workspace|presentation-shared|interactive\/types)/)
})
