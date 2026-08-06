/** file-preview：文本读取、容量/行数截断、二进制与非 UTF-8 识别、变化检测与 LRU 缓存。 */

import { expect, test } from "bun:test"
import { mkdtempSync, realpathSync, rmSync, writeFileSync, appendFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import {
  MAX_LINE_CHARS,
  MAX_PREVIEW_BYTES,
  MAX_PREVIEW_LINES,
  PREVIEW_CACHE_BYTES,
  PREVIEW_CACHE_LIMIT,
  PreviewCache,
  readPreview,
} from "../../src/workspace/file-preview"
import { WorkspaceError } from "../../src/workspace/path-policy"
import type { WorkspaceFilePreview } from "../../src/workspace/types"

/** 返回 realpath 后的临时根：macOS 上 /var 是 /private/var 的符号链接，必须统一真实路径。 */
function makeRoot(prefix: string): string {
  return realpathSync(mkdtempSync(join(tmpdir(), prefix)))
}

function expectErrorCode(promise: Promise<unknown>, code: string): Promise<void> {
  return promise.then(
    () => { throw new Error(`expected ${code} rejection`) },
    error => {
      expect(error).toBeInstanceOf(WorkspaceError)
      expect((error as WorkspaceError).code).toBe(code)
    },
  )
}

test("文本文件：正常读取，语言/行数/指纹正确且未截断", async () => {
  const root = makeRoot("za38-prev-ok-")
  try {
    const content = "const a = 1\nconst b = 2\n"
    writeFileSync(join(root, "a.ts"), content)
    const preview = await readPreview(root, "a.ts")
    expect(preview).toMatchObject({
      path: "a.ts",
      name: "a.ts",
      language: "typescript",
      sizeBytes: content.length,
      lineCount: 2,
      truncated: false,
    })
    expect(preview.content).toBe(content)
    expect(preview.version).toBe(`${preview.modifiedAtMs}:${preview.sizeBytes}`)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("300 KiB 文件：truncated=true 且 content 不超过 256 KiB", async () => {
  const root = makeRoot("za38-prev-big-")
  try {
    const content = "x".repeat(300 * 1024)
    writeFileSync(join(root, "big.txt"), content)
    const preview = await readPreview(root, "big.txt")
    expect(preview.truncated).toBe(true)
    expect(preview.content.length).toBeLessThanOrEqual(MAX_PREVIEW_BYTES)
    expect(preview.sizeBytes).toBe(content.length)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("超过 2000 行：lineCount 截断到 MAX_PREVIEW_LINES", async () => {
  const root = makeRoot("za38-prev-lines-")
  try {
    const content = Array.from({ length: MAX_PREVIEW_LINES + 50 }, (_, index) => `line ${index}`).join("\n")
    writeFileSync(join(root, "long.txt"), content)
    const preview = await readPreview(root, "long.txt")
    expect(preview.lineCount).toBe(MAX_PREVIEW_LINES)
    expect(preview.truncated).toBe(true)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("超长单行：行宽截断到 MAX_LINE_CHARS", async () => {
  const root = makeRoot("za38-prev-wide-")
  try {
    writeFileSync(join(root, "wide.txt"), "y".repeat(MAX_LINE_CHARS + 10))
    const preview = await readPreview(root, "wide.txt")
    expect(preview.content.length).toBe(MAX_LINE_CHARS)
    expect(preview.truncated).toBe(true)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("二进制文件（含 NUL）→ unsupported-file", async () => {
  const root = makeRoot("za38-prev-bin-")
  try {
    writeFileSync(join(root, "blob.bin"), Buffer.from([0x00, 0x01, 0x02, 0xff]))
    await expectErrorCode(readPreview(root, "blob.bin"), "unsupported-file")
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("非 UTF-8 文本 → unsupported-encoding", async () => {
  const root = makeRoot("za38-prev-enc-")
  try {
    // GBK 常见双字节序列不是合法 UTF-8。
    writeFileSync(join(root, "gbk.txt"), Buffer.from([0xc4, 0xe3, 0xba, 0xc3])) // "你好" GBK
    await expectErrorCode(readPreview(root, "gbk.txt"), "unsupported-encoding")
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("目录 → not-file", async () => {
  const root = makeRoot("za38-prev-dir-")
  try {
    await expectErrorCode(readPreview(root, "."), "not-file")
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("读取期间文件变化：自动重试，稳定后返回新内容", async () => {
  const root = makeRoot("za38-prev-change-")
  try {
    const filePath = join(root, "mutate.txt")
    writeFileSync(filePath, "v1")
    const first = await readPreview(root, "mutate.txt")
    expect(first.content).toBe("v1")
    // 内容增长 → stat 指纹变化 → 重试读取新内容。
    appendFileSync(filePath, " + more")
    const second = await readPreview(root, "mutate.txt")
    expect(second.content).toBe("v1 + more")
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("PreviewCache：命中刷新 LRU、容量淘汰与 invalidate", () => {
  const cache = new PreviewCache()
  const makePreview = (path: string, index: number): WorkspaceFilePreview => ({
    path,
    name: path,
    content: "c".repeat(100),
    language: null,
    sizeBytes: 100,
    lineCount: 1,
    modifiedAtMs: index,
    truncated: false,
    version: `v${index}`,
  })

  // 条目数上限淘汰最久未使用。
  for (let i = 0; i < PREVIEW_CACHE_LIMIT + 2; i++) {
    cache.put(`f${i}`, `v${i}`, makePreview(`f${i}`, i))
  }
  expect(cache.size).toBe(PREVIEW_CACHE_LIMIT)
  expect(cache.get("f0")).toBeUndefined() // 最旧已淘汰
  expect(cache.get(`f${PREVIEW_CACHE_LIMIT + 1}`)).toBeDefined()

  // 命中刷新：再次 get 的条目变成最旧后逐出的是它之后的条目。
  cache.get("f3")
  cache.put("overflow", "v-x", makePreview("overflow", 999))
  expect(cache.size).toBe(PREVIEW_CACHE_LIMIT)
  expect(cache.get("f3")).toBeDefined()

  // 字节预算：大内容触发字节淘汰，但至少保留一条避免空转。
  const big = makePreview("huge", 1)
  big.content = "z".repeat(PREVIEW_CACHE_BYTES + 100)
  cache.put("huge", "v-huge", big)
  expect(cache.size).toBe(1)
  expect(cache.get("huge")).toBeDefined()
  // 再放小条目 → 字节超限继续淘汰最旧（huge），保留新条目。
  cache.put("small", "v-s", makePreview("small", 1))
  expect(cache.get("huge")).toBeUndefined()
  expect(cache.size).toBe(1)

  // invalidate：该路径全部版本失效。
  cache.put("keep", "v1", makePreview("keep", 1))
  cache.put("keep", "v2", makePreview("keep", 2))
  cache.invalidate("keep")
  expect(cache.get("keep")).toBeUndefined()
  expect(cache.get("small")).toBeDefined()
})
