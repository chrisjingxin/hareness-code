/** 工作区提及解析器测试：文件读取、行号切片、128k 保守预算门禁与安全防御。 */

import { expect, describe, test, beforeAll, afterAll } from "bun:test"
import { mkdir, writeFile, rm, realpath, truncate } from "node:fs/promises"
import { join } from "node:path"
import { tmpdir } from "node:os"
import {
  MAX_MENTION_SLICE_SOURCE_BYTES,
  resolveMentions,
} from "../../src/workspace/mention-resolver"

describe("resolveMentions", () => {
  let testDir: string

  beforeAll(async () => {
    const rawDir = join(tmpdir(), `harness-mention-test-${Date.now()}`)
    await mkdir(rawDir, { recursive: true })
    testDir = await realpath(rawDir)

    // 1. 常规小文件 (约 10 行)
    const smallLines = Array.from({ length: 10 }, (_, i) => `console.log("line ${i + 1}")`).join("\n")
    await writeFile(join(testDir, "small.ts"), smallLines, "utf8")

    // 2. 中等文件 (100 行，每行约 30 字节)
    const mediumLines = Array.from({ length: 100 }, (_, i) => `export const item_${i + 1} = { id: ${i + 1}, name: "test" };`).join("\n")
    await writeFile(join(testDir, "medium.ts"), mediumLines, "utf8")

    // 3. 超大文件 (> 15KB)
    const bigLines = Array.from({ length: 500 }, (_, i) => `// Big file content line ${i + 1} with repeated padding data string`).join("\n")
    await writeFile(join(testDir, "big.ts"), bigLines, "utf8")

    // 4. 黑名单文件 (.lock)
    await writeFile(join(testDir, "pnpm-lock.yaml"), "lockfileVersion: '9.0'\nimporters: {}", "utf8")

    // 5. 带空格路径文件
    await mkdir(join(testDir, "docs"), { recursive: true })
    await writeFile(join(testDir, "docs", "my guide.md"), "# Guide\nThis is my guide.", "utf8")
    await writeFile(join(testDir, "docs", "fenced.md"), "# Example\n```ts\nconst value = 1\n```", "utf8")

    // 6. 未知扩展名二进制、精确边界与累计预算 fixture
    await writeFile(join(testDir, "payload.dat"), Buffer.from([0, 1, 2, 3, 255, 254, 253]))
    await writeFile(join(testDir, "exact-10kb.txt"), "x".repeat(10 * 1024), "utf8")
    await writeFile(join(testDir, "exact-300-lines.txt"), Array.from({ length: 300 }, () => "x").join("\n"), "utf8")
    for (const name of ["budget-a.txt", "budget-b.txt", "budget-c.txt"]) {
      await writeFile(join(testDir, name), "x".repeat(8 * 1024), "utf8")
    }

    // 7. 稀疏超大源文件：显式切片也必须在整体读取前受有界源文件门禁保护。
    const oversizedSliceSource = join(testDir, "oversized-slice-source.txt")
    await writeFile(oversizedSliceSource, "first\nsecond\n", "utf8")
    await truncate(oversizedSliceSource, MAX_MENTION_SLICE_SOURCE_BYTES + 1)
  })

  afterAll(async () => {
    await rm(testDir, { recursive: true, force: true })
  })

  test("无提及文本返回原文本，附加上下文为空", async () => {
    const res = await resolveMentions(testDir, "你好，请帮我写一段代码")
    expect(res.inlinedCount).toBe(0)
    expect(res.referenceCount).toBe(0)
    expect(res.contextBlock).toBe("")
    expect(res.prompt).toBe("你好，请帮我写一段代码")
  })

  test("小文件正常全文内联注入", async () => {
    const res = await resolveMentions(testDir, "分析 @small.ts")
    expect(res.inlinedCount).toBe(1)
    expect(res.referenceCount).toBe(0)
    expect(res.contextBlock).toContain("[Attached Context: small.ts (10 lines")
    expect(res.contextBlock).toContain("console.log(\"line 1\")")
    expect(res.prompt).toContain("[Attached Context: small.ts")
    expect(res.prompt).toContain("分析 @small.ts")
  })

  test("行号切片 (#L2-4) 精确内联指定行并标注总行数", async () => {
    const res = await resolveMentions(testDir, "解释 @small.ts#L2-4")
    expect(res.inlinedCount).toBe(1)
    expect(res.contextBlock).toContain("[Attached Context: small.ts (lines 2-4 of 10)]")
    expect(res.contextBlock).toContain("console.log(\"line 2\")")
    expect(res.contextBlock).toContain("console.log(\"line 4\")")
    expect(res.contextBlock).not.toContain("console.log(\"line 1\")")
    expect(res.contextBlock).not.toContain("console.log(\"line 5\")")
  })

  test("超出行号自动夹取到文件末行", async () => {
    const res = await resolveMentions(testDir, "解释 @small.ts#L8-50")
    expect(res.inlinedCount).toBe(1)
    expect(res.contextBlock).toContain("[Attached Context: small.ts (lines 8-10 of 10)]")
  })

  test("超过 10KB / 300 行的超大文件自动降级为占位引用", async () => {
    const res = await resolveMentions(testDir, "查看 @big.ts")
    expect(res.inlinedCount).toBe(0)
    expect(res.referenceCount).toBe(1)
    expect(res.contextBlock).toContain("[Mentioned File: big.ts (Size:")
    expect(res.contextBlock).toContain("too large to inline. Use read_file to inspect")
  })

  test("大文件指定小范围切片仍可内联", async () => {
    const res = await resolveMentions(testDir, "查看 @big.ts#L10-20")
    expect(res.inlinedCount).toBe(1)
    expect(res.referenceCount).toBe(0)
    expect(res.contextBlock).toContain("[Attached Context: big.ts (lines 10-20 of 500)]")
  })

  test("显式行号切片不会整体读取超过安全上限的源文件", async () => {
    const res = await resolveMentions(testDir, "查看 @oversized-slice-source.txt#L1-2")
    expect(res.inlinedCount).toBe(0)
    expect(res.referenceCount).toBe(1)
    expect(res.resolved[0]).toMatchObject({
      kind: "reference",
      reason: "too-large",
      sizeBytes: MAX_MENTION_SLICE_SOURCE_BYTES + 1,
    })
  })

  test("黑名单文件 (.lock) 自动降级为占位引用", async () => {
    const res = await resolveMentions(testDir, "检查 @pnpm-lock.yaml")
    expect(res.inlinedCount).toBe(0)
    expect(res.referenceCount).toBe(1)
    expect(res.contextBlock).toContain("[Mentioned File: pnpm-lock.yaml")
  })

  test("未知扩展名二进制文件降级为 binary 引用", async () => {
    const res = await resolveMentions(testDir, "检查 @payload.dat")
    expect(res.inlinedCount).toBe(0)
    expect(res.referenceCount).toBe(1)
    expect(res.resolved[0]).toMatchObject({ kind: "reference", reason: "binary" })
  })

  test("恰好 10KB 或 300 行的全文按 Task 边界降级", async () => {
    const res = await resolveMentions(testDir, "检查 @exact-10kb.txt @exact-300-lines.txt")
    expect(res.inlinedCount).toBe(0)
    expect(res.referenceCount).toBe(2)
    expect(res.resolved.map(item => item.reason)).toEqual(["too-large", "too-large"])
  })

  test("多文件累计超过 20KB 时后续文件降级", async () => {
    const res = await resolveMentions(testDir, "检查 @budget-a.txt @budget-b.txt @budget-c.txt")
    expect(res.inlinedCount).toBe(2)
    expect(res.referenceCount).toBe(1)
    expect(res.totalBytes).toBe(16 * 1024)
    expect(res.resolved[2]).toMatchObject({ kind: "reference", reason: "budget-exceeded" })
  })

  test("带空格路径 (@\"docs/my guide.md\") 正确解析与内联", async () => {
    const res = await resolveMentions(testDir, '查看 @"docs/my guide.md"')
    expect(res.inlinedCount).toBe(1)
    expect(res.contextBlock).toContain("[Attached Context: docs/my guide.md")
    expect(res.contextBlock).toContain("# Guide")
  })

  test("文件内容含 Markdown 代码围栏时使用更长外层 fence 保持上下文边界", async () => {
    const res = await resolveMentions(testDir, "查看 @docs/fenced.md")
    expect(res.contextBlock).toContain("````markdown\n# Example\n```ts")
    expect(res.contextBlock).toEndWith("```\n````")
  })

  test("越界路径 (../) 或不存在的文件静默忽略不中断", async () => {
    const res = await resolveMentions(testDir, "查看 @../../etc/passwd 和 @nonexistent.txt")
    expect(res.inlinedCount).toBe(0)
    expect(res.referenceCount).toBe(0)
    expect(res.contextBlock).toBe("")
    expect(res.prompt).toBe("查看 @../../etc/passwd 和 @nonexistent.txt")
  })
})
