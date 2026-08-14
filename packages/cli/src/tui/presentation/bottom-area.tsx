/** 底部槽位：输入栏、审批 Dock、问答 Dock 互斥。 */

import type { TextareaRenderable } from "@opentui/core"
import { useRef, useState } from "react"

import type { InteractiveSnapshot } from "../../interactive/types"
import {
  APPROVAL_DECISION_ORDER,
  DIRECTORY_TRUST_DECISION_ORDER,
  QUESTION_OTHER_VALUE,
  approvalDecisionDescription,
  approvalDecisionLabel,
  directoryTrustDecisionDescription,
  directoryTrustDecisionLabel,
  isApprovalDecision,
  isDirectoryTrustDecision,
} from "../../presentation-shared/interaction-policy"
import { diffTextForRenderer, parseFileDiff } from "../../presentation-shared/file-diff"
import { resolveLanguageForPath } from "../../presentation-shared/language-catalog"
import { getCommonSyntaxClient } from "../platform/syntax-parsers"
import { modeAccent, markdownSyntax, tuiTheme } from "./theme"
import type { ApprovalDecision, DirectoryTrustDecision } from "./types"

function tuiDiffViewForWidth(contentWidth: number): "split" | "unified" {
  return contentWidth >= 120 ? "split" : "unified"
}

export type BottomAreaKind = "input" | "approval" | "directory_trust" | "question"

/** 底部同时只出现一个可聚焦面。无选项或多选仍走输入栏；ask_user 单选即使允许其他项也走 Dock。 */
export function bottomAreaKind(interaction: InteractiveSnapshot["interaction"]): BottomAreaKind {
  if (interaction?.type === "approval") return "approval"
  if (interaction?.type === "directory_trust") return "directory_trust"
  if (interaction?.type !== "question") return "input"
  const question = interaction.questions[0]
  if (!question?.options.length || question.multiSelect) return "input"
  return "question"
}

/** 审批 Dock：选项与文件 Diff 预览，焦点在底部。 */
export function ApprovalDock(props: {
  interaction: Extract<InteractiveSnapshot["interaction"], { type: "approval" }>
  workMode: InteractiveSnapshot["workMode"]
  terminalWidth: number
  onApproval: (decision: ApprovalDecision) => void
}) {
  const accent = modeAccent(props.workMode)
  const allowed = props.interaction.decisions.filter(isApprovalDecision)
  const options = (allowed.length ? allowed : APPROVAL_DECISION_ORDER).map(decision => ({
    name: approvalDecisionLabel(decision),
    description: approvalDecisionDescription(decision),
    value: decision,
  }))
  return (
    <box
      flexShrink={0}
      marginLeft={2}
      marginRight={2}
      marginBottom={1}
      backgroundColor={tuiTheme.surfaceElevated}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
      paddingBottom={1}
      flexDirection="column"
    >
      <text fg={accent}>需要审批</text>
      {props.interaction.description ? <text content={props.interaction.description} fg={tuiTheme.text} /> : null}
      {props.interaction.presentation
        ? <FileDiffApprovalPreview presentation={props.interaction.presentation} terminalWidth={props.terminalWidth} />
        : null}
      <select
        focused
        height={Math.max(2, Math.min(10, options.length * 2))}
        showDescription
        wrapSelection
        options={options}
        onSelect={(_, option) => {
          const value = option?.value
          if (value === "approve_once" || value === "approve_thread" || value === "approve_project" || value === "reject" || value === "reject_with_feedback") {
            props.onApproval(value)
          }
        }}
      />
      <text fg={tuiTheme.muted}>↑↓ 选择 · Enter 确认</text>
    </box>
  )
}

/** 目录信任 Dock：选项在底部确认。 */
export function DirectoryTrustDock(props: {
  interaction: Extract<InteractiveSnapshot["interaction"], { type: "directory_trust" }>
  workMode: InteractiveSnapshot["workMode"]
  onDirectoryTrust: (decision: DirectoryTrustDecision) => void
}) {
  const accent = modeAccent(props.workMode)
  const allowed = props.interaction.decisions.filter(isDirectoryTrustDecision)
  const options = (allowed.length ? allowed : DIRECTORY_TRUST_DECISION_ORDER).map(decision => ({
    name: directoryTrustDecisionLabel(decision),
    description: directoryTrustDecisionDescription(decision),
    value: decision,
  }))
  const access = props.interaction.access === "write" ? "写入" : "读取"
  return (
    <box
      flexShrink={0}
      marginLeft={2}
      marginRight={2}
      marginBottom={1}
      backgroundColor={tuiTheme.surfaceElevated}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
      paddingBottom={1}
      flexDirection="column"
    >
      <text fg={accent}>目录信任</text>
      <text content={`工具：${props.interaction.toolName}（${access}）`} fg={tuiTheme.text} />
      <text content={`目标路径：${props.interaction.targetPath}`} fg={tuiTheme.text} />
      <text content={`待信任目录：${props.interaction.directory}`} fg={tuiTheme.warning} />
      {props.interaction.shadowsWorkspace ? (
        <text content="注意：该目录会遮蔽主工作区内的同名路径。" fg={tuiTheme.warning} />
      ) : null}
      <select
        focused
        height={Math.max(2, Math.min(6, options.length * 2))}
        showDescription
        wrapSelection
        options={options}
        onSelect={(_, option) => {
          const value = option?.value
          if (isDirectoryTrustDecision(value)) {
            props.onDirectoryTrust(value)
          }
        }}
      />
      <text fg={tuiTheme.muted}>↑↓ 选择 · Enter 确认</text>
    </box>
  )
}

