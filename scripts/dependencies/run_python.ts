#!/usr/bin/env bun
/** 使用项目虚拟环境运行 Python 命令，兼容 Unix 与 Windows。 */

import { existsSync } from "node:fs"
import { resolve } from "node:path"

const root = resolve(import.meta.dir, "../..")
const agent = resolve(root, "packages/agent")
const environment = process.env.UV_PROJECT_ENVIRONMENT
  ? resolve(agent, process.env.UV_PROJECT_ENVIRONMENT)
  : resolve(agent, ".venv")
const candidates = process.platform === "win32"
  ? [resolve(environment, "Scripts/python.exe")]
  : [resolve(environment, "bin/python")]
const python = process.env.HARNESS_AGENT_PYTHON ?? candidates.find(existsSync)
if (!python) {
  console.error("未找到 packages/agent/.venv 中的 Python，请先运行 bun run deps:install")
  process.exit(1)
}

const args = process.argv.slice(2)
if (args.length === 0) {
  console.error("用法：bun scripts/dependencies/run_python.ts <Python 参数>")
  process.exit(1)
}
const result = Bun.spawnSync([python, ...args], {
  cwd: root,
  env: process.env,
  stdout: "inherit",
  stderr: "inherit",
})
process.exit(result.exitCode)
