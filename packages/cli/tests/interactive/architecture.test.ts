/** Interactive Core 架构与依赖隔离性静态断言测试。 */

import { expect, test } from "bun:test"
import { readdir, readFile } from "node:fs/promises"
import { resolve } from "node:path"

const interactiveSrcDir = resolve(import.meta.dir, "../../src/interactive")

async function getSourceFiles(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { recursive: true, withFileTypes: true })
  return entries
    .filter(entry => entry.isFile() && entry.name.endsWith(".ts"))
    .map(entry => resolve(entry.parentPath, entry.name))
}

test("interactive/ 生产代码零 ../ipc/、零 JsonRpcRemoteError 依赖", async () => {
  const files = await getSourceFiles(interactiveSrcDir)
  expect(files.length).toBeGreaterThan(0)

  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    expect(content).not.toMatch(/from\s+["']\.\.\/ipc(?:\/.*)?["']/)
    expect(content).not.toContain("JsonRpcRemoteError")
  }
})

test("interactive/ 生产代码零 crypto.randomUUID() 与 Date.now() 直调（全量经 Port 注入）", async () => {
  const files = await getSourceFiles(interactiveSrcDir)

  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    expect(content).not.toContain("crypto.randomUUID()")
    expect(content).not.toContain("Date.now()")
  }
})

test("interactive/ 生产代码零 UI/平台库（react, @opentui, tui, web, WebSocket, node:*）", async () => {
  const files = await getSourceFiles(interactiveSrcDir)

  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    expect(content).not.toMatch(/from\s+["']react["']/)
    expect(content).not.toMatch(/from\s+["']@opentui\/.*["']/)
    expect(content).not.toMatch(/from\s+["']\.\.\/tui(?:\/.*)?["']/)
    expect(content).not.toMatch(/from\s+["']\.\.\/web(?:\/.*)?["']/)
    expect(content).not.toMatch(/from\s+["']node:.*["']/)
    expect(content).not.toContain("WebSocket")
  }
})
