/** 跨端共享 Interaction 展示策略：approval 选项顺序/文案、目录信任选项与 question 占位值。 */

import type { ApprovalDecision, DirectoryTrustDecision } from "../interactive/types"

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

/** 仅接受规范 decision 集合内的值；未知/畸形服务端数据在渲染层丢弃。 */
export function isApprovalDecision(value: unknown): value is ApprovalDecision {
  return typeof value === "string" && (APPROVAL_DECISION_ORDER as readonly string[]).includes(value)
}
/** TUI 先答首题；后续题补占位，避免多题 ask_user 因缺答被 Controller 卡住。 */
export function completeQuestionAnswers(
  questions: readonly { id: string }[],
  firstAnswer: string,
): Record<string, string[]> {
  const answers: Record<string, string[]> = {}
  for (const [index, question] of questions.entries()) {
    answers[question.id] = [index === 0 ? firstAnswer : "(no answer)"]
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

