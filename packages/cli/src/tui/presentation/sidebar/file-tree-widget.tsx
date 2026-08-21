/** TUI 侧边栏工作区文件树小部件。 */

import type { SidebarFileTreeState } from "../../application/adapter"
import type { WorkspaceTreeRow } from "../../../workspace/types"
import { tuiTheme } from "../theme"

export type FileTreeWidgetProps = {
  fileTree: SidebarFileTreeState
  focused: boolean
  onSelectIndex: (index: number) => void
  onToggleExpand: (path: string) => void
  onOpenFile?: (path: string) => void
}

/** 可见行过滤：祖先目录全部展开的行才可见（Git 全量树靠此隐藏已收起目录的后代）。 */
export function visibleTreeRows(rows: readonly WorkspaceTreeRow[]): readonly WorkspaceTreeRow[] {
  const collapsed = new Set<string>()
  const visible: WorkspaceTreeRow[] = []
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

/** 文件类型使用短文字而非 Emoji，避免 Windows 字体的双宽与缺字问题。 */
export function fileTypeBadge(name: string, kind: WorkspaceTreeRow["kind"]): string | null {
  if (kind === "directory") return null
  if (kind === "symlink") return "LINK"
  const dotIndex = name.lastIndexOf(".")
  if (dotIndex < 0 || dotIndex === name.length - 1) return "FILE"
  const extension = name.slice(dotIndex + 1).toUpperCase()
  return extension.length <= 4 ? extension : extension.slice(0, 4)
}

export function FileTreeWidget(props: FileTreeWidgetProps) {
  const { fileTree } = props
  const { status, rows, selectedIndex, limited, message } = fileTree
  const visible = visibleTreeRows(rows)

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0}>
      <box flexDirection="row" justifyContent="space-between" flexShrink={0} paddingBottom={1}>
        <text fg={tuiTheme.subtle}>
          <b>工作区文件</b>
        </text>
        <text fg={tuiTheme.muted}>
          {limited ? `(部分 ${rows.length})` : `(${rows.length})`}
        </text>
      </box>

      {status === "loading" && rows.length === 0 ? (
        <text fg={tuiTheme.muted}>加载目录中…</text>
      ) : status === "error" && rows.length === 0 ? (
        <text fg={tuiTheme.danger}>{message ?? "加载失败"}</text>
      ) : rows.length === 0 ? (
        <text fg={tuiTheme.subtle}>工作区为空</text>
      ) : (
        <scrollbox flexGrow={1} contentOptions={{ flexDirection: "column" }} overflow="hidden">
          {visible.map((row, index) => {
            const isSelected = index === selectedIndex
            const badge = fileTypeBadge(row.name, row.kind)
            const indent = "  ".repeat(row.depth)
            const collapseArrow = row.kind === "directory"
              ? (row.loading ? "… " : row.expanded ? "▾ " : "▸ ")
              : "  "

            const textColor = isSelected
              ? tuiTheme.background
              : (row.kind === "directory" ? tuiTheme.warning : tuiTheme.text)
            const arrowColor = isSelected
              ? tuiTheme.background
              : (row.kind === "directory" ? tuiTheme.warning : tuiTheme.subtle)

            return (
              <box
                key={row.path}
                flexDirection="row"
                alignItems="center"
                justifyContent="space-between"
                backgroundColor={isSelected ? tuiTheme.pickerActive : undefined}
                onMouseUp={() => {
                  props.onSelectIndex(index)
                  if (row.kind === "directory") {
                    props.onToggleExpand(row.path)
                  } else {
                    props.onOpenFile?.(row.path)
                  }
                }}
              >
                <text flexShrink={1}>
                  <span fg={arrowColor}>{indent}{collapseArrow}</span>
                  <span fg={textColor}>
                    {row.kind === "directory" ? <b>{row.name}</b> : row.name}
                  </span>
                </text>
                {badge ? (
                  <text fg={isSelected ? tuiTheme.background : tuiTheme.muted}>{badge}</text>
                ) : null}
              </box>
            )
          })}
        </scrollbox>
      )}
    </box>
  )
}
