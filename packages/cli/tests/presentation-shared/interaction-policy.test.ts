/** 共享 Interaction 展示策略测试：approval 选项顺序、目录信任选项、中文文案与 question 占位值。 */

import { expect, test } from "bun:test"
import {
  APPROVAL_DECISION_ORDER,
  DIRECTORY_TRUST_DECISION_ORDER,
  QUESTION_OTHER_VALUE,
  approvalDecisionDescription,
  approvalDecisionLabel,
  approvalPresentationKind,
  directoryTrustDecisionDescription,
  directoryTrustDecisionLabel,
  isDirectoryTrustDecision,
} from "../../src/presentation-shared/interaction-policy"

test("approval 选项顺序稳定：先批准类、再拒绝类", () => {
  expect(APPROVAL_DECISION_ORDER).toEqual(["approve_once", "approve_thread", "approve_project", "reject", "reject_with_feedback"])
})

test("approval 中文文案两端统一", () => {
  expect(approvalDecisionLabel("approve_once")).toBe("允许一次")
  expect(approvalDecisionLabel("approve_thread")).toBe("本会话允许")
  expect(approvalDecisionLabel("approve_project")).toBe("本项目允许")
  expect(approvalDecisionLabel("reject")).toBe("拒绝")
  expect(approvalDecisionLabel("reject_with_feedback")).toBe("拒绝并反馈")
})

test("approval 选项描述可读且不与标签重复", () => {
  expect(approvalDecisionDescription("approve_once")).toBe("继续执行当前操作")
  expect(approvalDecisionDescription("reject_with_feedback")).toBe("拒绝并附带修改建议")
  expect(approvalDecisionDescription("reject")).toBe("停止此操作并告知 Agent")
})

test("directory_trust 使用独立决策枚举与专用文案", () => {
  expect(DIRECTORY_TRUST_DECISION_ORDER).toEqual(["allow_session", "deny"])
  expect(directoryTrustDecisionLabel("allow_session")).toBe("允许")
  expect(directoryTrustDecisionLabel("deny")).toBe("拒绝")
  expect(directoryTrustDecisionDescription("allow_session")).toBe("本会话将该目录视作工作区")
  expect(directoryTrustDecisionDescription("deny")).toBe("不信任该目录并告知 Agent")
  expect(isDirectoryTrustDecision("allow_session")).toBe(true)
  expect(isDirectoryTrustDecision("approve_thread")).toBe(false)
})

test("presentation kind 读取对畸形结构安全降级", () => {
  expect(approvalPresentationKind({ kind: "file_diff" })).toBe("file_diff")
  expect(approvalPresentationKind({ kind: 1 })).toBeUndefined()
  expect(approvalPresentationKind(null)).toBeUndefined()
})

test("question 其他选项占位值与 agent 端约定一致", () => {
  expect(QUESTION_OTHER_VALUE).toBe("__other__")
})
