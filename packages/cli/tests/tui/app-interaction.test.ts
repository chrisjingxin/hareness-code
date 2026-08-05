import { expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { PassThrough } from "node:stream"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import { AgentClient } from "../../src/ipc/client"
import { StdioRpcTransport } from "../../src/ipc/stdio-transport"
import { Za38Tui } from "../../src/tui/app"
import type { InteractiveRuntime } from "../../src/interactive/runtime"

const runtime: InteractiveRuntime = {
  workspace: "/workspace/harness-code",
  cliVersion: "0.1.0",
  modelConfigured: true,
  modelProfileId: "enterprise-model",
  modelName: "enterprise-model",
  executionMode: "local",
  approvalMode: "default",
}

test("真实 textarea 在光标边界用上下键回填历史，而不是被全局快捷键截获", async () => {
  const historyHome = await mkdtemp(join(tmpdir(), "za38-tui-history-"))
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        promptHistoryFile: join(historyHome, "prompt-history.jsonl"),
        onRequestExit: () => undefined,
      }), { width: 80, height: 24 })
    })
    await act(async () => { await setup.flush() })

    await sendAndFinish(setup, client, requests, "第一条")
    await sendAndFinish(setup, client, requests, "第二条")

    // 空 composer 的两次 ↑ 应依次取回最新和上一条；Enter 读取 textarea 当前缓冲区。
    await act(async () => {
      setup.mockInput.pressArrow("up")
      setup.mockInput.pressArrow("up")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)?.message).toBe("第一条")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
    await rm(historyHome, { recursive: true, force: true })
  }
})

test("/status 只展示本地运行摘要，不创建 Agent run", async () => {
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 100, height: 28 })
    })
    await act(async () => {
      await setup.mockInput.typeText("/status")
      await setup.flush()
    })
    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })

    const frame = await setup.waitForFrame(value => value.includes("工作区"))
    expect(requests).toHaveLength(0)
    expect(frame).toContain("工作区")
    expect(frame).toContain("本机执行")
    expect(frame).toContain("default")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})


test("未知 Slash Command 只显示本地建议，不会创建 Agent run", async () => {
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/contnue")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    const frame = await setup.waitForFrame(value => value.includes("未知命令"))
    expect(frame).toContain("/resume")
    expect(requests).toHaveLength(0)
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})

test("双斜杠转义会原样向 Agent 提交单个前导斜杠", async () => {
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("//api/users 的路由在哪里")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)?.message).toBe("/api/users 的路由在哪里")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})


test("/skills 打开可搜索选择器，并把选中的 Skill 附到下一次运行", async () => {
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await sendAndFinish(setup, client, requests, "保留 thread 上下文")
    await act(async () => {
      await setup.mockInput.typeText("/skills")
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    let frame = await setup.waitForFrame(value => value.includes("repo-review-demo"))
    expect(frame).toContain("Skills")
    expect(frame).toContain("搜索 Skills")
    expect(frame).toContain("保留 thread")
    expect(frame).toContain("一条用于验证浮层描述单行")
    expect(frame).not.toContain("显示的长说明")
    expect(frame).not.toContain("┌")

    await act(async () => {
      await setup.mockInput.typeText("review")
      await setup.flush()
    })
    frame = setup.captureCharFrame()
    expect(frame).toContain("repo-review-demo")

    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => !value.includes("Skills") && value.includes("Skill") && value.includes("user/repo-review-demo"))
    expect(frame).toContain("下一条消息使用")

    await act(async () => {
      await setup.mockInput.typeText("审查当前改动")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)).toMatchObject({
      message: "审查当前改动",
      requestedSkill: { id: "user/repo-review-demo", args: "审查当前改动" },
    })
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})


test("SearchPicker 的 Esc 关闭浮层后会恢复 composer，且不把搜索文字带入下一次输入", async () => {
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/skills")
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    await setup.waitForFrame(value => value.includes("搜索 Skills"))

    await act(async () => {
      await setup.mockInput.typeText("review")
      setup.mockInput.pressEscape()
      await setup.flush()
      await Bun.sleep(0)
      await setup.flush()
    })
    await setup.waitForFrame(value => !value.includes("搜索 Skills"))
    await act(async () => {
      await Bun.sleep(5)
      await setup.flush()
    })

    await act(async () => {
      await setup.mockInput.typeText("关闭后继续执行")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)?.message).toBe("关闭后继续执行")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})


test("启动 --resume 等价于打开同一 thread 选择器", async () => {
  const { client } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        resume: true,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await Bun.sleep(0)
      await setup.flush()
    })
    const frame = await setup.waitForFrame(value => value.includes("Threads") && value.includes("修复索引结果"))
    expect(frame).toContain("修复索引结果")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})

test("Slash 菜单显示 skill:<id> 并可直接选择", async () => {
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 100, height: 30 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/skill:repo")
      await setup.flush()
    })
    let frame = await setup.waitForFrame(value => value.includes("/skill:user/repo-review-demo"))
    expect(frame).toContain("user · 只读代码审查")

    await act(async () => {
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    frame = await setup.waitForFrame(value => value.includes("Skill") && value.includes("user/repo-review-demo"))
    expect(frame).toContain("下一条消息使用")

    await act(async () => {
      await setup.mockInput.typeText("检查这个变更")
      setup.mockInput.pressEnter()
      await setup.flush()
    })
    expect(requests.at(-1)).toMatchObject({
      message: "检查这个变更",
      requestedSkill: { id: "user/repo-review-demo", args: "检查这个变更" },
    })
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})


