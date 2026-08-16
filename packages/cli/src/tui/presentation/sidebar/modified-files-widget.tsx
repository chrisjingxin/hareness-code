/** TUI 侧边栏变更文件列表小部件。 */

import type { TimelineItem } from "../../../interactive/types"
import { parseFileMutationArgs } from "../tools/file-mutation-args"
import { tuiTheme } from "../theme"

export type ModifiedFileItem = {
  relativePath: string
  status: "added" | "modified" | "deleted"
  addedLines: number
  removedLines: number
}

function countLines(text: string | null): number {
  if (!text) return 0
  const normalized = text.endsWith("\n") ? text.slice(0, -1) : text
  return normalized.split("\n").length
}

/** 从时间线中的工具调用卡片中提取当前会话产生的变更文件列表。 */
export function extractModifiedFiles(timeline: readonly TimelineItem[]): ModifiedFileItem[] {
  const fileMap = new Map<string, ModifiedFileItem>()

  for (const item of timeline) {
    if (item.type !== "tool") continue
    const { name, arguments: rawArgs } = item.tool
    const toolName = name.toLowerCase()

    if (toolName === "write_file" || toolName === "write") {
      const parsed = parseFileMutationArgs(rawArgs)
      if (parsed.path) {
        const lineCount = countLines(parsed.content) || 1
        const existing = fileMap.get(parsed.path)
        fileMap.set(parsed.path, {
          relativePath: parsed.path,
          status: existing ? "modified" : "added",
          addedLines: (existing?.addedLines ?? 0) + lineCount,
          removedLines: existing?.removedLines ?? 0,
        })
      }
    } else if (toolName === "edit_file" || toolName === "edit") {
      const parsed = parseFileMutationArgs(rawArgs)
      if (parsed.path) {
        const added = countLines(parsed.newString)
        const removed = countLines(parsed.oldString)
        const existing = fileMap.get(parsed.path)
        fileMap.set(parsed.path, {
          relativePath: parsed.path,
          status: "modified",
          addedLines: (existing?.addedLines ?? 0) + added,
          removedLines: (existing?.removedLines ?? 0) + removed,
        })
      }
    } else if (toolName === "delete_file" || toolName === "delete") {
      const parsed = parseFileMutationArgs(rawArgs)
      if (parsed.path) {
        const existing = fileMap.get(parsed.path)
        fileMap.set(parsed.path, {
          relativePath: parsed.path,
          status: "deleted",
          addedLines: existing?.addedLines ?? 0,
          removedLines: (existing?.removedLines ?? 0) + 1,
        })
      }
    }
  }

  return Array.from(fileMap.values())
}

export type ModifiedFilesWidgetProps = {
  timeline: readonly TimelineItem[]
  onSelectFile?: (path: string) => void
}

export function ModifiedFilesWidget(props: ModifiedFilesWidgetProps) {
  const files = extractModifiedFiles(props.timeline)

  return (
    <box flexDirection="column" paddingTop={1} paddingBottom={1} border={["bottom"]} borderColor={tuiTheme.border}>
      <box flexDirection="row" justifyContent="space-between">
        <text fg={tuiTheme.subtle}>
          <b>变更文件</b>
        </text>
        <text fg={tuiTheme.muted}>
          ({files.length})
        </text>
      </box>
      {files.length === 0 ? (
        <text fg={tuiTheme.subtle}>暂无文件变更</text>
      ) : (
        <box flexDirection="column" paddingTop={0}>
          {files.slice(0, 6).map(file => {
            const parts = file.relativePath.split("/")
            const basename = parts[parts.length - 1] || file.relativePath
            return (
              <box
                key={file.relativePath}
                flexDirection="row"
                justifyContent="space-between"
                onMouseUp={() => props.onSelectFile?.(file.relativePath)}
              >
                <text fg={tuiTheme.text}>{basename}</text>
                <box flexDirection="row" gap={1}>
                  {file.addedLines > 0 ? (
                    <text fg={tuiTheme.diffAdd}>+{file.addedLines}</text>
                  ) : null}
                  {file.removedLines > 0 ? (
                    <text fg={tuiTheme.diffRemove}>-{file.removedLines}</text>
                  ) : null}
                </box>
              </box>
            )
          })}
          {files.length > 6 ? (
            <text fg={tuiTheme.subtle}>+{files.length - 6} 更多文件…</text>
          ) : null}
        </box>
      )}
    </box>
  )
}
