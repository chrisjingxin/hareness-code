/** path-policy：路径安全校验、根内解析与 symlink 越界防护（真实临时目录）。 */

import { expect, test } from "bun:test"
import { mkdtempSync, mkdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { isSafeRelativePath, resolveWorkspaceRoot, resolveWithinRoot, WorkspaceError } from "../../src/workspace/path-policy"

/** 返回 realpath 后的临时根：macOS 上 /var 是 /private/var 的符号链接，必须统一真实路径。 */
function makeRoot(prefix: string): string {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  return realpathSync(dir)
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

test("isSafeRelativePath：接受普通相对路径", () => {
  expect(isSafeRelativePath("a")).toBe(true)
  expect(isSafeRelativePath("a/b/c.ts")).toBe(true)
  expect(isSafeRelativePath("目录/子目录/文件.txt")).toBe(true)
  expect(isSafeRelativePath(".hidden")).toBe(true)
})

test("isSafeRelativePath：拒绝空串/绝对路径/`..` 穿越/NUL/UNC 前缀", () => {
  expect(isSafeRelativePath("")).toBe(false)
  expect(isSafeRelativePath("/etc/passwd")).toBe(false)
  // `..` 在 normalize 后仍逃出根 → 拒绝
  expect(isSafeRelativePath("../a")).toBe(false)
  expect(isSafeRelativePath("..")).toBe(false)
  expect(isSafeRelativePath("a/../../x")).toBe(false)
  // `a/../b` normalize 为根内路径 `b`，不构成穿越 → 允许
  expect(isSafeRelativePath("a/../b")).toBe(true)
  expect(isSafeRelativePath("a\u0000b")).toBe(false)
  expect(isSafeRelativePath("\\\\server\\share")).toBe(false)
  expect(isSafeRelativePath("//etc")).toBe(false)
})

test("resolveWorkspaceRoot：返回 realpath", async () => {
  const root = makeRoot("za38-ws-root-")
  try {
    expect(await resolveWorkspaceRoot(root)).toBe(root)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("resolveWithinRoot：正常相对路径解析为根内真实路径", async () => {
  const root = makeRoot("za38-ws-normal-")
  try {
    mkdirSync(join(root, "src"), { recursive: true })
    writeFileSync(join(root, "src", "a.ts"), "x")
    expect(await resolveWithinRoot(root, "src/a.ts")).toBe(join(root, "src", "a.ts"))
    expect(await resolveWithinRoot(root, "src")).toBe(join(root, "src"))
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("resolveWithinRoot：`..` 穿越被拒绝（invalid-path）", async () => {
  const root = makeRoot("za38-ws-escape-")
  try {
    await expectErrorCode(resolveWithinRoot(root, "../outside"), "invalid-path")
    await expectErrorCode(resolveWithinRoot(root, "a/../../outside"), "invalid-path")
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("resolveWithinRoot：symlink 指向根外被拒绝（outside-workspace）", async () => {
  const root = makeRoot("za38-ws-linkout-")
  const outside = realpathSync(mkdtempSync(join(tmpdir(), "za38-ws-outside-")))
  try {
    symlinkSync(outside, join(root, "escape"))
    await expectErrorCode(resolveWithinRoot(root, "escape"), "outside-workspace")
  } finally {
    rmSync(root, { recursive: true, force: true })
    rmSync(outside, { recursive: true, force: true })
  }
})

test("resolveWithinRoot：根内 symlink 允许（解析到真实目标）", async () => {
  const root = makeRoot("za38-ws-linkin-")
  try {
    mkdirSync(join(root, "real"), { recursive: true })
    writeFileSync(join(root, "real", "a.ts"), "x")
    symlinkSync(join(root, "real"), join(root, "alias"))
    expect(await resolveWithinRoot(root, "alias/a.ts")).toBe(join(root, "real", "a.ts"))
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("resolveWithinRoot：不存在的路径 → not-found", async () => {
  const root = makeRoot("za38-ws-missing-")
  try {
    await expectErrorCode(resolveWithinRoot(root, "nope.txt"), "not-found")
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
