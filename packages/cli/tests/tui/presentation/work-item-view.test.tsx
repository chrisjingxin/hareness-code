import { expect, test } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import type { WorkItemView as WorkItemViewShape } from "../../../src/interactive/selectors"
import type { WorkItemProjection, WorkMode } from "../../../src/interactive/state"
import { WorkItemView } from "../../../src/tui/presentation/work-item-view"

/** 构造一个合法的 Work Item 投影，只覆盖需要变异的字段。 */
function workItem(overrides: Partial<WorkItemProjection> = {}): WorkItemProjection {
  return {
    workItemId: "wi-search",
    slug: "feature-search",
    title: "实现搜索",
    revision: 3,
    status: "active",
    currentActivity: "编写搜索索引",
    pendingDecision: null,
    blockedReason: null,
    ...overrides,
  }
}

/** 构造展示视图；modeLocked 始终由 threadMode 推导，与 selectWorkItemView 一致。 */
function viewOf(item: WorkItemProjection | null, threadMode: WorkMode | null = null): WorkItemViewShape {
  return { workItem: item, threadMode, modeLocked: threadMode !== null }
}

/** 渲染组件并返回整帧文本；销毁 renderer 避免泄漏。 */
async function render(view: WorkItemViewShape, width = 100, height = 20): Promise<string> {
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(WorkItemView, { view }), { width, height })
  })
  try {
    await act(async () => { await setup.flush() })
    return setup.captureCharFrame()
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
}

test("无 Work Item 时渲染占位空态与未锁定模式提示", async () => {
  const frame = await render(viewOf(null))
  expect(frame).toContain("暂无 Work Item")
  expect(frame).toContain("Tab 切换模式")
})

test("active 投影渲染标题、slug、revision 与中文状态", async () => {
  const frame = await render(viewOf(workItem({ status: "active" }), "compose"))
  expect(frame).toContain("进行中")
  expect(frame).toContain("实现搜索")
  expect(frame).toContain("feature-search")
  expect(frame).toContain("rev 3")
  expect(frame).toContain("活动：编写搜索索引")
})

test("waiting_user 显示待处理决定", async () => {
  const frame = await render(viewOf(workItem({ status: "waiting_user", pendingDecision: "确认迁移方案" }), "compose"))
  expect(frame).toContain("等待你的决定")
  expect(frame).toContain("待处理：确认迁移方案")
})

test("blocked 显示阻塞原因", async () => {
  const frame = await render(viewOf(workItem({ status: "blocked", blockedReason: "CI 构建失败" }), "compose"))
  expect(frame).toContain("需要你处理")
  expect(frame).toContain("阻塞：CI 构建失败")
})

test("completed 与 abandoned 显示终态文案", async () => {
  expect(await render(viewOf(workItem({ status: "completed" }), "compose"))).toContain("已完成")
  expect(await render(viewOf(workItem({ status: "abandoned" }), "compose"))).toContain("已放弃")
})

test("threadMode 锁定时显示 Compose 且无 Tab 切换提示", async () => {
  const locked = await render(viewOf(workItem({ status: "active" }), "compose"))
  expect(locked).toContain("Compose")
  expect(locked).toContain("已锁定")
  expect(locked).not.toContain("Tab")

  const unlocked = await render(viewOf(workItem({ status: "active" }), null))
  expect(unlocked).toContain("Tab 切换模式")
  expect(unlocked).not.toContain("已锁定")
})

test("窄终端渲染不抛异常", async () => {
  const frame = await render(viewOf(workItem({ status: "waiting_user", pendingDecision: "确认" }), "compose"), 20, 8)
  expect(typeof frame).toBe("string")
})
