/** explorer：load 幂等、Git/非 Git 树、懒加载展开、预览状态机、刷新保留展开、generation 门禁。 */

import { expect, test } from "bun:test"
import { execFileSync } from "node:child_process"
import { mkdtempSync, mkdirSync, realpathSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { createWorkspaceExplorer } from "../../src/workspace/explorer"
import type { WorkspaceExplorer, WorkspaceSnapshot } from "../../src/workspace/types"

function runGit(cwd: string, args: string[]): void {
  execFileSync("git", args, { cwd, stdio: "ignore" })
}

function initRepository(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "za38-exp-git-"))
  runGit(dir, ["init", "-q"])
  for (const [relativePath, content] of Object.entries(files)) {
    const fullPath = join(dir, relativePath)
    mkdirSync(join(fullPath, ".."), { recursive: true })
    writeFileSync(fullPath, content)
  }
  runGit(dir, ["add", "-A"])
  runGit(dir, ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"])
  runGit(dir, ["branch", "-M", "main"])
  return realpathSync(dir)
}

/** 返回 realpath 后的非 Git 临时根（macOS /var → /private/var 符号链接）。 */
function nonGitRoot(): string {
  return realpathSync(mkdtempSync(join(tmpdir(), "za38-exp-dir-")))
}

/** 订阅 explorer 并返回发布计数。 */
function countPublishes(explorer: WorkspaceExplorer): { count: () => number; snapshots: WorkspaceSnapshot[] } {
  const snapshots: WorkspaceSnapshot[] = []
  const unsubscribe = explorer.subscribe(snapshot => snapshots.push(snapshot))
  return {
    count: () => snapshots.length,
    snapshots,
    // 测试结束前解绑由外层 close() 清理；此引用仅供断言使用。
    unsubscribe,
  }
}

