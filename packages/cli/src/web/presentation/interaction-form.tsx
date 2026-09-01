/** Interaction 表单：approval 按钮、directory_trust 信任选项与 question 全量答案。 */
/** @jsxImportSource react */

import { useEffect, useRef, useState } from "react"
import { Check, Loader2 } from "lucide-react"

import type { ApprovalDecision, DirectoryTrustDecision, InteractiveInteraction, InteractiveQuestion, PlanDecision } from "../../interactive/types"
import {
  QUESTION_OTHER_VALUE,
  approvalDecisionLabel,
  directoryTrustDecisionDescription,
  directoryTrustDecisionLabel,
  isApprovalDecision,
  isDirectoryTrustDecision,
  isPlanDecision,
  isPlanFeedbackSubmitKey,
  createPlanAnnotation,
  formatPlanReviewFeedback,
  planDecisionDescription,
  planDecisionLabel,
  type PlanAnnotation,
} from "../../presentation-shared/interaction-policy"
import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"
import { DirectoryTrustApproval } from "./directory-trust-approval"
import { FileDiffApproval } from "./file-diff-approval"
import { Markdown } from "./markdown"

/** 把 approval 的 requests（unknown 载荷）安全收敛为 mono 预览 JSON；空或不可序列化时返回 null。 */
function requestPreview(requests: unknown): string | null {
  if (!Array.isArray(requests) || requests.length === 0) return null
  try {
    return JSON.stringify(requests, null, 2)
  } catch {
    return null
  }
}

/** 倒计时刷新间隔；只在 deadline 临近时精度才有意义，保持 1s 节流。 */
const DEADLINE_TICK_MS = 1000

/**
 * 渲染 snapshot.interactive.interaction 对应的 approval / directory_trust / question 表单。
 *
 * - adapter 已按 requestId 原子重置 interactionDraft；本组件不持有跨请求的本地答案。
 * - 每次 render 都从 snapshot.interactionDraft 派生当前值，避免提交过期答案。
 * - 提交期间用本地 submitting 锁住重复点击；snapshot 没有 busy 标志。
 */
export function InteractionForm(props: {
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void | Promise<void>
  disabled?: boolean
}): React.ReactNode {
  const interaction = props.snapshot.interactive.interaction
  if (!interaction) return null

  return (
    <section className="interaction-card" aria-label="待处理请求">
      {interaction.type === "approval"
        ? <ApprovalForm interaction={interaction} snapshot={props.snapshot} dispatch={props.dispatch} disabled={props.disabled === true} />
        : interaction.type === "directory_trust"
          ? <DirectoryTrustForm interaction={interaction} dispatch={props.dispatch} disabled={props.disabled === true} />
          : interaction.type === "plan"
            ? <PlanForm interaction={interaction} dispatch={props.dispatch} disabled={props.disabled === true} />
          : <QuestionForm interaction={interaction} snapshot={props.snapshot} dispatch={props.dispatch} disabled={props.disabled === true} />}
    </section>
  )
}

