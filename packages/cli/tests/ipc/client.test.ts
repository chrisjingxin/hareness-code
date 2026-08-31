/** v3 AgentClient 的 JSONL、错误、Interaction 与资源边界测试。 */

import { expect, test } from "bun:test"
import { PassThrough } from "node:stream"
import type { JsonRpcRequest } from "@za38/protocol"
import { AgentClient, JsonRpcRemoteError } from "../../src/ipc/client"
import { StdioRpcTransport } from "../../src/ipc/stdio-transport"

test("Peer 使用字符串 ID 发送请求并保留远端错误", async () => {
  const { client, stdin, stdout } = peer()
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      error: {
        code: -32010,
        message: "配置错误",
        data: { code: "INTERNAL_ERROR", retryable: false, details: { field: "model" } },
      },
    }) + "\n")
  })
  const error = await client.request("config.show", {}).catch(value => value)
  expect(error).toBeInstanceOf(JsonRpcRemoteError)
  expect(error).toMatchObject({
    code: -32010,
    data: { code: "INTERNAL_ERROR", retryable: false, details: { field: "model" } },
  })
})

test("Peer 在 run.start 中携带显式 requested_skill", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { thread_id: message.params.thread_id, run_id: message.params.run_id, accepted: true },
    }) + "\n")
  })
  const run = client.startRun({
    message: "检查",
    mode: "build",
    threadId: "t",
    requestedSkill: { id: "project/review", args: "快速" },
  })
  await run.accepted
  expect(requests[0].params).toEqual({
    message: "检查",
    mode: "build",
    thread_id: "t",
    run_id: run.ref.runId,
    requested_skill: { id: "project/review", args: "快速" },
  })
})

test("Peer 登记 CLI Registry 对 immutable Skill snapshot 的 exact command binding", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { snapshot_id: message.params.snapshot_id, accepted: true },
    }) + "\n")
  })
  await client.bindCommandRegistry({
    snapshot_id: "snapshot-1",
    bindings: [{ id: "plugin/local/bad/command/help", name: "bad.help" }],
  })
  expect(requests[0]).toMatchObject({
    method: "commands.bind",
    params: {
      snapshot_id: "snapshot-1",
      bindings: [{ id: "plugin/local/bad/command/help", name: "bad.help" }],
    },
  })
})

test("Peer 在 Plugin Command run.start 中携带 raw invocation 与 canonical identity", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { thread_id: message.params.thread_id, run_id: message.params.run_id, accepted: true },
    }) + "\n")
  })
  const rawInvocation = "/ZA38-SDD   创建登录功能  "
  const run = client.startRun({
    message: rawInvocation,
    mode: "build",
    threadId: "t",
    requestedSkill: {
      id: "plugin/local/za38/command/za38-sdd",
      args: "创建登录功能",
      raw_invocation: rawInvocation,
      command_name: "za38-sdd",
    },
  })
  await run.accepted
  expect(requests[0].params).toMatchObject({
    message: rawInvocation,
    requested_skill: {
      id: "plugin/local/za38/command/za38-sdd",
      args: "创建登录功能",
      raw_invocation: rawInvocation,
      command_name: "za38-sdd",
    },
  })
})

test("Peer 在 run.start 中携带冻结的工作模式", async () => {
  const { client, stdin, stdout } = peer()
  const requests: JsonRpcRequest[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString()) as JsonRpcRequest
    requests.push(message)
    const params = message.params ?? {}
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { thread_id: params["thread_id"], run_id: params["run_id"], accepted: true },
    }) + "\n")
  })
  const run = client.startRun({ message: "检查", threadId: "t", mode: "compose" })
  await run.accepted
  expect(requests[0].params).toEqual({
    message: "检查",
    thread_id: "t",
    run_id: run.ref.runId,
    mode: "compose",
  })
})

