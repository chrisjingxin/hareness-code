/** 文件树：按深度缩进渲染 workspaceTree.rows，键盘 ↑↓→←/Enter/Home/End 导航，ARIA tree 语义。 */
/** @jsxImportSource react */

import { useMemo, useRef, useState } from "react"
import { ChevronDown, ChevronRight, FileSymlink, Folder } from "lucide-react"

import type { WebAdapterSnapshot, WebIntent } from "../../application/adapter"
import { FileTypeIcon } from "./file-type-icon"

/** 每层缩进宽度。 */
const INDENT_PX = 12

/**
 * 行结构：与 WorkspaceTreeView.rows 完全一致的结构化类型。
 * 表现层不 import workspace 领域模块（经 WebIntent 间接通信），
 * 结构兼容性由网关契约保证。
 */
type FileTreeRow = {
  readonly path: string
  readonly name: string
  readonly kind: "directory" | "file" | "symlink"
  readonly depth: number
  readonly expanded: boolean
  readonly loading: boolean
  readonly hasChildren: boolean
}

/**
 * 渲染文件树。
 *
 * 可见行 = 所有祖先目录均展开的行；键盘焦点用本地 index（↑↓/Home/End 移动并
 * scrollIntoView），高亮选中态由 adapter 的 workspaceSidebar.selectedPath 驱动。
 * 目录行点击 dispatch workspace-directory-toggle，文件行 dispatch workspace-file-open。
 */
export function FileTree({
  snapshot,
  dispatch,
  disabled = false,
}: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void
  disabled?: boolean
}): React.ReactElement {
  const tree = snapshot.workspaceTree
  const rows = tree.rows
  const selectedPath = snapshot.workspaceSidebar.selectedPath
  const containerRef = useRef<HTMLDivElement | null>(null)
  // 焦点按行路径记录：行集变化（展开/收起/刷新）时保持行身份，避免 index 漂移。
  const [focusedPath, setFocusedPath] = useState<string | null>(null)

  const visible = useMemo(() => visibleRows(rows), [rows])
  const focused = focusedPath === null
    ? 0
    : Math.max(0, visible.findIndex(row => row.path === focusedPath))
  const focusedRow = visible[focused]

  const scrollRowIntoView = (index: number): void => {
    const element = containerRef.current?.querySelector<HTMLElement>(`[data-file-index="${index}"]`)
    element?.scrollIntoView?.({ block: "nearest" })
  }

  const moveFocus = (next: number): void => {
    const clamped = Math.max(0, Math.min(visible.length - 1, next))
    setFocusedPath(visible[clamped]?.path ?? null)
    scrollRowIntoView(clamped)
  }

  const activateRow = (row: FileTreeRow): void => {
    if (row.kind === "directory") dispatch({ type: "workspace-directory-toggle", path: row.path })
    else dispatch({ type: "workspace-file-open", path: row.path })
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (visible.length === 0) return
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault()
        moveFocus(focused + 1)
        return
      case "ArrowUp":
        event.preventDefault()
        moveFocus(focused - 1)
        return
      case "ArrowRight":
      case "ArrowLeft":
        // 目录行：切换展开/收起；文件行无折叠语义。
        event.preventDefault()
        if (focusedRow?.kind === "directory") activateRow(focusedRow)
        return
      case "Enter":
        event.preventDefault()
        if (focusedRow) activateRow(focusedRow)
        return
      case "Home":
        event.preventDefault()
        moveFocus(0)
        return
      case "End":
        event.preventDefault()
        moveFocus(visible.length - 1)
        return
    }
  }

  const handleRowClick = (index: number, row: FileTreeRow): void => {
    setFocusedPath(row.path)
    activateRow(row)
  }

  return (
    <div
      ref={containerRef}
      className="file-tree"
      role="tree"
      aria-label="文件列表"
      aria-activedescendant={focusedRow !== undefined ? `file-row-${focused}` : undefined}
      tabIndex={disabled ? -1 : 0}
      onKeyDown={disabled ? undefined : onKeyDown}
    >
      {tree.status === "loading" && rows.length === 0 ? (
        <p className="file-tree-status">加载中…</p>
      ) : null}
      {tree.status === "error" ? (
        <p className="file-tree-status file-tree-status-error" role="alert">
          {tree.message ?? "文件树加载失败"}
        </p>
      ) : null}
      {tree.status === "ready" && visible.length === 0 ? (
        <p className="file-tree-status">空目录</p>
      ) : null}
      {visible.map((row, index) => {
        const isSelected = row.path === selectedPath
        const isFocused = index === focused
        const rowDisabled = disabled || (row.kind === "directory" && !row.hasChildren)
        return (
          <div
            key={row.path}
            id={`file-row-${index}`}
            data-file-index={index}
            role="treeitem"
            aria-level={row.depth + 1}
            aria-expanded={row.kind === "directory" ? row.expanded : undefined}
            aria-selected={isSelected}
            className={`file-row${isSelected ? " is-selected" : ""}${isFocused ? " is-focused" : ""}`}
            style={{ paddingInlineStart: 8 + row.depth * INDENT_PX }}
            onClick={disabled ? undefined : () => handleRowClick(index, row)}
          >
            <span className="file-row-arrow" aria-hidden="true">
              {row.kind === "directory"
                ? (row.expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />)
                : null}
            </span>
            {row.kind === "directory" ? (
              <Folder aria-hidden="true" size={14} className="file-row-icon" />
            ) : row.kind === "symlink" ? (
              <FileSymlink aria-hidden="true" size={14} className="file-row-icon" />
            ) : (
              <FileTypeIcon name={row.name} />
            )}
            <span className="file-row-name">{row.name}</span>
            {row.loading ? <span className="file-row-loading">…</span> : null}
            <span className="sr-only">{rowDisabled ? "目录不可展开" : ""}</span>
          </div>
        )
      })}
      {tree.limited ? <p className="file-tree-limited">工作区文件较多，仅展示部分内容</p> : null}
    </div>
  )
}

/** 可见行过滤：祖先目录全部展开的行才可见（Git 全量树靠此隐藏已收起目录的后代）。 */
function visibleRows(rows: readonly FileTreeRow[]): readonly FileTreeRow[] {
  const collapsed = new Set<string>()
  const visible: FileTreeRow[] = []
  for (const row of rows) {
    if (hasCollapsedAncestor(row.path, collapsed)) continue
    if (row.kind === "directory") {
      visible.push(row)
      if (!row.expanded) collapsed.add(row.path)
    } else {
      visible.push(row)
    }
  }
  return visible
}

function hasCollapsedAncestor(filePath: string, collapsed: ReadonlySet<string>): boolean {
  const segments = filePath.split("/")
  for (let i = 1; i < segments.length; i++) {
    if (collapsed.has(segments.slice(0, i).join("/"))) return true
  }
  return false
}
