#!/usr/bin/env bun
/** 内网依赖安装入口：校验来源和工具链后冻结安装并应用 DeepAgents 补丁。 */

import { existsSync } from "node:fs"
import { resolve } from "node:path"
import {
  DependencyPreflightError,
  expectedBunVersion,
  resolveInternalSources,
  validateLockSources,
  validateToolchainVersions,
} from "./source_policy"

const root = resolve(import.meta.dir, "../..")
const agent = resolve(root, "packages/agent")
const environment = process.env.UV_PROJECT_ENVIRONMENT
  ? resolve(agent, process.env.UV_PROJECT_ENVIRONMENT)
  : resolve(agent, ".venv")
const uv = process.env.UV_BIN ?? "uv"
const pythonCommand = process.env.HARNESS_PYTHON ?? (process.platform === "win32" ? "python" : "python3")

type Phase = "freeze" | "resolve"

interface CommandResult {
  exitCode: number
  stdout: string
  stderr: string
}

function decode(output: Uint8Array | string | null | undefined): string {
  if (typeof output === "string") return output
  return output ? new TextDecoder().decode(output) : ""
}

function capture(command: string[], cwd: string): CommandResult {
  const result = Bun.spawnSync(command, {
    cwd,
    env: process.env,
    stdout: "pipe",
    stderr: "pipe",
  })
  return {
    exitCode: result.exitCode,
    stdout: decode(result.stdout),
    stderr: decode(result.stderr),
  }
}

function run(command: string[], cwd: string, env?: Record<string, string>): void {
  const result = Bun.spawnSync(command, {
    cwd,
    env: { ...process.env, ...env },
    stdout: "inherit",
    stderr: "inherit",
  })
  if (result.exitCode !== 0) {
    throw new Error(`命令失败（${result.exitCode}）：${command.join(" ")}`)
  }
}

function versionFromOutput(tool: string, result: CommandResult): string {
  if (result.exitCode !== 0) {
    const detail = result.stderr.trim() || result.stdout.trim() || "无输出"
    throw new DependencyPreflightError([`${tool} 不可用：${detail}`])
  }
  const match = result.stdout.match(/\d+\.\d+(?:\.\d+)?/)
  if (!match) throw new DependencyPreflightError([`无法从 ${tool} 输出解析版本：${result.stdout.trim()}`])
  return match[0]
}

function phaseFromArgs(): Phase {
  const args = process.argv.slice(2)
  if (args.length === 0) return "freeze"
  if (args.length === 1 && args[0] === "--resolve") return "resolve"
  throw new DependencyPreflightError(["用法：bun run deps:install（冻结安装）或 bun run deps:resolve（首次内网解析并冻结）"])
}

function preflight(phase: Phase) {
  const sources = resolveInternalSources()
  const expectedBun = expectedBunVersion(root)
  const uvVersion = versionFromOutput(uv, capture([uv, "--version"], root))
  const pythonVersion = versionFromOutput(pythonCommand, capture([pythonCommand, "--version"], root))
  const toolchainIssues = validateToolchainVersions({
    bun: Bun.version,
    uv: uvVersion,
    python: pythonVersion,
    expectedBun,
  })
  if (toolchainIssues.length > 0) throw new DependencyPreflightError(toolchainIssues)

  if (phase === "freeze") {
    const lockIssues = validateLockSources(root, sources)
    if (lockIssues.length > 0) {
      throw new DependencyPreflightError([
        ...lockIssues,
        "当前锁文件尚未证明来自该内网源；请在内网执行 `bun run deps:resolve`，提交审查重新解析后的锁文件后再冻结安装",
      ])
    }
  }
  return { sources }
}

function pythonPath(): string {
  const candidates = process.platform === "win32"
    ? [resolve(environment, "Scripts/python.exe")]
    : [resolve(environment, "bin/python")]
  const selected = candidates.find(existsSync)
  if (!selected) throw new Error("uv sync 后未找到 packages/agent/.venv 中的 Python")
  return selected
}

try {
  const phase = phaseFromArgs()
  const { sources } = preflight(phase)
  const sourceEnv = {
    HARNESS_NPM_REGISTRY: sources.npmRegistry.toString(),
    HARNESS_PYPI_INDEX: sources.pythonIndex.toString(),
    UV_DEFAULT_INDEX: sources.pythonIndex.toString(),
    UV_INDEX_URL: sources.pythonIndex.toString(),
  }

  if (phase === "resolve") {
    // 首次内网副本允许重新解析；完成后仍走冻结安装，确保 lock 与实际安装一致。
    run([process.execPath, "install", "--registry", sources.npmRegistry.toString()], root, sourceEnv)
    run([uv, "lock", "--refresh", "--default-index", sources.pythonIndex.toString(), "--no-python-downloads"], agent, sourceEnv)
    const lockIssues = validateLockSources(root, sources)
    if (lockIssues.length > 0) throw new DependencyPreflightError(lockIssues)
  }

  run([process.execPath, "install", "--frozen-lockfile", "--registry", sources.npmRegistry.toString()], root, sourceEnv)
  run(
    [
      uv,
      "sync",
      "--extra",
      "test",
      "--locked",
      "--no-python-downloads",
      "--link-mode",
      "copy",
      "--default-index",
      sources.pythonIndex.toString(),
    ],
    agent,
    { ...sourceEnv, UV_LINK_MODE: "copy", UV_PROJECT_ENVIRONMENT: environment },
  )
  run([pythonPath(), resolve(root, "scripts/dependencies/apply_deepagents_patch.py")], root)
  run([pythonPath(), resolve(root, "scripts/dependencies/check_dependencies.py")], root)
  run([pythonPath(), resolve(root, "scripts/dependencies/runtime_smoke.py")], root)
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error))
  process.exit(1)
}
