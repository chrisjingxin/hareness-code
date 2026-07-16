import { expect, test } from "bun:test"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { createServer } from "node:http"
import { tmpdir } from "node:os"
import { resolve } from "node:path"

const loopbackTest = process.env.HARNESS_RUN_LOOPBACK_E2E === "1" ? test : test.skip

loopbackTest("CLI, Python sidecar, and OpenAI-compatible streaming gateway work end to end", async () => {
  const server = createServer(async (request, response) => {
    const chunks: Uint8Array[] = []
    for await (const chunk of request) chunks.push(chunk)
    const payload = JSON.parse(Buffer.concat(chunks).toString("utf-8"))
    expect(payload.stream).toBe(true)
    const events = [
      {
        id: "mock",
        object: "chat.completion.chunk",
        created: 0,
        model: "mock",
        choices: [{ index: 0, delta: { role: "assistant", content: "gateway response" }, finish_reason: null }],
      },
      {
        id: "mock",
        object: "chat.completion.chunk",
        created: 0,
        model: "mock",
        choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
        usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
      },
    ]
    response.writeHead(200, { "content-type": "text/event-stream" })
    for (const event of events) response.write(`data: ${JSON.stringify(event)}\n\n`)
    response.end("data: [DONE]\n\n")
  })
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once("error", rejectListen)
    server.listen(0, "127.0.0.1", () => resolveListen())
  })
  const address = server.address()
  if (!address || typeof address === "string") throw new Error("Mock gateway did not expose a TCP address")

  const configDirectory = await mkdtemp(resolve(tmpdir(), "za38-gateway-e2e-"))
  const configPath = resolve(configDirectory, "config.toml")
  await writeFile(
    configPath,
    `[model]
provider = "openai-compatible"
name = "mock"
base_url = "http://127.0.0.1:${address.port}/v1"
api_key_env = "HARNESS_TEST_KEY"
`,
  )
  const packageDir = resolve(import.meta.dir, "..")
  const agentDir = resolve(packageDir, "../agent")
  try {
    const child = Bun.spawn({
      cmd: [process.execPath, "src/index.ts", "--non-interactive", "say hello", "--json", "--config", configPath],
      cwd: packageDir,
      env: {
        ...process.env,
        HARNESS_AGENT_PYTHON: resolve(agentDir, ".venv/bin/python"),
        PYTHONPATH: agentDir,
        HARNESS_TEST_KEY: "test-key",
      },
      stdout: "pipe",
      stderr: "pipe",
    })
    const [stdout, stderr, exitCode] = await Promise.all([
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
      child.exited,
    ])
    if (exitCode !== 0) throw new Error(`CLI exited with ${exitCode}: ${stderr}`)
    expect(stderr).toBe("")
    expect(JSON.parse(stdout)).toMatchObject({ text: "gateway response", usage: { input_tokens: 3, output_tokens: 2 } })
  } finally {
    await new Promise<void>(resolveClose => server.close(() => resolveClose()))
    await rm(configDirectory, { recursive: true, force: true })
  }
})
