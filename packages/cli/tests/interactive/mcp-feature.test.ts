/** MCP Feature：添加/删除 MCP 服务器的能力检查、RPC 调用与 catalog 刷新协调。 */

import { expect, test } from "bun:test"
import { Capability } from "@za38/protocol"
import { makeHarness, flush } from "./harness"

const noMcpManage = [Capability.MCP_READ]

test("mcp.add：缺少 mcp.manage 能力时直接拒绝，不发 RPC", async () => {
  const harness = makeHarness({ capabilities: noMcpManage })
  try {
    await flush()
    const before = harness.calls.filter(call => call === "mcp.add").length
    const outcome = await harness.controller.dispatch({
      type: "mcp.add",
      input: { name: "filesystem", transport: "stdio", command: "npx" },
    })
    expect(harness.calls.filter(call => call === "mcp.add").length).toBe(before)
    expect(outcome).toEqual({ status: "rejected", code: "capability-missing", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("mcp.add：stdio 服务器添加成功并刷新 MCP catalog", async () => {
  const harness = makeHarness()
  try {
    await flush()
    const outcome = await harness.controller.dispatch({
      type: "mcp.add",
      input: { name: "filesystem", transport: "stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem"] },
    })
    await flush()
    expect(outcome).toEqual({ status: "accepted" })
    expect(harness.calls).toContain("mcp.add")
    // onSuccess 刷新 catalog：mcp.status 在 add 之后被调用
    const addIndex = harness.calls.indexOf("mcp.add")
    expect(harness.calls.indexOf("mcp.status")).toBeGreaterThan(addIndex)
  } finally {
    await harness.controller.close()
  }
})

test("mcp.add：RPC 失败返回脱敏 agent-error", async () => {
  const harness = makeHarness()
  harness.port.mcpAdd = async () => {
    harness.calls.push("mcp.add")
    throw new Error("连接被拒绝")
  }
  try {
    await flush()
    const outcome = await harness.controller.dispatch({
      type: "mcp.add",
      input: { name: "bad-server", transport: "stdio", command: "missing-binary" },
    })
    expect(outcome).toEqual({ status: "rejected", code: "agent-error", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("mcp.remove：移除成功并刷新 MCP catalog；失败返回 agent-error", async () => {
  const harness = makeHarness()
  try {
    await flush()
    const outcome = await harness.controller.dispatch({ type: "mcp.remove", name: "filesystem" })
    await flush()
    expect(outcome).toEqual({ status: "accepted" })
    expect(harness.calls).toContain("mcp.remove")
    const removeIndex = harness.calls.indexOf("mcp.remove")
    expect(harness.calls.indexOf("mcp.status")).toBeGreaterThan(removeIndex)
  } finally {
    await harness.controller.close()
  }
})

test("mcp.remove：RPC 失败返回脱敏 agent-error", async () => {
  const harness = makeHarness()
  harness.port.mcpRemove = async () => {
    harness.calls.push("mcp.remove")
    throw new Error("服务器不存在")
  }
  try {
    await flush()
    const outcome = await harness.controller.dispatch({ type: "mcp.remove", name: "missing" })
    expect(outcome).toEqual({ status: "rejected", code: "agent-error", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})

test("mcp 变更：active Run 期间拒绝，不发 RPC", async () => {
  const harness = makeHarness()
  try {
    await flush()
    await harness.controller.dispatch({ type: "input.submit", value: "运行中" })
    const before = harness.calls.filter(call => call === "mcp.add" || call === "mcp.remove").length
    const addOutcome = await harness.controller.dispatch({
      type: "mcp.add",
      input: { name: "filesystem", transport: "stdio", command: "npx" },
    })
    const removeOutcome = await harness.controller.dispatch({ type: "mcp.remove", name: "filesystem" })
    expect(harness.calls.filter(call => call === "mcp.add" || call === "mcp.remove").length).toBe(before)
    expect(addOutcome).toEqual({ status: "rejected", code: "busy", message: expect.any(String) })
    expect(removeOutcome).toEqual({ status: "rejected", code: "busy", message: expect.any(String) })
  } finally {
    await harness.controller.close()
  }
})
