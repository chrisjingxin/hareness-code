#!/usr/bin/env bun
/** za38 CLI 启动层：管理 Python sidecar 生命周期并选择 TUI 或无头执行模式。 */
import { execFileSync, spawn } from "node:child_process"
import { existsSync, statSync } from "node:fs"
import { resolve } from "node:path"
import { PROTOCOL_VERSION, type EventEnvelope, type InitializeResult } from "@za38/protocol"

import { parseArgs, type Command } from "./args"
import { IpcClient } from "./ipc/client"
import { runTui } from "./tui/app"
import { CLI_VERSION, createTuiRuntime, type TuiRuntime } from "./tui/model"

type RunningAgent = {
  client: IpcClient
  runtime: TuiRuntime
  stop: () => Promise<void>
}

/** 根据命令实际是否存在反向交互处理器，声明最小协议能力集合。 */
export function clientCapabilities(command: Command): string[] {
  const capabilities = ["run.cancel", "run.multithread", "config.read"]
  if (command.kind === "run" && !command.nonInteractive) capabilities.push("threads.read")
  if (command.kind.startsWith("skills.") || (command.kind === "run" && !command.nonInteractive)) capabilities.push("skills.read")
  if (command.kind === "skills.set_enabled" || command.kind === "skills.install" || command.kind === "skills.update" || command.kind === "skills.remove") {
    capabilities.push("skills.manage")
  }
  if (command.kind === "run" && !command.nonInteractive) {
    capabilities.push("interactive.approval", "interactive.question")
  }
  return capabilities
}

/** 启动 Python sidecar、完成 initialize 握手，并返回可关闭的运行句柄。 */
async function startAgent(command: Command): Promise<RunningAgent> {
  validateWorkspace(command.cwd)
  const sourcePython = resolve(import.meta.dir, "../../agent/.venv/bin/python")
  const python = process.env.HARNESS_AGENT_PYTHON ?? (existsSync(sourcePython) ? sourcePython : "python3")
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
      PYTHONPATH: process.env.PYTHONPATH ? `${sourceAgent}:${process.env.PYTHONPATH}` : sourceAgent,
    },
    stdio: ["pipe", "pipe", "pipe"],
  })
  if (!child.stdin || !child.stdout || !child.stderr) throw new Error("Unable to create agent stdio pipes")
  let stderr = ""
  child.stderr.on("data", chunk => { stderr += chunk.toString("utf-8") })
  const client = new IpcClient(child.stdin, child.stdout)
  child.on("exit", code => {
    if (code && code !== 0) client.emit("agentExit", new Error(stderr || `Agent exited with code ${code}`))
  })
  const initialized = await client.call("initialize", {
    protocol: { major: PROTOCOL_VERSION.major, min_minor: 0, max_minor: PROTOCOL_VERSION.minor },
    client: { name: "za38-cli", version: CLI_VERSION },
    capabilities: clientCapabilities(command),
    cwd: command.cwd,
    config_path: command.configPath,
  }) as InitializeResult
  return {
    client,
    runtime: createTuiRuntime(initialized, command.cwd, {
      gitBranch: readGitBranch(command.cwd),
      cliVersion: CLI_VERSION,
    }),
    stop: async () => {
      try {
        await client.shutdown()
      } catch {
        // 进程可能已在退出，关闭阶段无需覆盖原始错误。
      }
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
async function runTurn(client: IpcClient, message: string, threadId?: string): Promise<{ text: string; threadId: string; runId: string; usage: unknown }> {
  let text = ""
  let active: { thread_id: string; run_id: string } | undefined
  const terminal = new Promise<{ usage: unknown }>((resolveTerminal, rejectTerminal) => {
    client.on("event", (event: EventEnvelope) => {
      if (active && (event.thread_id !== active.thread_id || event.run_id !== active.run_id)) return
      if (event.type === "content.delta" && typeof event.payload.text === "string") text += event.payload.text
      if (event.type === "run.completed") resolveTerminal({ usage: event.payload.usage })
      if (event.type === "run.cancelled") rejectTerminal(new Error(typeof event.payload.reason === "string" ? event.payload.reason : "Run cancelled"))
      if (event.type === "run.failed") {
        const error = event.payload.error as Record<string, unknown> | undefined
        rejectTerminal(new Error(`${error?.code ?? "AgentError"}: ${error?.message ?? "Run failed"}`))
      }
    })
  })
  active = await client.query(message, threadId)
  const result = await terminal
  return { text, threadId: active.thread_id, runId: active.run_id, usage: result.usage }
}

/** 根据解析后的命令选择配置查询、无头执行或交互式 TUI。 */
async function execute(command: Command): Promise<void> {
  if (command.kind === "run" && !command.nonInteractive) {
    validateInteractiveTerminal(process.stdin.isTTY, process.stdout.isTTY)
  }
  const agent = await startAgent(command)
  try {
    if (command.kind !== "run") {
      const result = await agent.client.call(command.kind, command.params ?? {})
      console.log(JSON.stringify(result, null, 2))
      return
    }
    if (command.nonInteractive) {
      const result = await runTurn(agent.client, command.message!)
      if (command.json) console.log(JSON.stringify(result))
      else process.stdout.write(`${result.text}\n`)
      return
    }

    await runTui({
      client: agent.client,
      runtime: agent.runtime,
      resume: command.resume,
      onRequestExit: () => undefined,
    })
  } finally {
    await agent.stop()
  }
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
