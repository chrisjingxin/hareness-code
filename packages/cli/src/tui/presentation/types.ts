/** TUI 表现层与 Adapter 之间共享的视图契约。 */

import type { KeyEvent, ScrollBoxRenderable, TextareaRenderable } from "@opentui/core"
import type { RefObject } from "react"

import type { CommandMenuItem, SkillMenuItem } from "../../interactive/commands"
import type { InteractiveSnapshot } from "../../interactive/types"
import type { ApprovalDecision, CommandMenuState } from "../application/adapter"

export type {
  ApprovalDecision,
  CommandMenuState,
  ThreadPickerItem,
} from "../application/adapter"

/** 首页与 Thread 视图共用的状态和交互入口。 */
export type SharedViewProps = {
  interactive: InteractiveSnapshot
  transientNotice?: { id: string; message: string }
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
  selectedSkill?: SkillMenuItem
  pickerVisible: boolean
  onClearSelectedSkill: () => void
  showToolDetails: boolean
  expandedTools: ReadonlySet<string>
  onToggleTool: (toolId: string) => void
  onApproval: (decision: ApprovalDecision) => void
  onQuestion: (answer: string) => void
}
