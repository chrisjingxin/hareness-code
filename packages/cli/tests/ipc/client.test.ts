/** v2 双向 Peer 的 JSONL、错误、反向请求与资源边界测试。 */

import { expect, test } from "bun:test"
import { PassThrough } from "node:stream"
import { IpcClient, JsonRpcRemoteError } from "../../src/ipc/client"

test("Peer 使用字符串 ID 发送请求并保留远端错误", async () => {
  const { client, stdin, stdout } = peer()
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    stdout.write(JSON.stringify({ jsonrpc: "2.0", id: message.id, error: { code: -32010, message: "配置错误", data: { field: "model" } } }) + "\n")
  })
  const error = await client.call("config.show").catch(value => value)
  expect(error).toBeInstanceOf(JsonRpcRemoteError)
  expect(error).toMatchObject({ code: -32010, data: { field: "model" } })
})

test("Peer 处理半帧、多帧和统一 event", async () => {
  const { client, stdout } = peer()
  const events: any[] = []
  client.on("event", event => events.push(event))
  const first = JSON.stringify({ jsonrpc: "2.0", method: "event", params: envelope("content.delta", 1, { text: "你好" }) })
  const second = JSON.stringify({ jsonrpc: "2.0", method: "event", params: envelope("run.completed", 2, {}) })
  const bytes = Buffer.from(`${first}\n${second}\n`)
  stdout.write(bytes.subarray(0, 23))
  stdout.write(bytes.subarray(23))
  await Bun.sleep(10)
  expect(events.map(item => item.type)).toEqual(["content.delta", "run.completed"])
})

test("Peer 响应 Agent 发起的审批 request", async () => {
  const { client, stdin, stdout } = peer()
  client.setRequestHandler(async request => ({ type: "approval", request_id: request.request_id, decision: "reject" }))
  const responses: any[] = []
  stdin.on("data", data => responses.push(...data.toString().trim().split("\n").map(JSON.parse)))
  stdout.write(JSON.stringify({
    jsonrpc: "2.0", method: "request", id: "approval-1",
    params: { request_id: "approval-1", type: "approval", thread_id: "t", run_id: "r", sequence: 1, timeout_ms: 1000, payload: { interrupt_id: "approval-1", description: "写文件", requests: {}, decisions: ["approve_once", "reject"] } },
  }) + "\n")
  await Bun.sleep(10)
  expect(responses[0]).toMatchObject({ id: "approval-1", result: { decision: "reject" } })
})

test("Peer 对畸形反向 request 返回结构化错误", async () => {
  const { stdin, stdout } = peer()
  const responses: any[] = []
  stdin.on("data", data => responses.push(JSON.parse(data.toString())))
  stdout.write(JSON.stringify({ jsonrpc: "2.0", method: "request", id: "bad-1", params: { type: "approval" } }) + "\n")
  await Bun.sleep(10)
  expect(responses[0]).toMatchObject({ id: "bad-1", error: { code: -32602 } })
})

test("Peer 拒绝超过限制的无换行帧并关闭 pending 请求", async () => {
  const { client, stdout } = peer(64)
  const errors: Error[] = []
  client.on("protocolError", error => errors.push(error))
  const pending = client.call("config.show", {}, 0).catch(error => error)
  stdout.write("x".repeat(65))
  expect(await pending).toBeInstanceOf(Error)
  expect(errors[0]?.message).toContain("exceeds")
})

function peer(limit?: number) {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  return { client: new IpcClient(stdin, stdout, limit), stdin, stdout }
}

function envelope(type: string, sequence: number, payload: Record<string, unknown>) {
  return { event_id: `e-${sequence}`, type, thread_id: "t", run_id: "r", sequence, timestamp_ms: 1, payload }
}
