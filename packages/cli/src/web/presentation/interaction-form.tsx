/** Interaction 表单：approval 按钮与 question 全量答案。 */
/** @jsxImportSource react */

import { useEffect, useState } from "react"
import { Check, Loader2 } from "lucide-react"

import type { ApprovalDecision, InteractiveInteraction, InteractiveQuestion } from "../../interactive/types"
import type { WebAdapterSnapshot, WebIntent } from "../application/adapter"

/** 把 approval 的 requests（unknown 载荷）安全收敛为 mono 预览 JSON；空或不可序列化时返回 null。 */
function requestPreview(requests: unknown): string | null {
  if (!Array.isArray(requests) || requests.length === 0) return null
  try {
    return JSON.stringify(requests, null, 2)
  } catch {
    return null
  }
}

/** approval decision 与中文显示文案；保持与 TUI 列表一致。 */
const APPROVAL_LABELS: Readonly<Record<ApprovalDecision, string>> = {
  approve_once: "允许一次",
  approve_thread: "允许此会话",
  approve_always: "始终允许",
  reject: "拒绝",
  reject_with_feedback: "拒绝并反馈",
}

/** question “其他”选项在答案数组中的占位值；与 agent 端约定。 */
const QUESTION_OTHER_VALUE = "__other__"

/** 倒计时刷新间隔；只在 deadline 临近时精度才有意义，保持 1s 节流。 */
const DEADLINE_TICK_MS = 1000

/**
 * 渲染 snapshot.interactive.interaction 对应的 approval / question 表单。
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

  const handleDecision = (next: ApprovalDecision) => {
    if (buttonsDisabled) return
    setDecision(next)
    dispatch({
      type: "interaction-draft-change",
      requestId: interaction.requestId,
      patch: { kind: "approval-decision", value: next },
    })
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
    setSubmitting(true)
    const response: { kind: "approval"; decision: ApprovalDecision; feedback?: string } = { kind: "approval", decision }
    if (decision === "reject_with_feedback" && feedback.length > 0) response.feedback = feedback
    try {
      await dispatch({ type: "interaction-submit", requestId: interaction.requestId, response })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="approval-form">
      <header className="interaction-header">
        <h3 className="interaction-title">需要批准</h3>
        <Deadline deadlineAtMs={interaction.deadlineAtMs} />
      </header>
      {interaction.description ? <p className="interaction-description">{interaction.description}</p> : null}
      {preview ? <pre className="approval-request-preview">{preview}</pre> : null}
      <div className="approval-buttons" role="group" aria-label="审批选项">
        {interaction.decisions.map(item => (
          <button
            type="button"
            key={item}
            className={decisionButtonClass(item, decision)}
            aria-pressed={decision === item}
            disabled={buttonsDisabled}
            onClick={() => handleDecision(item)}
          >
            {APPROVAL_LABELS[item]}
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

/** 倒计时展示：本地 setInterval 节流；过期时显示“已超时”。 */
function Deadline(props: { deadlineAtMs: number }): React.ReactElement {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), DEADLINE_TICK_MS)
    return () => clearInterval(timer)
  }, [props.deadlineAtMs])
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
