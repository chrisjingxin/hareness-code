/** 共享 Timeline 展示语义测试：activity/tool/interaction 状态的中文文案。 */

import { expect, test } from "bun:test"
import { activityLabel, interactionStatusLabel, toolStatusLabel } from "../../src/presentation-shared/timeline-presenter"

test("activityLabel：领域 Kind 全部映射为稳定中文标签", () => {
  const kinds = ["home", "idle", "starting", "running", "waiting-interaction", "cancelling", "completed", "cancelled", "failed"] as const
  for (const kind of kinds) {
    expect(activityLabel(kind)).toBeTypeOf("string")
  }
  expect(activityLabel("running")).toBe("正在运行")
  expect(activityLabel("completed")).toBe("已完成")
  expect(activityLabel("failed")).toBe("运行失败")
})

test("toolStatusLabel：运行中/完成/失败", () => {
  expect(toolStatusLabel("running")).toBe("运行中")
  expect(toolStatusLabel("completed")).toBe("已完成")
  expect(toolStatusLabel("failed")).toBe("失败")
})

test("interactionStatusLabel：历史交互结果标签", () => {
  expect(interactionStatusLabel("approved")).toBe("已允许")
  expect(interactionStatusLabel("rejected")).toBe("已拒绝")
  expect(interactionStatusLabel("answered")).toBe("已回答")
  expect(interactionStatusLabel("cancelled")).toBe("已超时")
  expect(interactionStatusLabel("resolved")).toBe("已解决")
  expect(interactionStatusLabel("pending")).toBe("等待中")
})
