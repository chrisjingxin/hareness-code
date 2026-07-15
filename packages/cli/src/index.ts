#!/usr/bin/env bun
import { execFileSync, spawn } from "node:child_process"
import { existsSync } from "node:fs"
import { resolve } from "node:path"
import type { InitializeResult } from "@za38/protocol"

import { parseArgs, type Command } from "./args"
import { IpcClient } from "./ipc/client"
import { runTui } from "./tui/app"
import { CLI_VERSION, createTuiRuntime, type TuiRuntime } from "./tui/model"

type RunningAgent = {
  client: IpcClient
  runtime: TuiRuntime
  stop: () => Promise<void>
}

async function startAgent(command: Command): Promise<RunningAgent> {
  const sourcePython = resolve(import.meta.dir, "../../agent/.venv/bin/python")
  const python = process.env.ZA38_AGENT_PYTHON ?? (existsSync(sourcePython) ? sourcePython : "python3")
  const sourceAgent = resolve(import.meta.dir, "../../agent")
  const child = spawn(python, ["-m", "za38_agent"], {
    cwd: command.cwd,
    env: {
      ...process.env,
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
    client_info: { name: "za38-cli", version: CLI_VERSION },
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

async function runTurn(client: IpcClient, message: string, threadId?: string): Promise<{ text: string; threadId: string; runId: string; usage: unknown }> {
  let text = ""
  let active: { thread_id: string; run_id: string } | undefined
  const terminal = new Promise<{ usage: unknown }>((resolveTerminal, rejectTerminal) => {
    client.on("message/delta", (event: { text?: string }) => { text += event.text ?? "" })
    client.once("run/completed", (event: { thread_id: string; run_id: string; usage: unknown }) => {
      if (!active || (event.thread_id === active.thread_id && event.run_id === active.run_id)) resolveTerminal({ usage: event.usage })
    })
    client.once("run/cancelled", (event: { reason?: string }) => rejectTerminal(new Error(event.reason ?? "Run cancelled")))
    client.once("run/failed", (event: { code?: string; message?: string }) => rejectTerminal(new Error(`${event.code ?? "AgentError"}: ${event.message ?? "Run failed"}`)))
  })
  active = await client.query(message, threadId)
  const result = await terminal
  return { text, threadId: active.thread_id, runId: active.run_id, usage: result.usage }
}

async function execute(command: Command): Promise<void> {
  const agent = await startAgent(command)
  try {
    if (command.kind !== "run") {
      const result = await agent.client.call(command.kind)
      console.log(JSON.stringify(result, null, 2))
      return
    }
    if (command.nonInteractive) {
      const result = await runTurn(agent.client, command.message!, command.threadId)
      if (command.json) console.log(JSON.stringify(result))
      else process.stdout.write(`${result.text}\n`)
      return
    }

    await runTui({
      client: agent.client,
      runtime: agent.runtime,
      threadId: command.threadId,
      onRequestExit: () => undefined,
    })
  } finally {
    await agent.stop()
  }
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("Usage: za38 [-n TEXT] [--json] [--config PATH] [--cwd PATH] [--resume THREAD_ID]")
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
