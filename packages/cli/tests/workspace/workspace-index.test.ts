/** workspace-index：Git 全量树（含子目录工作区）、非 Git 懒加载、排序与截断。 */

import { expect, test } from "bun:test"
import { execFileSync } from "node:child_process"
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { insertChildren, listDirectory, loadGitTree, MAX_DIR_NODES, MAX_TREE_FILES, sortRows, toggleRow, type TreeLoadResult } from "../../src/workspace/workspace-index"
import type { WorkspaceTreeRow } from "../../src/workspace/types"

/** 在临时目录里执行 git；测试进程本身运行在 git 仓库内，git 必然存在。 */
function runGit(cwd: string, args: string[]): void {
  execFileSync("git", args, { cwd, stdio: "ignore" })
}

/** 初始化仓库并提交给定文件（相对仓库根的路径 → 内容）。 */
function initRepository(prefix: string, files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  runGit(dir, ["init", "-q"])
  for (const [relativePath, content] of Object.entries(files)) {
    const fullPath = join(dir, relativePath)
    mkdirSync(join(fullPath, ".."), { recursive: true })
    writeFileSync(fullPath, content)
  }
  runGit(dir, ["add", "-A"])
  runGit(dir, ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"])
  runGit(dir, ["branch", "-M", "main"])
  return dir
}

function rowsOf(result: TreeLoadResult | null): WorkspaceTreeRow[] {
  if (result === null) throw new Error("expected git tree result")
  return [...result.rows]
}

test("loadGitTree：tracked 文件 + 推导祖先目录行，gitignore 内文件不出现", async () => {
  // .gitignore 必须在 add/commit 之前写入，才能验证"未跟踪且被忽略"的文件不出现。
  const dir = mkdtempSync(join(tmpdir(), "za38-ws-git-"))
  runGit(dir, ["init", "-q"])
  writeFileSync(join(dir, ".gitignore"), "ignored.log\n")
  for (const [relativePath, content] of Object.entries({ "src/a.ts": "a", "src/deep/b.ts": "b", "README.md": "readme", "ignored.log": "secret" })) {
    const fullPath = join(dir, relativePath)
    mkdirSync(join(fullPath, ".."), { recursive: true })
    writeFileSync(fullPath, content)
  }
  runGit(dir, ["add", "-A"])
  runGit(dir, ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"])
  runGit(dir, ["branch", "-M", "main"])
  try {
    const rows = rowsOf(await loadGitTree(dir))
    const paths = rows.map(row => row.path)
    expect(paths).toContain("src")
    expect(paths).toContain("src/deep")
    expect(paths).toContain("src/a.ts")
    expect(paths).toContain("src/deep/b.ts")
    expect(paths).toContain("README.md")
    expect(paths).not.toContain("ignored.log")
    const srcRow = rows.find(row => row.path === "src")!
    expect(srcRow).toMatchObject({ kind: "directory", depth: 0, expanded: false, hasChildren: true, loading: false })
    const fileRow = rows.find(row => row.path === "src/deep/b.ts")!
    expect(fileRow).toMatchObject({ kind: "file", depth: 2 })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("loadGitTree：子目录工作区输出相对该子目录，不泄漏仓库外文件", async () => {
  const dir = initRepository("za38-ws-git-sub-", {
    "packages/cli/src/index.ts": "cli",
    "packages/agent/main.py": "agent",
    "top.txt": "top",
  })
  try {
    const subdir = join(dir, "packages", "cli")
    const rows = rowsOf(await loadGitTree(subdir))
    const paths = rows.map(row => row.path)
    expect(paths).toContain("src")
    expect(paths).toContain("src/index.ts")
    expect(paths).not.toContain("top.txt")
    expect(paths).not.toContain("packages")
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("loadGitTree：非 git 目录返回 null", async () => {
  const dir = mkdtempSync(join(tmpdir(), "za38-ws-git-null-"))
  try {
    expect(await loadGitTree(dir)).toBeNull()
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("listDirectory：忽略清单不出现、目录优先排序、symlink 归类", async () => {
  const root = mkdtempSync(join(tmpdir(), "za38-ws-list-"))
  try {
    mkdirSync(join(root, "src"), { recursive: true })
    mkdirSync(join(root, "node_modules"), { recursive: true })
    mkdirSync(join(root, ".venv"), { recursive: true })
    mkdirSync(join(root, "dist"), { recursive: true })
    writeFileSync(join(root, "b.txt"), "b")
    writeFileSync(join(root, "a.ts"), "a")
    symlinkSync(join(root, "a.ts"), join(root, "link-a"))

    const { rows, limited } = await listDirectory(root, "")
    expect(limited).toBe(false)
    const names = rows.map(row => row.name)
    expect(names).not.toContain("node_modules")
    expect(names).not.toContain(".venv")
    expect(names).not.toContain("dist")
    // 目录优先：src 排在所有文件与 symlink 前
    expect(names[0]).toBe("src")
    const symlinkRow = rows.find(row => row.name === "link-a")!
    expect(symlinkRow.kind).toBe("symlink")
    expect(symlinkRow.hasChildren).toBe(false)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test("sortRows：目录 → 文件 → symlink，名称 zh 数字感知排序；树序父先于子", () => {
  const rows: WorkspaceTreeRow[] = [
    { path: "a2", name: "a2", kind: "file", depth: 0, expanded: false, loading: false, hasChildren: false },
    { path: "a10", name: "a10", kind: "file", depth: 0, expanded: false, loading: false, hasChildren: false },
    { path: "a", name: "a", kind: "directory", depth: 0, expanded: false, loading: false, hasChildren: true },
    { path: "a/z", name: "z", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
    { path: "a/b", name: "b", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
    { path: "a/link", name: "link", kind: "symlink", depth: 1, expanded: false, loading: false, hasChildren: false },
    { path: "b", name: "b", kind: "directory", depth: 0, expanded: false, loading: false, hasChildren: true },
    { path: "b/x", name: "x", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
  ]
  const sorted = sortRows(rows).map(row => row.path)
  // 树序：顶层目录先于顶层文件；目录 a 先于其后代；数字感知：a2 在 a10 前；symlink 排在该层文件后。
  expect(sorted).toEqual(["a", "a/b", "a/z", "a/link", "b", "b/x", "a2", "a10"])
})

test("toggleRow：immutable 切换 expanded；文件行不切换", () => {
  const rows: WorkspaceTreeRow[] = [
    { path: "a", name: "a", kind: "directory", depth: 0, expanded: false, loading: false, hasChildren: true },
    { path: "b", name: "b", kind: "file", depth: 0, expanded: false, loading: false, hasChildren: false },
  ]
  const toggled = toggleRow(rows, "a", true)
  expect(toggled[0]!.expanded).toBe(true)
  expect(rows[0]!.expanded).toBe(false) // 原数组不变
  expect(toggleRow(rows, "b", true)).toBe(rows) // 文件行原样返回
})

test("insertChildren：子行插入父行之后，保持传入顺序", () => {
  const rows: WorkspaceTreeRow[] = [
    { path: "a", name: "a", kind: "directory", depth: 0, expanded: true, loading: false, hasChildren: true },
    { path: "b", name: "b", kind: "file", depth: 0, expanded: false, loading: false, hasChildren: false },
  ]
  const children: WorkspaceTreeRow[] = [
    { path: "a/1", name: "1", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
    { path: "a/2", name: "2", kind: "file", depth: 1, expanded: false, loading: false, hasChildren: false },
  ]
  expect(insertChildren(rows, "a", children).map(row => row.path)).toEqual(["a", "a/1", "a/2", "b"])
  expect(insertChildren(rows, "nope", children)).toBe(rows)
})

test("超限截断：单目录节点数超 MAX_DIR_NODES 时 limited=true 且保留前部内容", async () => {
  const root = mkdtempSync(join(tmpdir(), "za38-ws-limit-"))
  try {
    for (let i = 0; i < MAX_DIR_NODES + 1; i++) {
      mkdirSync(join(root, `d${String(i).padStart(4, "0")}`))
    }
    const { rows, limited } = await listDirectory(root, "")
    expect(limited).toBe(true)
    expect(rows.length).toBe(MAX_DIR_NODES)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
