import { expect, test } from "bun:test"
import type { InteractiveSnapshot } from "../../src/interactive/types"
import { makeHarness, flush, notices, terminalEvent, approvalRequest, questionRequest } from "./harness"

test("空首页：无 initialThreadId 不恢复，snapshot 表达 null Thread", () => {
  const harness = makeHarness()
  const snapshot = harness.controller.getSnapshot()
  expect(snapshot.currentThreadId).toBeNull()
  expect(snapshot.activity.kind).toBe("home")
  expect(snapshot.connection.status).toBe("open")
  harness.controller.close()
})

test("显式 initialThreadId null 进入空首页；字符串恢复历史与模型绑定", async () => {
  const empty = makeHarness({ initialThreadId: null })
  await flush()
  expect(empty.controller.getSnapshot().currentThreadId).toBeNull()
  expect(empty.calls).not.toContain("threads.open")
  await empty.controller.close()

  const restored = makeHarness({ initialThreadId: "thread-2" })
  await flush()
  const snapshot = restored.controller.getSnapshot()
  expect(snapshot.currentThreadId).toBe("thread-2")
  expect(snapshot.timeline).toHaveLength(2)
  expect(snapshot.catalogs.models.status).toBe("ready")
  await restored.controller.close()
})

test("未知命令/转义/alias/动态 Skill 走同一解析路径", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "/contnue" })
    expect(notices(harness.controller.getSnapshot())).toContain("未知命令")
    expect(harness.calls).not.toContain("run.start")

    await harness.controller.dispatch({ type: "input.submit", value: "//api 路由" })
    expect(harness.port.lastRunSelection()).toMatchObject({ message: "/api 路由" })
    const escapedRun = harness.runHandles.at(-1)!
    harness.port.completeRun(escapedRun.threadId, escapedRun.runId)
    await flush()

    await harness.controller.dispatch({ type: "input.submit", value: "使用别名" })
    await flush()
    const aliasRun = harness.runHandles.at(-1)!
    harness.port.completeRun(aliasRun.threadId, aliasRun.runId)
    await flush()

    await harness.controller.dispatch({ type: "skill.arm", skillId: "user/repo-review-demo" })
    expect(harness.controller.getSnapshot().selection.armedSkill?.id).toBe("user/repo-review-demo")
    await harness.controller.dispatch({ type: "input.submit", value: "审查" })
    expect(harness.port.lastRunSelection()?.requestedSkill).toEqual({ id: "user/repo-review-demo", args: "审查" })
  } finally {
    await harness.controller.close()
  }
})

test("close 幂等；关闭后 dispatch no-op 且不再发布 snapshot", async () => {
  const harness = makeHarness()
  const snapshots: InteractiveSnapshot[] = []
  const unsubscribe = harness.controller.subscribe(snapshot => snapshots.push(snapshot))
  try {
    await harness.controller.close()
    await harness.controller.close()
    await harness.controller.dispatch({ type: "input.submit", value: "关闭后" })
    await flush()
    expect(harness.calls).not.toContain("run.start")
    const count = snapshots.length
    unsubscribe()
    await flush()
    expect(snapshots.length).toBe(count)
  } finally {
    await harness.controller.close()
  }
})

