/**
 * 工作区文件树构建：Git 全量树（免懒加载）与非 Git 目录懒加载两套来源。
 *
 * Git 树用 `git -C workspace ls-files` 相对输出（相对 cwd 天然相对 workspace，
 * 子目录工作区不会泄漏仓库外文件）；非 Git 走 readdir 并忽略常见依赖目录。
 * 排序统一在 CLI 完成（目录优先 → 名称 zh 数字排序），React 端不排序。
 */

import { execFile } from "node:child_process"
import { readdir } from "node:fs/promises"
import { promisify } from "node:util"
import path from "node:path"

import type { WorkspaceTreeRow } from "./types"
import { mapFsError, resolveWithinRoot } from "./path-policy"

const execFileAsync = promisify(execFile)

/** Git 全量树的文件行上限；超过即截断并置 limited。 */
export const MAX_TREE_FILES = 20_000
/** 非 Git 单目录的节点上限；超过即截断并置 limited。 */
export const MAX_DIR_NODES = 2_000

/** 非 Git 目录懒加载的忽略清单：依赖/构建产物，避免把噪音灌进文件树。 */
const IGNORED_DIRS: Record<string, boolean> = {
  ".git": true,
  node_modules: true,
  ".venv": true,
  __pycache__: true,
  ".cache": true,
  dist: true,
  build: true,
  coverage: true,
}

export type TreeLoadResult = {
  readonly rows: readonly WorkspaceTreeRow[]
  readonly limited: boolean
}

