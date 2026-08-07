/** 跨端共享 Interaction 展示策略：approval 选项顺序/文案与 question 占位值。 */

import type { ApprovalDecision } from "../interactive/types"

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

export function approvalDecisionLabel(decision: ApprovalDecision): string {
  return APPROVAL_DECISION_META[decision]?.label ?? decision
}

export function approvalDecisionDescription(decision: ApprovalDecision): string {
  return APPROVAL_DECISION_META[decision]?.description ?? ""
}

/** 仅接受规范 decision 集合内的值；未知/畸形服务端数据在渲染层丢弃。 */
export function isApprovalDecision(value: unknown): value is ApprovalDecision {
  return typeof value === "string" && (APPROVAL_DECISION_ORDER as readonly string[]).includes(value)
}
