/** 底部槽位：输入栏、审批 Dock、问答 Dock 互斥。 */

import type { TextareaRenderable } from "@opentui/core"
import { useKeyboard } from "@opentui/react"
import { useRef, useState } from "react"

import type { InteractiveSnapshot } from "../../interactive/types"
import {
  APPROVAL_DECISION_ORDER,
  DIRECTORY_TRUST_DECISION_ORDER,
  PLAN_DECISION_ORDER,
  QUESTION_OTHER_VALUE,
  answersByQuestionId,
  approvalDecisionDescription,
  approvalDecisionLabel,
  directoryTrustDecisionDescription,
  directoryTrustDecisionLabel,
  createPlanAnnotation,
  formatPlanReviewFeedback,
  isApprovalDecision,
  isDirectoryTrustDecision,
  isPlanDecision,
  planDecisionDescription,
  planDecisionLabel,
  planPreviewHeight,
  recordAskUserAnswer,
  type PlanAnnotation,
} from "../../presentation-shared/interaction-policy"
import { diffTextForRenderer, parseFileDiff } from "../../presentation-shared/file-diff"
import { resolveLanguageForPath } from "../../presentation-shared/language-catalog"
import { getCommonSyntaxClient } from "../platform/syntax-parsers"
import { SUBMIT_ON_ENTER_KEY_BINDINGS } from "./input-bar"
import { modeAccent, markdownSyntax, tuiTheme } from "./theme"
import type { ApprovalDecision, DirectoryTrustDecision, PlanDecision } from "../../interactive/types"

function tuiDiffViewForWidth(contentWidth: number): "split" | "unified" {
  return contentWidth >= 120 ? "split" : "unified"
}

function approvalDockTitle(interaction: Extract<InteractiveSnapshot["interaction"], { type: "approval" }>): string {
  return interaction.agentId && interaction.agentId !== "main"
    ? `子代理 ${interaction.agentId} 需要审批`
    : "需要审批"
}

function directoryTrustDockTitle(interaction: Extract<InteractiveSnapshot["interaction"], { type: "directory_trust" }>): string {
  return interaction.agentId && interaction.agentId !== "main"
    ? `子代理 ${interaction.agentId} 需要目录信任`
    : "目录信任"
}

export type BottomAreaKind = "input" | "approval" | "directory_trust" | "question" | "plan"

