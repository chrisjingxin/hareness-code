/**
 * E2E CLI harness：经 Python pty 桥以真实 TTY 启动交互式 CLI（echo fake Agent，
 * 无真实模型凭据），等待 /web 页面 URL 写入文件，暴露输入/输出/退出句柄。
 */

import { spawn, type ChildProcess } from "node:child_process"
import { mkdtemp, readFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

export type CliHarness = {
  /** /web 生成的页面 URL（含 UI token）。 */
  url: string
  /** 向 TUI 写入一行输入（经 PTY）。 */
  writeInput(text: string): void
  /** 等待 opener 把新的 /web URL 写入文件（内容不同于 previous），带超时。 */
  waitForNewUrl(previous: string, timeoutMs?: number): Promise<string>
  /** 等待并返回 TUI 输出中是否出现子串（轮询，带超时）。 */
  waitForOutput(substring: string, timeoutMs?: number): Promise<boolean>
  /** 终止 CLI（幂等）。 */
  stop(): Promise<void>
  /** CLI 是否已退出。 */
  exited: Promise<number | null>
}

const e2eDir = fileURLToPath(new URL(".", import.meta.url))
const cliDir = resolve(e2eDir, "../..")
const repoRoot = resolve(cliDir, "../../..")
const ptyBridge = resolve(repoRoot, "scripts/e2e-pty.py")
const agentDir = resolve(repoRoot, "packages/agent")
const agentPython = resolve(agentDir, ".venv/bin/python")

/** 启动一个独立的交互式 CLI（echo Agent + URL 文件 opener），自动执行 /web 并返回首个 URL。 */
export async function startCli(extraEnv: Record<string, string> = {}): Promise<CliHarness> {
  const workDir = await mkdtemp(join(tmpdir(), "za38-e2e-cli-"))
  const urlFile = join(workDir, "url.txt")
  const home = join(workDir, "home")

  const child = spawn(
    agentPython,
    [ptyBridge, "--", process.execPath, "run", "src/index.ts"],
    {
      cwd: cliDir,
      env: {
        ...process.env,
        HOME: home,
        HARNESS_ECHO_MODE: "1",
        HARNESS_AGENT_PYTHON: agentPython,
        PYTHONPATH: agentDir,
        HARNESS_E2E_WEB_URL_FILE: urlFile,
        ...extraEnv,
      },
      stdio: ["pipe", "pipe", "pipe"],
    },
  )
  let output = ""
  const outputBuffer: string[] = []
  child.stdout?.on("data", chunk => {
    output += chunk.toString("utf8")
    outputBuffer.push(chunk.toString("utf8"))
  })
  child.stderr?.on("data", chunk => {
    outputBuffer.push(`[stderr] ${chunk.toString("utf8")}`)
  })

  // TUI 渲染就绪后自动执行 /web；URL 由测试专用 opener 写入文件。
  await new Promise(resolveTimer => setTimeout(resolveTimer, 5_000))
  child.stdin?.write("/web\r")
  const url = await waitForUrlFile(urlFile)
  return {
    url,
    writeInput(text: string) {
      child.stdin?.write(`${text}\r`)
    },
    async waitForNewUrl(previous: string, timeoutMs = 20_000) {
      const deadline = Date.now() + timeoutMs
      while (Date.now() < deadline) {
        try {
          const content = (await readFile(urlFile, "utf8")).trim()
          if (content && content !== previous) return content
        } catch {
          // 尚未写入。
        }
        await new Promise(resolveTimer => setTimeout(resolveTimer, 250))
      }
      throw new Error(`E2E：等待新的 /web URL 超时`)
    },
    async waitForOutput(substring: string, timeoutMs = 15_000) {
      const deadline = Date.now() + timeoutMs
      while (Date.now() < deadline) {
        if (output.includes(substring)) return true
        await new Promise(resolveTimer => setTimeout(resolveTimer, 100))
      }
      return output.includes(substring)
    },
    async stop() {
      if (child.exitCode === null) child.kill()
      await new Promise<void>(resolveTimer => child.once("exit", () => resolveTimer()))
      await rm(workDir, { recursive: true, force: true })
    },
    exited: new Promise<number | null>(resolveTimer => child.once("exit", code => resolveTimer(code))),
  }
}

async function waitForUrlFile(urlFile: string, timeoutMs = 30_000): Promise<string> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const content = (await readFile(urlFile, "utf8")).trim()
      if (content) return content
    } catch {
      // 文件尚未写入。
    }
    await new Promise(resolveTimer => setTimeout(resolveTimer, 250))
  }
  throw new Error(`E2E：等待 /web URL 超时（${urlFile}）`)
}

/** 把页面 URL 转为 /ui WebSocket URL（含 UI token）。 */
export function uiSocketUrl(pageUrl: string): string {
  const url = new URL(pageUrl)
  const token = url.hash.replace(/^#ui=/, "")
  return `ws://${url.host}${url.pathname}/ui?ui=${encodeURIComponent(token)}`
}
