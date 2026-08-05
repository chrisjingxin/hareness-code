/** AgentClientGateway (及兼容别名 AgentClientInteractiveAdapter) 把 AgentGateway 调用映射到 AgentClient 的契约测试。 */

import { expect, test } from "bun:test"
import { PassThrough } from "node:stream"
import { AgentClient } from "../../src/ipc/client"
import { StdioRpcTransport } from "../../src/ipc/stdio-transport"
import { AgentClientGateway } from "../../src/infrastructure/agent-client-gateway"
import { AgentGatewayError } from "../../src/interactive/ports/agent-gateway"

test("listSkills(true) 转发为 skills.list，参数 include_disabled=true", async () => {
  const { gateway, nextRequest } = setupPeer(({ message, stdout }) => {
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { snapshot: {}, skills: [], diagnostics: [] },
    }) + "\n")
  })
  const captured = nextRequest()
  const result = await gateway.listSkills(true)
  expect(result).toEqual({ snapshot: {}, skills: [], diagnostics: [] })
  expect(await captured).toMatchObject({ method: "skills.list", params: { include_disabled: true } })
})

test("listSkills(false) 转发为 skills.list，参数 include_disabled=false", async () => {
  const { gateway, nextRequest } = setupPeer(({ message, stdout }) => {
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { snapshot: {}, skills: [], diagnostics: [] },
    }) + "\n")
  })
  const captured = nextRequest()
  await gateway.listSkills(false)
  expect(await captured).toMatchObject({ method: "skills.list", params: { include_disabled: false } })
})

test("setSkillEnabled 转发为 skills.set_enabled，参数 { id, enabled }", async () => {
  const { gateway, nextRequest } = setupPeer(({ message, stdout }) => {
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { updated: true },
    }) + "\n")
  })
  const captured = nextRequest()
  const result = await gateway.setSkillEnabled("user/repo-review-demo", false)
  expect(result).toEqual({ updated: true })
  expect(await captured).toMatchObject({
    method: "skills.set_enabled",
    params: { id: "user/repo-review-demo", enabled: false },
  })
})

test("setSkillEnabled 失败时将 JsonRpcRemoteError 转换为 AgentGatewayError 稳定错误", async () => {
  const { gateway } = setupPeer(({ message, stdout }) => {
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
  const pending = gateway.setSkillEnabled("user/x", true)
  await expect(pending).rejects.toBeInstanceOf(AgentGatewayError)
  await expect(pending).rejects.toMatchObject({ code: "-32010", message: "skills.manage 未协商" })
})

type PeerContext = {
  message: { id: string; method: string; params: Record<string, unknown> }
  stdout: PassThrough
  stdin: PassThrough
}

type PeerHandle = {
  gateway: AgentClientGateway
  /** 等待下一条写入 transport 的 JSON-RPC 请求并返回其 method/params。 */
  nextRequest(): Promise<{ method: string; params: Record<string, unknown> }>
}

/** 用 PassThrough + StdioRpcTransport 构造可控对端；respond 在 request 进入时立即回写。 */
function setupPeer(respond: (ctx: PeerContext) => void, limit?: number): PeerHandle {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const client = new AgentClient(new StdioRpcTransport(stdin, stdout, limit))
  const gateway = new AgentClientGateway(client)

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
    gateway,
    nextRequest() {
      return new Promise(resolve => {
        waiters.push(resolve)
      })
    },
  }
}
