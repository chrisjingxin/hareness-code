/** TUI 表现层与 Controller 之间共享的视图契约。 */

import type { KeyEvent, ScrollBoxRenderable, TextareaRenderable } from "@opentui/core"
import type { RefObject } from "react"

import type { CommandMenuItem } from "../application/commands"
import type {
  ApprovalDecision,
  CommandMenuState,
  SelectedSkill,
} from "../application/controller"
import type { TuiRuntime } from "../application/model"
import type { TuiState } from "../application/state"

export type {
  ApprovalDecision,
  CommandMenuState,
  SelectedSkill,
  ThreadPickerItem,
} from "../application/controller"

/** 首页与 Thread 视图共用的状态和交互入口。 */
export type SharedViewProps = {
  runtime: TuiRuntime
  state: TuiState
  terminalWidth: number
  terminalHeight: number
  inputRef: RefObject<TextareaRenderable | null>
  conversationScrollRef: RefObject<ScrollBoxRenderable | null>
  value: string
  onInput: (value: string) => void
  onComposerKeyDown: (event: KeyEvent) => void
  onSubmit: () => void
  commandMenu: CommandMenuState
  commandOptions: readonly CommandMenuItem[]
  onSelectCommand: (command: CommandMenuItem) => void
  onHoverCommand: (index: number) => void
  selectedSkill?: SelectedSkill
  pickerVisible: boolean
  onClearSelectedSkill: () => void
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
}