/** 从 git ls-files 输出构建全量树：文件行 + 推导的祖先目录行。 */
export async function loadGitTree(workspace: string): Promise<TreeLoadResult | null> {
  try {
    const { stdout } = await execFileAsync(
      "git",
      ["-C", workspace, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
      { encoding: "utf8", timeout: 5_000, windowsHide: true, maxBuffer: 16 * 1024 * 1024 },
    )
    const files = stdout.split("\0").filter(Boolean)
    if (files.length === 0) return { rows: [], limited: false }
    const rows = buildTreeRows(files)
    return trimToLimit(rows)
  } catch {
    // 非 git 仓库 / git 缺失 / 超时：统一返回 null，调用方回退非 Git 懒加载。
    return null
  }
}

/** 由 git 文件路径列表推导目录行，并按统一排序输出（全部 collapsed，免懒加载）。 */
function buildTreeRows(files: readonly string[]): WorkspaceTreeRow[] {
  const rows: WorkspaceTreeRow[] = []
  const seenDirs = new Set<string>()
  for (const file of files) {
    const segments = file.split("/")
    if (segments.length === 0) continue
    for (let i = 1; i < segments.length; i++) {
      const dirPath = segments.slice(0, i).join("/")
      if (seenDirs.has(dirPath)) continue
      seenDirs.add(dirPath)
      rows.push({
        path: dirPath,
        name: segments[i - 1]!,
        kind: "directory",
        depth: i - 1,
        expanded: false,
        loading: false,
        hasChildren: true,
      })
    }
    rows.push({
      path: file,
      name: segments.at(-1)!,
      kind: "file",
      depth: segments.length - 1,
      expanded: false,
      loading: false,
      hasChildren: false,
    })
  }
  return sortRows(rows)
}

/** 非 Git 懒加载单目录：忽略清单过滤 + symlink 归类 + 排序；relativePath 为空表示根。 */
export async function listDirectory(root: string, relativePath: string): Promise<TreeLoadResult> {
  // 根本身已经过 realpath 校验；子目录经 resolveWithinRoot 防 symlink 越界。
  const target = relativePath === "" ? root : await resolveWithinRoot(root, relativePath)
  let entries
  try {
    entries = await readdir(target, { withFileTypes: true })
  } catch (error) {
    throw mapFsError(error)
  }
  const rows: WorkspaceTreeRow[] = []
  for (const entry of entries) {
    if (entry.isDirectory() && IGNORED_DIRS[entry.name]) continue
    const rowPath = relativePath === "" ? entry.name : `${relativePath}/${entry.name}`
    if (entry.isSymbolicLink()) {
      // 目录 symlink 不展开（hasChildren: false）；文件 symlink 仍可经 resolveWithinRoot 预览。
      rows.push({ path: rowPath, name: entry.name, kind: "symlink", depth: segmentCount(rowPath) - 1, expanded: false, loading: false, hasChildren: false })
      continue
    }
    if (entry.isDirectory()) {
      rows.push({ path: rowPath, name: entry.name, kind: "directory", depth: segmentCount(rowPath) - 1, expanded: false, loading: false, hasChildren: true })
    } else {
      rows.push({ path: rowPath, name: entry.name, kind: "file", depth: segmentCount(rowPath) - 1, expanded: false, loading: false, hasChildren: false })
    }
  }
  return trimToLimit(sortRows(rows), MAX_DIR_NODES)
}

/** 按目录层级做树序（parent 先于其全部后代），同一层级内目录 → 文件 → symlink，再按 zh 数字感知字典序。 */
export function sortRows(rows: readonly WorkspaceTreeRow[]): WorkspaceTreeRow[] {
  const byParent = new Map<string, WorkspaceTreeRow[]>()
  for (const row of rows) {
    const parent = parentPathOf(row.path)
    const bucket = byParent.get(parent)
    if (bucket) bucket.push(row)
    else byParent.set(parent, [row])
  }
  const sorted: WorkspaceTreeRow[] = []
  const visited = new Set<string>()
  const visit = (parent: string): void => {
    if (visited.has(parent)) return
    visited.add(parent)
    const children = byParent.get(parent)
    if (!children) return
    children.sort(compareSiblings)
    for (const child of children) {
      sorted.push(child)
      if (child.kind === "directory") visit(child.path)
    }
  }
  visit("")
  // 单层懒加载列表的父目录不在 rows 中，从根遍历覆盖不到；兜底遍历其余父桶。
  for (const parent of byParent.keys()) {
    if (!visited.has(parent)) visit(parent)
  }
  return sorted
}

const KIND_RANK: Record<WorkspaceTreeRow["kind"], number> = { directory: 0, file: 1, symlink: 2 }

/** 同层兄弟比较：目录 → 文件 → symlink，再按名称排序。 */
function compareSiblings(a: WorkspaceTreeRow, b: WorkspaceTreeRow): number {
  const rankDiff = KIND_RANK[a.kind] - KIND_RANK[b.kind]
  if (rankDiff !== 0) return rankDiff
  return a.name.localeCompare(b.name, "zh", { numeric: true, sensitivity: "base" })
}

/** 父目录路径：`a/b` → `a`，`a` → ``。 */
function parentPathOf(relativePath: string): string {
  const index = relativePath.lastIndexOf("/")
  return index === -1 ? "" : relativePath.slice(0, index)
}

/** 切换目录行的 expanded（immutable）；文件/不存在行原样返回。 */
export function toggleRow(rows: readonly WorkspaceTreeRow[], pathToToggle: string, expanded: boolean): readonly WorkspaceTreeRow[] {
  let changed = false
  const next = rows.map(row => {
    if (row.path !== pathToToggle || row.kind !== "directory" || row.expanded === expanded) return row
    changed = true
    return { ...row, expanded, loading: false }
  })
  return changed ? next : rows
}

/** 展开后把子行插入父行之后（children 已按统一排序）。 */
export function insertChildren(rows: readonly WorkspaceTreeRow[], parentPath: string, children: readonly WorkspaceTreeRow[]): readonly WorkspaceTreeRow[] {
  if (children.length === 0) return rows
  const index = rows.findIndex(row => row.path === parentPath)
  if (index === -1) return rows
  const next = rows.slice()
  next.splice(index + 1, 0, ...children)
  return next
}

/** 按上限截断并返回 limited 标记；未超限时原样返回。 */
function trimToLimit(rows: readonly WorkspaceTreeRow[], limit = MAX_TREE_FILES): TreeLoadResult {
  if (rows.length <= limit) return { rows, limited: false }
  return { rows: rows.slice(0, limit), limited: true }
}

/** 路径段数：空串为 0，`a/b` 为 2。 */
function segmentCount(relativePath: string): number {
  if (relativePath === "") return 0
  return relativePath.split("/").length
}
