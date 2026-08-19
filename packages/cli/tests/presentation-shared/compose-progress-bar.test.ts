/** Compose 进度条纯函数：步骤名固定，状态只出现在 hint。 */

import { expect, test } from "bun:test"

import {
  composeStepperHint,
  composeStepperSegments,
  composeStepperTrackFilled,
} from "../../src/presentation-shared/compose-progress-bar"
import type { ComposeProjection } from "../../src/interactive/state"

function progress(overrides: Partial<ComposeProjection> = {}): ComposeProjection {
  return {
    threadId: "thread-1",
    slug: "search",
    complexity: "simple",
    status: "waiting_user",
    currentStage: "plan",
    waiting: "plan_confirm",
    stages: [
      { id: "requirement", state: "confirmed" },
      { id: "spec", state: "skipped" },
      { id: "plan", state: "current" },
      { id: "implement", state: "pending" },
      { id: "review", state: "pending" },
    ],
    documents: [],
    fixRounds: 0,
    revision: 1,
    ...overrides,
  }
}

test("步骤标签固定为五段中文，不把等你/失败拼进步骤名", () => {
  const segments = composeStepperSegments(progress())
  expect(segments.map(item => item.label)).toEqual(["需求", "规格", "计划", "实现", "检视"])
  expect(segments.map(item => item.mark)).toEqual(["done", "skipped", "current", "pending", "pending"])
  expect(composeStepperHint(progress())).toBe("等你确认")
  expect(segments.some(item => item.label.includes("等你"))).toBeFalse()
})

test("失败只出现在 hint，步骤名仍是实现", () => {
  const failed = progress({
    currentStage: "implement",
    waiting: "ask_user",
    stages: [
      { id: "requirement", state: "confirmed" },
      { id: "spec", state: "skipped" },
      { id: "plan", state: "confirmed" },
      { id: "implement", state: "failed" },
      { id: "review", state: "pending" },
    ],
  })
  expect(composeStepperSegments(failed).find(item => item.id === "implement")).toEqual({
    id: "implement",
    label: "实现",
    mark: "failed",
  })
  expect(composeStepperHint(failed)).toBe("失败")
})

test("轨道只在已完成或跳过的步骤之后填实，当前/失败之后留空", () => {
  expect(composeStepperTrackFilled("done")).toBeTrue()
  expect(composeStepperTrackFilled("skipped")).toBeTrue()
  expect(composeStepperTrackFilled("current")).toBeFalse()
  expect(composeStepperTrackFilled("failed")).toBeFalse()
  expect(composeStepperTrackFilled("pending")).toBeFalse()
})
