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

import { getFileIconInfo } from "./file-icons"

export function FileTreeWidget(props: FileTreeWidgetProps) {
  const { fileTree, focused } = props
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
        <scrollbox flexGrow={1} flexDirection="column" overflow="hidden">
          {visible.map((row, index) => {
            const isSelected = index === selectedIndex
            const iconInfo = getFileIconInfo(row.name, row.kind, row.expanded)
            const indent = "  ".repeat(row.depth)
            const collapseArrow = row.kind === "directory"
              ? (row.loading ? "… " : row.expanded ? "▾ " : "▸ ")
              : "  "

            const textColor = isSelected && focused
              ? tuiTheme.primary
              : (row.kind === "directory" ? iconInfo.color : tuiTheme.text)
            const iconColor = isSelected && focused ? tuiTheme.primary : iconInfo.color
            const arrowColor = isSelected && focused
              ? tuiTheme.primary
              : (row.kind === "directory" ? iconInfo.color : tuiTheme.subtle)

            return (
              <box
                key={row.path}
                flexDirection="row"
                alignItems="center"
                backgroundColor={isSelected && focused ? tuiTheme.surfaceElevated : undefined}
                onMouseUp={() => {
                  props.onSelectIndex(index)
                  if (row.kind === "directory") {
                    props.onToggleExpand(row.path)
                  } else {
                    props.onOpenFile?.(row.path)
                  }
                }}
              >
                <text>
                  <span fg={isSelected && focused ? tuiTheme.primary : tuiTheme.subtle}>
                    {isSelected && focused ? "› " : "  "}
                  </span>
                  <span fg={arrowColor}>{indent}{collapseArrow}</span>
                  <span fg={iconColor}>{iconInfo.icon}</span>
                  <span fg={textColor}>
                    {row.kind === "directory" ? <b>{row.name}</b> : row.name}
                  </span>
                </text>
              </box>
            )
          })}
        </scrollbox>
      )}
    </box>
  )
}
