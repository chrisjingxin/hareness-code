#!/usr/bin/env bun
/** za38 CLI 启动层：管理 Python sidecar 生命周期并选择 TUI 或无头执行模式。 */
import { execFileSync, spawn } from "node:child_process"
import { existsSync, statSync } from "node:fs"
import { delimiter, resolve } from "node:path"
import { Capability, EventType, PROTOCOL_VERSION, isClientMethod } from "@za38/protocol"

import { parseArgs, type Command } from "./args"
import { AgentClient } from "./ipc/client"
import { StdioRpcTransport } from "./ipc/stdio-transport"
import { runTui } from "./tui/app"
import { CLI_VERSION, createInteractiveRuntime, type InteractiveRuntime } from "./interactive/runtime"
import { createSystemBrowserOpener } from "./web/browser"
import { browserBundle } from "./web/bundle"
import { webHtml } from "./web/html"
import {
  createWebHandoffCoordinator,
  type WebHandoffCoordinator,
} from "./web/handoff-coordinator"
import { createWebServer } from "./web/server"

type RunningAgent = {
  client: AgentClient
  runtime: InteractiveRuntime
  stop: () => Promise<void>
}

/** 根据命令实际是否存在反向交互处理器，声明最小协议能力集合。 */
export function clientCapabilities(command: Command): string[] {
  const capabilities: string[] = [Capability.RUN_CANCEL, Capability.RUN_MULTITHREAD, Capability.CONFIG_READ]
  if (command.kind === "run" && !command.nonInteractive) capabilities.push(
    Capability.CONFIG_WRITE,
    Capability.THREADS_READ,
    Capability.CONTEXT_MANAGE,
    Capability.MODELS_READ,
    Capability.MODELS_SELECT,
    Capability.MCP_READ,
    Capability.MCP_MANAGE,
  )
  if (command.kind === "run" && !command.nonInteractive) capabilities.push(Capability.HOST_ATTACH, Capability.HOST_CONTROL)
  if (command.kind.startsWith("skills.") || (command.kind === "run" && !command.nonInteractive)) capabilities.push(Capability.SKILLS_READ)
  if (command.kind === "skills.set_enabled" || command.kind === "skills.install" || command.kind === "skills.update" || command.kind === "skills.remove") {
    capabilities.push(Capability.SKILLS_MANAGE)
  }
  return capabilities
}

/** 声明当前表现层能够处理的反向 Interaction。 */
export function clientInteractionHandles(command: Command): Array<"approval" | "question"> {
  return command.kind === "run" && !command.nonInteractive ? ["approval", "question"] : []
}

/** 启动 Python sidecar、完成 initialize 握手，并返回可关闭的运行句柄。 */
async function startAgent(command: Command): Promise<RunningAgent> {
  validateWorkspace(command.cwd)
  const sourcePython = resolve(import.meta.dir, "../../agent/.venv/bin/python")
  const sourcePythonWin = resolve(import.meta.dir, "../../agent/.venv/Scripts/python.exe")
  // Windows 上 .venv 布局是 Scripts/python.exe，Unix 是 bin/python；两者都探测后再降级到 PATH。
  const python = process.env.HARNESS_AGENT_PYTHON
    ?? (existsSync(sourcePython) ? sourcePython : existsSync(sourcePythonWin) ? sourcePythonWin : "python3")
  const sourceAgent = resolve(import.meta.dir, "../../agent")
  const sandboxEnvironment = command.kind === "run" && command.sandbox !== undefined
    // CLI 显式参数必须高于用户环境变量；sidecar 仅把这个内部字段当作
    // 最后一层覆盖，不对外暴露为可长期配置的环境变量。
    ? { HARNESS_CLI_SANDBOX: command.sandbox ? "remote" : "false" }
    : {}
  const child = spawn(python, ["-m", "harness_agent"], {
    cwd: command.cwd,
    env: {
      ...process.env,
      ...sandboxEnvironment,
      ...(command.configPath ? { HARNESS_AGENT_CONFIG_PATH: command.configPath } : {}),
      PYTHONPATH: process.env.PYTHONPATH ? `${sourceAgent}${delimiter}${process.env.PYTHONPATH}` : sourceAgent,
    },
    stdio: ["pipe", "pipe", "pipe"],
  })
  if (!child.stdin || !child.stdout || !child.stderr) throw new Error("Unable to create agent stdio pipes")
  let stderr = ""
  child.stderr.on("data", chunk => { stderr += chunk.toString("utf-8") })
  const client = new AgentClient(new StdioRpcTransport(child.stdin, child.stdout))
  child.on("exit", code => {
    if (code && code !== 0) client.emit("agentExit", new Error(stderr || `Agent exited with code ${code}`))
  })
  const requested = clientCapabilities(command)
  const initialized = await client.initialize({
    protocol: { major: PROTOCOL_VERSION.major, min_minor: 0, max_minor: PROTOCOL_VERSION.minor },
    client: { name: "harness-cli", version: CLI_VERSION, kind: command.kind === "run" && !command.nonInteractive ? "tui" : "cli" },
    capabilities: {
      requests: requested,
      handles: clientInteractionHandles(command),
    },
  })
  return {
    client,
    runtime: createInteractiveRuntime(initialized, command.cwd, {
      gitBranch: readGitBranch(command.cwd),
      cliVersion: CLI_VERSION,
    }),
    stop: async () => {
      client.destroy()
      child.kill()
    },
  }
}

