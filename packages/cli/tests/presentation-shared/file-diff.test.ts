/** 文件审批 unified diff parser 与 split 对齐的纯函数测试。 */

import { expect, test } from "bun:test"

import { alignFileDiffHunk, diffTextForRenderer, parseFileDiff } from "../../src/presentation-shared/file-diff"
import { resolveLanguageForPath } from "../../src/presentation-shared/language-catalog"

test("解析多个 hunk、行号与 no-newline marker", () => {
  const parsed = parseFileDiff([
    "--- /a.py",
    "+++ /a.py",
    "@@ -1,2 +1,2 @@",
    " keep",
    "-old",
    "+new",
    "\\ No newline at end of file",
    "@@ -8 +8,2 @@",
    "-tail",
    "+next",
    "+tail",
  ].join("\n"))

  expect(parsed.status).toBe("parsed")
  if (parsed.status !== "parsed") return
  expect(parsed.hunks).toHaveLength(2)
  expect(parsed.hunks[0]?.lines).toEqual([
    { kind: "context", text: "keep", oldLine: 1, newLine: 1 },
    { kind: "remove", text: "old", oldLine: 2, newLine: null },
    { kind: "add", text: "new", oldLine: null, newLine: 2 },
    { kind: "no-newline", text: "\\ No newline at end of file", oldLine: null, newLine: null },
  ])
})

test("split 只在同一 change block 内按最大长度对齐", () => {
  const parsed = parseFileDiff([
    "--- /a.ts",
    "+++ /a.ts",
    "@@ -1,3 +1,4 @@",
    "-one",
    "-two",
    "+first",
    "+second",
    "+third",
    " keep",
  ].join("\n"))
  if (parsed.status !== "parsed") throw new Error(parsed.reason)

  const rows = alignFileDiffHunk(parsed.hunks[0]!)

  expect(rows).toHaveLength(4)
  expect(rows[0]?.left?.text).toBe("one")
  expect(rows[0]?.right?.text).toBe("first")
  expect(rows[2]?.left).toBeNull()
  expect(rows[2]?.right?.text).toBe("third")
  expect(rows[3]?.left?.kind).toBe("context")
  expect(rows[3]?.right?.kind).toBe("context")
})

test("截断预览会移除 marker 并按可见行修正原生 renderer 的 hunk 计数", () => {
  const diff = "--- /a.ts\n+++ /a.ts\n@@ -1,400 +1,500 @@ function demo\n-old\n+new\n[diff 因行数或字节上限截断]"
  expect(parseFileDiff(diff).status).toBe("parsed")
  expect(diffTextForRenderer(diff)).toBe(
    "--- /a.ts\n+++ /a.ts\n@@ -1,1 +1,1 @@ function demo\n-old\n+new",
  )
})

test("大文件创建的截断 hunk 使用实际可见新行数", () => {
  const diff = "--- /dev/null\n+++ /large.ts\n@@ -0,0 +1,269 @@\n+first\n+second\n[diff 因行数或字节上限截断]"
  expect(diffTextForRenderer(diff)).toContain("@@ -0,0 +1,2 @@")
})

test("空 diff 合法，畸形行安全返回 invalid", () => {
  expect(parseFileDiff("")).toEqual({ status: "parsed", hunks: [] })
  expect(parseFileDiff("not a diff")).toEqual({ status: "invalid", reason: "missing-hunk-header" })
  expect(parseFileDiff("@@ -1 +1 @@\n?bad")).toEqual({ status: "invalid", reason: "invalid-hunk-line" })
})

test("逻辑路径按最后扩展名解析双端语言", () => {
  expect(resolveLanguageForPath("/src/app.tsx").canonical).toBe("tsx")
  expect(resolveLanguageForPath("/src/archive.d.ts").canonical).toBe("typescript")
  expect(resolveLanguageForPath("/src/README").canonical).toBe("plaintext")
  expect(resolveLanguageForPath("/src/.env").canonical).toBe("plaintext")
})