/** 底部同时只出现一个可聚焦面。所有 ask_user 提问都走问答 Dock，不再把文本题藏进输入栏。 */
export function bottomAreaKind(interaction: InteractiveSnapshot["interaction"]): BottomAreaKind {
  if (interaction?.type === "approval") return "approval"
  if (interaction?.type === "directory_trust") return "directory_trust"
  if (interaction?.type === "plan") return "plan"
  if (interaction?.type === "question") return "question"
  return "input"
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
      <text fg={accent}>{approvalDockTitle(props.interaction)}</text>
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
      <text fg={accent}>{directoryTrustDockTitle(props.interaction)}</text>
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

/** 全屏审阅计划：正文支持 Markdown 高亮、光标移动、多行选择与就地批注，Tab 自由切换焦点。 */
export function PlanDock(props: {
  interaction: Extract<InteractiveSnapshot["interaction"], { type: "plan" }>
  workMode: InteractiveSnapshot["workMode"]
  terminalHeight: number
  onPlan: (decision: PlanDecision, feedback?: string) => void
  onClose: () => void
}) {
  const accent = modeAccent(props.workMode)
  const lines = props.interaction.hasPlan ? props.interaction.planMarkdown.split("\n") : []
  const [cursorLine, setCursorLine] = useState(1)
  const [visualStart, setVisualStart] = useState<number | null>(null)
  const [annotationRange, setAnnotationRange] = useState<{ startLine: number; endLine: number } | null>(null)
  const [annotations, setAnnotations] = useState<PlanAnnotation[]>([])
  const [focusSection, setFocusSection] = useState<"document" | "decision">(props.interaction.hasPlan ? "document" : "decision")
  const [awaitingFeedback, setAwaitingFeedback] = useState(false)
  const feedbackRef = useRef<TextareaRenderable | null>(null)
  const annotationRef = useRef<TextareaRenderable | null>(null)

  const allowed = props.interaction.decisions.filter(isPlanDecision)
  const options = annotations.length > 0
    ? [
        {
          name: `提交批注并继续打磨 (${annotations.length} 条批注)`,
          description: "按所选行批注意见调整计划后再提交审阅",
          value: "revise" as PlanDecision,
        },
        {
          name: "放弃计划",
          description: "放弃本次规划，退出计划模式",
          value: "abandoned" as PlanDecision,
        },
      ].filter(item => allowed.length === 0 || allowed.includes(item.value))
    : [
        {
          name: "批准并开始实现",
          description: "按计划开始修改代码",
          value: "approved" as PlanDecision,
        },
        {
          name: "继续打磨",
          description: "调整计划后再提交审阅",
          value: "revise" as PlanDecision,
        },
        {
          name: "放弃计划",
          description: "放弃本次规划，退出计划模式",
          value: "abandoned" as PlanDecision,
        },
      ].filter(item => allowed.length === 0 || allowed.includes(item.value))

  useKeyboard(key => {
    // 当正文批注输入框或打回意见输入框打开时，只处理 Esc 取消
    if (annotationRange !== null || awaitingFeedback) {
      if (key.name === "escape") {
        key.preventDefault()
        setAnnotationRange(null)
        setAwaitingFeedback(false)
      }
      return
    }

    // 只读预览下 Esc 关闭
    if (props.interaction.readOnly && key.name === "escape") {
      key.preventDefault()
      props.onClose()
      return
    }

    // Tab 键在正文浏览和底部选项之间无缝切换焦点
    if (key.name === "tab") {
      key.preventDefault()
      setFocusSection(current => current === "document" ? "decision" : "document")
      return
    }

    // 焦点在底部选项时，Esc 切回正文，其余键留给 select 组件
    if (focusSection === "decision") {
      if (key.name === "escape") {
        key.preventDefault()
        setFocusSection("document")
      }
      return
    }

    // 以下为正文（document）焦点下的键盘操作
    if (key.name === "up" || key.sequence === "k") {
      key.preventDefault()
      setCursorLine(current => Math.max(1, current - 1))
      return
    }
    if (key.name === "down" || key.sequence === "j") {
      key.preventDefault()
      setCursorLine(current => Math.min(Math.max(1, lines.length), current + 1))
      return
    }
    if (key.name === "pageup") {
      key.preventDefault()
      setCursorLine(current => Math.max(1, current - 8))
      return
    }
    if (key.name === "pagedown") {
      key.preventDefault()
      setCursorLine(current => Math.min(Math.max(1, lines.length), current + 8))
      return
    }
    if (key.name === "home") {
      key.preventDefault()
      setCursorLine(1)
      return
    }
    if (key.name === "end") {
      key.preventDefault()
      setCursorLine(Math.max(1, lines.length))
      return
    }

    // 多行范围选择切换 (按 v 开启/取消)
    if ((key.sequence === "v" || key.name === "v") && !props.interaction.readOnly && lines.length > 0) {
      key.preventDefault()
      setVisualStart(current => current === null ? cursorLine : null)
      return
    }

    // 就地批注当前行 / 选中范围 (按 c 或 Enter)
    if ((key.sequence === "c" || key.name === "c" || key.name === "return") && !props.interaction.readOnly && lines.length > 0) {
      key.preventDefault()
      const start = visualStart === null ? cursorLine : Math.min(visualStart, cursorLine)
      const end = visualStart === null ? cursorLine + 1 : Math.max(visualStart, cursorLine) + 1
      setAnnotationRange({ startLine: start, endLine: end })
      setVisualStart(null)
      return
    }

    // 删除当前行上的批注 (按 d)
    if ((key.sequence === "d" || key.name === "d") && !props.interaction.readOnly) {
      key.preventDefault()
      setAnnotations(current => current.filter(ann => !(cursorLine >= ann.startLine && cursorLine < ann.endLine)))
      return
    }
  })

  const isVisualRange = (lineNumber: number) => {
    if (visualStart === null) return false
    const min = Math.min(visualStart, cursorLine)
    const max = Math.max(visualStart, cursorLine)
    return lineNumber >= min && lineNumber <= max
  }

  const isEditingRange = (lineNumber: number) => {
    if (annotationRange === null) return false
    return lineNumber >= annotationRange.startLine && lineNumber < annotationRange.endLine
  }

  const getLineAnnotation = (lineNumber: number) => {
    return annotations.find(a => lineNumber >= a.startLine && lineNumber < a.endLine)
  }

  return (
    <box
      flexGrow={1}
      minHeight={0}
      flexDirection="column"
      backgroundColor={tuiTheme.background}
      paddingLeft={2}
      paddingRight={2}
      paddingTop={1}
    >
      <box flexShrink={0} flexDirection="row" justifyContent="space-between" gap={2}>
        <text fg={accent}>{props.interaction.readOnly ? "当前计划" : "审阅计划"}</text>
        <text fg={tuiTheme.subtle}>plan</text>
      </box>
      <box flexShrink={0} flexDirection="row" justifyContent="space-between" gap={2}>
        <text content={props.interaction.planDisplayPath} fg={tuiTheme.muted} />
        {annotations.length > 0 ? (
          <text fg={accent}>{annotations.length} 条批注</text>
        ) : null}
      </box>
      {visualStart !== null ? (
        <box flexShrink={0} marginTop={1} paddingLeft={1} backgroundColor={tuiTheme.surfaceElevated}>
          <text fg={accent}>
            [选区 第 {Math.min(visualStart, cursorLine)}~{Math.max(visualStart, cursorLine)} 行] · 按 c 批注 · 按 v 取消
          </text>
        </box>
      ) : null}
      <box height={1} flexShrink={0} marginTop={1} backgroundColor={tuiTheme.border} />

      {props.interaction.hasPlan ? (
        <scrollbox flexGrow={1} minHeight={0} marginTop={1} stickyScroll={false}>
          <box flexDirection="column">
            {lines.map((line, index) => {
              const lineNumber = index + 1
              const isCursor = lineNumber === cursorLine
              const isVisual = isVisualRange(lineNumber)
              const isEditing = isEditingRange(lineNumber)
              const lineAnn = getLineAnnotation(lineNumber)
              const isAnnotated = Boolean(lineAnn)
              const isInRange = isVisual || isEditing || isAnnotated

              const lineAnnotations = annotations.filter(a => a.endLine - 1 === lineNumber)
              const isEditorLine = annotationRange !== null && annotationRange.endLine - 1 === lineNumber

              const marker = isCursor && focusSection === "document"
                ? "▶"
                : isInRange
                  ? "▎"
                  : " "

              const statusMarker = isEditing || isAnnotated ? "*" : " "

              const lineBg = isEditing || isVisual
                ? (isCursor && focusSection === "document" ? tuiTheme.surfaceElevated : tuiTheme.surface)
                : isAnnotated
                  ? tuiTheme.surface
                  : (isCursor && focusSection === "document" ? tuiTheme.surfaceElevated : "transparent")

              const markerFg = isCursor && focusSection === "document"
                ? accent
                : isInRange
                  ? accent
                  : tuiTheme.muted

              return (
                <box key={lineNumber} flexDirection="column">
                  <box
                    flexDirection="row"
                    gap={1}
                    alignItems="center"
                    backgroundColor={lineBg}
                  >
                    <text fg={markerFg}>
                      {marker} {String(lineNumber).padStart(3, " ")} {statusMarker}
                    </text>
                    <box flexGrow={1} minWidth={0}>
                      {line.trim().length > 0 ? (
                        <markdown
                          content={line}
                          syntaxStyle={markdownSyntax}
                          treeSitterClient={getCommonSyntaxClient()}
                          streaming={false}
                          fg={isInRange ? tuiTheme.text : tuiTheme.muted}
                          conceal
                          concealCode={false}
                          internalBlockMode="top-level"
                        />
                      ) : (
                        <text content=" " />
                      )}
                    </box>
                  </box>

                  {isEditorLine ? (
                    <box
                      flexDirection="column"
                      marginLeft={6}
                      marginRight={2}
                      marginTop={1}
                      marginBottom={1}
                      paddingLeft={1}
                      paddingRight={1}
                      paddingTop={1}
                      paddingBottom={1}
                      backgroundColor={tuiTheme.surfaceElevated}
                    >
                      <text fg={accent}>
                        批注意见（第 {annotationRange.startLine === annotationRange.endLine - 1 ? annotationRange.startLine : `${annotationRange.startLine}-${annotationRange.endLine - 1}`} 行）· Enter 保存 · Esc 取消
                      </text>
                      <textarea
                        ref={annotationRef}
                        focused
                        width="100%"
                        minHeight={2}
                        maxHeight={5}
                        placeholder="写下修改意见后按 Enter 保存"
                        placeholderColor={tuiTheme.muted}
                        textColor={tuiTheme.text}
                        focusedTextColor={tuiTheme.text}
                        backgroundColor={tuiTheme.surface}
                        focusedBackgroundColor={tuiTheme.surface}
                        cursorColor={accent}
                        keyBindings={SUBMIT_ON_ENTER_KEY_BINDINGS}
                        onSubmit={() => {
                          const text = annotationRef.current?.plainText?.trim() ?? ""
                          if (text) {
                            const annotation = createPlanAnnotation(
                              props.interaction.planMarkdown,
                              annotationRange.startLine,
                              annotationRange.endLine,
                              text,
                            )
                            if (annotation) setAnnotations(current => [...current, annotation])
                          }
                          setAnnotationRange(null)
                          setVisualStart(null)
                        }}
                      />
                    </box>
                  ) : null}

                  {lineAnnotations.map(ann => (
                    <box
                      key={ann.id}
                      flexDirection="column"
                      marginLeft={6}
                      marginRight={2}
                      marginTop={1}
                      marginBottom={1}
                      paddingLeft={1}
                      paddingRight={1}
                      paddingTop={1}
                      paddingBottom={1}
                      backgroundColor={tuiTheme.surfaceElevated}
                    >
                      <text fg={accent}>
                        批注 (第 {ann.startLine === ann.endLine - 1 ? ann.startLine : `${ann.startLine}-${ann.endLine - 1}`} 行) · [光标移至该行按 d 键删除]
                      </text>
                      <text fg={tuiTheme.muted} content={`> ${ann.excerpt}`} />
                      <text fg={tuiTheme.text} content={ann.text} />
                    </box>
                  ))}
                </box>
              )
            })}
          </box>
        </scrollbox>
      ) : (
        <box flexGrow={1} minHeight={0} marginTop={1}>
          <text content="还没有写出计划。仍可批准、继续打磨或放弃。" fg={tuiTheme.text} />
        </box>
      )}

      <box height={1} flexShrink={0} marginTop={1} backgroundColor={tuiTheme.border} />

      <box
        flexShrink={0}
        backgroundColor={tuiTheme.surfaceElevated}
        paddingLeft={1}
        paddingRight={1}
        paddingTop={1}
        paddingBottom={1}
        marginTop={1}
        marginBottom={1}
        flexDirection="column"
      >
        {props.interaction.readOnly ? (
          <select
            focused={focusSection === "decision"}
            height={2}
            options={[{ name: "关闭计划", description: "返回会话 (Esc)", value: "close" }]}
            onSelect={() => props.onClose()}
          />
        ) : awaitingFeedback ? (
          <>
            <text fg={tuiTheme.text}>打回整体意见（可空 · Enter 提交 · Esc 取消）</text>
            <textarea
              ref={feedbackRef}
              focused
              width="100%"
              minHeight={2}
              maxHeight={5}
              placeholder="写下意见后按 Enter 提交"
              placeholderColor={tuiTheme.muted}
              textColor={tuiTheme.text}
              focusedTextColor={tuiTheme.text}
              backgroundColor={tuiTheme.surface}
              focusedBackgroundColor={tuiTheme.surface}
              cursorColor={accent}
              keyBindings={SUBMIT_ON_ENTER_KEY_BINDINGS}
              onSubmit={() => {
                props.onPlan("revise", formatPlanReviewFeedback(annotations, feedbackRef.current?.plainText ?? ""))
              }}
            />
            <text fg={tuiTheme.muted}>Enter 提交 · Shift+Enter 换行 · Esc 取消</text>
          </>
        ) : (
          <>
            <box flexDirection="row" justifyContent="space-between" marginBottom={1}>
              <text fg={focusSection === "document" ? accent : tuiTheme.muted}>
                {focusSection === "document"
                  ? "[↑↓/jk] 浏览正文 · [c] 添加批注 · [v] 多行选区 · [d] 删批注 · [Tab] 切至下方选项"
                  : "[↑↓] 选择审批操作 · [Enter] 确认提交 · [Tab] 切回正文浏览"}
              </text>
            </box>
            <select
              focused={focusSection === "decision"}
              height={Math.max(2, Math.min(6, options.length * 2))}
              showDescription
              wrapSelection
              options={options}
              onSelect={(_, option) => {
                const value = option?.value
                if (!isPlanDecision(value)) return
                if (value === "revise") {
                  if (annotations.length > 0) {
                    props.onPlan("revise", formatPlanReviewFeedback(annotations))
                    return
                  }
                  setAwaitingFeedback(true)
                  return
                }
                props.onPlan(value, value === "approved" ? formatPlanReviewFeedback(annotations) : undefined)
              }}
            />
          </>
        )}
      </box>
    </box>
  )
}

/** 问答 Dock：相关多题在底部逐题作答，全部答完再一次提交。 */
export function QuestionDock(props: {
  interaction: Extract<InteractiveSnapshot["interaction"], { type: "question" }>
  workMode: InteractiveSnapshot["workMode"]
  onQuestion: (answers: Record<string, string[]>) => void
}) {
  const accent = modeAccent(props.workMode)
  const questions = props.interaction.questions
  const [index, setIndex] = useState(0)
  const [collected, setCollected] = useState<Record<string, string>>({})
  const [awaitingOther, setAwaitingOther] = useState(false)
  const otherRef = useRef<TextareaRenderable | null>(null)
  const currentIndex = Math.min(index, Math.max(0, questions.length - 1))
  const question = questions[currentIndex]
  const isChoice = Boolean(question?.options.length) && !question?.multiSelect
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

  const accept = (answer: string) => {
    if (!question) return
    const next = recordAskUserAnswer(questions, collected, question.id, answer)
    if (!next.done) {
      setCollected(next.collected)
      setIndex(currentIndex + 1)
      setAwaitingOther(false)
      return
    }
    props.onQuestion(answersByQuestionId(questions, next.collected))
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
      <text fg={accent}>
        {questions.length > 1 ? `Agent 需要你的回答 · ${currentIndex + 1}/${questions.length}` : "Agent 需要你的回答"}
      </text>
      {question?.question ? <text content={question.question} fg={tuiTheme.text} /> : null}
      {!isChoice || awaitingOther ? (
        <>
          <textarea
            key={`${question?.id ?? "q"}-text`}
            ref={otherRef}
            focused
            placeholder={isChoice ? "输入其他答案后按 Enter" : "输入回答后按 Enter"}
            placeholderColor={tuiTheme.muted}
            textColor={tuiTheme.text}
            focusedTextColor={tuiTheme.text}
            backgroundColor={tuiTheme.surface}
            focusedBackgroundColor={tuiTheme.surface}
            cursorColor={accent}
            minHeight={1}
            maxHeight={4}
            keyBindings={SUBMIT_ON_ENTER_KEY_BINDINGS}
            onSubmit={() => {
              const value = otherRef.current?.plainText.trim() ?? ""
              if (value) accept(value)
            }}
          />
          <text fg={tuiTheme.muted}>输入后按 Enter{questions.length > 1 && currentIndex + 1 < questions.length ? "，继续下一题" : ""}</text>
        </>
      ) : (
        <>
          <select
            key={question?.id ?? "q"}
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
              accept(option.value)
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
