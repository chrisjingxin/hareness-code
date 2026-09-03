/** GoalBanner 的可访问摘要投影。 */
/** @jsxImportSource react */

import { afterAll, expect, test } from "bun:test"

import { GoalBanner } from "../../../src/web/presentation/goal-view"
import { makeInteractive } from "./fixtures"
import { registerTestDom, render } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())

test("active Goal 显示状态、revision、note、pending 和最近活动", () => {
  const interactive = makeInteractive({
    goal: {
      goal_id: "goal-1",
      revision: 2,
      status: "active",
      objective: "完成登录闭环",
      assumptions: [],
      criteria: [
        { criterion_id: "criterion-1", text: "登录成功" },
        { criterion_id: "criterion-2", text: "错误提示可见" },
      ],
      note: "优先覆盖登录失败路径",
      prior_blocker: null,
      grader: { selection: "inherit", configured_profile_id: null, actual_profile_id: null },
      max_iterations: 3,
      created_at_ms: 1,
      updated_at_ms: 2,
      completed_at_ms: null,
    },
    goalPending: {
      request_id: "request-2",
      kind: "amend",
      status: "reviewing",
      base_goal_id: "goal-1",
      base_revision: 2,
      input_text: "补充错误态",
      proposed_objective: "完成登录闭环",
      proposed_assumptions: [],
      proposed_criteria: ["错误提示可见"],
      created_at_ms: 3,
      updated_at_ms: 4,
      error_code: null,
    },
    goalActivities: [
      { activity_id: "activity-1", kind: "proposal", summary: "目标修订等待审核", created_at_ms: 4 },
    ],
  })
  const handle = render(<GoalBanner interactive={interactive} />)
  try {
    const banner = handle.container.querySelector('[aria-label="当前目标"]')
    expect(banner?.textContent).toContain("active · 完成登录闭环")
    expect(banner?.textContent).toContain("r2 · 2 条验收标准")
    expect(banner?.textContent).toContain("备注：优先覆盖登录失败路径")
    expect(banner?.textContent).toContain("待处理：reviewing · 补充错误态")
    expect(banner?.textContent).toContain("目标修订等待审核")
  } finally {
    handle.unmount()
  }
})
