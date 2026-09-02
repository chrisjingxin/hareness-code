/** @ 文件补全菜单的精简渲染回归测试。 */

import { expect, test } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import { MentionMenu } from "../../../src/tui/presentation/mention-menu"

test("候选行只展示相对路径，不展示文件图标或语言标签", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(MentionMenu, {
        options: [{
          path: "src/app.ts",
          name: "app.ts",
          kind: "file",
          language: "typescript",
          matchRanges: [{ start: 4, end: 7 }],
        }],
        totalMatches: 1,
        truncated: false,
        selectedIndex: 0,
        windowStart: 0,
        visibleRows: 8,
        terminalWidth: 60,
        browsePath: "",
        workspaceStatus: "ready",
        workspaceLimited: false,
        onSelect: () => undefined,
        onHover: () => undefined,
        placement: "inline-below",
        accent: "#00ff00",
      }), { width: 60, height: 8 })
    })

    const frame = setup!.captureCharFrame()
    expect(frame).toContain("src/app.ts")
    expect(frame).not.toContain("📄")
    expect(frame).not.toContain("typescript")
    expect(frame).toContain("1/1")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})

test("候选很多时只渲染当前 8 行窗口并展示真实总数与截断提示", async () => {
  const options = Array.from({ length: 12 }, (_, index) => ({
    path: `src/file-${index}.ts`,
    name: `file-${index}.ts`,
    kind: "file" as const,
    language: "typescript",
    matchRanges: [{ start: 4, end: 8 }],
  }))
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(MentionMenu, {
        options,
        totalMatches: 1_205,
        truncated: true,
        selectedIndex: 8,
        windowStart: 1,
        visibleRows: 8,
        terminalWidth: 60,
        browsePath: "",
        workspaceStatus: "ready",
        workspaceLimited: true,
        onSelect: () => undefined,
        onHover: () => undefined,
        placement: "inline-below",
        accent: "#00ff00",
      }), { width: 60, height: 14 })
    })

    const frame = setup!.captureCharFrame()
    expect(frame).not.toContain("src/file-0.ts")
    expect(frame).toContain("src/file-1.ts")
    expect(frame).toContain("src/file-8.ts")
    expect(frame).not.toContain("src/file-9.ts")
    expect(frame).toContain("9/1205")
    expect(frame).toContain("前 1000 项")
    expect(frame).toContain("扫描受限")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})

test("目录候选以末尾斜杠区分并显示当前浏览路径", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(MentionMenu, {
        options: [{ path: "packages/cli", name: "cli", kind: "directory", language: null, matchRanges: [] }],
        totalMatches: 1,
        truncated: false,
        selectedIndex: 0,
        windowStart: 0,
        visibleRows: 8,
        terminalWidth: 60,
        browsePath: "packages",
        workspaceStatus: "ready",
        workspaceLimited: false,
        onSelect: () => undefined,
        onHover: () => undefined,
        placement: "inline-below",
        accent: "#00ff00",
      }), { width: 60, height: 8 })
    })

    const frame = setup!.captureCharFrame()
    expect(frame).toContain("packages/cli/")
    expect(frame).toContain("@ / packages")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})
