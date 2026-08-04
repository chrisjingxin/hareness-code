/** CLI 启动层测试：验证工作区错误能在启动 Python 前得到清晰诊断。 */
import { expect, test } from "bun:test"
import { mkdtemp, readFile, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { resolve } from "node:path"

import {
  clientCapabilities,
  clientInteractionHandles,
  resolveAgentRuntimeLocations,
  validateInteractiveTerminal,
  validateWorkspace,
} from "../src/index"
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

test("源码与 dist CLI 都解析到 packages/agent sidecar", () => {
  const packageDir = resolve(import.meta.dir, "..")
  const agentDir = resolve(packageDir, "../agent")
  const source = resolveAgentRuntimeLocations(resolve(packageDir, "src"))
  const dist = resolveAgentRuntimeLocations(resolve(packageDir, "dist"))

  expect(source.agentDirectories).toContain(agentDir)
  expect(dist.agentDirectories).toContain(agentDir)
  expect(source.pythonExecutables).toContain(resolve(agentDir, ".venv/bin/python"))
  expect(dist.pythonExecutables).toContain(resolve(agentDir, ".venv/bin/python"))
})

test("根 dev 命令直接启动 CLI，避免 workspace 转发丢失 TTY", async () => {
  const rootPackage = JSON.parse(await readFile(resolve(import.meta.dir, "../../../package.json"), "utf8")) as {
    scripts: Record<string, string>
  }
  expect(rootPackage.scripts.dev).toBe("bun packages/cli/src/index.ts")
})

test("无头 CLI 不声明 Interaction handler", () => {
  const headless = parseArgs(["-n", "读取 README"])
  const interactive = parseArgs([])
  expect(clientCapabilities(headless)).toEqual([
    "run.cancel",
    "run.multithread",
    "config.read",
  ])
  expect(clientInteractionHandles(headless)).toEqual([])
  expect(clientInteractionHandles(interactive)).toEqual(["approval", "question"])
  expect(clientCapabilities(interactive)).toContain("threads.read")
  expect(clientCapabilities(interactive)).toContain("context.manage")
  expect(clientCapabilities(interactive)).toContain("host.attach")
  expect(clientCapabilities(interactive)).toContain("mcp.read")
  expect(clientCapabilities(interactive)).toContain("mcp.manage")
})
