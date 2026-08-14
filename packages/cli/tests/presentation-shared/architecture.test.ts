/** presentation-shared 依赖隔离静态断言：纯展示策略，零平台/组件库 import。 */

import { expect, test } from "bun:test"
import { readdir, readFile } from "node:fs/promises"
import { basename, resolve } from "node:path"

const sharedSrcDir = resolve(import.meta.dir, "../../src/presentation-shared")

async function getSourceFiles(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { recursive: true, withFileTypes: true })
  return entries
    .filter(entry => entry.isFile() && entry.name.endsWith(".ts"))
    .map(entry => resolve(entry.parentPath, entry.name))
}

test("presentation-shared/ 存在全部 7 个共享策略模块", async () => {
  const files = await getSourceFiles(sharedSrcDir)
  const names = files.map(path => basename(path))
  for (const module of [
    "language-catalog.ts",
    "formatters.ts",
    "semantic-tone.ts",
    "tool-output-policy.ts",
    "command-menu-policy.ts",
    "interaction-policy.ts",
    "timeline-presenter.ts",
  ]) {
    expect(names).toContain(module)
  }
})

test("presentation-shared/ 生产代码零 react/opentui/ipc/tui/web/platform import", async () => {
  const files = await getSourceFiles(sharedSrcDir)
  expect(files.length).toBeGreaterThan(0)

  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    expect(content).not.toMatch(/from\s+["']react["']/)
    expect(content).not.toMatch(/from\s+["']react-dom["']/)
    expect(content).not.toMatch(/from\s+["']@opentui\/.*["']/)
    expect(content).not.toMatch(/from\s+["']\.\.\/tui(?:\/.*)?["']/)
    expect(content).not.toMatch(/from\s+["']\.\.\/web(?:\/.*)?["']/)
    expect(content).not.toMatch(/from\s+["']\.\.\/platform(?:\/.*)?["']/)
    expect(content).not.toMatch(/from\s+["']\.\.\/ipc(?:\/.*)?["']/)
    expect(content).not.toMatch(/from\s+["']node:.*["']/)
    expect(content).not.toContain("WebSocket")
  }
})
