/**
 * WorkspaceExplorer：工作区文件树与文件预览的领域入口。
 *
 * 独立于 InteractiveController 与 TUI/Web 表现层，只通过 dispatch 受理意图、
 * 通过 subscribe 发布快照。树来源二选一：Git 全量树（免懒加载）或非 Git
 * 目录懒加载；generation 门禁保证刷新/快速切换时旧异步结果不会覆盖新状态
 *（先例：interactive/features/catalog-feature.ts 的 epoch 模式）。
 */

import { resolveWorkspaceRoot, workspaceError, WorkspaceError } from "./path-policy"
import { insertChildren, listDirectory, loadGitTree, toggleRow, type TreeLoadResult } from "./workspace-index"
import { readPreview, PreviewCache } from "./file-preview"
import type {
  WorkspaceExplorer,
  WorkspaceIntent,
  WorkspaceOutcome,
  WorkspacePreviewState,
  WorkspaceSnapshot,
  WorkspaceTreeRow,
  WorkspaceTreeState,
} from "./types"

/** 创建 WorkspaceExplorer；workspace 根解析失败时树进入 error 状态，不抛异常不阻断启动。 */
export async function createWorkspaceExplorer(workspace: string): Promise<WorkspaceExplorer> {
  const explorer = new WorkspaceExplorerImpl(workspace)
  await explorer.init()
  return explorer
}

class WorkspaceExplorerImpl implements WorkspaceExplorer {
  private readonly workspace: string
  private root: string | null = null
  private tree: WorkspaceTreeState = { status: "idle", rows: [], selectedPath: null, limited: false }
  private preview: WorkspacePreviewState = { status: "idle" }
  private treeGeneration = 0
  private previewGeneration = 0
  private gitTree = false
  private readonly cache = new PreviewCache()
  private readonly listeners = new Set<(snapshot: WorkspaceSnapshot) => void>()
  private closed = false

  constructor(workspace: string) {
    this.workspace = workspace
  }

  /** 解析固定根；失败 → 树 error（工作区路径不可用）。 */
  async init(): Promise<void> {
    try {
      this.root = await resolveWorkspaceRoot(this.workspace)
    } catch {
      this.tree = { status: "error", rows: [], selectedPath: null, limited: false, message: "工作区路径不可用" }
    }
  }

  getSnapshot(): WorkspaceSnapshot {
    return { tree: this.tree, preview: this.preview }
  }