test("窄终端中的 /skills 使用单列浮层且保持可操作", async () => {
  const { client } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 58, height: 18 })
      await setup.flush()
    })
    await act(async () => {
      await setup.mockInput.typeText("/skills")
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })

    const frame = await setup.waitForFrame(value => value.includes("repo-review-demo"))
    expect(frame).toContain("Skills")
    expect(frame).toContain("repo-review-demo")
    expect(frame).not.toContain("只读代码审查")
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})

test("无 Web launcher 时 /web 显示宿主级通知，不创建 Agent run", async () => {
  const { client, requests } = createMockClient()
  let setup: Awaited<ReturnType<typeof testRender>>
  try {
    await act(async () => {
      setup = await testRender(createElement(Za38Tui, {
        client,
        runtime,
        onRequestExit: () => undefined,
      }), { width: 100, height: 28 })
    })
    await act(async () => { await setup.flush() })
    await act(async () => {
      await setup.mockInput.typeText("/web")
      await setup.flush()
    })
    await act(async () => {
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    const frame = await setup.waitForFrame(value => value.includes("未提供 Web launcher"))
    expect(frame).toContain("未提供 Web launcher")
    expect(requests).toHaveLength(0)
  } finally {
    if (setup!) await act(async () => { setup.renderer.destroy() })
    client.destroy()
  }
})

async function sendAndFinish(
  setup: Awaited<ReturnType<typeof testRender>>,
  client: AgentClient,
  requests: Array<{ message: string; threadId: string; runId: string; requestedSkill?: { id: string; args?: string }; modelProfile?: string; modelSelection?: { primary_profile: string } }>,
  message: string,
) {
  await act(async () => {
    await setup.mockInput.typeText(message)
    setup.mockInput.pressEnter()
    await setup.flush()
  })
  const run = requests.at(-1)
  expect(run?.message).toBe(message)
  await act(async () => {
    client.emit("event", {
      event_id: crypto.randomUUID(),
      type: "run.completed",
      thread_id: run?.threadId,
      run_id: run?.runId,
      sequence: 1,
      timestamp_ms: Date.now(),
      payload: { duration_ms: 1, usage: { input_tokens: 0, output_tokens: 0 } },
    })
    await setup.flush()
  })
}

function createMockClient() {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const client = new AgentClient(new StdioRpcTransport(stdin, stdout))
  const requests: Array<{ message: string; threadId: string; runId: string; requestedSkill?: { id: string; args?: string }; modelProfile?: string; modelSelection?: { primary_profile: string } }> = []

  stdin.on("data", data => {
    for (const line of data.toString("utf8").split("\n")) {
      if (!line.trim()) continue
      const request = JSON.parse(line) as { id?: string; method?: string; params?: Record<string, unknown> }
      if (typeof request.id !== "string") continue
      if (request.method === "initialize") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            protocol_version: "v3",
            agent: { name: "test-agent", version: "1.0.0" },
            capabilities: { enabled: ["skills.manage", "models.read", "mcp.manage", "config.manage"] },
            security: { approval_mode: "default" },
            execution: { mode: "local" },
          },
        })}
`)
        continue
      }
      if (request.method === "config.details") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            revision: 1,
            config: { model: { default: "enterprise-model" } },
          },
        })}
`)
        continue
      }
      if (request.method === "models.list") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            profiles: [{ id: "enterprise-model", name: "Enterprise Model" }],
            thread_selection: { primary_profile: "enterprise-model" },
          },
        })}
`)
        continue
      }
      if (request.method === "skills.list") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            snapshot: { id: "snapshot", count: 2 },
            skills: [{
              id: "user/repo-review-demo",
              name: "repo-review-demo",
              description: "只读代码审查",
              source: "user",
              enabled: true,
              user_invocable: true,
              argument_hint: "下一条消息使用",
            }, {
              id: "builtin/long-description-demo",
              name: "long-description-demo",
              description: "一条用于验证浮层描述单行截断且不应换行显示的长说明",
              source: "builtin",
              enabled: true,
              user_invocable: true,
            }],
            diagnostics: [],
          },
        })}
`)
        continue
      }
      if (request.method === "threads.list") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
            threads: [{
              thread_id: "opaque-thread-1",
              created_at_ms: 1,
              updated_at_ms: 2,
              first_message: "此前的需求",
              latest_message: "此前的回答",
              message_count: 2,
            }, {
              thread_id: "opaque-thread-2",
              created_at_ms: 3,
              updated_at_ms: 4,
              first_message: "修复索引结果",
              latest_message: "需要继续处理索引",
              message_count: 4,
            }],
          },
        })}
`)
        continue
      }
      if (request.method !== "run.start") continue
      const message = typeof request.params?.message === "string" ? request.params.message : ""
      const threadId = typeof request.params?.thread_id === "string" ? request.params.thread_id : "thread-1"
      const runId = typeof request.params?.run_id === "string" ? request.params.run_id : "run-1"
      const requestedSkill = request.params?.requested_skill
      requests.push({
        message,
        threadId,
        runId,
        requestedSkill: requestedSkill && typeof requestedSkill === "object"
          ? requestedSkill as { id: string; args?: string }
          : undefined,
      })
      stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { accepted: true, thread_id: threadId, run_id: runId } })}
`)
    }
  })
  return { client, requests }
}