test("load：idle 首次刷新，二次 accepted 幂等不重刷", async () => {
  const dir = nonGitRoot()
  try {
    writeFileSync(join(dir, "a.txt"), "a")
    const explorer = await createWorkspaceExplorer(dir)
    const observer = countPublishes(explorer)
    try {
      const first = await explorer.dispatch({ type: "workspace.load" })
      expect(first).toEqual({ status: "accepted" })
      const afterFirst = observer.count()
      expect(afterFirst).toBeGreaterThan(0)
      expect(explorer.getSnapshot().tree.status).toBe("ready")

      const second = await explorer.dispatch({ type: "workspace.load" })
      expect(second).toEqual({ status: "accepted" })
      expect(observer.count()).toBe(afterFirst) // 幂等：不再发布
      expect(explorer.getSnapshot().tree.rows.map(row => row.path)).toEqual(["a.txt"])
    } finally {
      await explorer.close()
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("Git 树：目录行推导 + 展开/收起切换 flag（全量树免懒加载）", async () => {
  const dir = initRepository({ "src/a.ts": "a", "src/b.ts": "b", "README.md": "r" })
  try {
    const explorer = await createWorkspaceExplorer(dir)
    try {
      await explorer.dispatch({ type: "workspace.load" })
      const tree = explorer.getSnapshot().tree
      expect(tree.status).toBe("ready")
      const src = tree.rows.find(row => row.path === "src")!
      expect(src).toMatchObject({ kind: "directory", depth: 0 })

      await explorer.dispatch({ type: "workspace.toggle-directory", path: "src" })
      expect(explorer.getSnapshot().tree.rows.find(row => row.path === "src")?.expanded).toBe(true)
      await explorer.dispatch({ type: "workspace.toggle-directory", path: "src" })
      expect(explorer.getSnapshot().tree.rows.find(row => row.path === "src")?.expanded).toBe(false)
    } finally {
      await explorer.close()
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("非 Git 树：toggle 懒加载子行，收起移除后代", async () => {
  const dir = nonGitRoot()
  try {
    mkdirSync(join(dir, "src"), { recursive: true })
    writeFileSync(join(dir, "src", "a.ts"), "a")
    writeFileSync(join(dir, "top.txt"), "t")
    const explorer = await createWorkspaceExplorer(dir)
    try {
      await explorer.dispatch({ type: "workspace.load" })
      expect(explorer.getSnapshot().tree.rows.map(row => row.path)).toEqual(["src", "top.txt"])

      await explorer.dispatch({ type: "workspace.toggle-directory", path: "src" })
      const expanded = explorer.getSnapshot().tree.rows.map(row => row.path)
      expect(expanded).toEqual(["src", "src/a.ts", "top.txt"])

      await explorer.dispatch({ type: "workspace.toggle-directory", path: "src" })
      expect(explorer.getSnapshot().tree.rows.map(row => row.path)).toEqual(["src", "top.txt"])
    } finally {
      await explorer.close()
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("toggle 非目录：rejected not-directory", async () => {
  const dir = nonGitRoot()
  try {
    writeFileSync(join(dir, "a.txt"), "a")
    const explorer = await createWorkspaceExplorer(dir)
    try {
      await explorer.dispatch({ type: "workspace.load" })
      const outcome = await explorer.dispatch({ type: "workspace.toggle-directory", path: "a.txt" })
      expect(outcome).toEqual({ status: "rejected", code: "not-directory", message: "目标不是目录" })
    } finally {
      await explorer.close()
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("preview-file：ready（language/lineCount）/ not-found / unsupported", async () => {
  const dir = nonGitRoot()
  try {
    writeFileSync(join(dir, "code.ts"), "const x = 1\nconst y = 2\n")
    writeFileSync(join(dir, "blob.bin"), Buffer.from([0x00, 0xff]))
    const explorer = await createWorkspaceExplorer(dir)
    try {
      await explorer.dispatch({ type: "workspace.load" })

      const ready = await explorer.dispatch({ type: "workspace.preview-file", path: "code.ts" })
      expect(ready).toEqual({ status: "accepted" })
      const preview = explorer.getSnapshot().preview
      expect(preview.status).toBe("ready")
      if (preview.status === "ready") {
        expect(preview.file.language).toBe("typescript")
        expect(preview.file.lineCount).toBe(2)
        expect(preview.file.content).toBe("const x = 1\nconst y = 2\n")
      }

      const missing = await explorer.dispatch({ type: "workspace.preview-file", path: "nope.ts" })
      expect(missing).toMatchObject({ status: "rejected", code: "not-found" })
      expect(explorer.getSnapshot().preview).toMatchObject({ status: "error", path: "nope.ts", code: "not-found" })

      const unsupported = await explorer.dispatch({ type: "workspace.preview-file", path: "blob.bin" })
      expect(unsupported).toMatchObject({ status: "rejected", code: "unsupported-file" })
      const unsupportedState = explorer.getSnapshot().preview
      expect(unsupportedState.status).toBe("unsupported")
      if (unsupportedState.status === "unsupported") {
        expect(unsupportedState.sizeBytes).toBe(2)
        expect(unsupportedState.reason).toContain("二进制")
      }
    } finally {
      await explorer.close()
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("refresh：新文件出现且展开状态保留", async () => {
  const dir = nonGitRoot()
  try {
    mkdirSync(join(dir, "src"), { recursive: true })
    writeFileSync(join(dir, "src", "a.ts"), "a")
    const explorer = await createWorkspaceExplorer(dir)
    try {
      await explorer.dispatch({ type: "workspace.load" })
      await explorer.dispatch({ type: "workspace.toggle-directory", path: "src" })
      expect(explorer.getSnapshot().tree.rows.map(row => row.path)).toEqual(["src", "src/a.ts"])

      writeFileSync(join(dir, "src", "b.ts"), "b")
      await explorer.dispatch({ type: "workspace.refresh" })
      const rows = explorer.getSnapshot().tree.rows.map(row => row.path)
      expect(rows).toContain("src/b.ts")
      expect(explorer.getSnapshot().tree.rows.find(row => row.path === "src")?.expanded).toBe(true)
      expect(rows).toContain("src/a.ts")
    } finally {
      await explorer.close()
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("generation 门禁：快速连续 preview 两个文件，后一个生效", async () => {
  const dir = nonGitRoot()
  try {
    writeFileSync(join(dir, "a.ts"), "aaa")
    writeFileSync(join(dir, "b.ts"), "bbbb")
    const explorer = await createWorkspaceExplorer(dir)
    try {
      // 不 await 第一个：两次 dispatch 同批在飞，generation 使旧结果丢弃。
      const first = explorer.dispatch({ type: "workspace.preview-file", path: "a.ts" })
      const second = explorer.dispatch({ type: "workspace.preview-file", path: "b.ts" })
      await Promise.all([first, second])
      const preview = explorer.getSnapshot().preview
      expect(preview.status).toBe("ready")
      if (preview.status === "ready") {
        expect(preview.file.path).toBe("b.ts")
      }
    } finally {
      await explorer.close()
    }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("根不可用：树进入 error 状态，不抛异常", async () => {
  const missing = join(tmpdir(), "za38-exp-missing-root")
  rmSync(missing, { recursive: true, force: true })
  const explorer = await createWorkspaceExplorer(missing)
  try {
    expect(explorer.getSnapshot().tree.status).toBe("error")
    expect(explorer.getSnapshot().tree.message).toBe("工作区路径不可用")
    const outcome = await explorer.dispatch({ type: "workspace.load" })
    expect(outcome.status).toBe("accepted") // 树已 error，load 幂等 accepted
  } finally {
    await explorer.close()
  }
})

test("close 后 dispatch 返回 rejected io-error", async () => {
  const dir = nonGitRoot()
  try {
    const explorer = await createWorkspaceExplorer(dir)
    await explorer.close()
    const outcome = await explorer.dispatch({ type: "workspace.refresh" })
    expect(outcome).toEqual({ status: "rejected", code: "io-error", message: "工作区浏览已关闭" })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
