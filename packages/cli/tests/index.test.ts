/** CLI 启动层测试：验证工作区错误能在启动 Python 前得到清晰诊断。 */
import { expect, test } from "bun:test"
import { mkdtemp, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { resolve } from "node:path"

import { clientCapabilities, validateInteractiveTerminal, validateWorkspace } from "../src/index"
import { parseArgs } from "../src/args"

test("不存在的工作区会给出明确错误", () => {
  const missing = resolve(tmpdir(), `za38-missing-${crypto.randomUUID()}`)
  expect(() => validateWorkspace(missing)).toThrow("Workspace does not exist")
})

test("工作区必须是目录", async () => {
  const root = await mkdtemp(resolve(tmpdir(), "za38-workspace-test-"))
  const file = resolve(root, "file.txt")
  await writeFile(file, "not a directory")
  expect(() => validateWorkspace(file)).toThrow("Workspace is not a directory")
})

test("交互界面拒绝经过管道或任务复用器启动", () => {
  expect(() => validateInteractiveTerminal(undefined, true)).toThrow("requires a real terminal")
  expect(() => validateInteractiveTerminal(true, false)).toThrow("requires a real terminal")
  expect(() => validateInteractiveTerminal(true, true)).not.toThrow()
})

test("无头 CLI 不协商审批或问答能力", () => {
  expect(clientCapabilities(parseArgs(["-n", "读取 README"]))).toEqual([
    "run.cancel",
    "run.multithread",
    "config.read",
  ])
  expect(clientCapabilities(parseArgs([]))).toContain("interactive.approval")
  expect(clientCapabilities(parseArgs([]))).toContain("interactive.question")
})