/** approval 表单：决策按钮 + 可选反馈输入。 */
function ApprovalForm(props: {
  interaction: Extract<InteractiveInteraction, { type: "approval" }>
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void | Promise<void>
  disabled: boolean
}): React.ReactElement {
  const { interaction, snapshot, dispatch, disabled } = props
  const draft = pickDraft(snapshot, interaction.requestId)
  const [submitting, setSubmitting] = useState(false)
  const [decision, setDecision] = useState<ApprovalDecision | null>(draft?.approvalDecision ?? null)
  const [feedback, setFeedback] = useState<string>(draft?.feedback ?? "")

  // 适配器按 requestId 原子清空草稿：组件本身不依赖跨请求本地答案。
  useEffect(() => {
    setDecision(draft?.approvalDecision ?? null)
    setFeedback(draft?.feedback ?? "")
  }, [draft, interaction.requestId])

  const allowFeedback = interaction.decisions.includes("reject_with_feedback")
  const showFeedback = allowFeedback && decision === "reject_with_feedback"
  const expired = isExpired(interaction.deadlineAtMs)
  const buttonsDisabled = submitting || disabled || expired
  const preview = requestPreview(interaction.requests)

  const submitApproval = async (next: ApprovalDecision, nextFeedback = "") => {
    setSubmitting(true)
    const response: { kind: "approval"; decision: ApprovalDecision; feedback?: string } = { kind: "approval", decision: next }
    if (next === "reject_with_feedback" && nextFeedback.length > 0) response.feedback = nextFeedback
    try {
      await dispatch({ type: "interaction-submit", requestId: interaction.requestId, response })
    } finally {
      setSubmitting(false)
    }
  }

  const handleDecision = async (next: ApprovalDecision) => {
    if (buttonsDisabled) return
    setDecision(next)
    await dispatch({
      type: "interaction-draft-change",
      requestId: interaction.requestId,
      patch: { kind: "approval-decision", value: next },
    })
    // 拒绝是 fail-closed 安全动作，点击即生效；批准仍保留二次提交，避免误触写入。
    if (next === "reject") await submitApproval(next)
  }

  const handleFeedback = (value: string) => {
    if (buttonsDisabled) return
    setFeedback(value)
    dispatch({
      type: "interaction-draft-change",
      requestId: interaction.requestId,
      patch: { kind: "feedback", value },
    })
  }

  const handleSubmit = async () => {
    if (buttonsDisabled) return
    if (!decision) return
    if (decision === "reject_with_feedback" && feedback.trim().length === 0) return
    await submitApproval(decision, feedback)
  }

  const presentation = interaction.presentation
  const title = interaction.agentId && interaction.agentId !== "main"
    ? `子代理 ${interaction.agentId} 需要审批`
    : "需要批准"

  return (
    <div className="approval-form">
      <header className="interaction-header">
        <h3 className="interaction-title">{title}</h3>
        <Deadline deadlineAtMs={interaction.deadlineAtMs} />
      </header>
      {interaction.description ? <p className="interaction-description">{interaction.description}</p> : null}
      {presentation
        ? <FileDiffApproval presentation={presentation} requests={interaction.requests} />
        : preview ? <pre className="approval-request-preview">{preview}</pre> : null}
      <div className="approval-buttons" role="group" aria-label="审批选项">
        {interaction.decisions.filter(isApprovalDecision).map(item => (
          <button
            type="button"
            key={item}
            className={decisionButtonClass(item, decision)}
            aria-pressed={decision === item}
            disabled={buttonsDisabled}
            onClick={() => { void handleDecision(item) }}
          >
            {approvalDecisionLabel(item)}
          </button>
        ))}
      </div>
      {allowFeedback ? (
        <label className="interaction-feedback">
          <span className="interaction-feedback-label">反馈（仅拒绝时必填）</span>
          <textarea
            className="interaction-feedback-input"
            rows={3}
            value={feedback}
            disabled={buttonsDisabled || !showFeedback}
            onInput={event => handleFeedback(event.currentTarget.value)}
            placeholder={showFeedback ? "请说明拒绝原因" : "选择「拒绝并反馈」后填写"}
            aria-label="拒绝反馈"
          />
        </label>
      ) : null}
      <div className="interaction-actions">
        <button
          type="button"
          className="interaction-submit"
          onClick={() => { void handleSubmit() }}
          disabled={buttonsDisabled || !decision || (decision === "reject_with_feedback" && feedback.trim().length === 0)}
        >
          {submitting ? <Loader2 aria-hidden="true" className="interaction-submit-spinner" /> : <Check aria-hidden="true" />}
          <span>提交</span>
        </button>
      </div>
    </div>
  )
}

