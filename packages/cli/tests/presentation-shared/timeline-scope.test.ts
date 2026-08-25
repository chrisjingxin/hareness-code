/** 父子时间线切片：父视图不含 child 工具，子视图不含父项。 */

import { expect, test } from "bun:test"
import { scopeTimeline, timelineItemExecutionId } from "../../src/presentation-shared/timeline-scope"
import type { TimelineItem } from "../../src/interactive/state"

function tool(id: string, executionId?: string): TimelineItem {
  return {
    type: "tool",
    tool: {
      id,
      runId: "run-1",
      name: id === "t-task" ? "task" : "read_file",
      arguments: "{}",
      output: "",
      status: "running",
      ...(executionId ? { executionId } : {}),
    },
  }
}

test("父视图丢掉 child execution 的工具，保留未标记和 root 前缀的卡片", () => {
  const items = [tool("t-task"), tool("t-root", "root-run-1"), tool("t-read", "child-1")]
  const scoped = scopeTimeline(items, "root")
  expect(scoped.map(item => item.type === "tool" ? item.tool.id : "")).toEqual(["t-task", "t-root"])
})

test("子视图只留对应 execution，不串到另一个 child", () => {
  const items = [tool("t-task"), tool("t-root", "root-run-1"), tool("t-a", "child-1"), tool("t-b", "child-2")]
  const scoped = scopeTimeline(items, "child-1")
  expect(scoped).toHaveLength(1)
  expect(scoped[0]!.type === "tool" && scoped[0]!.tool.id).toBe("t-a")
  expect(timelineItemExecutionId(scoped[0]!)).toBe("child-1")
})
