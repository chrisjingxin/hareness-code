/** CLI 启动层测试：验证工作区错误能在启动 Python 前得到清晰诊断。 */
import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"
import { mkdtemp, readFile, realpath, writeFile } from "node:fs/promises"
import { resolve } from "node:path"
import { tmpdir } from "node:os"
import { Readable } from "node:stream"

import {
  clientCapabilities,
  clientInteractionHandles,
  dispatchClientCommand,
  execute,
  readSettingValue,
  resolveAgentRuntimeLocations,
  validateInteractiveTerminal,
  validateWorkspace,
  workspaceFingerprint,
} from "../src/index"
import {
  bindPluginCommands,
  COMMANDS_BIND_MIN_MINOR,
} from "../src/ipc/command-binding"
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

test("sidecar stderr 使用有界 drain，不拼接原文", () => {
  const source = readFileSync(resolve(import.meta.dir, "../src/index.ts"), "utf8")
  expect(source).toContain("SidecarStderrDrain")
  expect(source).not.toContain("stderr += ")
  expect(source).not.toContain("stderrChunks")
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
  expect(clientInteractionHandles(interactive)).toEqual(["approval", "question", "directory_trust", "plan"])
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

test("harness logs 命令路由在 startAgent 之前，不创建 sidecar、不调用 spawn", () => {
  const logsCmd = parseArgs(["logs", "--run", "r1"])
  expect(logsCmd.kind).toBe("logs")

  // 源码证据：execute 中 logs 处理在 startAgent 调用之前，保证 spawn=0
  const src = readFileSync(resolve(import.meta.dir, "../src/index.ts"), "utf8")
  const startAgentIdx = src.indexOf("const agent = await (dependencies.startAgent ?? startAgent)(command)")
  const logsCheckIdx = src.indexOf('command.kind === "logs"')
  expect(logsCheckIdx).toBeGreaterThan(-1)
  expect(logsCheckIdx).toBeLessThan(startAgentIdx)

  const querySource = readFileSync(resolve(import.meta.dir, "../src/diagnostic-log/query.ts"), "utf8")
  expect(querySource).not.toContain("child_process")
  expect(querySource).not.toContain("Bun.spawn")
  expect(querySource).not.toContain("Database")
  expect(querySource).not.toContain("sqlite")
})

test("Plugin Settings commands negotiate settings capabilities", () => {
  const list = parseArgs(["plugins", "settings", "list"], "/work")
  const set = parseArgs([
    "plugins", "settings", "set", "plugin/local/za38", "ZA38_TOKEN",
    "--package-digest", "a".repeat(64),
    "--declaration-digest", "b".repeat(64),
    "--expected-store-revision", "0",
    "--secret-stdin",
  ], "/work")
  expect(clientCapabilities(list)).toContain("settings.read")
  expect(clientCapabilities(list)).not.toContain("settings.manage")
  expect(clientCapabilities(set)).toContain("settings.read")
  expect(clientCapabilities(set)).toContain("settings.manage")
})

test("Plugin Settings CLI 经过 parse→dispatch 向 Agent 发出 canonical RPC", async () => {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = []
  const request = async (method: string, params: Record<string, unknown>) => {
    calls.push({ method, params })
    return { ok: true }
  }
  const readValue = async (_secretStdin: boolean) => "generated-fake-secret"

  await dispatchClientCommand(
    parseArgs(["plugins", "settings", "list"], "/work"),
    request,
    readValue,
  )
  await dispatchClientCommand(
    parseArgs([
      "plugins", "settings", "set", "plugin/local/za38", "ZA38_TOKEN",
      "--package-digest", "a".repeat(64),
      "--declaration-digest", "b".repeat(64),
      "--expected-store-revision", "0",
      "--secret-stdin",
    ], "/work"),
    request,
    readValue,
  )
  await dispatchClientCommand(
    parseArgs([
      "plugins", "settings", "remove", "plugin/local/za38", "ZA38_TOKEN",
      "--package-digest", "a".repeat(64),
      "--declaration-digest", "b".repeat(64),
      "--expected-store-revision", "1",
    ], "/work"),
    request,
    readValue,
  )

  expect(calls.map(call => call.method)).toEqual([
    "settings.list",
    "settings.set",
    "settings.remove",
  ])
  expect(calls[1]?.params.value).toBe("generated-fake-secret")
  expect(calls[0]?.params).toEqual({ scope: "user" })
})

test("Plugin Settings/Plugin removal CLI 经过 parse→execute→fake Agent request 完成管理命令", async () => {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = []
  const startAgent = async () => ({
    client: {
      request: async (method: string, params: Record<string, unknown>) => {
        calls.push({ method, params })
        return { method }
      },
    } as never,
    runtime: undefined as never,
    stop: async () => {},
  })

  await execute(parseArgs(["plugins", "settings", "list"], "/work"), { startAgent })
  await execute(
    parseArgs(
      [
        "plugins", "settings", "set", "plugin/local/za38", "ZA38_TOKEN",
        "--package-digest", "a".repeat(64),
        "--declaration-digest", "b".repeat(64),
        "--expected-store-revision", "0", "--secret-stdin",
      ],
      "/work",
    ),
    { startAgent, readSettingValue: async () => "generated-fake-secret" },
  )
  await execute(
    parseArgs(
      [
        "plugins", "settings", "remove", "plugin/local/za38", "ZA38_TOKEN",
        "--package-digest", "a".repeat(64),
        "--declaration-digest", "b".repeat(64),
        "--expected-store-revision", "1",
      ],
      "/work",
    ),
    { startAgent },
  )
  await execute(
    parseArgs(["plugins", "remove", "plugin/local/za38", "--purge-data"], "/work"),
    { startAgent },
  )

  expect(calls.map(call => call.method)).toEqual([
    "settings.list",
    "settings.set",
    "settings.remove",
    "plugins.remove",
  ])
  expect(calls.at(-1)?.params).toEqual({
    id: "plugin/local/za38",
    purge_data: true,
  })
})

test("CLI stdin validator accepts one framing newline after the maximum value", async () => {
  const original = Object.getOwnPropertyDescriptor(process, "stdin")
  Object.defineProperty(process, "stdin", {
    configurable: true,
    value: Readable.from([Buffer.from("x".repeat(65_536) + "\n")]),
  })
  try {
    await expect(readSettingValue(true)).resolves.toBe("x".repeat(65_536))
  } finally {
    if (original) Object.defineProperty(process, "stdin", original)
  }
})

test("CLI stdin validator accepts maximum value followed by CRLF", async () => {
  const original = Object.getOwnPropertyDescriptor(process, "stdin")
  Object.defineProperty(process, "stdin", {
    configurable: true,
    value: Readable.from([Buffer.from("x".repeat(65_536) + "\r\n")]),
  })
  try {
    await expect(readSettingValue(true)).resolves.toBe("x".repeat(65_536))
  } finally {
    if (original) Object.defineProperty(process, "stdin", original)
  }
})

test("Plugin Command binding 遵守协商 minor，不向 v5 Agent 调用未知方法", async () => {
  expect(COMMANDS_BIND_MIN_MINOR).toBe(6)
  const calls: unknown[] = []
  const client = {
    bindCommandRegistry: async (params: unknown) => {
      calls.push(params)
      return { snapshot_id: "snapshot-v6", accepted: true as const }
    },
  }

  await bindPluginCommands(client, 6, "snapshot-v6", [
    { id: "plugin/za38/command/sdd", name: "za38-sdd" },
  ])
  expect(calls).toEqual([{
    snapshot_id: "snapshot-v6",
    bindings: [{ id: "plugin/za38/command/sdd", name: "za38-sdd" }],
  }])

  await expect(
    bindPluginCommands(client, 5, "snapshot-v5", [
      { id: "plugin/za38/command/sdd", name: "za38-sdd" },
    ]),
  ).rejects.toThrow("COMMANDS_BIND_PROTOCOL_MINOR_REQUIRED")
  expect(calls).toHaveLength(1)

  await bindPluginCommands(client, 5, "snapshot-v5", [])
  expect(calls).toHaveLength(1)
})
