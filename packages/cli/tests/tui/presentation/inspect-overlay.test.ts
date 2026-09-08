/** 查看浮层：长正文可滚动，Markdown 高亮，页脚保持可见。 */

import { expect, test } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import { registerCommonSyntaxParsers } from "../../../src/tui/platform/syntax-parsers"
import { InspectOverlay } from "../../../src/tui/presentation/inspect-overlay"

async function captureUntil(setup: Awaited<ReturnType<typeof testRender>>, predicate: (frame: string) => boolean): Promise<string> {
  let frame = setup.captureCharFrame()
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate(frame)) return frame
    await act(async () => { await setup.flush() })
    frame = setup.captureCharFrame()
  }
  return frame
}

test("过长 plan-view 正文保留标题与关闭提示，不把页脚挤出屏幕", async () => {
  registerCommonSyntaxParsers()
  const body = Array.from({ length: 40 }, (_, index) => `## 第 ${index + 1} 节\n\n这是一段计划正文。`).join("\n")
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(InspectOverlay, {
        visible: true,
        title: "~/.harness/plans/thread-1.md",
        body,
        terminalWidth: 100,
        terminalHeight: 24,
        onClose: () => {},
      }), { width: 100, height: 24 })
    })
    const frame = await captureUntil(setup, text => text.includes("Esc 关闭"))
    expect(frame).toContain("~/.harness/plans/thread-1.md")
    expect(frame).toContain("Esc 关闭")
    const footerIndex = frame.split("\n").findIndex(line => line.includes("Esc 关闭"))
    expect(footerIndex).toBeGreaterThanOrEqual(0)
    expect(footerIndex).toBeLessThan(24)
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})

test("plan-view 浮层在终端中水平垂直居中", async () => {
  registerCommonSyntaxParsers()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(InspectOverlay, {
        visible: true,
        title: "当前计划",
        body: "短正文",
        terminalWidth: 100,
        terminalHeight: 24,
        onClose: () => {},
      }), { width: 100, height: 24 })
    })
    const frame = await captureUntil(setup, text => text.includes("当前计划") && text.includes("Esc 关闭"))
    const lines = frame.split("\n")
    const titleIndex = lines.findIndex(line => line.includes("当前计划"))
    const footerIndex = lines.findIndex(line => line.includes("Esc 关闭"))
    expect(titleIndex).toBeGreaterThanOrEqual(0)
    expect(footerIndex).toBeGreaterThan(titleIndex)
    const titleLine = lines[titleIndex]!
    const left = titleLine.search(/\S/)
    // 标题在面板内左对齐；用左边距判断面板是否离开左缘、大致落在中段。
    expect(left).toBeGreaterThan(10)
    expect(left).toBeLessThan(30)
    expect(titleIndex).toBeGreaterThan(1)
    expect(footerIndex).toBeLessThan(lines.length - 2)
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})

test("plan-view 浮层用 Markdown 渲染标题标记，而不是原文 ##", async () => {
  registerCommonSyntaxParsers()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(InspectOverlay, {
        visible: true,
        title: "当前计划",
        body: "# 完成停点 4\n\n- 第一项\n- 第二项",
        terminalWidth: 80,
        terminalHeight: 22,
        onClose: () => {},
      }), { width: 80, height: 22 })
    })
    const frame = await captureUntil(setup, text => text.includes("完成停点 4") && text.includes("第一项"))
    expect(frame).toContain("完成停点 4")
    expect(frame).toContain("第一项")
    expect(frame).not.toContain("# 完成停点")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})
