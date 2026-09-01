/** 跨端共享 Interaction 展示策略：approval 选项顺序/文案、目录信任选项与 question 占位值。 */

import type { ApprovalDecision, DirectoryTrustDecision, PlanDecision } from "../interactive/types"

/** question “其他”选项在答案数组中的占位值；与 agent 端约定。 */
export const QUESTION_OTHER_VALUE = "__other__"

/** approval 选项的稳定展示顺序：先批准类、再拒绝类。 */
export const APPROVAL_DECISION_ORDER: readonly ApprovalDecision[] = [
  "approve_once",
  "approve_thread",
  "approve_project",
  "reject",
  "reject_with_feedback",
]

/** approval decision 的展示元数据：中文标签与简短描述，两端共用一份。 */
const APPROVAL_DECISION_META: Readonly<Record<ApprovalDecision, { label: string; description: string }>> = {
  approve_once: { label: "允许一次", description: "继续执行当前操作" },
  approve_thread: { label: "本会话允许", description: "当前会话内不再询问" },
  approve_project: { label: "本项目允许", description: "本项目内同类操作自动放行" },
  reject: { label: "拒绝", description: "停止此操作并告知 Agent" },
  reject_with_feedback: { label: "拒绝并反馈", description: "拒绝并附带修改建议" },
}

/** 目录信任只保留"允许（本会话信任）/ 拒绝"两个选项。 */
export const DIRECTORY_TRUST_DECISION_ORDER: readonly DirectoryTrustDecision[] = [
  "allow_session",
  "deny",
]

/** 目录信任 decision 的展示元数据：独立于审批的 Claude 式文案。 */
const DIRECTORY_TRUST_DECISION_META: Readonly<Record<DirectoryTrustDecision, { label: string; description: string }>> = {
  allow_session: { label: "允许", description: "本会话将该目录视作工作区" },
  deny: { label: "拒绝", description: "不信任该目录并告知 Agent" },
}

export type ApprovalPresentationKind = "file_diff" | string | undefined

export function approvalDecisionLabel(decision: ApprovalDecision): string {
  return APPROVAL_DECISION_META[decision]?.label ?? decision
}

export function approvalDecisionDescription(decision: ApprovalDecision): string {
  return APPROVAL_DECISION_META[decision]?.description ?? ""
}

export function directoryTrustDecisionLabel(decision: DirectoryTrustDecision): string {
  return DIRECTORY_TRUST_DECISION_META[decision]?.label ?? decision
}

export function directoryTrustDecisionDescription(decision: DirectoryTrustDecision): string {
  return DIRECTORY_TRUST_DECISION_META[decision]?.description ?? ""
}

/** 计划审批三个动作，禁止出现 auto-edit 等档位选项。 */
export const PLAN_DECISION_ORDER: readonly PlanDecision[] = [
  "approved",
  "revise",
  "abandoned",
]

const PLAN_DECISION_META: Readonly<Record<PlanDecision, { label: string; description: string }>> = {
  approved: { label: "批准并开始实现", description: "退出计划模式，按进入前权限开始改代码" },
  revise: { label: "继续打磨", description: "留下意见，Agent 继续改计划" },
  abandoned: { label: "放弃计划", description: "退出计划模式，不开始写代码" },
}

export function planDecisionLabel(decision: PlanDecision): string {
  return PLAN_DECISION_META[decision]?.label ?? decision
}

export function planDecisionDescription(decision: PlanDecision): string {
  return PLAN_DECISION_META[decision]?.description ?? ""
}

export function isPlanDecision(value: unknown): value is PlanDecision {
  return typeof value === "string" && (PLAN_DECISION_ORDER as readonly string[]).includes(value)
}

/** 全屏审阅：预览吃掉标题、动作栏和底栏之外的全部行。 */
export function planPreviewHeight(terminalHeight: number): number {
  const chrome = 14
  return Math.max(8, terminalHeight - chrome)
}

