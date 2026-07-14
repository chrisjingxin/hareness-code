import { expect, test } from "bun:test"
import { resolve } from "node:path"

test("CLI starts the Python sidecar and returns a completed JSON run", async () => {
  const packageDir = resolve(import.meta.dir, "..")
  const agentDir = resolve(packageDir, "../agent")
  const child = Bun.spawn({
    cmd: [process.execPath, "src/index.ts", "--non-interactive", "hello from cli", "--json"],
    cwd: packageDir,
    env: {
      ...process.env,
      ZA38_ECHO_MODE: "1",
      ZA38_AGENT_PYTHON: resolve(agentDir, ".venv/bin/python"),
      PYTHONPATH: agentDir,
    },
    stdout: "pipe",
    stderr: "pipe",
  })

  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ])
  expect(exitCode).toBe(0)
  expect(stderr).toBe("")
  expect(JSON.parse(stdout)).toMatchObject({ text: "hello from cli", usage: { input_tokens: 0, output_tokens: 0 } })
})
