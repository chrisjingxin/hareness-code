/**
 * 提及菜单组件：以紧凑的纯路径列表渲染 @ 文件补全候选。
 */

import { type MentionOption } from "../../presentation-shared/mention-filter-policy"
import { ensureMentionWindow } from "../../presentation-shared/mention-window-policy"
import { tuiTheme } from "./theme"

const PROMPT_BORDER = {
  topLeft: " ",
  topRight: " ",
  bottomLeft: "╹",
  bottomRight: " ",
  horizontal: " ",
  vertical: "│",
  topT: " ",
  bottomT: " ",
  leftT: "│",
  rightT: " ",
  cross: "│",
} as const

/** 渲染可筛选的文件提及候选列表，并共享键盘与鼠标选择回调。 */
export function MentionMenu(props: {
  options: readonly MentionOption[]
  totalMatches: number
  truncated: boolean
  selectedIndex: number
  windowStart: number
  visibleRows: number
  terminalWidth: number
  browsePath: string
  workspaceStatus: "idle" | "loading" | "ready" | "error"
  workspaceLimited: boolean
  workspaceMessage?: string
  onSelect: (option: MentionOption) => void
  onHover: (index: number) => void
  placement: "above" | "inline-below"
  accent: string
}) {
  const window = ensureMentionWindow(
    props.selectedIndex,
    props.windowStart,
    props.options.length,
    props.visibleRows,
  )
  const visibleOptions = props.options.slice(window.start, window.end)
  const maxPathWidth = Math.max(12, props.terminalWidth - 7)
  return (
    <box
      marginTop={props.placement === "inline-below" ? 1 : 0}
      marginBottom={props.placement === "above" ? 1 : 0}
      border={["left"]}
      borderColor={props.accent}
      customBorderChars={PROMPT_BORDER}
    >
      <box backgroundColor={tuiTheme.menu} paddingTop={1} paddingBottom={1}>
        {visibleOptions.length ? (
          visibleOptions.map((item, localIndex) => {
            const index = window.start + localIndex
            const selected = index === props.selectedIndex
            const displayPath = shortenMiddle(item.kind === "directory" ? `${item.path}/` : item.path, maxPathWidth)
            return (
              <box
                key={item.path}
                backgroundColor={selected ? tuiTheme.surfaceElevated : tuiTheme.menu}
                paddingLeft={2}
                paddingRight={2}
                onMouseOver={() => props.onHover(index)}
                onMouseUp={() => props.onSelect(item)}
              >
                <text width="100%" wrapMode="none" overflow="hidden" fg={selected ? props.accent : tuiTheme.text}>
                  {highlightPath(displayPath, item, props.accent)}
                </text>
              </box>
            )
          })
        ) : (
          <box paddingLeft={2} paddingRight={2}>
            <text fg={props.workspaceStatus === "error" ? tuiTheme.danger : tuiTheme.muted}>
              {emptyStateText(props.workspaceStatus, props.workspaceMessage)}
            </text>
          </box>
        )}
        <box paddingLeft={2} paddingRight={2} paddingTop={1}>
          <text fg={tuiTheme.muted} wrapMode="none" overflow="hidden">
            {footerText(props)}
          </text>
        </box>
      </box>
    </box>
  )
}

function emptyStateText(status: "idle" | "loading" | "ready" | "error", message?: string): string {
  if (status === "idle" || status === "loading") return "正在扫描工作区…"
  if (status === "error") return message || "工作区读取失败"
  return "没有匹配的文件"
}

function footerText(props: {
  options: readonly MentionOption[]
  totalMatches: number
  truncated: boolean
  selectedIndex: number
  browsePath: string
  workspaceLimited: boolean
}): string {
  const parts: string[] = [`@ /${props.browsePath ? ` ${props.browsePath}` : ""}`]
  if (props.totalMatches > 0) parts.push(`${Math.min(props.selectedIndex + 1, props.totalMatches)}/${props.totalMatches}`)
  if (props.truncated) parts.push("仅载入前 1000 项")
  if (props.workspaceLimited) parts.push("工作区扫描受限")
  return parts.join(" · ") || "输入路径以筛选"
}

function shortenMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value
  if (maxLength <= 1) return "…"
  const tailLength = Math.ceil((maxLength - 1) * 0.65)
  const headLength = maxLength - 1 - tailLength
  return `${value.slice(0, headLength)}…${value.slice(-tailLength)}`
}

function highlightPath(displayPath: string, item: MentionOption, accent: string) {
  const range = item.matchRanges[0]
  if (!range) return displayPath
  const matchedText = item.path.slice(range.start, range.end)
  const displayStart = displayPath.toLowerCase().indexOf(matchedText.toLowerCase())
  if (displayStart < 0) return displayPath
  return (
    <>
      {displayPath.slice(0, displayStart)}
      <span fg={accent}><b>{displayPath.slice(displayStart, displayStart + matchedText.length)}</b></span>
      {displayPath.slice(displayStart + matchedText.length)}
    </>
  )
}
