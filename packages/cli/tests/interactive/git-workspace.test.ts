/** detectGitWorkspace：真实 git 仓库下区分 branch / detached / not-repository / unavailable。 */

import { expect, test } from "bun:test"
import { execFileSync } from "node:child_process"
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { detectGitChangedFiles, detectGitWorkspace } from "../../src/infrastructure/git-workspace"

/** 在临时目录里执行 git；测试进程本身运行在 git 仓库内，git 必然存在。 */
function runGit(cwd: string, args: string[]): void {
  execFileSync("git", args, { cwd, stdio: "ignore" })
}

/** 初始化一个固定 main 分支、含一次空提交的仓库，返回临时目录。 */
function initRepository(prefix: string): string {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  runGit(dir, ["init", "-q"])
  runGit(dir, ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"])
  runGit(dir, ["branch", "-M", "main"])
  return dir
}

test("branch 仓库返回分支名与仓库根", async () => {
  const dir = initRepository("za38-git-branch-")
  try {
    const state = await detectGitWorkspace(dir)
    expect(state).toEqual({ kind: "branch", branch: "main", root: realpathSync(dir) })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("detached HEAD 返回短 SHA 与仓库根", async () => {
  const dir = initRepository("za38-git-detached-")
  try {
    runGit(dir, ["checkout", "-q", "--detach"])
    const state = await detectGitWorkspace(dir)
    expect(state.kind).toBe("detached")
    if (state.kind === "detached") {
      expect(state.shortSha).toMatch(/^[0-9a-f]{7,}$/)
      expect(state.root).toBe(realpathSync(dir))
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("空目录返回 not-repository", async () => {
  const dir = mkdtempSync(join(tmpdir(), "za38-git-norepo-"))
  try {
    expect(await detectGitWorkspace(dir)).toEqual({ kind: "not-repository" })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("不存在的目录返回 unavailable", async () => {
  const missing = join(tmpdir(), "za38-git-missing")
  rmSync(missing, { recursive: true, force: true })
  const state = await detectGitWorkspace(missing)
  expect(state.kind).toBe("unavailable")
})

test("Git 变更列表包含路径与状态，非 Git 返回 null", async () => {
  const dir = initRepository("za38-git-changes-")
  const plainDir = mkdtempSync(join(tmpdir(), "za38-git-changes-plain-"))
  try {
    writeFileSync(join(dir, "tracked.txt"), "before\n")
    writeFileSync(join(dir, "renamed-before.txt"), "rename\n")
    runGit(dir, ["add", "tracked.txt", "renamed-before.txt"])
    runGit(dir, ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "tracked"])
    writeFileSync(join(dir, "tracked.txt"), "after\n")
    writeFileSync(join(dir, "untracked.txt"), "new\n")
    runGit(dir, ["mv", "renamed-before.txt", "renamed-after.txt"])
    writeFileSync(join(dir, "renamed-after.txt"), "rename\nadded\n")

    expect(await detectGitChangedFiles(dir)).toEqual([
      { path: "renamed-after.txt", status: "renamed", addedLines: 1, removedLines: 0 },
      { path: "tracked.txt", status: "modified", addedLines: 1, removedLines: 1 },
      { path: "untracked.txt", status: "untracked", addedLines: 1, removedLines: 0 },
    ])
    expect(await detectGitChangedFiles(plainDir)).toBeNull()
  } finally {
    rmSync(dir, { recursive: true, force: true })
    rmSync(plainDir, { recursive: true, force: true })
  }
})
