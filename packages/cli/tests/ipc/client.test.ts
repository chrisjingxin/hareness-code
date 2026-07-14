import { test, expect } from "bun:test"
import { PassThrough } from "node:stream"
import { IpcClient } from "../../src/ipc/client"

test("IpcClient 发送 initialize 并收到响应", async () => {
  const mockStdout = new PassThrough()
  const mockStdin = new PassThrough()

  const client = new IpcClient(mockStdin, mockStdout)

  // 模拟 Python 在 stdout 上响应
  mockStdin.on("data", (data) => {
    const lines = data.toString().split("\n").filter(l => l.trim())
    for (const line of lines) {
      const msg = JSON.parse(line)
      if (msg.method === "initialize") {
        mockStdout.write(JSON.stringify({
          jsonrpc: "2.0",
          result: {
            server_info: { name: "za38-agent", version: "0.1.0" },
            capabilities: { streaming: true, hitl: true },
          },
          id: msg.id,
        }) + "\n")
      }
    }
  })

  const result = await client.call("initialize", { client_info: { name: "test", version: "0.1.0" } }) as any
  expect(result.server_info.name).toBe("za38-agent")
  expect(result.capabilities.streaming).toBe(true)
})

test("IpcClient 接收通知", async () => {
  const mockStdout = new PassThrough()
  const mockStdin = new PassThrough()
  const client = new IpcClient(mockStdin, mockStdout)

  const notifications: any[] = []
  client.on("stream/text", (params) => notifications.push(params))

  mockStdout.write(JSON.stringify({
    jsonrpc: "2.0",
    method: "stream/text",
    params: { text: "hello", thread_id: "t1" },
  }) + "\n")

  await new Promise(r => setTimeout(r, 50))

  expect(notifications).toHaveLength(1)
  expect(notifications[0].text).toBe("hello")
})

test("IpcClient query 发送 query 方法", async () => {
  const mockStdout = new PassThrough()
  const mockStdin = new PassThrough()
  const client = new IpcClient(mockStdin, mockStdout)

  let sentMessage: any
  mockStdin.on("data", (data) => {
    const lines = data.toString().split("\n").filter(l => l.trim())
    for (const line of lines) {
      sentMessage = JSON.parse(line)
      if (sentMessage.method === "query") {
        mockStdout.write(JSON.stringify({
          jsonrpc: "2.0",
          result: { thread_id: "t1", accepted: true },
          id: sentMessage.id,
        }) + "\n")
      }
    }
  })

  await client.query("hello world")
  expect(sentMessage.method).toBe("query")
  expect(sentMessage.params.message).toBe("hello world")
})