test("AgentRun 使用原生 UUID 并携带 Thread 模型选择", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      result: { thread_id: message.params.thread_id, run_id: message.params.run_id, accepted: true },
    }) + "\n")
  })
  const run = client.startRun({
    message: "使用 pro",
    mode: "build",
    threadId: "t",
    modelSelection: { primary_profile: "pro" },
  })
  await run.accepted
  expect(run.ref.runId).toMatch(/^[0-9a-f-]{36}$/)
  expect(requests[0].params).toEqual({
    message: "使用 pro",
    mode: "build",
    thread_id: "t",
    run_id: run.ref.runId,
    model_selection: { primary_profile: "pro" },
  })
})

test("run.start 受理不设置会产生幽灵 Run 的本地超时", async () => {
  const { client, stdin, stdout } = peer()
  let request: any
  stdin.on("data", data => { request = JSON.parse(data.toString()) })

  const run = client.startRun({ message: "等待受理", mode: "build", threadId: "thread-slow-start" })
  await Bun.sleep(0)
  expect((client as any).pending.get(request.id).timeout).toBeUndefined()

  stdout.write(JSON.stringify({
    jsonrpc: "2.0",
    id: request.id,
    result: { thread_id: request.params.thread_id, run_id: request.params.run_id, accepted: true },
  }) + "\n")
  await run.accepted
})

test("Peer 通过受控配置接口传递详情、预览和 CAS 提交参数", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    const result = message.method === "config.details"
      ? { revision: "r1", fields: [], immutable_fields: [] }
      : { revision: "r2", changes: [], applies_to: ["restart"] }
    stdout.write(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }) + "\n")
  })

  await client.configDetails()
  const preview = await client.previewConfig([{ path: "approval.mode", value: "plan" }])
  await client.commitConfig(preview.revision, [{ path: "approval.mode", value: "plan" }])

  expect(requests.map(request => request.params)).toEqual([
    {},
    { changes: [{ path: "approval.mode", value: "plan" }] },
    { expected_revision: "r2", changes: [{ path: "approval.mode", value: "plan" }] },
  ])
})

test("Peer 通过类型化 Plugin 接口传递来源、trust 指纹和 data 删除选择", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    stdout.write(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: {} }) + "\n")
  })

  await client.listPlugins()
  await client.inspectPlugin("local-source/review")
  await client.validatePlugin("./review.zip", "claude-code")
  await client.installPlugin("./review.zip")
  await client.setPluginEnabled("local-source/review", true, "a".repeat(64))
  await client.removePlugin("local-source/review", true)

  expect(requests.map(request => [request.method, request.params])).toEqual([
    ["plugins.list", { include_disabled: true }],
    ["plugins.inspect", { id: "local-source/review" }],
    ["plugins.validate", { source: "./review.zip", format: "claude-code" }],
    ["plugins.install", { source: "./review.zip", format: "auto" }],
    ["plugins.set_enabled", {
      id: "local-source/review",
      enabled: true,
      capability_fingerprint: "a".repeat(64),
    }],
    ["plugins.remove", { id: "local-source/review", purge_data: true }],
  ])
})

test("Peer 通过类型化 Settings 接口传递摘要与 CAS，而不隐藏 value", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: message.id,
      error: { code: -32010, message: "SETTINGS_BACKEND_UNAVAILABLE", data: { code: "SETTINGS_BACKEND_UNAVAILABLE", retryable: true } },
    }) + "\n")
  })
  await client.listSettings("workspace").catch(() => undefined)
  await client.setSetting({
    scope: "user",
    plugin_id: "plugin/local/za38",
    package_digest: "a".repeat(64),
    declaration_digest: "b".repeat(64),
    setting_key: "ZA38_TOKEN",
    env_var: "ZA38_TOKEN",
    value: "transient-fake-value",
    expected_store_revision: 0,
  }).catch(() => undefined)
  await client.removeSetting({
    scope: "user",
    plugin_id: "plugin/local/za38",
    package_digest: "a".repeat(64),
    declaration_digest: "b".repeat(64),
    setting_key: "ZA38_TOKEN",
    env_var: "ZA38_TOKEN",
    expected_store_revision: 0,
  }).catch(() => undefined)
  expect(requests.map(request => [request.method, request.params])).toEqual([
    ["settings.list", { scope: "workspace" }],
    ["settings.set", {
      scope: "user",
      plugin_id: "plugin/local/za38",
      package_digest: "a".repeat(64),
      declaration_digest: "b".repeat(64),
      setting_key: "ZA38_TOKEN",
      env_var: "ZA38_TOKEN",
      value: "transient-fake-value",
      expected_store_revision: 0,
    }],
    ["settings.remove", {
      scope: "user",
      plugin_id: "plugin/local/za38",
      package_digest: "a".repeat(64),
      declaration_digest: "b".repeat(64),
      setting_key: "ZA38_TOKEN",
      env_var: "ZA38_TOKEN",
      expected_store_revision: 0,
    }],
  ])
})

