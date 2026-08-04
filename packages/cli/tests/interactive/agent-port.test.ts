/** AgentClientInteractiveAdapter 把 InteractiveAgentPort 调用映射到 AgentClient.request 的契约测试。 */

import { expect, test } from "bun:test"
import { PassThrough } from "node:stream"
import { AgentClient } from "../../src/ipc/client"
import { StdioRpcTransport } from "../../src/ipc/stdio-transport"
import { AgentClientInteractiveAdapter } from "../../src/interactive/agent-port"

test("listSkills(true) 转发为 skills.list，参数 include_disabled=true", async () => {
  const { adapter, nextRequest } = setupPeer(({ message, stdout }) => {
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { snapshot: {}, skills: [], diagnostics: [] },
    }) + "\n")
  })
  const captured = nextRequest()
  const result = await adapter.listSkills(true)
  expect(result).toEqual({ snapshot: {}, skills: [], diagnostics: [] })
  expect(await captured).toMatchObject({ method: "skills.list", params: { include_disabled: true } })
})

test("listSkills(false) 转发为 skills.list，参数 include_disabled=false", async () => {
  const { adapter, nextRequest } = setupPeer(({ message, stdout }) => {
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { snapshot: {}, skills: [], diagnostics: [] },
    }) + "\n")
  })
  const captured = nextRequest()
  await adapter.listSkills(false)
  expect(await captured).toMatchObject({ method: "skills.list", params: { include_disabled: false } })
})

test("setSkillEnabled 转发为 skills.set_enabled，参数 { id, enabled }", async () => {
  const { adapter, nextRequest } = setupPeer(({ message, stdout }) => {
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { updated: true },
    }) + "\n")
  })
  const captured = nextRequest()
  const result = await adapter.setSkillEnabled("user/repo-review-demo", false)
  expect(result).toEqual({ updated: true })
  expect(await captured).toMatchObject({
    method: "skills.set_enabled",
    params: { id: "user/repo-review-demo", enabled: false },
  })
})

test("setSkillEnabled 失败时透传远端错误", async () => {
  const { adapter } = setupPeer(({ message, stdout }) => {
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      error: {
        code: -32010,
        message: "skills.manage 未协商",
        data: { code: "SKILLS_MANAGE_REQUIRED", retryable: false },
      },
    }) + "\n")
  })
  await expect(adapter.setSkillEnabled("user/x", true)).rejects.toMatchObject({ code: -32010 })
})

type PeerContext = {
  message: { id: string; method: string; params: Record<string, unknown> }
  stdout: PassThrough
  stdin: PassThrough
}

type PeerHandle = {
  adapter: AgentClientInteractiveAdapter
  /** 等待下一条写入 transport 的 JSON-RPC 请求并返回其 method/params。 */
  nextRequest(): Promise<{ method: string; params: Record<string, unknown> }>
}

/** 用 PassThrough + StdioRpcTransport 构造可控对端；respond 在 request 进入时立即回写。 */
function setupPeer(respond: (ctx: PeerContext) => void, limit?: number): PeerHandle {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const client = new AgentClient(new StdioRpcTransport(stdin, stdout, limit))
  const adapter = new AgentClientInteractiveAdapter(client)

  let buffer = ""
  const waiters: Array<(value: { method: string; params: Record<string, unknown> }) => void> = []
  stdin.on("data", chunk => {
    buffer += chunk.toString("utf8")
    while (true) {
      const newline = buffer.indexOf("\n")
      if (newline === -1) return
      const line = buffer.slice(0, newline)
      buffer = buffer.slice(newline + 1)
      const message = JSON.parse(line)
      respond({ message, stdout, stdin })
      const waiter = waiters.shift()
      if (waiter) waiter(message)
    }
  })

  return {
    adapter,
    nextRequest() {
      return new Promise(resolve => {
        waiters.push(resolve)
      })
    },
  }
}
