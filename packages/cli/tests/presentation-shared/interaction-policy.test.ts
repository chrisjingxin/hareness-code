/** 共享 Interaction 展示策略测试：approval 选项顺序、中文文案与 question 占位值。 */

import { expect, test } from "bun:test"
import {
  APPROVAL_DECISION_ORDER,
  QUESTION_OTHER_VALUE,
  approvalDecisionDescription,
  approvalDecisionLabel,
} from "../../src/presentation-shared/interaction-policy"

test("approval 选项顺序稳定：先批准类、再拒绝类", () => {
  expect(APPROVAL_DECISION_ORDER).toEqual(["approve_once", "approve_thread", "approve_always", "reject", "reject_with_feedback"])
})

test("approval 中文文案两端统一", () => {
  expect(approvalDecisionLabel("approve_once")).toBe("允许一次")
  expect(approvalDecisionLabel("approve_thread")).toBe("本线程允许")
  expect(approvalDecisionLabel("approve_always")).toBe("永久允许")
  expect(approvalDecisionLabel("reject")).toBe("拒绝")
  expect(approvalDecisionLabel("reject_with_feedback")).toBe("拒绝并反馈")
})

test("approval 选项描述可读且不与标签重复", () => {
  expect(approvalDecisionDescription("approve_once")).toBe("继续执行当前操作")
  expect(approvalDecisionDescription("reject_with_feedback")).toBe("拒绝并附带修改建议")
  expect(approvalDecisionDescription("reject")).toBe("停止此操作并告知 Agent")
})

test("question 其他选项占位值与 agent 端约定一致", () => {
  expect(QUESTION_OTHER_VALUE).toBe("__other__")
})