  subscribe(listener: (snapshot: WorkspaceSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  async dispatch(intent: WorkspaceIntent): Promise<WorkspaceOutcome> {
    if (this.closed) {
      return { status: "rejected", code: "io-error", message: "工作区浏览已关闭" }
    }
    switch (intent.type) {
      case "workspace.load":
        return this.load()
      case "workspace.refresh":
        return this.refresh()
      case "workspace.toggle-directory":
        return this.toggleDirectory(intent.path)
      case "workspace.preview-file":
        return this.previewFile(intent.path)
      case "workspace.refresh-preview":
        return this.refreshPreview(intent.path)
    }
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.listeners.clear()
    this.cache.clear()
  }

  /** 首次加载：idle 时才刷新，已加载过幂等 accepted。 */
  private async load(): Promise<WorkspaceOutcome> {
    if (this.tree.status === "idle") {
      await this.refreshTree()
    }
    return { status: "accepted" }
  }

  private async refresh(): Promise<WorkspaceOutcome> {
    await this.refreshTree()
    return { status: "accepted" }
  }

  /** 重建树：Git 全量 / 非 Git 单层 + 恢复旧展开状态；generation 门禁丢弃过期结果。 */
  private async refreshTree(): Promise<void> {
    if (this.closed) return
    const generation = ++this.treeGeneration
    const previousRows = this.tree.rows
    const expandedPaths = previousRows
      .filter(row => row.kind === "directory" && row.expanded)
      .map(row => row.path)
    this.tree = { status: "loading", rows: previousRows, selectedPath: this.tree.selectedPath, limited: false }
    this.publish()

    if (this.root === null) {
      this.tree = { status: "error", rows: [], selectedPath: null, limited: false, message: "工作区路径不可用" }
      this.publish()
      return
    }

    // 极速探测：若本地根目录为空（0 项），直接毫秒级置为 ready，避免外部 git 扫描超时
    try {
      const topLevel = await listDirectory(this.root, "")
      if (topLevel.rows.length === 0) {
        if (this.closed || generation !== this.treeGeneration) return
        this.tree = { status: "ready", rows: [], selectedPath: null, limited: false }
        this.publish()
        return
      }
    } catch {
      // 忽略探测异常，继续正常分支
    }

    const git = await loadGitTree(this.workspace)
    if (this.closed || generation !== this.treeGeneration) return
    if (git !== null) {
      this.gitTree = true
      let rows: readonly WorkspaceTreeRow[] = [...git.rows]
      for (const dirPath of expandedPaths) {
        rows = toggleRow(rows, dirPath, true)
      }
      this.tree = { status: "ready", rows, selectedPath: this.tree.selectedPath, limited: git.limited }
      this.publish()
      return
    }

    // 非 Git：单层列表 + 按旧展开路径逐层恢复（父先子后，排序保证）。
    this.gitTree = false
    try {
      const { rows: topLevel, limited } = await listDirectory(this.root, "")
      if (this.closed || generation !== this.treeGeneration) return
      let rows: readonly WorkspaceTreeRow[] = [...topLevel]
      let currentPaths = new Set(rows.map(row => row.path))
      for (const dirPath of expandedPaths) {
        if (!currentPaths.has(dirPath)) continue
        rows = await this.expandDirectoryInto(rows, dirPath, generation)
        currentPaths = new Set(rows.map(row => row.path))
      }
      if (this.closed || generation !== this.treeGeneration) return
      this.tree = { status: "ready", rows, selectedPath: this.tree.selectedPath, limited }
      this.publish()
    } catch (error) {
      if (this.closed || generation !== this.treeGeneration) return
      const err = toWorkspaceError(error)
      this.tree = { status: "error", rows: this.tree.rows, selectedPath: null, limited: false, message: err.message }
      this.publish()
    }
  }

  /** 非 Git 懒加载单个目录并插入子行；generation 过期时丢弃结果。 */
  private async expandDirectoryInto(
    rows: readonly WorkspaceTreeRow[],
    dirPath: string,
    generation: number,
  ): Promise<readonly WorkspaceTreeRow[]> {
    if (this.root === null) return rows
    const loadingRows = markLoading(rows, dirPath, true)
    if (generation === this.treeGeneration) {
      this.tree = { ...this.tree, rows: loadingRows }
      this.publish()
    }
    let children: TreeLoadResult
    try {
      children = await listDirectory(this.root, dirPath)
    } catch (error) {
      const err = toWorkspaceError(error)
      const cleared = markLoading(rows, dirPath, false)
      if (generation === this.treeGeneration) {
        this.tree = { ...this.tree, status: "error", rows: cleared, message: err.message }
        this.publish()
      }
      return cleared
    }
    if (this.closed || generation !== this.treeGeneration) return rows
    // 基于本次重建的 rows 收尾：refresh 循环内部以局部列表为准，
    // 并发 toggle 在刷新展开在飞期间的小窗口变更由下次 toggle 重放覆盖（非破坏）。
    return toggleRow(markLoading(insertChildren(rows, dirPath, children.rows), dirPath, false), dirPath, true)
  }

  /** 目录展开/收起：Git 树切 flag；非 Git 懒加载子行（收起时移除后代行）。 */
  private async toggleDirectory(dirPath: string): Promise<WorkspaceOutcome> {
    const row = this.tree.rows.find(candidate => candidate.path === dirPath)
    if (!row || row.kind === "file") {
      return { status: "rejected", code: "not-directory", message: "目标不是目录" }
    }
    if (row.kind === "symlink" || !row.hasChildren) {
      // 目录 symlink 与空目录都不展开：无害 no-op。
      return { status: "accepted" }
    }

    if (this.gitTree) {
      this.tree = { ...this.tree, rows: toggleRow(this.tree.rows, dirPath, !row.expanded) }
      this.publish()
      return { status: "accepted" }
    }
    if (row.expanded) {
      this.tree = { ...this.tree, rows: removeDescendants(toggleRow(this.tree.rows, dirPath, false), dirPath) }
      this.publish()
      return { status: "accepted" }
    }

    const generation = this.treeGeneration
    this.tree = { ...this.tree, rows: markLoading(this.tree.rows, dirPath, true) }
    this.publish()
    try {
      const children = await listDirectory(this.root!, dirPath)
      if (this.closed || generation !== this.treeGeneration) return { status: "accepted" }
      const rows = toggleRow(
        markLoading(insertChildren(this.tree.rows, dirPath, children.rows), dirPath, false),
        dirPath,
        true,
      )
      this.tree = { ...this.tree, rows, limited: this.tree.limited || children.limited }
      this.publish()
      return { status: "accepted" }
    } catch (error) {
      if (this.closed || generation !== this.treeGeneration) return { status: "accepted" }
      const err = toWorkspaceError(error)
      this.tree = { ...this.tree, status: "error", rows: markLoading(this.tree.rows, dirPath, false), message: err.message }
      this.publish()
      return { status: "rejected", code: err.code, message: err.message }
    }
  }

  /** 预览文件：缓存命中直接使用；未命中读取 → ready/unsupported/error，写回缓存。 */
  private async previewFile(filePath: string): Promise<WorkspaceOutcome> {
    const generation = ++this.previewGeneration
    this.preview = { status: "loading", path: filePath }
    this.publish()
    if (this.root === null) {
      this.preview = { status: "error", path: filePath, code: "not-found", message: "工作区路径不可用" }
      this.publish()
      return { status: "rejected", code: "not-found", message: "工作区路径不可用" }
    }
    const cached = this.cache.get(filePath)
    if (cached !== undefined) {
      this.preview = { status: "ready", file: cached }
      this.publish()
      return { status: "accepted" }
    }
    try {
      const preview = await readPreview(this.root, filePath)
      if (this.closed || generation !== this.previewGeneration) return { status: "accepted" }
      this.cache.put(filePath, preview.version, preview)
      this.preview = { status: "ready", file: preview }
      this.publish()
      return { status: "accepted" }
    } catch (error) {
      if (this.closed || generation !== this.previewGeneration) return { status: "accepted" }
      const err = toWorkspaceError(error)
      this.preview = toPreviewState(filePath, err)
      this.publish()
      return { status: "rejected", code: err.code, message: err.message }
    }
  }

  /** 强制重读：先使缓存失效，再走 preview-file 全流程。 */
  private async refreshPreview(filePath: string): Promise<WorkspaceOutcome> {
    this.cache.invalidate(filePath)
    return this.previewFile(filePath)
  }

  private publish(): void {
    if (this.closed) return
    const snapshot = this.getSnapshot()
    for (const listener of [...this.listeners]) listener(snapshot)
  }
}

/** 把任意异常收敛为 WorkspaceError；未知错误统一 io-error 且消息脱敏。 */
function toWorkspaceError(error: unknown): WorkspaceError {
  if (error instanceof WorkspaceError) return error
  return workspaceError("io-error", "读取文件失败")
}

/** unsupported 带 sizeBytes 走元信息展示（非红色错误页）；其余走 error 状态。 */
function toPreviewState(filePath: string, error: WorkspaceError): WorkspacePreviewState {
  if (error.code === "unsupported-file") {
    const sizeBytes = (error as WorkspaceError & { sizeBytes?: number }).sizeBytes ?? 0
    return { status: "unsupported", path: filePath, reason: error.message, sizeBytes }
  }
  return { status: "error", path: filePath, code: error.code, message: error.message }
}

/** 目录行 loading 标记（immutable）。 */
function markLoading(rows: readonly WorkspaceTreeRow[], dirPath: string, loading: boolean): readonly WorkspaceTreeRow[] {
  return rows.map(row =>
    row.path === dirPath && row.kind === "directory" ? { ...row, loading } : row,
  )
}

/** 移除某目录的全部后代行（非 Git 收起时清空懒加载的子行）。 */
function removeDescendants(rows: readonly WorkspaceTreeRow[], dirPath: string): readonly WorkspaceTreeRow[] {
  const prefix = `${dirPath}/`
  return rows.filter(row => !row.path.startsWith(prefix))
}
