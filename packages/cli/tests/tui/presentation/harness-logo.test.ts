import { expect, test } from "bun:test"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import { HarnessCodeLogo, HARNESS_WORDMARK_DIMENSIONS } from "../../../src/tui/presentation/harness-logo"

const HARNESS_SHADOW = [
  "██╗  ██╗ █████╗  ██████╗ ███╗   ██╗███████╗███████╗███████╗",
  "██║  ██║██╔══██╗ ██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝",
  "███████║███████║ ██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗",
  "██╔══██║██╔══██║ ██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║",
  "██║  ██║██║  ██║ ██║  ██║██║ ╚████║███████╗███████║███████║",
  "╚═╝  ╚═╝╚═╝  ╚═╝ ╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝",
]

const CODE_SHADOW = [
  " ██████╗  ██████╗  ██████╗  ███████╗",
  "██╔════╝ ██╔═══██╗ ██╔══██╗ ██╔════╝",
  "██║      ██║   ██║ ██║  ██║ █████╗  ",
  "██║      ██║   ██║ ██║  ██║ ██╔══╝  ",
  "╚██████╗ ╚██████╔╝ ██████╔╝ ███████╗",
  " ╚═════╝  ╚═════╝  ╚═════╝  ╚══════╝",
]

test("ANSI Shadow font preview", () => {
  console.log("=== HARNESS (ANSI Shadow) ===")
  HARNESS_SHADOW.forEach(l => console.log(l))

  console.log("=== CODE (ANSI Shadow) ===")
  CODE_SHADOW.forEach(l => console.log(l))

  expect(HARNESS_SHADOW[0].length).toBe(59)
  expect(CODE_SHADOW[0].length).toBe(36)
})

test("Harness Code 字标使用 MiMo 风格的半块比例，为右下角 powered by 提供稳定锚点", () => {
  expect(HARNESS_WORDMARK_DIMENSIONS.width).toBe(54)
  expect(HARNESS_WORDMARK_DIMENSIONS.height).toBe(5)
})

test("宽屏字标渲染为三行 MiMo 风格半块字，且 powered by 贴合右下角", async () => {
  let setup: Awaited<ReturnType<typeof testRender>>
  await act(async () => {
    setup = await testRender(createElement(HarnessCodeLogo, { compact: false }), { width: 80, height: 12 })
  })
  try {
    await act(async () => { await setup.flush() })
    const frame = setup.captureCharFrame()
    expect(frame).toContain("█  █ █▀▀█ █▀▀▄ █▄ █ █▀▀▀ ▄▀▀▀ ▄▀▀▀ █▀▀▀ ▄▀▀▄ █▀▀▄ █▀▀▀")
    expect(frame).toContain("powered by za38")
  } finally {
    await act(async () => { setup.renderer.destroy() })
  }
})
