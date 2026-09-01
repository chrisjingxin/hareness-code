/** OpenTUI 历史回合回退选择器（UndoPicker）与二次确认对话框（UndoDialog）组件。 */

import type { RefObject, ReactNode } from "react"
import type { TextareaRenderable } from "@opentui/core"
import type { TurnSummary } from "@za38/protocol"
import type { UndoMode } from "../application/adapter"
import { SearchPicker, DialogShell, type SearchPickerRenderContext } from "./overlays"
import { tuiTheme } from "./theme"

export type { UndoMode }

export type UndoPickerProps = {
  visible: boolean
  loading: boolean
  error?: string
  turns: readonly TurnSummary[]
  query: string
  selectedIndex: number
  terminalWidth: number
  terminalHeight: number
  searchRef: RefObject<TextareaRenderable | null>
  restoreFocusRef?: RefObject<TextareaRenderable | null>
  shouldRestoreFocus?: boolean
  workMode?: "build" | "compose"
  onSearch: (query: string) => void
  onSelect: (turn: TurnSummary) => void
  onHover: (selectedIndex: number) => void
  onClose: () => void
}

export type UndoDialogProps = {
  visible: boolean
  targetTurn: TurnSummary
  selectedMode: UndoMode
  isGit: boolean
  terminalWidth: number
  terminalHeight: number
  restoreFocusRef?: RefObject<TextareaRenderable | null>
  shouldRestoreFocus?: boolean
  onSelectMode: (mode: UndoMode) => void
  onConfirm: () => void
  onCancel: () => void
}

/** 历史回合选择器浮层 */
export function UndoPicker(props: UndoPickerProps) {
  return (
    <SearchPicker<TurnSummary>
      visible={props.visible}
      loading={props.loading}
      error={props.error}
      items={props.turns}
      workMode={props.workMode}
      query={props.query}
      selectedIndex={props.selectedIndex}
      terminalWidth={props.terminalWidth}
      terminalHeight={props.terminalHeight}
      searchRef={props.searchRef}
      restoreFocusRef={props.restoreFocusRef}
      shouldRestoreFocus={props.shouldRestoreFocus}
      searchId="undo-search"
      title="会话回退与快照还原"
      searchPlaceholder="搜索提问内容..."
      emptyMessage="当前会话没有可回退的历史回合"
      loadingMessage="正在读取历史快照…"
      footer="回退为暂存状态，提交新 Prompt 前可用 /redo 随时恢复"
      itemKey={turn => turn.turn_id}
      renderItem={(turn, context) => undoPickerRow(turn, context)}
      onSearch={props.onSearch}
      onSelect={props.onSelect}
      onHover={props.onHover}
      onClose={props.onClose}
    />
  )
}

/** 渲染单行历史回合信息 */
export function undoPickerRow(turn: TurnSummary, context: SearchPickerRenderContext): ReactNode {
  const titleWidth = context.compact
    ? Math.max(18, context.width - 6)
    : Math.max(26, Math.min(42, Math.floor(context.width * 0.45)))

  const turnLabel = `第 ${turn.turn_index} 轮: ${turn.user_prompt.trim().replace(/\s+/g, " ")}`
  
  let statsLabel = ""
  if (turn.diff_stats) {
    const { insertions, deletions, files } = turn.diff_stats
    const fileCount = files ? files.length : (turn.files_changed_count ?? 0)
    statsLabel = `${fileCount} 个文件 (+${insertions} -${deletions}) · ${timeAgo(turn.created_at)}`
  } else if (turn.files_changed_count > 0) {
    statsLabel = `${turn.files_changed_count} 个文件变动 · ${timeAgo(turn.created_at)}`
  } else if (!turn.has_git_checkpoint) {
    statsLabel = `无代码快照 · ${timeAgo(turn.created_at)}`
  } else {
    statsLabel = `无代码变动 · ${timeAgo(turn.created_at)}`
  }

  return (
    <>
      <text width={titleWidth} fg={context.selected ? tuiTheme.background : tuiTheme.primary} wrapMode="none" overflow="hidden">
        {shorten(turnLabel, titleWidth)}
      </text>
      {!context.compact ? (
        <text flexGrow={1} fg={context.selected ? tuiTheme.background : tuiTheme.muted} wrapMode="none" overflow="hidden">
          {shorten(statsLabel, Math.max(18, context.width - titleWidth - 6))}
        </text>
      ) : null}
    </>
  )
}

