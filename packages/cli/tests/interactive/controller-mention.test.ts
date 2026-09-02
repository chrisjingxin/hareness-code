/** Controller 提及上下文注入端到端测试。 */

import { expect, describe, test, beforeAll, afterAll } from "bun:test"
import { mkdir, writeFile, rm, realpath } from "node:fs/promises"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { makeHarness, flush } from "./harness"

describe("Controller @ 提及端到端注入", () => {
  let testDir: string

  beforeAll(async () => {
    const rawDir = join(tmpdir(), `harness-controller-mention-${Date.now()}`)
    await mkdir(rawDir, { recursive: true })
    testDir = await realpath(rawDir)

    const smallCode = `function add(a: number, b: number) {\n  return a + b\n}\n\nexport default add`
    await writeFile(join(testDir, "math.ts"), smallCode, "utf8")
  })

  afterAll(async () => {
    await rm(testDir, { recursive: true, force: true })
  })

  test("提交带 @ 提及的消息时：Gateway 接收附加上下文，Timeline 保留用户原始输入", async () => {
    const harness = makeHarness({
      workspace: testDir,
    })

    await flush()

    const rawInput = "请帮我解释 @math.ts#L1-3 这段实现"
    await harness.controller.dispatch({
      type: "input.submit",
      value: rawInput,
    })

    // 1. 断言 Gateway 接收到的 Run message 包含结构化附加上下文
    const lastRun = harness.port.lastRunSelection()
    expect(lastRun).toBeDefined()
    expect(lastRun?.message).toContain("[Attached Context: math.ts (lines 1-3 of 5)]")
    expect(lastRun?.message).toContain("function add(a: number, b: number)")
    expect(lastRun?.message).toContain(rawInput)

    // 2. 断言 Controller 时间线呈现的是干净的用户手打原消息，不污染 UI 历史
    const snapshot = harness.controller.getSnapshot()
    const userMessage = snapshot.timeline.find(t => t.type === "message" && t.message.role === "user")
    expect(userMessage).toBeDefined()
    if (userMessage && userMessage.type === "message") {
      expect(userMessage.message.content).toBe(rawInput)
    }

    await harness.controller.close()
  })
})
