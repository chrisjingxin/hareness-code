/** Compose Timeline 活动分组纯函数测试。 */

import { expect, test } from "bun:test"
import type { TimelineItem } from "../../src/interactive/state"
import {
  activityGroupTitle,
  composeLiveStatusLine,
  isGroupExpandedByDefault,
  itemActivityKey,
  segmentTimeline,
} from "../../src/presentation-shared/timeline-activity-groups"

test("Build 无 scope 条目保持扁平，不生成 activity 分组", () => {
  const timeline: TimelineItem[] = [
    { type: "message", message: { id: "u1", role: "user", content: "hi", runId: "run-1" } },
    { type: "tool", tool: { id: "t1", runId: "run-1", name: "read_file", arguments: "", output: "ok", status: "completed" } },
  ]
  const segments = segmentTimeline(timeline)
  expect(segments.every(segment => segment.kind === "flat")).toBeTrue()
  expect(itemActivityKey(timeline[1]!)).toBeNull()
})

test("同 activity 连续条目合成一组，中间 root 项打断分组", () => {
  const scopeA = { activityId: "act-a", stage: "understand" as const, attempt: 1 }
  const scopeB = { activityId: "act-b", stage: "plan" as const, attempt: 1 }
  const timeline: TimelineItem[] = [
    {
      type: "tool",
      tool: {
        id: "call-1",
        runId: "run-1",
        name: "read_file",
        arguments: "",
        output: "a",
        status: "completed",
        executionId: "child-a",
        activityId: "act-a",
        agentId: "understand",
      },
    },
    {
      type: "compose-summary",
      summary: {
        id: "sum-a",
        runId: "run-1",
        status: "passed",
        text: "理解完成",
        executionId: "child-a",
        activityId: "act-a",
        agentId: "understand",
        composeScope: scopeA,
      },
    },
    { type: "message", message: { id: "sys", role: "system", content: "note", runId: "run-1" } },
    {
      type: "reasoning",
      reasoning: {
        id: "r1",
        runId: "run-1",
        text: "思考",
        active: true,
        executionId: "child-b",
        activityId: "act-b",
        agentId: "plan",
      },
    },
    {
      type: "compose-summary",
      summary: {
        id: "sum-b",
        runId: "run-1",
        status: "passed",
        text: "计划完成",
        activityId: "act-b",
        composeScope: scopeB,
      },
    },
  ]
  const segments = segmentTimeline(timeline)
  expect(segments.map(segment => segment.kind)).toEqual(["group", "flat", "group"])
  if (segments[0]?.kind === "group") {
    expect(segments[0].group.activityId).toBe("act-a")
    expect(segments[0].group.stage).toBe("understand")
    expect(segments[0].group.terminal).toBeTrue()
    expect(isGroupExpandedByDefault(segments[0].group)).toBeFalse()
    expect(activityGroupTitle(segments[0].group)).toContain("理解")
  }
  if (segments[2]?.kind === "group") {
    expect(segments[2].group.activityId).toBe("act-b")
    // 摘要终态但 reasoning 仍 active → 不视为 terminal 折叠
    expect(segments[2].group.terminal).toBeFalse()
  }
})

test("compose live 状态行包含阶段与相位", () => {
  expect(composeLiveStatusLine({
    stage: "build",
    taskTitle: "实现搜索",
    agentId: "builder",
    phaseLabel: "模型处理中",
    elapsedLabel: "18s",
  })).toBe("Compose · 构建 · 实现搜索 · builder · 模型处理中 · 已运行 18s")
})