/** 回退范围二次确认与单选对话框 */
export function UndoDialog(props: UndoDialogProps) {
  const { targetTurn, selectedMode, isGit } = props
  const fileCount = targetTurn.diff_stats?.files ? targetTurn.diff_stats.files.length : targetTurn.files_changed_count
  const diffInfo = targetTurn.diff_stats
    ? `${fileCount} 个文件变动 (+${targetTurn.diff_stats.insertions} -${targetTurn.diff_stats.deletions})`
    : targetTurn.files_changed_count > 0
      ? `${targetTurn.files_changed_count} 个文件变动`
      : "无代码快照"

  const options: Array<{ mode: UndoMode; label: string; desc: string; disabled?: boolean; disabledReason?: string }> = [
    {
      mode: "both",
      label: "同时回退代码与对话（推荐）",
      desc: "还原工作区文件至该回合快照，并将对话回退至该轮",
      disabled: !isGit,
      disabledReason: "非 Git 仓库不可用",
    },
    {
      mode: "conversation",
      label: "仅回退对话",
      desc: "保留当前工作区所有代码修改，仅回退对话上下文",
      disabled: false,
    },
    {
      mode: "code",
      label: "仅还原代码",
      desc: "还原工作区代码至该回合快照，保留当前全部对话记录",
      disabled: !isGit,
      disabledReason: "非 Git 仓库不可用",
    },
  ]

  return (
    <DialogShell
      visible={props.visible}
      title={`确认回退至第 ${targetTurn.turn_index} 轮？`}
      message={`目标提问: "${targetTurn.user_prompt.trim().slice(0, 60)}${targetTurn.user_prompt.trim().length > 60 ? "…" : ""}" (${diffInfo})`}
      terminalWidth={props.terminalWidth}
      terminalHeight={props.terminalHeight}
      restoreFocusRef={props.restoreFocusRef}
      shouldRestoreFocus={props.shouldRestoreFocus}
      confirmLabel="Enter 执行回退"
      cancelLabel="Esc 取消"
      onConfirm={props.onConfirm}
      onCancel={props.onCancel}
    >
      <box flexDirection="column" gap={1} paddingTop={1} paddingBottom={1}>
        <text fg={tuiTheme.muted}>选择回退范围（方向键 ↑/↓ 或数字 1/2/3 切换）：</text>
        {options.map((opt, idx) => {
          const isSelected = selectedMode === opt.mode
          const isItemDisabled = opt.disabled
          return (
            <box
              key={opt.mode}
              flexDirection="column"
              backgroundColor={isSelected ? tuiTheme.primarySoft : undefined}
              paddingLeft={1}
              paddingRight={1}
              onMouseUp={() => {
                if (!isItemDisabled) props.onSelectMode(opt.mode)
              }}
            >
              <box flexDirection="row" gap={1}>
                <text fg={isSelected ? tuiTheme.primary : isItemDisabled ? tuiTheme.subtle : tuiTheme.text}>
                  {isSelected ? "●" : "○"} {idx + 1}. {opt.label}
                </text>
                {opt.disabledReason ? (
                  <text fg={tuiTheme.warning}>({opt.disabledReason})</text>
                ) : null}
              </box>
              {!opt.disabledReason ? (
                <text fg={tuiTheme.muted} paddingLeft={3}>— {opt.desc}</text>
              ) : null}
            </box>
          )
        })}
      </box>
    </DialogShell>
  )
}

function timeAgo(timestamp: number): string {
  const elapsedMs = Date.now() - (timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp)
  const elapsedMinutes = Math.max(0, Math.floor(elapsedMs / 60_000))
  if (elapsedMinutes < 1) return "刚刚"
  if (elapsedMinutes < 60) return `${elapsedMinutes} 分钟前`
  const elapsedHours = Math.floor(elapsedMinutes / 60)
  if (elapsedHours < 24) return `${elapsedHours} 小时前`
  return `${Math.floor(elapsedHours / 24)} 天前`
}

function shorten(value: string, limit: number): string {
  if (value.length <= limit) return value
  return `${value.slice(0, Math.max(0, limit - 1))}…`
}
