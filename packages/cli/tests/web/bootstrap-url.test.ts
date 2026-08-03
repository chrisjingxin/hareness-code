/** Browser bootstrap fragment 解析与 loopback endpoint 校验测试。 */

import { expect, test } from "bun:test"

import {
  parseBootstrapFragment,
  validateAgentEndpoint,
} from "../../src/web/bootstrap-url"

const FULL = "endpoint=ws%3A%2F%2F127.0.0.1%3A8123&token=tok&attachment=att-1&thread=thread-9"

test("完整 fragment 解析出全部字段，thread 缺失时返回 null", () => {
  expect(parseBootstrapFragment(`#${FULL}`)).toEqual({
    endpoint: "ws://127.0.0.1:8123",
    token: "tok",
    attachmentId: "att-1",
    threadId: "thread-9",
  })
  expect(parseBootstrapFragment("#endpoint=ws%3A%2F%2F127.0.0.1%3A8123&token=tok&attachment=att-1")).toEqual({
    endpoint: "ws://127.0.0.1:8123",
    token: "tok",
    attachmentId: "att-1",
    threadId: null,
  })
})

test("缺失或空的关键字段与非法 Thread 返回 undefined", () => {
  expect(parseBootstrapFragment("")).toBeUndefined()
  expect(parseBootstrapFragment("#token=tok&attachment=att-1")).toBeUndefined()
  expect(parseBootstrapFragment("#endpoint=ws%3A%2F%2F127.0.0.1%3A8123&attachment=att-1")).toBeUndefined()
  expect(parseBootstrapFragment("#endpoint=ws%3A%2F%2F127.0.0.1%3A8123&token=tok")).toBeUndefined()
  expect(parseBootstrapFragment("#endpoint=&token=tok&attachment=att-1")).toBeUndefined()
  expect(parseBootstrapFragment("#endpoint=ws%3A%2F%2F127.0.0.1%3A8123&token=tok&attachment=att-1&thread=")).toBeUndefined()
})

test("endpoint 校验只约束 loopback WebSocket 形状，端口可与页面不同", () => {
  expect(validateAgentEndpoint("ws://127.0.0.1:8123")).toBe(true)
  expect(validateAgentEndpoint("ws://127.0.0.1:9999")).toBe(true)
  expect(validateAgentEndpoint("ws://127.0.0.1:8123/")).toBe(true)
  expect(validateAgentEndpoint("http://127.0.0.1:8123")).toBe(false)
  expect(validateAgentEndpoint("ws://localhost:8123")).toBe(false)
  expect(validateAgentEndpoint("ws://evil.example:8123")).toBe(false)
  expect(validateAgentEndpoint("ws://127.0.0.1")).toBe(false)
  expect(validateAgentEndpoint("ws://user:pass@127.0.0.1:8123")).toBe(false)
  expect(validateAgentEndpoint("ws://127.0.0.1:8123?token=1")).toBe(false)
  expect(validateAgentEndpoint("ws://127.0.0.1:8123#frag")).toBe(false)
  expect(validateAgentEndpoint("ws://127.0.0.1:8123/not-root")).toBe(false)
  expect(validateAgentEndpoint("not a url")).toBe(false)
})