/** 问答 Dock：单选选项在底部确认。 */
export function QuestionDock(props: {
  interaction: Extract<InteractiveSnapshot["interaction"], { type: "question" }>
  workMode: InteractiveSnapshot["workMode"]
  onQuestion: (answer: string) => void
}) {
  const accent = modeAccent(props.workMode)
  const question = props.interaction.questions[0]
  const [awaitingOther, setAwaitingOther] = useState(false)
  const otherRef = useRef<TextareaRenderable | null>(null)
  const options = (question?.options ?? []).map(option => ({
    name: option.label,
    description: option.description || option.label,
    value: option.value,
  }))
  if (question?.allowOther) {
    options.push({
      name: "其他",
      description: "输入自定义回答",
      value: QUESTION_OTHER_VALUE,
    })
  }
  return (
    <box
      flexShrink={0}
      marginLeft={2}
      marginRight={2}
      marginBottom={1}
      backgroundColor={tuiTheme.surfaceElevated}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
      paddingBottom={1}
      flexDirection="column"
    >
      <text fg={accent}>Agent 需要你的回答</text>
      {question?.question ? <text content={question.question} fg={tuiTheme.text} /> : null}
      {awaitingOther ? (
        <>
          <textarea
            ref={otherRef}
            focused
            placeholder="输入其他答案后按 Enter"
            placeholderColor={tuiTheme.muted}
            textColor={tuiTheme.text}
            focusedTextColor={tuiTheme.text}
            backgroundColor={tuiTheme.surface}
            focusedBackgroundColor={tuiTheme.surface}
            cursorColor={accent}
            minHeight={1}
            maxHeight={4}
            onSubmit={() => {
              const value = otherRef.current?.plainText.trim() ?? ""
              if (value) props.onQuestion(value)
            }}
          />
          <text fg={tuiTheme.muted}>输入自定义回答后按 Enter</text>
        </>
      ) : (
        <>
          <select
            focused
            height={Math.max(2, Math.min(8, Math.max(1, options.length) * 2))}
            showDescription
            wrapSelection
            options={options}
            onSelect={(_, option) => {
              if (typeof option?.value !== "string") return
              if (option.value === QUESTION_OTHER_VALUE) {
                setAwaitingOther(true)
                return
              }
              props.onQuestion(option.value)
            }}
          />
          <text fg={tuiTheme.muted}>{question?.allowOther ? "↑↓ 选择 · Enter 确认 · 选「其他」可自定义" : "↑↓ 选择 · Enter 确认"}</text>
        </>
      )}
    </box>
  )
}

/** 审批文件 Diff 预览；失败不阻断允许/拒绝。 */
function FileDiffApprovalPreview(props: {
  presentation: NonNullable<Extract<InteractiveSnapshot["interaction"], { type: "approval" }>["presentation"]>
  terminalWidth: number
}) {
  const { presentation } = props
  const contentWidth = Math.max(1, props.terminalWidth - 10)
  const view = tuiDiffViewForWidth(contentWidth)
  const language = resolveLanguageForPath(presentation.path).tuiParser
  const parsed = parseFileDiff(presentation.unified_diff)
  const operation = presentation.operation === "write" ? "创建文件" : presentation.operation === "delete" ? "删除文件" : "编辑文件"
  const summary = `${operation} · ${presentation.path} · +${presentation.added_lines} / -${presentation.removed_lines}`
  return (
    <box flexDirection="column" marginTop={1}>
      <text content={summary} fg={tuiTheme.text} />
      {presentation.truncated ? (
        <text content="预览已按 200 行或 16 KiB 上限截断；批准仍会应用完整变更。" fg={tuiTheme.warning} />
      ) : null}
      {presentation.unified_diff === "" ? (
        <text content="创建空文件（没有可显示的内容行）" fg={tuiTheme.muted} />
      ) : parsed.status === "invalid" ? (
        <>
          <text content="无法解析结构化 Diff，以下按纯文本展示。" fg={tuiTheme.warning} />
          <text content={presentation.unified_diff} fg={tuiTheme.text} />
        </>
      ) : (
        <diff
          width="100%"
          diff={diffTextForRenderer(presentation.unified_diff)}
          view={view}
          syncScroll
          filetype={language === "plaintext" ? undefined : language}
          syntaxStyle={markdownSyntax}
          treeSitterClient={getCommonSyntaxClient()}
          showLineNumbers
          wrapMode="word"
          fg={tuiTheme.text}
          lineNumberFg={tuiTheme.muted}
          lineNumberBg={tuiTheme.toolSurface}
          addedBg={tuiTheme.diffAddedBackground}
          removedBg={tuiTheme.diffRemovedBackground}
          contextBg={tuiTheme.toolSurface}
          addedSignColor={tuiTheme.success}
          removedSignColor={tuiTheme.danger}
          addedLineNumberBg={tuiTheme.diffAddedBackground}
          removedLineNumberBg={tuiTheme.diffRemovedBackground}
        />
      )}
    </box>
  )
}
