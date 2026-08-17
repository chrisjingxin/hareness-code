/** WorkItemBanner：六种 Work Item 状态、模式锁定指示器与窄宽度渲染。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"

import { WorkItemBanner } from "../../../src/web/presentation/work-item-view"
import type { WorkItemView } from "../../../src/interactive/selectors"
import type { WorkItemProjection } from "../../../src/interactive/state"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())

function makeWorkItem(overrides: Partial<WorkItemProjection> = {}): WorkItemProjection {
  return {
    workItemId: "work-1",
    slug: "implement-auth",
    title: "实现登录认证",
    revision: 3,
    status: "active",
    currentActivity: "编写单元测试",
    pendingDecision: null,
    blockedReason: null,
    ...overrides,
  }
}

function makeView(overrides: Partial<WorkItemView> = {}): WorkItemView {
  return { workItem: null, threadMode: null, modeLocked: false, ...overrides }
}

function mount(view: WorkItemView): RenderHandle {
  return render(<WorkItemBanner view={view} />)
}

function textOf(handle: RenderHandle): string {
  return handle.container.textContent ?? ""
}

describe("WorkItemBanner", () => {
  test("workItem 为 null 时不渲染任何内容（模式指示与空态已并入 Composer rail）", () => {
    const handle = mount(makeView())
    try {
      expect(handle.container.querySelector(".work-item-banner")).toBeNull()
      expect(textOf(handle)).not.toContain("当前无进行中的工作项")
      expect(textOf(handle)).not.toContain("Tab 切换工作模式")
    } finally {
      handle.unmount()
    }
  })

  test("有 workItem 时只渲染工作项卡，不再有模式 pill", () => {
    const handle = mount(makeView({ workItem: makeWorkItem(), threadMode: "compose", modeLocked: true }))
    try {
      const text = textOf(handle)
      expect(text).toContain("实现登录认证")
      expect(handle.container.querySelector(".work-item-mode")).toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("active 状态渲染标题/slug/revision/进行中/当前活动", () => {
    const handle = mount(makeView({ workItem: makeWorkItem({ status: "active" }) }))
    try {
      const text = textOf(handle)
      expect(text).toContain("实现登录认证")
      expect(text).toContain("implement-auth")
      expect(text).toContain("rev 3")
      expect(text).toContain("进行中")
      expect(text).toContain("编写单元测试")
    } finally {
      handle.unmount()
    }
  })

  test("waiting_user 显示等待你的决定与待处理", () => {
    const handle = mount(makeView({ workItem: makeWorkItem({ status: "waiting_user", pendingDecision: "选择方案 A 或 B" }) }))
    try {
      const text = textOf(handle)
      expect(text).toContain("等待你的决定")
      expect(text).toContain("待处理：选择方案 A 或 B")
    } finally {
      handle.unmount()
    }
  })

  test("blocked 显示需要你处理与阻塞原因", () => {
    const handle = mount(makeView({ workItem: makeWorkItem({ status: "blocked", blockedReason: "缺少数据库凭据" }) }))
    try {
      const text = textOf(handle)
      expect(text).toContain("需要你处理")
      expect(text).toContain("阻塞：缺少数据库凭据")
    } finally {
      handle.unmount()
    }
  })

  test("completed 与 abandoned 显示终态文案", () => {
    const completed = mount(makeView({ workItem: makeWorkItem({ status: "completed" }) }))
    try {
      expect(textOf(completed)).toContain("已完成")
    } finally {
      completed.unmount()
    }
    const abandoned = mount(makeView({ workItem: makeWorkItem({ status: "abandoned" }) }))
    try {
      expect(textOf(abandoned)).toContain("已放弃")
    } finally {
      abandoned.unmount()
    }
  })

  test("窄容器宽度渲染不抛异常且内容完整", () => {
    const longTitle = "实现一个非常非常长的登录认证与权限系统".repeat(4)
    const handle = mount(makeView({ workItem: makeWorkItem({ status: "blocked", blockedReason: "需要更新数据库凭据", title: longTitle }) }))
    try {
      handle.container.style.width = "320px"
      const text = textOf(handle)
      expect(text).toContain("需要你处理")
      expect(text).toContain("阻塞：需要更新数据库凭据")
    } finally {
      handle.unmount()
    }
  })
})