/** 打回意见：Enter 提交，Shift+Enter 换行；输入法合成中不提交。 */
export function isPlanFeedbackSubmitKey(event: {
  key: string
  shiftKey?: boolean
  nativeEvent?: { isComposing?: boolean }
}): boolean {
  return event.key === "Enter" && event.shiftKey !== true && event.nativeEvent?.isComposing !== true
}

/** 计划正文的行级批注；endLine 使用半开区间，便于直接映射 String.split 后的 slice。 */
export type PlanAnnotation = {
  id: string
  startLine: number
  endLine: number
  text: string
  excerpt: string
}

let planAnnotationSequence = 0

/** 从原始 Markdown 创建 1-based 半开行范围批注；非法范围或空意见直接拒绝。 */
export function createPlanAnnotation(
  markdown: string,
  startLine: number,
  endLine: number,
  text: string,
): PlanAnnotation | null {
  const lines = markdown.split("\n")
  const normalized = text.trim()
  if (
    !Number.isInteger(startLine)
    || !Number.isInteger(endLine)
    || startLine < 1
    || endLine <= startLine
    || endLine > lines.length + 1
    || !normalized
  ) return null
  planAnnotationSequence += 1
  return {
    id: `plan-annotation-${planAnnotationSequence}`,
    startLine,
    endLine,
    text: normalized,
    excerpt: lines.slice(startLine - 1, endLine - 1).join("\n"),
  }
}

/** 把批准/打回使用的行批注与整体意见编成同一段稳定、可读的模型反馈。 */
export function formatPlanReviewFeedback(
  annotations: readonly PlanAnnotation[],
  overallFeedback = "",
): string {
  const blocks = annotations.map(annotation => {
    const lastLine = annotation.endLine - 1
    const range = annotation.startLine === lastLine
      ? `line ${annotation.startLine}`
      : `lines ${annotation.startLine}-${lastLine}`
    const quoted = annotation.excerpt.split("\n").map(line => `> ${line}`).join("\n")
    return `Proposed plan ${range}:\n${quoted}\nComment: ${annotation.text}`
  })
  const normalized = overallFeedback.trim()
  if (normalized) blocks.push(`Additional feedback:\n${normalized}`)
  return blocks.join("\n\n")
}

/** 仅接受规范 decision 集合内的值；未知/畸形服务端数据在渲染层丢弃。 */
export function isApprovalDecision(value: unknown): value is ApprovalDecision {
  return typeof value === "string" && (APPROVAL_DECISION_ORDER as readonly string[]).includes(value)
}
/** 与 ask_user 工具上限一致：一次最多 5 道相关题。 */
export const MAX_ASK_USER_QUESTIONS = 5

/** 记下当前题的回答；全部收齐后才算完成，不得给未答题填占位。 */
export function recordAskUserAnswer(
  questions: readonly { id: string }[],
  collected: Readonly<Record<string, string>>,
  questionId: string,
  answer: string,
): { collected: Record<string, string>; done: boolean } {
  const next = { ...collected, [questionId]: answer.trim() }
  const done = questions.length > 0 && questions.every(question => Boolean(next[question.id]))
  return { collected: next, done }
}

/** 把已收齐的逐题答案编成 Interaction 需要的 answers map。 */
export function answersByQuestionId(
  questions: readonly { id: string }[],
  collected: Readonly<Record<string, string>>,
): Record<string, string[]> {
  const answers: Record<string, string[]> = {}
  for (const question of questions) {
    const value = collected[question.id]
    if (value) answers[question.id] = [value]
  }
  return answers
}

/** 仅接受规范目录信任 decision 集合内的值。 */
export function isDirectoryTrustDecision(value: unknown): value is DirectoryTrustDecision {
  return typeof value === "string" && (DIRECTORY_TRUST_DECISION_ORDER as readonly string[]).includes(value)
}

/** 从 presentation 中安全读取 kind；未知结构返回 undefined。 */
export function approvalPresentationKind(presentation: unknown): ApprovalPresentationKind {
  if (!presentation || typeof presentation !== "object") return undefined
  const kind = (presentation as { kind?: unknown }).kind
  return typeof kind === "string" ? kind : undefined
}
