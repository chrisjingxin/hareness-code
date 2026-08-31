import { expect, test } from "bun:test"
import { Capability } from "@za38/protocol"
import type { InteractiveSnapshot } from "../../src/interactive/types"
import { createCommandRegistry } from "../../src/interactive/commands"
import { makeHarness, flush, notices, terminalEvent, approvalRequest, questionRequest } from "./harness"

test("空首页：无 initialThreadId 不恢复，snapshot 表达 null Thread", () => {
  const harness = makeHarness()
  const snapshot = harness.controller.getSnapshot()
  expect(snapshot.currentThreadId).toBeNull()
  expect(snapshot.activity.kind).toBe("home")
  expect(snapshot.connection.status).toBe("open")
  harness.controller.close()
})

test("显式 initialThreadId null 进入空首页；字符串恢复历史与模型绑定", async () => {
  const empty = makeHarness({ initialThreadId: null })
  await flush()
  expect(empty.controller.getSnapshot().currentThreadId).toBeNull()
  expect(empty.calls).not.toContain("threads.open")
  await empty.controller.close()

  const restored = makeHarness({ initialThreadId: "thread-2" })
  await flush()
  const snapshot = restored.controller.getSnapshot()
  expect(snapshot.currentThreadId).toBe("thread-2")
  expect(snapshot.timeline).toHaveLength(2)
  expect(snapshot.catalogs.models.status).toBe("ready")
  await restored.controller.close()
})

test("Controller input.submit 使用握手生成的 Plugin Registry 执行参数和无参数命令", async () => {
  const command = {
    id: "plugin/local/ZA38/command/za38-sdd",
    name: "za38-sdd",
    description: "生成软件设计文档",
    argument_hint: "<goal>",
    requested_skill_id: "plugin/local/ZA38/command/za38-sdd",
    plugin_id: "local/ZA38",
  } as const
  const commandRegistry = createCommandRegistry([command])
  const harness = makeHarness({
    agentCommands: [command],
    commandRegistry,
    capabilities: [
      ...((runtimeCapabilities()) as Capability[]),
      Capability.SKILLS_READ,
    ],
  })
  try {
    const menuItem = harness.controller.getSnapshot().commands.find(item =>
      item.kind === "command" && item.command.id === command.id,
    )
    expect(menuItem?.kind === "command" ? menuItem.command.name : undefined).toBe("za38-sdd")

    await harness.controller.dispatch({ type: "input.submit", value: "/ZA38-SDD   创建登录功能  " })
    expect(harness.calls).toContain("run.start")
    expect(harness.port.lastRunSelection()).toMatchObject({
      message: "/ZA38-SDD   创建登录功能  ",
      requestedSkill: { id: command.id, args: "创建登录功能" },
    })
    const firstRun = harness.runHandles.at(-1)!
    harness.port.completeRun(firstRun.threadId, firstRun.runId)
    await flush()

    await harness.controller.dispatch({ type: "input.submit", value: "/za38-sdd" })
    expect(harness.calls.filter(call => call === "run.start")).toHaveLength(2)
    expect(harness.port.lastRunSelection()).toMatchObject({
      message: "/za38-sdd",
      requestedSkill: { id: command.id, args: "" },
    })
    expect(notices(harness.controller.getSnapshot())).not.toContain("未知命令")
  } finally {
    await harness.controller.close()
  }
})

function runtimeCapabilities(): readonly string[] {
  return [
    Capability.THREADS_READ,
    Capability.CONTEXT_MANAGE,
    Capability.MODELS_READ,
    Capability.MODELS_SELECT,
    Capability.CONFIG_WRITE,
    Capability.MCP_READ,
    Capability.MCP_MANAGE,
  ]
}

test("未知命令/转义/alias/动态 Skill 走同一解析路径", async () => {
  const harness = makeHarness()
  try {
    await harness.controller.dispatch({ type: "input.submit", value: "/contnue" })
    expect(notices(harness.controller.getSnapshot())).toContain("未知命令")
    expect(harness.calls).not.toContain("run.start")

    await harness.controller.dispatch({ type: "input.submit", value: "//api 路由" })
    expect(harness.port.lastRunSelection()).toMatchObject({ message: "/api 路由" })
    const escapedRun = harness.runHandles.at(-1)!
    harness.port.completeRun(escapedRun.threadId, escapedRun.runId)
    await flush()

    await harness.controller.dispatch({ type: "input.submit", value: "使用别名" })
    await flush()
    const aliasRun = harness.runHandles.at(-1)!
    harness.port.completeRun(aliasRun.threadId, aliasRun.runId)
    await flush()

    await harness.controller.dispatch({ type: "skill.arm", skillId: "user/repo-review-demo" })
    expect(harness.controller.getSnapshot().selection.armedSkill?.id).toBe("user/repo-review-demo")
    await harness.controller.dispatch({ type: "input.submit", value: "审查" })
    expect(harness.port.lastRunSelection()?.requestedSkill).toEqual({ id: "user/repo-review-demo", args: "审查" })
  } finally {
    await harness.controller.close()
  }
})

test("close 幂等；关闭后 dispatch no-op 且不再发布 snapshot", async () => {
  const harness = makeHarness()
  const snapshots: InteractiveSnapshot[] = []
  const unsubscribe = harness.controller.subscribe(snapshot => snapshots.push(snapshot))
  try {
    await harness.controller.close()
    await harness.controller.close()
    await harness.controller.dispatch({ type: "input.submit", value: "关闭后" })
    await flush()
    expect(harness.calls).not.toContain("run.start")
    const count = snapshots.length
    unsubscribe()
    await flush()
    expect(snapshots.length).toBe(count)
  } finally {
    await harness.controller.close()
  }
})
