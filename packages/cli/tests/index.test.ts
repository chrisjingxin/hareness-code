/** CLI 启动层测试：验证工作区错误能在启动 Python 前得到清晰诊断。 */
import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"
import { mkdtemp, readFile, realpath, writeFile } from "node:fs/promises"
import { resolve } from "node:path"
import { tmpdir } from "node:os"

import {
  clientCapabilities,
  clientInteractionHandles,
  resolveAgentRuntimeLocations,
  validateInteractiveTerminal,
  validateWorkspace,
  workspaceFingerprint,
} from "../src/index"
import { parseArgs } from "../src/args"

test("CLI shutdown 顺序：runTui 返回后 gateway → coordinator → controller.close，agent.stop 最后", () => {
  const source = readFileSync(resolve(import.meta.dir, "../src/index.ts"), "utf8")
  const runTuiAt = source.indexOf("await runTui(")
  const gatewayCloseAt = source.indexOf("await webUiGateway?.close()")
  const coordinatorCloseAt = source.indexOf("await presentationCoordinator?.close()")
  const controllerCloseAt = source.indexOf("await controller?.close()")
  const agentStopAt = source.indexOf("await agent.stop()")
  expect(runTuiAt).toBeGreaterThan(-1)
  expect(gatewayCloseAt).toBeGreaterThan(runTuiAt)
  expect(coordinatorCloseAt).toBeGreaterThan(gatewayCloseAt)
  expect(controllerCloseAt).toBeGreaterThan(coordinatorCloseAt)
  expect(agentStopAt).toBeGreaterThan(controllerCloseAt)
})

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

test("CLI workspace fingerprint 使用平台真实路径且不暴露路径", async () => {
  const root = await mkdtemp(resolve(tmpdir(), "za38-fingerprint-test-"))
  expect(await workspaceFingerprint(root)).toBe(
    createHash("sha256").update(await realpath(root)).digest("hex"),
  )
})

test("生产日志路径不再使用同步文件写入或旧 debug 目录", () => {
  const source = readFileSync(resolve(import.meta.dir, "../src/diagnostic-log/runtime/index.ts"), "utf8")
  expect(source).not.toContain("writeSync")
  expect(source).not.toContain('.harness", "debug')
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
  expect(clientInteractionHandles(interactive)).toEqual(["approval", "question", "directory_trust"])
  expect(clientCapabilities(interactive)).toContain("threads.read")
  expect(clientCapabilities(interactive)).toContain("context.manage")
  // ZC-114：内置 Web 不再直连 Host，CLI 不声明 attachment/control 能力。
  expect(clientCapabilities(interactive)).not.toContain("host.attach")
  expect(clientCapabilities(interactive)).not.toContain("host.control")
  expect(clientCapabilities(interactive)).toContain("mcp.read")
  expect(clientCapabilities(interactive)).toContain("mcp.manage")
  expect(clientCapabilities(interactive)).toContain("agents.read")
  expect(clientCapabilities(interactive)).toContain("teams.read")
  expect(clientCapabilities(interactive)).toContain("teams.manage")
})

test("Plugin CLI 按操作声明最小读写能力", () => {
  const validation = parseArgs(["plugins", "validate", "./plugin"])
  const install = parseArgs(["plugins", "install", "./plugin"])
  expect(clientCapabilities(validation)).toContain("plugins.read")
  expect(clientCapabilities(validation)).not.toContain("plugins.manage")
  expect(clientCapabilities(install)).toContain("plugins.read")
  expect(clientCapabilities(install)).toContain("plugins.manage")
})