test("Peer 在 v3.6 协商结果下不会发送未知 Settings RPC", async () => {
  const { client, stdin } = peer()
  const requests: any[] = []
  stdin.on("data", data => requests.push(JSON.parse(data.toString())))
  ;(client as any).initializedInfo = { protocol: { major: 3, minor: 6 } }
  expect(() => client.listSettings("user")).toThrow("SETTINGS_PROTOCOL_MINOR_REQUIRED")
  expect(requests).toEqual([])
})

test("Peer 通过类型化 Agent 与 Team 接口传递受控目录和运行参数", async () => {
  const { client, stdin, stdout } = peer()
  const requests: any[] = []
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    requests.push(message)
    const result: Record<string, unknown> = {
      "agents.list": { snapshot_id: "snapshot", agents: [], diagnostics: [] },
      "agents.inspect": {
        id: "lead", description: null, purpose: "lead", model_profile_id: "fast",
        execution_policy_id: "read", requested_skills: [], requested_mcp_servers: [],
        max_turns: null, color: null, approval_mode: null, permission_mode: null,
        source: "plugin:test", fingerprint: "fingerprint",
        kind: "plugin", tools: [],
      },
      "teams.list": { teams: [], diagnostics: [] },
      "teams.inspect": {},
      "teams.generate": {
        id: "review", description: null, max_parallelism: 2,
        failure_policy: "continue-to-synthesis", tasks: [{
          id: "worker", agent_id: "worker", depends_on: [], access: "read", timeout_seconds: 300,
        }],
      },
      "teams.run": { team_id: "review", run_id: "run-1", accepted: true },
      "teams.cancel": { run_id: "run-1", cancelled: true },
    }[message.method] as Record<string, unknown>
    stdout.write(JSON.stringify({ jsonrpc: "2.0", id: message.id, result }) + "\n")
  })

  await client.listAgents()
  await client.inspectAgent("lead")
  await client.listTeams()
  await client.inspectTeam("run", "run-1")
  await client.generateTeam({
    id: "review",
    lead_agent_id: "lead",
    worker_agent_ids: ["worker"],
    max_parallelism: 2,
  })
  await client.runTeam({
    team_id: "review",
    request: "检查变更",
    thread_id: "thread-1",
    run_id: "run-1",
  })
  await client.cancelTeam("run-1")

  expect(requests.map(request => [request.method, request.params])).toEqual([
    ["agents.list", {}],
    ["agents.inspect", { id: "lead" }],
    ["teams.list", {}],
    ["teams.inspect", { kind: "run", id: "run-1" }],
    ["teams.generate", {
      id: "review",
      lead_agent_id: "lead",
      worker_agent_ids: ["worker"],
      max_parallelism: 2,
    }],
    ["teams.run", {
      team_id: "review",
      request: "检查变更",
      thread_id: "thread-1",
      run_id: "run-1",
    }],
    ["teams.cancel", { run_id: "run-1" }],
  ])
})

