import { expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { PassThrough } from "node:stream"
import { testRender } from "@opentui/react/test-utils"
import { act, createElement } from "react"

import { IpcClient } from "../../src/ipc/client"
import { Za38Tui } from "../../src/tui/app"
import type { TuiRuntime } from "../../src/tui/model"

const runtime: TuiRuntime = {
  workspace: "/workspace/harness-code",
  cliVersion: "0.1.0",
  modelConfigured: true,
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
    expect(frame).toContain("enterprise-model")
    expect(frame).toContain("本机执行")
    expect(frame).toContain("default")
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
    await sendAndFinish(setup, client, requests, "保留会话上下文")
    await act(async () => {
      await setup.mockInput.typeText("/skills")
      setup.mockInput.pressEnter()
      await Bun.sleep(0)
      await setup.flush()
    })
    let frame = await setup.waitForFrame(value => value.includes("repo-review-demo"))
    expect(frame).toContain("Skills")
    expect(frame).toContain("搜索 Skills")
    expect(frame).toContain("保留会话")
    expect(frame).toContain("harness-code")
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

test("/skill <id> [args] 不经选择器直接发起运行", async () => {
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
      await setup.mockInput.typeText("/skill user/repo-review-demo 检查当前改动")
      setup.mockInput.pressEnter()
      await setup.flush()
    })

    expect(requests.at(-1)).toMatchObject({
      message: "检查当前改动",
      requestedSkill: { id: "user/repo-review-demo", args: "检查当前改动" },
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

async function sendAndFinish(
  setup: Awaited<ReturnType<typeof testRender>>,
  client: IpcClient,
  requests: Array<{ message: string; threadId: string; runId: string; requestedSkill?: { id: string; args?: string } }>,
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
  const client = new IpcClient(stdin, stdout)
  const requests: Array<{ message: string; threadId: string; runId: string; requestedSkill?: { id: string; args?: string } }> = []
  stdin.on("data", data => {
    for (const line of data.toString("utf8").split("\n")) {
      if (!line.trim()) continue
      const request = JSON.parse(line) as { id?: string; method?: string; params?: Record<string, unknown> }
      if (request.method === "skills.list" && typeof request.id === "string") {
        stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result: {
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
          },
        })}\n`)
        continue
      }
      if (request.method !== "run.start" || typeof request.id !== "string") continue
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
      stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { accepted: true, thread_id: threadId, run_id: runId } })}\n`)
    }
  })
  return { client, requests }
}
