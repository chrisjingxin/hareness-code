/** Interactive Core 架构与依赖隔离性静态断言测试。 */

import { expect, test } from "bun:test"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"

import { sourceFiles } from "../acceptance/arch-imports"

const interactiveSrcDir = resolve(import.meta.dir, "../../src/interactive")

test("interactive/ 生产代码零 ../ipc/、零 JsonRpcRemoteError 依赖", async () => {
  const files = sourceFiles(interactiveSrcDir)
  expect(files.length).toBeGreaterThan(0)

  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    expect(content).not.toMatch(/from\s+["']\.\.\/ipc(?:\/.*)?["']/)
    expect(content).not.toContain("JsonRpcRemoteError")
  }
})

test("interactive/ 生产代码零 crypto.randomUUID() 与 Date.now() 直调（全量经 Port 注入）", async () => {
  const files = sourceFiles(interactiveSrcDir)

  for (const filePath of files) {
    const content = await readFile(filePath, "utf8")
    expect(content).not.toContain("crypto.randomUUID()")
    expect(content).not.toContain("Date.now()")
  }
})

test("interactive/ 生产代码零 UI/平台库（react, @opentui, tui, web, WebSocket, node:*）", async () => {
  const files = sourceFiles(interactiveSrcDir)

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