test("Peer 处理半帧、多帧和统一 event", async () => {
  const { client, stdout } = peer()
  const events: any[] = []
  client.on("event", event => events.push(event))
  const first = JSON.stringify({ jsonrpc: "2.0", method: "event", params: envelope("content.delta", 1, { text: "你好" }) })
  const second = JSON.stringify({
    jsonrpc: "2.0",
    method: "event",
    params: envelope("run.completed", 2, {
      usage: { input_tokens: 1, output_tokens: 1 },
      duration_ms: 1,
      finish_reason: "stop",
      context: {},
    }),
  })
  const bytes = Buffer.from(`${first}\n${second}\n`)
  stdout.write(bytes.subarray(0, 23))
  stdout.write(bytes.subarray(23))
  await Bun.sleep(10)
  expect(events.map(item => item.type)).toEqual(["content.delta", "run.completed"])
})

test("Peer 接受 Python BuildRunAdapter 的 Plugin Command provenance 并保持序列连续", async () => {
  const { client, stdin, stdout } = peer()
  const errors: Error[] = []
  const events: any[] = []
  const requests: any[] = []
  client.on("protocolError", error => errors.push(error))
  client.on("event", event => events.push(event))
  stdin.on("data", data => {
    const request = JSON.parse(data.toString())
    requests.push(request)
    if (request.method !== "run.start") return
    stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: request.id,
      result: { thread_id: request.params.thread_id, run_id: request.params.run_id, accepted: true },
    }) + "\n")
    const provenance = {
      plugin_id: "local/ZA38",
      package_digest: "a".repeat(64),
      command_id: "plugin/local/ZA38/command/za38-sdd",
      snapshot_id: "snapshot-1",
    }
    const eventFrames = [
      envelope("run.started", 1, {
        mode: "build",
        resumed: false,
        skills_snapshot_id: "snapshot-1",
        command_provenance: provenance,
      }),
      envelope("skill.loaded", 2, {
        skill_id: "plugin/local/ZA38/command/za38-sdd",
        source: "plugin:local/ZA38",
        version: "0.2.0",
        snapshot_id: "snapshot-1",
        provenance,
      }),
      envelope("content.delta", 3, { text: "done" }),
      envelope("run.completed", 4, {
        usage: { input_tokens: 1, output_tokens: 1 },
        duration_ms: 1,
        finish_reason: "stop",
        context: {},
      }),
    ]
    for (const event of eventFrames) {
      stdout.write(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { ...event, thread_id: request.params.thread_id, run_id: request.params.run_id } }) + "\n")
    }
  })

  const rawInvocation = "/ZA38-SDD   创建登录功能  "
  const run = client.startRun({
    message: rawInvocation,
    mode: "build",
    threadId: "t",
    requestedSkill: {
      id: "plugin/local/ZA38/command/za38-sdd",
      args: "创建登录功能",
      raw_invocation: rawInvocation,
      command_name: "za38-sdd",
    },
  })
  await run.accepted
  await run.completion
  expect(requests[0].method).toBe("run.start")
  expect(requests[0].params.message).toBe(rawInvocation)
  expect(events.map(event => event.sequence)).toEqual([1, 2, 3, 4])
  expect(errors).toEqual([])
})

test("Peer 响应 Agent 发起的审批 request", async () => {
  const { client, stdin, stdout } = peer()
  client.setRequestHandler(async request => ({ type: "approval", request_id: request.request_id, decision: "reject" }))
  const responses: any[] = []
  stdin.on("data", data => responses.push(...data.toString().trim().split("\n").map(JSON.parse)))
  stdout.write(JSON.stringify({
    jsonrpc: "2.0", method: "interaction.approval", id: "approval-1",
    params: { thread_id: "t", run_id: "r", timeout_ms: 1000, payload: { interrupt_id: "approval-1", description: "写文件", requests: {}, decisions: ["approve_once", "reject"] } },
  }) + "\n")
  await Bun.sleep(10)
  expect(responses[0]).toMatchObject({ id: "approval-1", result: { decision: "reject" } })
})

test("Peer 对畸形反向 request 返回结构化错误", async () => {
  const { stdin, stdout } = peer()
  const responses: any[] = []
  stdin.on("data", data => responses.push(JSON.parse(data.toString())))
  stdout.write(JSON.stringify({ jsonrpc: "2.0", method: "interaction.approval", id: "bad-1", params: {} }) + "\n")
  await Bun.sleep(10)
  expect(responses[0]).toMatchObject({ id: "bad-1", error: { code: -32602 } })
})

