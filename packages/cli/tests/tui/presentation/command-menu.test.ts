/** Slash 命令菜单窗口化渲染回归测试。 */

import { expect, test } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import type { CommandMenuItem } from "../../../src/interactive/commands"
import { CommandMenu } from "../../../src/tui/presentation/input-bar"

function commandItem(name: string): CommandMenuItem {
  return {
    kind: "command",
    command: {
      id: `test.${name}`,
      name,
      description: `${name} 说明`,
      source: { type: "builtin" },
      presentation: "action",
    },
    availability: { state: "available" },
  }
}

test("候选很多时只渲染当前窗口内的命令，并展示选中位置与总数", async () => {
  const options = Array.from({ length: 12 }, (_, index) => commandItem(`cmd-${index}`))
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(CommandMenu, {
        options,
        selectedIndex: 8,
        windowStart: 1,
        visibleRows: 8,
        onSelect: () => undefined,
        onHover: () => undefined,
        placement: "inline-below",
        accent: "#00ff00",
      }), { width: 60, height: 14 })
    })

    const frame = setup!.captureCharFrame()
    expect(frame).not.toContain("/cmd-0")
    expect(frame).toContain("/cmd-1")
    expect(frame).toContain("/cmd-8")
    expect(frame).not.toContain("/cmd-9")
    expect(frame).toContain("9/12")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
  }
})