/** 在启动子进程前校验工作区，避免把无效 cwd 误报为 Python 可执行文件不存在。 */
export function validateWorkspace(cwd: string): void {
  if (!existsSync(cwd)) {
    throw new Error(`Workspace does not exist: ${cwd}. Create it first or pass an existing directory with --cwd.`)
  }
  if (!statSync(cwd).isDirectory()) {
    throw new Error(`Workspace is not a directory: ${cwd}`)
  }
}

/** OpenTUI 必须独占真实终端；管道或任务复用器会让控制序列进入普通文本流。 */
export function validateInteractiveTerminal(stdinIsTty: boolean | undefined, stdoutIsTty: boolean | undefined): void {
  if (!stdinIsTty || !stdoutIsTty) {
    throw new Error("Interactive TUI requires a real terminal. Run the root command directly, or use -n for non-interactive mode.")
  }
}

/** Git 信息仅用于底部状态栏；失败时不影响 Agent 启动或非 Git 工作区。 */
export function readGitBranch(cwd: string): string | undefined {
  try {
    const branch = execFileSync("git", ["-C", cwd, "branch", "--show-current"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 500,
    }).trim()
    return branch || undefined
  } catch {
    return undefined
  }
}

/** 无头模式下收集单次流式输出，并等待对应运行的终态事件。 */
async function runTurn(client: AgentClient, message: string, threadId?: string): Promise<{ text: string; threadId: string; runId: string; usage: unknown }> {
  let text = ""
  const run = client.startRun({ message, threadId })
  await run.accepted
  for await (const event of run.events) {
    if (event.type === EventType.CONTENT_DELTA) text += event.payload.text
  }
  const completion = await run.completion
  if (completion.outcome === "cancelled") throw new Error(completion.event.payload.reason)
  if (completion.outcome === "failed") {
    const error = completion.event.payload.error
    throw new Error(`${error.code}: ${error.message}`)
  }
  return {
    text,
    threadId: run.ref.threadId,
    runId: run.ref.runId,
    usage: completion.event.payload.usage,
  }
}

/** 根据解析后的命令选择配置查询、无头执行或交互式 TUI。 */
async function execute(command: Command): Promise<void> {
  if (command.kind === "run" && !command.nonInteractive) {
    validateInteractiveTerminal(process.stdin.isTTY, process.stdout.isTTY)
  }
  const agent = await startAgent(command)
  try {
    if (command.kind !== "run") {
      if (!isClientMethod(command.kind)) throw new Error(`Unsupported command operation: ${command.kind}`)
      const result = await agent.client.request(command.kind, command.params ?? {})
      console.log(JSON.stringify(result, null, 2))
      return
    }
    if (command.nonInteractive) {
      const result = await runTurn(agent.client, command.message!)
      if (command.json) console.log(JSON.stringify(result))
      else process.stdout.write(`${result.text}\n`)
      return
    }

    let webHandoff: WebHandoffCoordinator | undefined
    try {
      if (!command.nonInteractive) {
        const server = createWebServer({
          html: webHtml,
          getScript: browserBundle,
          isActiveHandoff: handoffId =>
            webHandoff !== undefined && isActiveHandoff(webHandoff, handoffId),
          attachLifecycle: (handoffId, channel) =>
            webHandoff!.attachLifecycle(handoffId, channel),
        })
        webHandoff = createWebHandoffCoordinator({
          host: agent.client,
          server,
          openBrowser: createSystemBrowserOpener(),
        })
      }
      await runTui({
        client: agent.client,
        runtime: agent.runtime,
        resume: command.resume,
        onRequestExit: () => undefined,
        webHandoff,
        openWeb: threadId => webHandoff!.open(threadId),
      })
    } finally {
      await webHandoff?.close()
    }
  } finally {
    await agent.stop()
  }
}

/** 只有当前 handoff 匹配的路径才被静态 server 服务；idle 后旧路径立即 404。 */
function isActiveHandoff(
  coordinator: WebHandoffCoordinator,
  handoffId: string,
): boolean {
  const snapshot = coordinator.getSnapshot()
  return snapshot.phase !== "idle" && snapshot.handoffId === handoffId
}

/** CLI 主入口：处理帮助/版本短路逻辑后执行用户命令。 */
export async function main(argv = process.argv.slice(2)): Promise<void> {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("Usage: harness [--resume] [-n TEXT] [--json] [--config PATH] [--cwd PATH] [--sandbox[=remote|false]] | harness skills <list|inspect|enable|disable|trust|install|update|remove|market>")
    return
  }
  if (argv.includes("--version") || argv.includes("-v")) {
    console.log(`za38-cli ${CLI_VERSION}`)
    return
  }
  await execute(parseArgs(argv))
}

if (import.meta.main) {
  main().catch(error => {
    console.error(`za38: ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  })
}