test("Peer 拒绝超过限制的无换行帧并关闭 pending 请求", async () => {
  const { client, stdout } = peer(64)
  const errors: Error[] = []
  client.on("protocolError", error => errors.push(error))
  const pending = client.request("config.show", {}, 0).catch(error => error)
  stdout.write("x".repeat(65))
  expect(await pending).toBeInstanceOf(Error)
  expect(errors[0]?.message).toContain("exceeds")
})

test("context.compact 等待服务端终态而不使用通用请求超时", async () => {
  const { client, stdin, stdout } = peer()
  let request: any
  stdin.on("data", data => { request = JSON.parse(data.toString()) })

  let settled = false
  const result = client.compactContext("thread-compact").finally(() => { settled = true })
  await Bun.sleep(40)
  expect(settled).toBeFalse()

  stdout.write(JSON.stringify({
    jsonrpc: "2.0",
    id: request.id,
    result: {
      compacted: false,
      context: {
        action: "manual_skipped",
        estimated_tokens: 10,
        input_cap_tokens: 100,
        context_window_tokens: 128,
        dynamic_tokens: 10,
        cache_status: "unknown",
        cached_tokens: null,
        miss_reason: "short_history",
        artifact_ids: [],
      },
    },
  }) + "\n")

  expect(await result).toMatchObject({ compacted: false, context: { action: "manual_skipped" } })
})

test("Peer 只忽略已超时 ID 的迟到响应并继续报告真正未知 ID", async () => {
  const { client, stdin, stdout } = peer()
  const errors: Error[] = []
  let request: any
  client.on("protocolError", error => errors.push(error))
  stdin.on("data", data => { request = JSON.parse(data.toString()) })

  const timedOut = await client.request("config.show", {}, 5).catch(error => error)
  expect(timedOut).toBeInstanceOf(Error)
  expect(timedOut.message).toContain("Timed out waiting for config.show")

  stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result: {} }) + "\n")
  stdout.write(JSON.stringify({ jsonrpc: "2.0", id: "never-sent", result: {} }) + "\n")
  await Bun.sleep(10)

  expect(errors).toHaveLength(1)
  expect(errors[0]?.message).toBe("Unknown JSON-RPC response id: never-sent")
})

test("Peer 在请求终态记录 client IPC 诊断且不含协议原文", async () => {
  const records: Array<{ level: string, event: string, fields: Record<string, unknown> }> = []
  const log = {
    child() { return this },
    debug(event: string, fields: Record<string, unknown>) { records.push({ level: "debug", event, fields }) },
    info(event: string, fields: Record<string, unknown>) { records.push({ level: "info", event, fields }) },
    warn(event: string, fields: Record<string, unknown>) { records.push({ level: "warn", event, fields }) },
    error(event: string, fields: Record<string, unknown>) { records.push({ level: "error", event, fields }) },
  }
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const client = new AgentClient(new StdioRpcTransport(stdin, stdout), log as any)
  stdin.on("data", data => {
    const message = JSON.parse(data.toString())
    stdout.write(JSON.stringify({ jsonrpc: "2.0", id: message.id, result: {} }) + "\n")
  })
  await client.request("config.show", {})
  await client.close()
  const completed = records.find(item => item.event === "ipc.request.completed")
  expect(completed?.fields).toMatchObject({ side: "client", method: "config.show" })
  const closed = records.find(item => item.event === "ipc.transport.closed")
  expect(closed?.fields).toMatchObject({ side: "client", outcome: "completed" })
  expect(JSON.stringify(records)).not.toContain("jsonrpc")
})

function peer(limit?: number) {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  return { client: new AgentClient(new StdioRpcTransport(stdin, stdout, limit)), stdin, stdout }
}

function envelope(type: string, sequence: number, payload: Record<string, unknown>) {
  return { event_id: `e-${sequence}`, type, thread_id: "t", run_id: "r", sequence, timestamp_ms: 1, payload }
}