/** 目录信任表单：Claude 式独立卡片，点击选项即回写决策。 */
function DirectoryTrustForm(props: {
  interaction: Extract<InteractiveInteraction, { type: "directory_trust" }>
  dispatch: (intent: WebIntent) => void | Promise<void>
  disabled: boolean
}): React.ReactElement {
  const { interaction, dispatch, disabled } = props
  const [submitting, setSubmitting] = useState(false)
  const expired = isExpired(interaction.deadlineAtMs)
  const buttonsDisabled = submitting || disabled || expired

  const submit = async (decision: DirectoryTrustDecision) => {
    if (buttonsDisabled) return
    setSubmitting(true)
    try {
      await dispatch({
        type: "interaction-submit",
        requestId: interaction.requestId,
        response: { kind: "directory_trust", decision },
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="approval-form directory-trust-form">
      <header className="interaction-header">
        <h3 className="interaction-title">{
          interaction.agentId && interaction.agentId !== "main"
            ? `子代理 ${interaction.agentId} 需要目录信任`
            : "目录信任"
        }</h3>
        <Deadline deadlineAtMs={interaction.deadlineAtMs} />
      </header>
      <p className="interaction-description">是否将此目录加入白名单？</p>
      <DirectoryTrustApproval interaction={interaction} />
      <div className="approval-buttons" role="group" aria-label="目录信任选项">
        {interaction.decisions.filter(isDirectoryTrustDecision).map(item => (
          <button
            type="button"
            key={item}
            className={item === "deny" ? "approval-button approval-button-negative" : "approval-button approval-button-positive"}
            disabled={buttonsDisabled}
            onClick={() => { void submit(item) }}
          >
            {submitting ? <Loader2 aria-hidden="true" className="interaction-submit-spinner" /> : null}
            <span>{directoryTrustDecisionLabel(item)}</span>
            <span className="approval-button-description">{directoryTrustDecisionDescription(item)}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

/** 计划审批卡：内联交互式审阅 + 就地行批注卡片 + 三个动作。 */
function PlanForm(props: {
  interaction: Extract<InteractiveInteraction, { type: "plan" }>
  dispatch: (intent: WebIntent) => void | Promise<void>
  disabled: boolean
}): React.ReactElement {
  const { interaction, dispatch, disabled } = props
  const [submitting, setSubmitting] = useState(false)
  const [feedback, setFeedback] = useState("")
  const [annotations, setAnnotations] = useState<PlanAnnotation[]>([])
  const [annotating, setAnnotating] = useState(false)
  const [selection, setSelection] = useState<{ startLine: number; endLine: number } | null>(null)
  const [editingRange, setEditingRange] = useState<{ startLine: number; endLine: number } | null>(null)
  const [annotationText, setAnnotationText] = useState("")
  const annotationTextRef = useRef<HTMLTextAreaElement | null>(null)
  const expired = isExpired(interaction.deadlineAtMs)
  const buttonsDisabled = submitting || disabled || (!interaction.readOnly && expired)
  const decisions = interaction.decisions.filter(isPlanDecision)
  const lines = interaction.planMarkdown ? interaction.planMarkdown.split("\n") : []

  const submit = async (decision: PlanDecision) => {
    if (buttonsDisabled) return
    setSubmitting(true)
    const reviewFeedback = formatPlanReviewFeedback(annotations, feedback)
    try {
      await dispatch({
        type: "interaction-submit",
        requestId: interaction.requestId,
        response: {
          kind: "plan",
          decision,
          feedback: decision === "abandoned" || !reviewFeedback ? undefined : reviewFeedback,
        },
      })
    } finally {
      setSubmitting(false)
    }
  }

  const handleLineClick = (lineNumber: number) => {
    if (interaction.readOnly) return
    setSelection(current => {
      const next = current
        ? {
            startLine: Math.min(current.startLine, lineNumber),
            endLine: Math.max(current.endLine, lineNumber + 1),
          }
        : { startLine: lineNumber, endLine: lineNumber + 1 }
      setEditingRange(next)
      return next
    })
  }

  const startAnnotatingForLine = (lineNumber: number, event?: React.MouseEvent) => {
    event?.stopPropagation()
    setSelection({ startLine: lineNumber, endLine: lineNumber + 1 })
    setEditingRange({ startLine: lineNumber, endLine: lineNumber + 1 })
    setAnnotationText("")
  }

  const startAnnotating = () => {
    setAnnotating(true)
    setSelection(null)
    setEditingRange(null)
    setAnnotationText("")
  }

  const saveAnnotation = () => {
    const range = editingRange ?? selection
    if (!range) return
    const text = annotationTextRef.current?.value ?? annotationText
    const annotation = createPlanAnnotation(
      interaction.planMarkdown,
      range.startLine,
      range.endLine,
      text,
    )
    if (!annotation) return
    setAnnotations(current => [...current, annotation])
    setSelection(null)
    setEditingRange(null)
    setAnnotationText("")
    setAnnotating(false)
  }

  const cancelAnnotation = () => {
    setSelection(null)
    setEditingRange(null)
    setAnnotationText("")
    setAnnotating(false)
  }

  const removeAnnotation = (id: string) => {
    setAnnotations(current => current.filter(item => item.id !== id))
  }

  const activeRange = editingRange ?? selection

  return (
    <div className="approval-form plan-form">
      <header className="interaction-header">
        <h3 className="interaction-title">{interaction.readOnly ? "当前计划" : "审阅计划"}</h3>
        {interaction.readOnly ? null : <Deadline deadlineAtMs={interaction.deadlineAtMs} />}
      </header>
      <div className="plan-meta-row">
        <p className="interaction-description">{interaction.planDisplayPath}</p>
        {!interaction.readOnly && interaction.hasPlan ? (
          <div className="plan-annotation-status-bar">
            {annotations.length > 0 ? (
              <span className="plan-annotation-count-badge">{annotations.length} 条批注</span>
            ) : null}
            {!activeRange ? (
              <button
                type="button"
                className="plan-add-annotation-btn"
                onClick={startAnnotating}
              >
                + 添加批注
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="plan-preview" aria-label="计划正文">
        {interaction.hasPlan ? (
          <div className="plan-line-container">
            {lines.map((line, index) => {
              const lineNumber = index + 1
              const isSelected = Boolean(activeRange && lineNumber >= activeRange.startLine && lineNumber < activeRange.endLine)
              const lineAnnotations = annotations.filter(a => a.endLine - 1 === lineNumber)
              const isEditorLine = Boolean(activeRange && activeRange.endLine - 1 === lineNumber)

              return (
                <div key={lineNumber} className="plan-line-wrapper">
                  <div
                    className={`plan-line-row plan-annotation-line${isSelected ? " is-selected" : ""}`}
                    role="button"
                    tabIndex={0}
                    aria-pressed={isSelected}
                    onClick={() => handleLineClick(lineNumber)}
                    onKeyDown={event => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault()
                        handleLineClick(lineNumber)
                      }
                    }}
                  >
                    <div className="plan-line-gutter">
                      <span className="plan-line-number">{lineNumber}</span>
                      {!interaction.readOnly ? (
                        <button
                          type="button"
                          className="plan-line-add-btn"
                          title={`为第 ${lineNumber} 行添加批注`}
                          aria-label={`为第 ${lineNumber} 行添加批注`}
                          onClick={event => startAnnotatingForLine(lineNumber, event)}
                        >
                          + 批注
                        </button>
                      ) : null}
                    </div>
                    <div className="plan-line-content">
                      {line.trim().length > 0 ? (
                        <Markdown text={line} />
                      ) : (
                        <span className="plan-line-blank">&nbsp;</span>
                      )}
                    </div>
                  </div>

                  {isEditorLine && activeRange && !interaction.readOnly ? (
                    <div className="plan-inline-editor">
                      <div className="plan-inline-editor-header">
                        <span>批注 (第 {activeRange.startLine === activeRange.endLine - 1 ? activeRange.startLine : `${activeRange.startLine}-${activeRange.endLine - 1}`} 行)</span>
                      </div>
                      <textarea
                        ref={annotationTextRef}
                        rows={2}
                        value={annotationText}
                        onInput={event => setAnnotationText(event.currentTarget.value)}
                        onKeyDown={event => {
                          if (isPlanFeedbackSubmitKey(event)) {
                            event.preventDefault()
                            saveAnnotation()
                          } else if (event.key === "Escape") {
                            event.preventDefault()
                            cancelAnnotation()
                          }
                        }}
                        placeholder="说明所选原始 Markdown 行需要怎样调整 (Enter 保存，Shift+Enter 换行，Esc 取消)"
                        aria-label="行批注意见"
                        autoFocus
                      />
                      <div className="plan-inline-editor-actions">
                        <button type="button" className="plan-btn-cancel" onClick={cancelAnnotation}>取消</button>
                        <button type="button" className="plan-btn-save" onClick={saveAnnotation}>保存批注</button>
                      </div>
                    </div>
                  ) : null}

                  {lineAnnotations.map(ann => (
                    <div key={ann.id} className="plan-inline-annotation-card">
                      <div className="plan-annotation-card-header">
                        <span>批注 (第 {ann.startLine === ann.endLine - 1 ? ann.startLine : `${ann.startLine}-${ann.endLine - 1}`} 行)</span>
                        {!interaction.readOnly ? (
                          <button
                            type="button"
                            className="plan-annotation-card-delete"
                            title="删除批注"
                            onClick={event => {
                              event.stopPropagation()
                              removeAnnotation(ann.id)
                            }}
                          >
                            ✕
                          </button>
                        ) : null}
                      </div>
                      <blockquote className="plan-annotation-card-excerpt">{ann.excerpt}</blockquote>
                      <div className="plan-annotation-card-text">{ann.text}</div>
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        ) : (
          <div className="plan-empty-placeholder">还没有写出计划。仍可批准、继续打磨或放弃。</div>
        )}
      </div>

      {interaction.readOnly ? (
        <div className="interaction-actions">
          <button type="button" className="interaction-submit" onClick={() => { void dispatch({ type: "plan-view-close" }) }}>关闭</button>
        </div>
      ) : (
        <>
          {annotations.length === 0 ? (
            <label className="interaction-feedback">
              <span className="interaction-feedback-label">整体意见（选择「继续打磨」时可选）</span>
              <textarea
                className="interaction-feedback-input"
                rows={2}
                value={feedback}
                disabled={buttonsDisabled}
                onInput={event => setFeedback(event.currentTarget.value)}
                onKeyDown={event => {
                  if (!isPlanFeedbackSubmitKey(event)) return
                  event.preventDefault()
                  void submit("revise")
                }}
                placeholder="选择「继续打磨」时可写下意见；Enter 提交，Shift+Enter 换行"
                aria-label="计划打回意见"
              />
            </label>
          ) : null}
          <div className="approval-buttons" role="group" aria-label="计划审批选项">
            {(annotations.length > 0
              ? [
                  {
                    decision: "revise" as PlanDecision,
                    label: `提交批注并继续打磨 (${annotations.length} 条批注)`,
                    description: "按所选行批注意见调整计划后再提交审阅",
                  },
                  {
                    decision: "abandoned" as PlanDecision,
                    label: "放弃计划",
                    description: "放弃本次规划，退出计划模式",
                  },
                ]
              : decisions.map(item => ({
                  decision: item,
                  label: planDecisionLabel(item),
                  description: planDecisionDescription(item),
                }))
            ).filter(item => decisions.includes(item.decision)).map(item => (
              <button
                type="button"
                key={item.decision}
                className={item.decision === "abandoned" ? "approval-button approval-button-negative" : "approval-button approval-button-positive"}
                disabled={buttonsDisabled}
                onClick={() => { void submit(item.decision) }}
              >
                {submitting ? <Loader2 aria-hidden="true" className="interaction-submit-spinner" /> : null}
                <span>{item.label}</span>
                <span className="approval-button-description">{item.description}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/** question 表单：所有问题一次性渲染，单选/多选/其他。 */
function QuestionForm(props: {
  interaction: Extract<InteractiveInteraction, { type: "question" }>
  snapshot: WebAdapterSnapshot
  dispatch: (intent: WebIntent) => void | Promise<void>
  disabled: boolean
}): React.ReactElement {
  const { interaction, snapshot, dispatch, disabled } = props
  const draft = pickDraft(snapshot, interaction.requestId)
  const answers = draft?.answers ?? {}
  const [submitting, setSubmitting] = useState(false)
  const [textDraft, setTextDraft] = useState<Record<string, string>>({})

  useEffect(() => {
    setTextDraft({})
  }, [interaction.requestId])

  const allAnswered = interaction.questions.every(question => isAnswered(
    question,
    answers,
    textDraft[question.id] ?? "",
  ))
  const expired = isExpired(interaction.deadlineAtMs)
  const buttonsDisabled = submitting || disabled || !allAnswered || expired

  const handleSelect = (question: InteractiveQuestion, values: readonly string[]) => {
    if (submitting || disabled || expired) return
    dispatch({
      type: "interaction-draft-change",
      requestId: interaction.requestId,
      patch: { kind: "answer", questionId: question.id, values: values.slice() },
    })
  }

  const handleSubmit = async () => {
    if (buttonsDisabled) return
    setSubmitting(true)
    const payload: Record<string, string[]> = {}
    for (const question of interaction.questions) {
      const values = collectValues(question, answers[question.id] ?? [], textDraft[question.id] ?? "")
      payload[question.id] = values
    }
    try {
      await dispatch({
        type: "interaction-submit",
        requestId: interaction.requestId,
        response: { kind: "question", answers: payload },
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="question-form">
      <header className="interaction-header">
        <h3 className="interaction-title">需要回答</h3>
        <Deadline deadlineAtMs={interaction.deadlineAtMs} />
      </header>
      <div className="question-list">
        {interaction.questions.map(question => (
          <QuestionGroup
            key={question.id}
            question={question}
            selected={answers[question.id] ?? []}
            otherText={textDraft[question.id] ?? ""}
            disabled={submitting || disabled || expired}
            onSelect={values => handleSelect(question, values)}
            onOtherTextChange={value => setTextDraft(previous => ({ ...previous, [question.id]: value }))}
          />
        ))}
      </div>
      <div className="interaction-actions">
        <button
          type="button"
          className="interaction-submit"
          onClick={() => { void handleSubmit() }}
          disabled={buttonsDisabled || expired}
        >
          {submitting ? <Loader2 aria-hidden="true" className="interaction-submit-spinner" /> : <Check aria-hidden="true" />}
          <span>提交</span>
        </button>
      </div>
    </div>
  )
}

/** 单个 question 渲染：radio / checkbox + 可选“其他”文本。 */
function QuestionGroup(props: {
  question: InteractiveQuestion
  selected: readonly string[]
  otherText: string
  disabled: boolean
  onSelect: (values: readonly string[]) => void
  onOtherTextChange: (value: string) => void
}): React.ReactElement {
  const { question, selected, otherText, disabled } = props
  const inputType = question.multiSelect ? "checkbox" : "radio"
  const otherSelected = selected.includes(QUESTION_OTHER_VALUE)

  const handleCheck = (value: string, checked: boolean) => {
    if (question.multiSelect) {
      const next = new Set(selected)
      if (checked) next.add(value)
      else next.delete(value)
      props.onSelect([...next])
      return
    }
    props.onSelect([value])
  }

  return (
    <fieldset className="question-group" disabled={disabled}>
      <legend className="question-group-title">{question.question}</legend>
      {question.header ? <p className="question-group-header">{question.header}</p> : null}
      {question.body ? <p className="question-group-body">{question.body}</p> : null}
      <div className="question-options">
        {question.options.map(option => {
          const checked = selected.includes(option.value)
          return (
            <label key={option.value} className="question-option" data-checked={checked}>
              <input
                type={inputType}
                name={`question-${question.id}`}
                value={option.value}
                checked={checked}
                onChange={event => handleCheck(option.value, event.target.checked)}
              />
              <span className="question-option-label">{option.label}</span>
              {option.description ? <span className="question-option-description">{option.description}</span> : null}
            </label>
          )
        })}
        {question.allowOther ? (
          <label className="question-option question-option-other" data-checked={otherSelected}>
            <input
              type={inputType}
              name={`question-${question.id}-other`}
              value={QUESTION_OTHER_VALUE}
              checked={otherSelected}
              onChange={event => {
                if (question.multiSelect) {
                  handleCheck(QUESTION_OTHER_VALUE, event.target.checked)
                } else {
                  props.onSelect([QUESTION_OTHER_VALUE])
                }
              }}
            />
            <span className="question-option-label">其他</span>
            <input
              type="text"
              className="question-option-other-input"
              value={otherText}
              onInput={event => props.onOtherTextChange(event.currentTarget.value)}
              disabled={!otherSelected || disabled}
              aria-label={`${question.question} · 其他`}
            />
          </label>
        ) : null}
      </div>
    </fieldset>
  )
}

/** 倒计时展示：本地 setInterval 节流；无超时或无限时返回 null。 */
function Deadline(props: { deadlineAtMs: number }): React.ReactElement | null {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!Number.isFinite(props.deadlineAtMs)) return
    const timer = setInterval(() => setNow(Date.now()), DEADLINE_TICK_MS)
    return () => clearInterval(timer)
  }, [props.deadlineAtMs])
  if (!Number.isFinite(props.deadlineAtMs)) return null
  const remaining = props.deadlineAtMs - now
  if (remaining <= 0) {
    return <span className="interaction-deadline interaction-deadline-expired">已超时</span>
  }
  return <span className="interaction-deadline">剩余 {formatRemaining(remaining)}</span>
}

/** 把毫秒格式化为“X分Y秒”或“N秒”。 */
function formatRemaining(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes > 0) return `${minutes}分${seconds}秒`
  return `${seconds}秒`
}

/** deadlineAtMs 已经过去：直接禁用提交。 */
function isExpired(deadlineAtMs: number): boolean {
  return deadlineAtMs - Date.now() <= 0
}

function decisionButtonClass(decision: ApprovalDecision, selected: ApprovalDecision | null): string {
  const base = "approval-button"
  const tone = decision.startsWith("approve") ? "approval-button-positive" : "approval-button-negative"
  const state = decision === selected ? "approval-button-selected" : ""
  return [base, tone, state].filter(Boolean).join(" ")
}

function pickDraft(snapshot: WebAdapterSnapshot, requestId: string) {
  const draft = snapshot.interactionDraft
  if (!draft || draft.requestId !== requestId) return null
  return draft
}

function collectValues(
  question: InteractiveQuestion,
  selected: readonly string[],
  otherText: string,
): string[] {
  const values = selected.filter(value => value !== QUESTION_OTHER_VALUE)
  if (question.allowOther && selected.includes(QUESTION_OTHER_VALUE)) {
    const trimmed = otherText.trim()
    if (trimmed.length > 0) values.push(trimmed)
  }
  return values
}

function isAnswered(
  question: InteractiveQuestion,
  answers: Readonly<Record<string, readonly string[]>>,
  otherText: string,
): boolean {
  const values = answers[question.id] ?? []
  if (values.length === 0) return false
  if (values.length === 1 && values[0] === QUESTION_OTHER_VALUE) return otherText.trim().length > 0
  return true
}
