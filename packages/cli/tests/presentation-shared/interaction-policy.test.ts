/** 共享 Interaction 展示策略测试：approval 选项顺序、目录信任选项、中文文案与 question 占位值。 */

import { expect, test } from "bun:test"
import {
  APPROVAL_DECISION_ORDER,
  DIRECTORY_TRUST_DECISION_ORDER,
  QUESTION_OTHER_VALUE,
  approvalDecisionDescription,
  approvalDecisionLabel,
  approvalPresentationKind,
  MAX_ASK_USER_QUESTIONS,
  answersByQuestionId,
  recordAskUserAnswer,
  directoryTrustDecisionDescription,
  directoryTrustDecisionLabel,
  isDirectoryTrustDecision,
  PLAN_DECISION_ORDER,
  isPlanDecision,
  planDecisionDescription,
  planDecisionLabel,
  planPreviewHeight,
  isPlanFeedbackSubmitKey,
  createPlanAnnotation,
  formatPlanReviewFeedback,
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

test("计划审批三个中文动作，不含审批档位选项", () => {
  expect(PLAN_DECISION_ORDER).toEqual(["approved", "revise", "abandoned"])
  expect(planDecisionLabel("approved")).toBe("批准并开始实现")
  expect(planDecisionLabel("revise")).toBe("继续打磨")
  expect(planDecisionLabel("abandoned")).toBe("放弃计划")
  expect(planDecisionDescription("approved")).toContain("进入前权限")
  expect(isPlanDecision("approved")).toBe(true)
  expect(isPlanDecision("auto-edit")).toBe(false)
  expect(isPlanDecision("approve_once")).toBe(false)
})

test("计划预览占满终端减去标题和动作栏", () => {
  expect(planPreviewHeight(24)).toBe(10)
  expect(planPreviewHeight(40)).toBe(26)
  expect(planPreviewHeight(80)).toBe(66)
})

test("打回意见 Enter 提交、Shift+Enter 换行", () => {
  expect(isPlanFeedbackSubmitKey({ key: "Enter" })).toBe(true)
  expect(isPlanFeedbackSubmitKey({ key: "Enter", shiftKey: true })).toBe(false)
  expect(isPlanFeedbackSubmitKey({ key: "Enter", nativeEvent: { isComposing: true } })).toBe(false)
  expect(isPlanFeedbackSubmitKey({ key: "a" })).toBe(false)
})

test("计划批注锚定原始 Markdown 的 1-based 行范围，并保留摘录", () => {
  expect(createPlanAnnotation(
    "# 方案\n第一步：保留协议\n第二步：替换界面\n验证测试",
    2,
    4,
    "这两步不要拆开",
  )).toEqual({
    id: expect.any(String),
    startLine: 2,
    endLine: 4,
    text: "这两步不要拆开",
    excerpt: "第一步：保留协议\n第二步：替换界面",
  })
  expect(createPlanAnnotation("只有一行", 0, 1, "越界")).toBeNull()
  expect(createPlanAnnotation("只有一行", 1, 2, "   ")).toBeNull()
})

test("计划批注与整体意见统一编成模型可读 feedback", () => {
  const annotation = createPlanAnnotation(
    "# 方案\n先改协议\n再改界面",
    2,
    3,
    "不要修改协议",
  )!
  const feedback = formatPlanReviewFeedback([annotation], "请先补风险说明")
  expect(feedback).toContain("Proposed plan line 2:")
  expect(feedback).toContain("> 先改协议")
  expect(feedback).toContain("Comment: 不要修改协议")
  expect(feedback).toContain("Additional feedback:\n请先补风险说明")
})

test("presentation kind 读取对畸形结构安全降级", () => {
  expect(approvalPresentationKind({ kind: "file_diff" })).toBe("file_diff")
  expect(approvalPresentationKind({ kind: 1 })).toBeUndefined()
  expect(approvalPresentationKind(null)).toBeUndefined()
})

test("question 其他选项占位值与 agent 端约定一致", () => {
  expect(QUESTION_OTHER_VALUE).toBe("__other__")
})

test("多题必须收齐每题答案，不得给未答题填占位", () => {
  const questions = [{ id: "question-1" }, { id: "question-2" }]
  const first = recordAskUserAnswer(questions, {}, "question-1", "基础语法示例")
  expect(first.done).toBe(false)
  expect(first.collected).toEqual({ "question-1": "基础语法示例" })
  const second = recordAskUserAnswer(questions, first.collected, "question-2", "给同事看")
  expect(second.done).toBe(true)
  expect(answersByQuestionId(questions, second.collected)).toEqual({
    "question-1": ["基础语法示例"],
    "question-2": ["给同事看"],
  })
})

test("ask_user 一次最多 5 题", () => {
  expect(MAX_ASK_USER_QUESTIONS).toBe(5)
})
